#!/usr/bin/env python3
"""Session picker hook with per-bot numbering and generation-aware reopen."""

import html
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from telebots_hooklib import bot_metadata, event_log, fleet_home, state_path, telegram


def run_lister(*args):
    return subprocess.run([str(fleet_home() / "bin/fleet-sessions"), *args],
                          capture_output=True, text=True, timeout=14)


def inline_capability(state):
    try:
        caps = dict(line.split("=", 1) for line in (state / "caps").read_text().splitlines() if "=" in line)
        pid = caps.get("pid", "").strip()
        current = (state / "bot.pid").read_text().strip()
        if caps.get("resume-buttons", "").strip() != "1" or not re.fullmatch(r"[1-9]\d{0,7}", pid) or pid != current:
            return False
        result = subprocess.run(["/bin/ps", "-p", pid, "-o", "pid="],
                                capture_output=True, text=True, timeout=2)
        return result.returncode == 0 and result.stdout.strip() == pid
    except Exception:
        return False


def send_list(chat, text, markup):
    payload = {"chat_id": chat, "text": text, "parse_mode": "HTML"}
    if markup:
        payload["reply_markup"] = markup
    if os.environ.get("CC_RESUME_DRYRUN") == "1":
        print(json.dumps({"dry_run": True, "payload": payload}, ensure_ascii=False))
        return True
    if telegram("sendMessage", payload, timeout=8).get("ok") is True:
        return True
    if markup:
        payload.pop("reply_markup", None)
        if telegram("sendMessage", payload, timeout=8).get("ok") is True:
            return True
    payload.pop("parse_mode", None)
    payload["text"] = html.unescape(re.sub(r"<[^>]*>", "", text))
    return telegram("sendMessage", payload, timeout=8).get("ok") is True


def main():
    state = state_path()
    if state is None or not state.is_dir():
        return 0
    data = json.load(sys.stdin)
    if not isinstance(data, dict):
        return 0
    prompt = data.get("prompt") or ""
    if not isinstance(prompt, str) or "plugin:telegram:telegram" not in prompt:
        return 0
    match = re.search(r'<channel\b((?:[^>\"]|\"[^\"]*\")*)>(.*?)</channel>', prompt, re.S)
    if not match or re.search(r'via="(?:button|reaction|email_confirm)"', match.group(1)):
        return 0
    body = re.sub(r"\s+", " ", match.group(2)).strip().lower()
    pick = re.fullmatch(r"/(?:resume|sessions|open|reopen)\s+([0-9]{1,2})(?:\s+g([0-9a-f]{6}))?", body)
    list_mode = body in ("/resume", "/sessions", "/history", "/past")
    if not list_mode and not pick:
        return 0
    bot, _ = bot_metadata()
    if not bot or not bot.get("key") or not bot.get("workdir"):
        return 0
    chat_match = re.search(r'chat_id="(-?\d+)"', match.group(1))
    if not chat_match:
        return 0
    chat = chat_match.group(1)
    sid = str(data.get("session_id") or "")
    key = str(bot["key"])
    if list_mode:
        style = "inline" if inline_capability(state) else "reply"
        started = time.monotonic()
        rendered = run_lister("render", "--limit", "20", "--exclude-uuid", sid,
                              "--bot", key, "--style", style)
        text, markup = "", None
        if rendered.returncode == 0:
            try:
                item = json.loads(rendered.stdout)
                text = item.get("text") or ""
                markup = item.get("reply_markup")
                if not isinstance(text, str):
                    text = ""
            except Exception:
                pass
        if not text.strip() and time.monotonic() - started < 12:
            fallback = run_lister("list", "--limit", "20", "--exclude-uuid", sid, "--bot", key)
            text = fallback.stdout.strip() if fallback.returncode == 0 else ""
            markup = None
        text = text or "Couldn't pull your chat list in time. Send /resume again in a moment."
        sent = send_list(chat, text, markup)
        event_log("resume-hook.log", "RESUME", mode="list", style=style, ok=sent)
        if sent:
            print("resume-hook: session list handled", file=sys.stderr)
            return 2
        return 0
    number, generation = str(int(pick.group(1))), pick.group(2)
    args = ["resolve", number, "--bot", key]
    if generation:
        args += ["--gen", generation]
    result = run_lister(*args)
    uuid = result.stdout.strip().split("\t", 1)[0]
    if result.returncode or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,99}", uuid):
        if result.returncode == 3:
            text = "Send /resume first to pull up the numbered list, then /resume " + number + "."
        elif result.returncode == 5:
            text = "That list has changed. Send /resume to refresh it, then /resume " + number + "."
        else:
            text = "No session #" + number + " in the current list. Send /resume to refresh it."
        return 2 if send_list(chat, text, None) else 0
    engine = fleet_home() / "bin/fleet-resume-open"
    if not os.access(engine, os.X_OK):
        return 2 if send_list(chat, "Reopen failed: the reopen helper is missing. Run fleet-doctor on your Mac.", None) else 0
    workdir = str(Path(bot["workdir"]).expanduser())
    if os.environ.get("CC_RESUME_DRYRUN") == "1":
        print(json.dumps({"dry_run": True, "engine": engine.name, "args": [uuid, workdir, chat]}))
        return 2
    env = dict(os.environ, CC_RESUME_PICK=number)
    logdir = state / "logs"
    logdir.mkdir(exist_ok=True)
    with (logdir / "resume-open.log").open("a") as sink:
        subprocess.Popen([str(engine), uuid, workdir, chat], stdin=subprocess.DEVNULL,
                         stdout=sink, stderr=sink, env=env, start_new_session=True)
    event_log("resume-hook.log", "RESUME", mode="open", ok=True)
    print("resume-hook: reopen requested", file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
