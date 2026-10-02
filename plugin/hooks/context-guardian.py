#!/usr/bin/env python3
"""Offer one nudge for the highest newly crossed context threshold."""

import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from telebots_hooklib import bot_metadata, chat_id, config, event_log, fleet_home, state_path, telegram


def thresholds(state, options):
    raw = options.get("guardian_thresholds", "60,70,80,90")
    try:
        raw = (state / "guardian-thresholds").read_text().strip() or raw
    except OSError:
        pass
    values = {int(v.strip()) for v in str(raw).split(",")
              if re.fullmatch(r"\s*\d{1,3}\s*", v) and 1 <= int(v.strip()) <= 100}
    return sorted(values or {60, 70, 80, 90}, reverse=True)


def nudge(state, bot, count, sid, pct, options):
    dry = os.environ.get("CC_GUARDIAN_DRYRUN") == "1"
    with contextlib.ExitStack() as cleanup:
        if not dry:
            try:
                lock = cleanup.enter_context((state / "guardian.lock").open("a"))
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, BlockingIOError):
                event_log("guardian.log", "GUARDIAN", reason=6, status="busy")
                return
        crossed = [v for v in thresholds(state, options)
                   if pct >= v and not (state / ("guardian-nudged-%s-%d" % (sid, v))).exists()]
        if not crossed:
            event_log("guardian.log", "GUARDIAN", reason=6, pct=pct)
            return
        chat = chat_id(state)
        if not chat:
            event_log("guardian.log", "GUARDIAN", reason=7, pct=pct)
            return
        labels = (["Hand off"] if count > 1 else []) + ["Keep going", "Compact now"]
        row = [{"text": label, "callback_data": "ho:pick" if label == "Hand off" else "qr:0:%d" % i}
               for i, label in enumerate(labels)]
        question = "Hand this off to another bot, compact it, or keep going?" if count > 1 else "Compact it or keep going?"
        payload = {"chat_id": chat, "text": "🧠 Context at %d%% on %s. %s" % (pct, bot.get("name", "Claude"), question),
                   "reply_markup": {"inline_keyboard": [row]}}
        if dry:
            print(json.dumps({"dry_run": True, "threshold": crossed[0], "payload": payload}, ensure_ascii=False))
            event_log("guardian.log", "GUARDIAN", reason=9, pct=pct, threshold=crossed[0])
            return
        result = telegram("sendMessage", payload, timeout=8)
        sent = result.get("ok") is True
        if sent:
            for threshold in crossed:
                try:
                    (state / ("guardian-nudged-%s-%d" % (sid, threshold))).touch(exist_ok=True)
                except OSError:
                    pass
        event_log("guardian.log", "GUARDIAN", reason=8 if sent else 7,
                  pct=pct, threshold=crossed[0], ok=sent)


def main():
    state = state_path()
    if state is None or not state.is_dir():
        return
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print("Context guardian: Stop hook; CC_GUARDIAN_DRYRUN=1 prints the payload. Always exits 0.")
        return
    bot, count = bot_metadata()
    if not bot:
        return
    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict):
            raise ValueError()
    except Exception:
        event_log("guardian.log", "GUARDIAN", reason=2)
        return
    if data.get("stop_hook_active") is True or data.get("agent_id") or data.get("agentId"):
        event_log("guardian.log", "GUARDIAN", reason=3)
        return
    sid = re.sub(r"[^A-Za-z0-9._-]", "", str(data.get("session_id") or data.get("sessionId") or ""))[:100]
    transcript = data.get("transcript_path") or data.get("transcriptPath") or ""
    if not sid or not isinstance(transcript, str) or not Path(transcript).is_file():
        event_log("guardian.log", "GUARDIAN", reason=4)
        return
    helper = fleet_home() / "bin/fleet-context-pct"
    try:
        result = subprocess.run([sys.executable, str(helper), transcript, str(state / "model-state")],
                                capture_output=True, text=True, timeout=5)
        first = result.stdout.splitlines()[0].strip()
        if result.returncode or not re.fullmatch(r"\d{1,3}", first):
            raise ValueError()
        pct = int(first)
        if not 0 <= pct <= 100:
            raise ValueError()
    except Exception:
        event_log("guardian.log", "GUARDIAN", reason=5)
        return
    options = config().get("options") or {}
    nudge(state, bot, count, sid, pct, options)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
