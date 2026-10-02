#!/usr/bin/env python3
"""Shared configuration and bounded Telegram I/O for the channel hooks."""

import datetime
import json
import os
import re
import sys
from pathlib import Path
from urllib import parse, request


def fleet_home():
    return Path(os.environ.get("FLEET_HOME") or Path.home() / ".claude/telegram-fleet").expanduser()


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return default


def config():
    value = read_json(fleet_home() / "fleet.json", {})
    return value if isinstance(value, dict) else {}


def state_path():
    value = os.environ.get("TELEGRAM_STATE_DIR", "")
    return Path(value).expanduser() if value else None


def bot_metadata():
    """Resolve by state directory, never by a fixed name or directory suffix."""
    state = state_path()
    data = config()
    bots = data.get("bots") or []
    bots = [b for b in bots if isinstance(b, dict)] if isinstance(bots, list) else []
    for bot in bots:
        raw = bot.get("state_dir")
        if not state or not isinstance(raw, str):
            continue
        if Path(raw).expanduser().resolve() == state.resolve():
            return bot, len(bots)
    return {}, len(bots)


def read_token(state=None):
    state = state or state_path()
    if state is None:
        return ""
    try:
        for line in (state / ".env").read_text().splitlines():
            if line.strip().startswith("TELEGRAM_BOT_TOKEN="):
                return line.split("=", 1)[1].strip().strip("\"'")
    except Exception:
        pass
    return ""


def chat_id(state=None):
    state = state or state_path()
    data = read_json(state / "access.json", {}) if state else {}
    allowed = data.get("allowFrom") or [] if isinstance(data, dict) else []
    return str(allowed[0]) if isinstance(allowed, list) and allowed else ""


def api_base():
    """A test can contact loopback only; production ignores every override."""
    override = os.environ.get("TELEBOTS_API_BASE") or os.environ.get("TELEGRAM_API_BASE")
    if os.environ.get("TELEBOTS_TEST") == "1":
        if override:
            try:
                url = parse.urlsplit(override)
                if (url.scheme in ("http", "https") and
                        url.hostname in ("127.0.0.1", "localhost", "::1") and
                        not url.username and not url.password and not url.query and not url.fragment):
                    return override.rstrip("/")
            except ValueError:
                pass
            event_log("hook-events.log", "API-OVERRIDE", status="refused")
        return None
    if override:
        event_log("hook-events.log", "API-OVERRIDE", status="ignored")
    return "https://api.telegram.org"


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def telegram(method, payload, timeout=6, token=None):
    """Return decoded API data, with no error text, token, or payload in logs."""
    base = api_base()
    token = read_token() if token is None else token
    if not base or not token or not re.fullmatch(r"[A-Za-z]+", method):
        return {"ok": False}
    try:
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(base + "/bot" + token + "/" + method, data=body,
                              headers={"content-type": "application/json"}, method="POST")
        opener = request.build_opener(request.ProxyHandler({}), _NoRedirect())
        with opener.open(req, timeout=timeout) as response:
            result = json.loads(response.read(1_000_000).decode("utf-8"))
            return result if isinstance(result, dict) else {"ok": False}
    except Exception:
        return {"ok": False}


def event_log(filename, event, **fields):
    state = state_path()
    if state is None or not state.is_dir():
        return
    try:
        logdir = state / "logs"
        logdir.mkdir(exist_ok=True)
        # Callers supply only fixed labels, counts, percentages, and result flags.
        words = [datetime.datetime.now().astimezone().isoformat(timespec="seconds"), event]
        words += [str(k) + "=" + re.sub(r"[^A-Za-z0-9_.:+/-]", "_", str(v).lower() if isinstance(v, bool) else str(v))
                  for k, v in fields.items()]
        with (logdir / filename).open("a", encoding="utf-8") as handle:
            handle.write(" ".join(words) + "\n")
    except Exception:
        pass


def main():
    if state_path() is None:
        return
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "metadata":
        bot, count = bot_metadata()
        opts = config().get("options") or {}
        for value in (bot.get("key", ""), bot.get("name", "Claude"), bot.get("workdir", ""),
                      count, opts.get("guardian_thresholds", "60,70,80,90")):
            print(re.sub(r"[\r\n\x00]", " ", str(value)))
    elif command == "send":
        data = json.load(sys.stdin)
        method = data.pop("method", "sendMessage")
        timeout = data.pop("_timeout", 6)
        result = telegram(method, data, timeout=timeout)
        safe = {"ok": result.get("ok") is True}
        inner = result.get("result") or {}
        if isinstance(inner, dict) and isinstance(inner.get("message_id"), int):
            safe["result"] = {"message_id": inner["message_id"]}
        print(json.dumps(safe, separators=(",", ":")))
    elif command == "project":
        bot, _ = bot_metadata()
        workdir = bot.get("workdir") or ""
        if workdir:
            absolute = str(Path(workdir).expanduser().resolve())
            print(Path.home() / ".claude/projects" / re.sub(r"[^A-Za-z0-9]", "-", absolute))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
