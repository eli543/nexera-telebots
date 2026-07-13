#!/usr/bin/env python3
"""
UserPromptSubmit hook: GUARANTEED instant emoji reaction on the inbound Telegram
message.

The bot's "react with a relevant emoji" behavior is a model instruction, so it
gets forgotten under load. This hook makes the ack a HARD guarantee: the harness
fires a content-aware reaction the moment the message lands, before the model acts,
on every channel message. The model can still override with a sharper pick by hand.

Content-aware (not static): it reads the message and picks a fitting emoji
(👀 question/look, 🔥 praise, 🎉 win, 🙏 thanks, 💯 agreement, 🫡 directive),
rotating a varied default otherwise. All picks are from Telegram's reaction
whitelist; an invalid pick falls back to 👀.

GUARANTEES: only runs inside a channel bot session (env-gated on
TELEGRAM_STATE_DIR); zero stdout (never injects context); never raises or blocks a
turn; per-bot isolated; the <state_dir>/ack.off kill switch disables it instantly.
"""

import sys
import os
import re
import json
from urllib import request, parse

HTTP_TIMEOUT = 4
FALLBACK = "\U0001F440"  # 👀 - always whitelist-valid

# default rotation for messages with no clear sentiment (varied, not static)
DEFAULTS = ["\U0001F440", "\U0001F525", "\U0001F44D", "\U0001FAE1", "\U0001F4AF"]  # 👀 🔥 👍 🫡 💯


def hard_exit():
    sys.exit(0)


def read_env_token(env_path):
    try:
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def read_chat_id(access_path):
    try:
        with open(access_path, "r") as f:
            data = json.load(f)
        allow = data.get("allowFrom") or []
        if allow:
            return str(allow[0])
    except Exception:
        pass
    return ""


def pick_emoji(text, state_dir):
    """Content-aware pick from the user's inbound message; rotates a varied default."""
    t = (text or "").lower()
    try:
        # explicit emoji-in-message checks (real emoji chars - raw-regex \U would be literal)
        if any(e in t for e in ("\U0001F525", "\U0001F4AA", "\U00002764", "\U0001F60D")):
            return "\U0001F525"  # 🔥/💪/❤/😍 -> 🔥
        if "\U0001F389" in t or "\U0001F38A" in t:
            return "\U0001F389"  # 🎉/🎊 -> 🎉
        if "\U0001F64F" in t:
            return "\U0001F64F"  # 🙏
        if re.search(r"\b(thank|thanks|thx|appreciate|grateful)\b", t):
            return "\U0001F64F"  # 🙏
        if re.search(r"(amazing|incredible|awesome|perfect|love (it|this|that)|brilliant|genius|"
                     r"nailed it|so good|great (job|work|stuff)|well done|looks great|beautiful|"
                     r"killer|legend)", t):
            return "\U0001F525"  # 🔥
        if re.search(r"\b(done|shipped|it works|works now|working now|fixed|live now|let'?s go|"
                     r"congrats|we did it|finally|deployed)\b", t):
            return "\U0001F389"  # 🎉
        if re.search(r"\b(exactly|spot on|100%|correct|agreed|yep|yes\b.*\!|nailed)\b", t):
            return "\U0001F4AF"  # 💯
        if re.search(r"\b(do it|build it|fix it|send it|go ahead|make it|proceed|ship it|"
                     r"go for it|let'?s do it|get it done|run it)\b", t):
            return "\U0001FAE1"  # 🫡
        if "?" in t or re.search(r"^\s*(how|why|what|can|could|should|would|is|are|does|do|did|"
                                 r"where|when|who|which|will)\b", t):
            return "\U0001F440"  # 👀
        # default: rotate for visible variety
        cf = os.path.join(state_dir, "ack-rotate")
        n = 0
        try:
            with open(cf, "r") as f:
                n = int((f.read().strip() or "0"))
        except Exception:
            n = 0
        emoji = DEFAULTS[n % len(DEFAULTS)]
        try:
            with open(cf, "w") as f:
                f.write(str((n + 1) % 1000000))
        except Exception:
            pass
        return emoji
    except Exception:
        return FALLBACK


def set_reaction(token, chat_id, message_id, emoji):
    url = "https://api.telegram.org/bot%s/setMessageReaction" % token
    params = {
        "chat_id": str(chat_id),
        "message_id": str(message_id),
        "reaction": json.dumps([{"type": "emoji", "emoji": emoji}]),
    }
    try:
        body = parse.urlencode(params).encode()
        req = request.Request(url, data=body)
        with request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            data = json.loads(r.read().decode())
            return bool(data and data.get("ok"))
    except Exception:
        return False


def main():
    state_dir = os.environ.get("TELEGRAM_STATE_DIR", "")
    if not state_dir or not os.path.isdir(state_dir):
        hard_exit()
    if os.path.exists(os.path.join(state_dir, "ack.off")):
        hard_exit()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "") or read_env_token(
        os.path.join(state_dir, ".env"))
    if not token:
        hard_exit()

    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}
    ptext = data.get("prompt")
    if ptext is None:
        ptext = data.get("user_prompt") or data.get("message") or ""
    if not isinstance(ptext, str):
        ptext = ""

    s = ptext.lstrip()
    if not s.startswith("<channel"):
        hard_exit()  # only react to real inbound channel messages

    m_mid = re.search(r'message_id="(\d+)"', s)
    if not m_mid:
        hard_exit()
    message_id = m_mid.group(1)
    m_cid = re.search(r'chat_id="(-?\d+)"', s)
    chat_id = m_cid.group(1) if m_cid else read_chat_id(
        os.path.join(state_dir, "access.json"))
    if not chat_id:
        hard_exit()

    gt = s.find(">")
    end = s.rfind("</channel>")
    body = s[gt + 1:end].strip() if (gt != -1 and end != -1 and end > gt) else ""
    if body.startswith("/"):
        hard_exit()  # slash-command -> no reaction

    emoji = pick_emoji(body, state_dir)

    if os.environ.get("ACK_DRY") == "1":
        sys.stderr.write("[DRY] react %s -> chat %s msg %s | body: %s\n"
                         % (emoji, chat_id, message_id, body[:70]))
        hard_exit()

    ok = set_reaction(token, chat_id, message_id, emoji)
    if not ok and emoji != FALLBACK:
        set_reaction(token, chat_id, message_id, FALLBACK)
    hard_exit()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    hard_exit()
