#!/usr/bin/env python3
import sys, os, json, hashlib, re, subprocess, time
try:
    import fcntl
except Exception:
    fcntl = None


_CIRC = {c: i + 1 for i, c in enumerate("①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳")}


_TLDR_NUM = re.compile(r"^\s*(?:\d+[.)]|\(\d+\))\s")


_TLDR_HEAD = re.compile(r"^\s*tl;?dr\s*[:\-]?\s*$", re.I)


_TLDR_ANY = re.compile(r"^\s*tl;?dr", re.I)


_INVISIBLE = str.maketrans("", "", "\u200b\u200c\u200d\ufeff")


_JUDGE_MODEL = "claude-haiku-4-5"

_KEYCHAIN_SERVICE = "nexera-telebots-judge"
_API_URL = "https://api.anthropic.com/v1/messages"
_KEY_TIMEOUT = 3.0
_JUDGE_TIMEOUT = 5.0
_MAX_JUDGE_INPUT = 8000


_JUDGE_MIN_CHARS = 900
_JUDGE_MIN_NL = 10
_ACK_MAX_CHARS = 400

_NUMBER_SYSTEM = (
    "An assistant is about to send this reply to the user on Telegram. The user wants "
    "replies NUMBERED by default. Decide if this reply has 2 OR MORE distinct points "
    "worth a numbered jump-index (① ② ③) - a reader would want to jump to a specific "
    "one. Return TRUE for almost any substantive reply with 2+ separable points, even "
    "when they all concern one subject (a change + a caveat + a next step; a status + "
    "a location + a deadline; two findings). Return FALSE ONLY if the reply is a single "
    "atomic point - one fact, one answer, or one continuous argument with no separable "
    "parts. When in doubt, return TRUE.\n\n"
    "Respond with ONLY compact JSON - no prose, no code fence:\n"
    '{"number": true|false, "topics": ["short label", ...]}\n'
    "topics: 2-6 labels (<=6 words each) for the distinct points in order; empty list "
    "if number is false."
)

_COHERENCE_SYSTEM = (
    "An assistant wrote this Telegram reply to the user with a numbered key "
    "(① ② ③) marking topics in the body and mirrored in the TLDR at the bottom. Verify "
    "the LOGIC is correct: does each numbered line in the TLDR accurately summarize the "
    "body block that carries the SAME number, in the same order, with nothing "
    "mismatched or pointing at the wrong topic? The summary can be terse - it only has "
    "to refer to the RIGHT thing.\n\n"
    "Return coherent=false ONLY if a TLDR number clearly refers to a different topic "
    "than its same-numbered body block (a genuine mismatch). If the mapping is right, "
    "return true - do not nitpick wording.\n\n"
    "Respond with ONLY compact JSON - no prose, no code fence:\n"
    '{"coherent": true|false, "problem": "..."}\n'
    "problem: one short phrase naming the mismatch (e.g. \"TLDR ② summarizes the "
    "third topic\"); empty string if coherent."
)

NO_TLDR_REASON = (
    "This long reply has no TLDR. Per the Telebots rule (Numbered TLDR on long channel "
    "replies): end long replies with a TLDR at the very bottom. Number by default - if "
    "it has 2+ distinct points, mark each in the body with a circled number at the "
    "start of its header line (① ② ③) and mirror the SAME numbers, same order, one "
    "line each, in the TLDR. If it is a single atomic point, a one or two line plain "
    "TLDR is fine. If this message is exempt (a pure deliverable like a "
    "drafted email / list / table / code, or a live progress update), resend it "
    "UNCHANGED and it will go through."
)

NO_KEY_REASON = (
    "Your TLDR is a plain numbered list (1. 2. ...) instead of the circled key. Per "
    "the Telebots rule (Numbered TLDR on long channel replies): mark each distinct topic "
    "in the BODY with a circled number at the start of its header line (① ② ③) and "
    "mirror those SAME circled numbers, same order, one terse line each, in the TLDR. "
    "Swap the plain 1. 2. for ① ② (not keycap emoji). If this is a SINGLE "
    "atomic point, drop the numbering for a one or two line plain TLDR. (If truly "
    "exempt, resend UNCHANGED and it will go through.)"
)


def _no_number_reason(topics):
    """Nudge for the aggressive NUMBER judge: names the points so the model knows what
    to number."""
    labels = [str(t).strip() for t in topics[:6] if str(t).strip()]
    listed = "; ".join(labels)
    return (
        f"Number this reply. It has {len(labels)} distinct points ({listed}), and the user "
        "wants replies numbered by default. Per the Telebots rule (Numbered TLDR on long "
        "channel replies): mark each point in the BODY with a circled number at the "
        "start of its header line (① ② ③ ...) and mirror the SAME circled numbers, "
        "same order, one terse line each, in the TLDR. Use the circled numbers ① ② ③ "
        "(not keycap emoji, not plain 1. 2.). If you judge this is ONE atomic "
        "point, resend it UNCHANGED and it will go through."
    )


def _mapping_reason(detail):
    """Nudge for a numbered reply whose key does not form a correct 1:1 ordered index."""
    return (
        f"Your numbered key does not line up: {detail}. Per the Telebots rule (Numbered "
        "TLDR on long channel replies), the numbers must be a perfect index: mark the "
        "body topics ① ② ③ ... in order at the start of their header lines, and mirror "
        "the EXACT same numbers, same order, one line each, in the TLDR - no gaps, no "
        "extra or missing numbers, TLDR line ② must be the ② body block. Fix the "
        "mapping so it lines up, or if it is really one atomic point drop the numbers "
        "for a plain TLDR."
    )


def _coherence_nudge(problem):
    p = str(problem).strip()
    tail = f" ({p})" if p else ""
    return (
        "Your numbered TLDR points at the wrong topic" + tail + ". Per the Telebots rule "
        "(Numbered TLDR on long channel replies), TLDR line ② must summarize the ② "
        "block in the body, ③ the ③ block, and so on - same number, same thing, same "
        "order. Re-map each TLDR line to its matching body topic so the index is "
        "correct, then resend. (If it is actually correct, resend UNCHANGED and it "
        "will go through.)"
    )


def _line_markers(lines):
    """Ordered list of circled-number values that appear at the START of a line."""
    out = []
    for ln in lines:
        s = ln.lstrip()
        if s and s[0] in _CIRC:
            out.append(_CIRC[s[0]])
    return out


def _tldr_index(lines):
    """Index of the TLDR header line, or None. The rule puts the TLDR at the very
    bottom, so the LAST candidate is always the real header: a strict header line
    ("TLDR" alone) wins outright, and only if there is none does a looser "tldr..."
    prefix line count. This keeps a reply that OPENS with "TLDR compliance is on" from
    being split at the top and bounced for an unmarked body."""
    strict = [i for i, l in enumerate(lines) if _TLDR_HEAD.match(l)]
    if strict:
        return strict[-1]
    loose = [i for i, l in enumerate(lines) if l.strip().lower().startswith("tldr")]
    return loose[-1] if loose else None


def _split_body_tldr(text):
    """(body_lines, tldr_lines) around the TLDR header line. If there is no TLDR
    header, everything is body and the TLDR side is empty."""
    lines = text.splitlines()
    idx = _tldr_index(lines)
    if idx is None:
        return lines, []
    return lines[:idx], lines[idx + 1:]


def _tldr_is_empty(text):
    """True when the reply carries a bare TLDR header with nothing under it - a TLDR in
    name only, which counts as no TLDR at all. A header carrying its own content
    ("TLDR Migration done - verify DNS tonight") is NOT empty: that one-line style is
    the approved shape for a single atomic point. Emptiness is judged with the
    zero-width characters deleted, so a section holding only U+200B and friends is the
    empty TLDR the reader actually sees."""
    lines = text.splitlines()
    idx = _tldr_index(lines)
    if idx is None:
        return False
    if not _TLDR_HEAD.match(lines[idx].translate(_INVISIBLE)):
        return False
    return not any(l.translate(_INVISIBLE).strip() for l in lines[idx + 1:])


def _has_markers(text):
    return bool(_line_markers(text.splitlines()))


def _is_long(text):


    if len(text) > 500 or text.count("\n") > 5:
        return True
    bullets = sum(1 for ln in text.splitlines() if re.match(r"^\s*[-*•]\s", ln))
    return bullets >= 2


def _substantive(text):
    """Lower bar than _is_long: worth CONSIDERING for a numbered key (the user wants
    numbering by default). The NUMBER judge then decides atomic-vs-multi; this gate
    just avoids paying for a judge call on a genuine one-liner / quick ack."""
    if len(text) > 220 or text.count("\n") > 2:
        return True
    return sum(1 for ln in text.splitlines() if re.match(r"^\s*[-*•]\s", ln)) >= 2


def _has_tldr(text):
    """Presence of a TLDR anywhere in the reply, in either spelling. _tldr_index still
    owns WHERE the header is; this only answers whether there is one at all."""
    return any(_TLDR_ANY.match(ln) for ln in text.splitlines())


def _needs_key(text):
    """True when a long reply's TLDR is a plain NUMBERED jump-index (1. 2. ...) with no
    circled key. Free fast-path to convert plain digits to the circled key."""
    if _has_markers(text):
        return False
    _, tldr_lines = _split_body_tldr(text)
    tldr_num = sum(1 for l in tldr_lines if l.strip() and _TLDR_NUM.match(l))
    return tldr_num >= 2


def structural_reason(text):
    """Free, deterministic coherence of the circled key. Returns a nudge if the reply
    USES the key but the body/TLDR numbers do not form a correct, matching, ordered
    1..N index, or if the key is a single numbered point; None if the key is
    coherent OR the reply uses no key at all."""
    body_lines, tldr_lines = _split_body_tldr(text)
    body = _line_markers(body_lines)
    tl = _line_markers(tldr_lines)
    if not body and not tl:
        return None
    n = max(max(body, default=0), max(tl, default=0))
    expected = list(range(1, n + 1))
    if not tl:
        return _mapping_reason(
            "the body is numbered but the TLDR has no matching key - mirror the same "
            "circled numbers in the TLDR")
    if not body:
        return _mapping_reason(
            "the TLDR is numbered but the body topics are not marked - put the same "
            "circled numbers at the start of each topic's header line")
    if tl != expected:
        return _mapping_reason(
            "the TLDR numbers are " + _fmt(tl) + " but should be " + _fmt(expected) +
            " in order (no gaps, no repeats, starting at ①)")
    if body != expected:
        return _mapping_reason(
            "the body markers are " + _fmt(body) + " but should be " + _fmt(expected) +
            " in order to match the TLDR")


    if n == 1:
        return _mapping_reason(
            "there is only ONE numbered point (" + _fmt([1]) + "), and a single atomic "
            "point should not be numbered - drop the key and close with a one or two "
            "line plain TLDR instead")


    return None


def _fmt(nums):
    inv = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
    return " ".join(inv[i - 1] if 1 <= i <= len(inv) else str(i) for i in nums) or "(none)"


def reason_for(text):
    """Pure classification (no I/O). Returns a nudge reason or None.
      - if the reply uses the circled key -> structural coherence check
      - else if long + no TLDR, or only a bare header with nothing under it -> NO_TLDR
      - else if long + plain-digit TLDR -> NO_KEY (convert to circled)
      - else -> None (a long unnumbered prose reply; the NUMBER judge decides in main)
    Short unnumbered replies -> None (allowed)."""
    if _has_markers(text):
        return structural_reason(text)
    if not _is_long(text):
        return None
    if not _has_tldr(text) or _tldr_is_empty(text):
        return NO_TLDR_REASON
    if _needs_key(text):
        return NO_KEY_REASON
    return None


def _api_key():
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=_KEY_TIMEOUT,
        )
        return r.stdout.strip() or None
    except Exception:
        return None


def _parse_verdict(txt):
    s = (txt or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s).rstrip("`").strip()
    try:
        return json.loads(s)
    except Exception:
        m = re.search(r"\{.*\}", s, re.S)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except Exception:
            return None


def _judge(text, key, system):
    """Call Haiku with a given system prompt. Returns the parsed verdict dict, or None
    on any error (-> fail open). Stdlib only."""
    import urllib.request
    body = json.dumps({
        "model": _JUDGE_MODEL,
        "max_tokens": 220,
        "system": system,
        "messages": [{"role": "user", "content": text[:_MAX_JUDGE_INPUT]}],
    }).encode("utf-8")
    req = urllib.request.Request(
        _API_URL, data=body, method="POST",
        headers={
            "content-type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        },
    )
    with urllib.request.urlopen(req, timeout=_JUDGE_TIMEOUT) as resp:
        payload = json.loads(resp.read().decode("utf-8", "ignore"))
    parts = payload.get("content", []) or []
    out = next((b.get("text", "") for b in parts if b.get("type") == "text"), "")
    return _parse_verdict(out)


def _judge_off(state_dir):
    if os.path.exists(os.path.join(state_dir, "tldr_judge_off")):
        return True
    try:
        root = os.environ.get("FLEET_HOME") or os.path.expanduser("~/.claude/telegram-fleet")
        with open(os.path.join(root, "fleet.json")) as handle:
            options = json.load(handle).get("options") or {}
        return options.get("tldr_judge") is not True
    except Exception:
        return True


def number_reason(text, state_dir):
    """NUMBER judge: a substantive UNNUMBERED reply -> should it carry a numbered key?
    Aggressive (the user wants numbering by default), gated on _substantive so one-liners
    and quick acks are never judged. Fails OPEN."""
    if not _judge_worthy(text) or _has_markers(text) or _judge_off(state_dir):
        return None
    key = _api_key()
    if not key:
        return None
    v = _judge(text, key, _NUMBER_SYSTEM)


    if not isinstance(v, dict) or v.get("number") is not True:
        return None
    raw = v.get("topics")
    if not isinstance(raw, list):
        return None
    topics = [t for t in raw if str(t).strip()]
    if len(topics) < 2:
        return None
    return _no_number_reason(topics)


def coherence_reason(text, state_dir):
    """COHERENCE judge: a structurally-sound NUMBERED reply -> do the TLDR numbers
    correctly correspond, in order, to the body blocks? Fails OPEN."""
    if not _has_markers(text) or not _judge_worthy(text) or _judge_off(state_dir):
        return None
    key = _api_key()
    if not key:
        return None
    v = _judge(text, key, _COHERENCE_SYSTEM)
    if not isinstance(v, dict) or v.get("coherent") is not False:
        return None
    return _coherence_nudge(v.get("problem", ""))


_SEEN_KEEP = 1000
_LOCK_TRIES = 10
_LOCK_WAIT = 0.15


def _seen_file(state_dir):
    return os.path.join(state_dir, "tldr_seen.txt")


def _lock(f):
    """Take the exclusive lock, never blocking on it. A wedged holder (suspended mid
    write, crashed) must not stall the reply path, so the wait is bounded and a lock
    still busy at the end degrades to the unlocked write - the same graceful path taken
    when fcntl is missing entirely."""
    if fcntl is None:
        return False
    for i in range(_LOCK_TRIES):
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            pass
        except Exception:
            return False
        if i + 1 < _LOCK_TRIES:
            time.sleep(_LOCK_WAIT)
    return False


def _seen_check(state_dir, h):
    """True when this exact text was already nudged. Any error -> False (re-check it)."""
    try:
        with open(_seen_file(state_dir)) as f:
            return h in f.read().split()
    except Exception:
        return False


def _seen_record(state_dir, h):
    """Add h under an exclusive lock, trimmed to the last _SEEN_KEEP. Reports what
    happened, because only a hash that is durably on disk may be answered with a deny:
      "recorded" - h is now in the file and a resend will be recognised
      "seen"     - another invocation recorded h first (the membership re-check under
                   the lock is what keeps two racing copies of one text to ONE deny)
      "failed"   - nothing could be written (bad state dir, permissions, full disk)
    Both non-"recorded" answers are allow answers at the call site."""
    try:
        with open(_seen_file(state_dir), "a+") as f:
            locked = _lock(f)
            try:
                f.seek(0)
                seen = f.read().split()
                if h in seen:
                    return "seen"
                seen.append(h)


                f.seek(0)
                f.truncate()
                f.write("\n".join(seen[-_SEEN_KEEP:]))
                f.flush()
            finally:
                if locked:
                    try:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                    except Exception:
                        pass
    except Exception:
        return "failed"
    return "recorded"


def _exempt_ack(text):
    """Short acknowledgements and live progress notes are never checked at all
    (the user, 2026-08-06): at most _ACK_MAX_CHARS chars, at most 4 lines, no bullet
    lines. Anything bigger falls through to the normal checks."""
    if len(text) > _ACK_MAX_CHARS or text.count("\n") > 4:
        return False
    return not any(re.match(r"^\s*[-*\u2022]\s", ln) for ln in text.splitlines())


def _judge_worthy(text):
    """The paid Haiku judges only run on large replies."""
    return len(text) >= _JUDGE_MIN_CHARS or text.count("\n") >= _JUDGE_MIN_NL


_BUBBLE_ASK = (
    "reply with", "pick one", "pick a", "which one", "which do you", "want me to",
    "should i ", "shall i", "do you want", "yes or no", "say go", "say yes",
    "or say ", "tell me which", "your call", "let me know which", "which of these",
)


_BUBBLE_ENUM = re.compile(r"^\s*(?:[\u2460-\u2463]|\d+\.\s)")

_BUBBLE_ENUM_WORDS = ("pick", "choose", "reply with")

_BUBBLE_ACK_OPENERS = ("on it", "got it", "done")

_BUBBLE_MIN_CHARS = 60

BUBBLE_REASON = (
    "This reply asks the user to pick or confirm but has no bubbles. Resend it with `buttons` "
    "(2-4 short labels, under 18 characters: the option names, or \u2460 \u2461 \u2462 "
    "for numbered options, plus a 'Not now' style escape). Email uses email_preview and email_send. "
    "Resending it unchanged goes through."
)


def _bubble_scannable(text):
    """The part of the reply the guard reads: everything ABOVE the TLDR header. A TLDR
    line is a recap of what was already said, so a rhetorical "which one?" down there is
    not a live question - and the circled key every long reply carries down there would
    otherwise read as a menu. _tldr_index owns where the header is (it already takes the
    LAST one, and accepts the TL;DR spellings), so more text is ignored here rather than
    less, which errs toward allowing the send."""
    lines = text.splitlines()
    idx = _tldr_index(lines)
    return lines if idx is None else lines[:idx]


def bubble_reason(text, tool_input):
    """Deny reason when a reply asks the user to choose or confirm and carries no bubbles;
    None otherwise. Pure, free and deterministic - no I/O of any kind."""
    ti = tool_input if isinstance(tool_input, dict) else {}


    buttons = ti.get("buttons")
    if isinstance(buttons, (list, tuple)) and any(str(b).strip() for b in buttons):
        return None
    if len(text) < _BUBBLE_MIN_CHARS:
        return None
    if "```" in text:
        return None
    if (text.lstrip().lower()).startswith(_BUBBLE_ACK_OPENERS):
        return None
    files = ti.get("files")
    if (isinstance(files, (list, tuple)) and any(str(f).strip() for f in files)
            and "?" not in text):
        return None


    lines = _bubble_scannable(text)
    body = "\n".join(lines).lower()
    if any(s in body for s in _BUBBLE_ASK):
        return BUBBLE_REASON
    for ln in lines:
        s = ln.strip().lower()
        if s.endswith("?") and " or " in s:
            return BUBBLE_REASON
    enumerated = sum(1 for ln in lines if _BUBBLE_ENUM.match(ln))
    if enumerated >= 2 and any(w in body for w in _BUBBLE_ENUM_WORDS):
        return BUBBLE_REASON
    return None


def main():
    state_dir = os.environ.get("TELEGRAM_STATE_DIR")
    if not state_dir:
        return
    if os.path.exists(os.path.join(state_dir, "tldr_hook_off")):
        return
    raw = sys.stdin.read()
    if not raw.strip():
        return
    data = json.loads(raw)
    if data.get("tool_name") != "mcp__plugin_telegram_telegram__reply":
        return
    text = str(data.get("tool_input", {}).get("text", ""))


    h = hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:16]


    breason = bubble_reason(text, data.get("tool_input"))
    if breason:
        if _seen_check(state_dir, h):
            return
        if _seen_record(state_dir, h) != "recorded":
            return
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": breason,
        }}))
        return

    if _exempt_ack(text):
        return


    if _seen_check(state_dir, h):
        return

    reason = reason_for(text)
    if reason is None:
        if _has_markers(text):
            reason = coherence_reason(text, state_dir)
        else:
            reason = number_reason(text, state_dir)
    if reason is None:
        return


    if _seen_record(state_dir, h) != "recorded":
        return
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
