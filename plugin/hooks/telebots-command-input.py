#!/usr/bin/env python3
"""Extract a direct channel command as NUL-delimited fields for Bash hooks."""

import json
import re
import sys


def main():
    try:
        event = json.load(sys.stdin)
        prompt = event.get("prompt", "")
        if not isinstance(prompt, str):
            return
        match = re.search(r"<channel\b([^>]*)>(.*?)</channel>", prompt, re.DOTALL)
        if not match:
            return
        attrs = match.group(1)
        via = re.search(r'\bvia="([^"]+)"', attrs)
        # The origin decision happens before the body is read or normalized.
        if via and via.group(1) in {"button", "reaction", "email_confirm"}:
            return
        body = match.group(2)
        if any(ord(char) < 32 or ord(char) == 127 for char in body):
            return
        chat = re.search(r'\bchat_id="([^"]+)"', attrs)
        chat_id = chat.group(1) if chat else ""
        session_id = event.get("session_id", "")
        if not isinstance(session_id, str):
            session_id = ""
        for field in (body.strip(), chat_id, session_id):
            if "\x00" in field:
                return
        sys.stdout.buffer.write((body.strip() + "\0" + chat_id + "\0" + session_id + "\0").encode())
    except (ValueError, TypeError, UnicodeError):
        return


if __name__ == "__main__":
    main()
