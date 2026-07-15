#!/usr/bin/env python3
# PreToolUse hook: enforce a numbered TLDR on Telegram replies, and enforce that the
# numbered key is LOGICALLY CORRECT - every ① ② ③ in the TLDR maps to the right block
# in the body. Policy: number by DEFAULT (even a single-subject reply with 2+ parts);
# the one hard rule is that the number->topic mapping must be perfect.
#
# Advisory and safe by design:
#   - fires ONLY on mcp__plugin_telegram_telegram__reply (matcher + in-script check)
#   - env-gated on TELEGRAM_STATE_DIR (inert outside a channel session)
#   - kill switches:
#       touch $TELEGRAM_STATE_DIR/tldr_hook_off   -> disable the WHOLE hook
#       touch $TELEGRAM_STATE_DIR/tldr_judge_off  -> disable only the AI-judge checks
#                                                    (keeps the free regex/structural
#                                                     checks, stops ALL API spend)
#   - nudges a given message AT MOST ONCE (resending the same text always passes),
#     so it can never get stuck blocking the reply path
#   - fails OPEN: any error -> allow the send (never blocks on a bug)
#
# Three checks, cheapest first. Any one can nudge; a nudge DENIES the reply once with a
# specific fix, and the model either fixes it or resends unchanged (which always sends).
#   A. STRUCTURAL (free, deterministic) - if the reply uses the circled key, the body
#      markers and the TLDR markers must be a correct, matching, ordered 1..N index
#      (no gaps, no orphans, no wrong order, no key-in-body-but-not-TLDR). This is the
#      backbone of "the logic must be perfect". Also: a long reply with no TLDR, or a
#      plain-digit "1. 2." TLDR, is caught here for free. These free checks ALWAYS run.
#   B. COHERENCE JUDGE (Haiku, ~1s) - for a reply that is STRUCTURALLY sound, a small
#      model verifies the SEMANTICS: does the ② TLDR line actually summarize the ②
#      body block, in order, nothing mismatched? Catches a key that lines up on the
#      numbers but points the wrong summary at the wrong topic.
#   C. NUMBER JUDGE (Haiku, ~1s) - for a long UNNUMBERED reply, a small model decides
#      if it has 2+ distinct points worth a numbered index (aggressive: default yes
#      for substantive replies; a genuinely atomic one-liner stays plain).
#
# The AI judge (checks B and C) is OFF BY DEFAULT. It runs only when BOTH:
#   (1) `fleet-config option tldr_judge` is true (in fleet.json options), and
#   (2) an API key resolves via this chain, in order:
#         env ANTHROPIC_API_KEY_TLDR  ->  $TELEGRAM_STATE_DIR/.env line  ->
#         macOS Keychain service "nexera-telebots-judge" (darwin only).
# The judge sends the draft reply text to the Anthropic API (the user's own key, their
# own content). The key is never stored in this file. If the config is off or no key
# resolves, the judge is silently skipped and only the free structural checks apply.
import sys, os, json, hashlib, re, subprocess

# Circled numbers ① .. ⑳ -> their integer value. A MARKER is one of these at the START
# of a line (the rule puts topic numbers at the start of the header line and of each
# TLDR line). A circled char mid-sentence is decoration, not a marker, so we only look
# at line starts.
_CIRC = {c: i + 1 for i, c in enumerate("①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳")}
# A TLDR line that opens a plain NUMBERED item ("1." / "1)" / "(1)"). Trailing \s stops
# it matching decimals/prices ("1.9s", "$3").
_TLDR_NUM = re.compile(r"^\s*(?:\d+[.)]|\(\d+\))\s")

# --- AI-judge config -------------------------------------------------------------
_JUDGE_MODEL = "claude-haiku-4-5"                   # fastest + cheapest capable Claude
_KEYCHAIN_SERVICE = "nexera-telebots-judge"   # darwin Keychain service (3rd in chain)
_ENV_KEY_NAMES = ("ANTHROPIC_API_KEY_TLDR", "ANTHROPIC_API_KEY")  # accepted .env line names
_API_URL = "https://api.anthropic.com/v1/messages"
_KEY_TIMEOUT = 3.0                                  # security(1) fetch ceiling (s)
_JUDGE_TIMEOUT = 5.0                                # HTTP ceiling (s); typical ~1s
_MAX_JUDGE_INPUT = 8000                             # cap chars sent (bounds tokens)

# Fleet config lookup (for the default-OFF judge gate). FLEET_HOME env override, else
# the standard install dir. The `tldr_judge` option lookup is cached for the process.
_FLEET_HOME = os.environ.get("FLEET_HOME") or os.path.expanduser("~/.claude/telegram-fleet")
_FLEET_CONFIG = os.path.join(_FLEET_HOME, "bin", "fleet-config")
_CONFIG_TIMEOUT = 3.0
_config_judge_cache = None  # None = not yet looked up; then True/False

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
    "This long reply has no TLDR. Per the global rule (Numbered TLDR on long channel "
    "replies): end long replies with a TLDR at the very bottom. Number by default - if "
    "it has 2+ distinct points, mark each in the body with a circled number at the "
    "start of its header line (① ② ③) and mirror the SAME numbers, same order, one "
    "line each, in the TLDR. If it is a single atomic point, a one or two line plain "
    "TLDR is fine. If this message is genuinely exempt (a pure deliverable like a "
    "drafted email / list / table / code, or a live progress update), resend it "
    "UNCHANGED and it will go through."
)

NO_KEY_REASON = (
    "Your TLDR is a plain numbered list (1. 2. ...) instead of the circled key. Per "
    "the global rule (Numbered TLDR on long channel replies): mark each distinct topic "
    "in the BODY with a circled number at the start of its header line (① ② ③) and "
    "mirror those SAME circled numbers, same order, one terse line each, in the TLDR. "
    "Swap the plain 1. 2. for ① ② (not keycap emoji). If this is genuinely a SINGLE "
    "atomic point, drop the numbering for a one or two line plain TLDR. (If truly "
    "exempt, resend UNCHANGED and it will go through.)"
)


def _no_number_reason(topics):
    """Nudge for the aggressive NUMBER judge: names the points so the model knows what
    to number."""
    labels = [str(t).strip() for t in topics[:6] if str(t).strip()]
    listed = "; ".join(labels)
    return (
        f"Number this reply. It has {len(labels)} distinct points ({listed}), and the "
        "user wants replies numbered by default. Per the global rule (Numbered TLDR on "
        "long "
        "channel replies): mark each point in the BODY with a circled number at the "
        "start of its header line (① ② ③ ...) and mirror the SAME circled numbers, "
        "same order, one terse line each, in the TLDR. Use the circled numbers ① ② ③ "
        "(not keycap emoji, not plain 1. 2.). If you judge this is genuinely ONE atomic "
        "point, resend it UNCHANGED and it will go through."
    )


def _mapping_reason(detail):
    """Nudge for a numbered reply whose key does not form a correct 1:1 ordered index."""
    return (
        f"Your numbered key does not line up: {detail}. Per the global rule (Numbered "
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
        "Your numbered TLDR points at the wrong topic" + tail + ". Per the global rule "
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


def _split_body_tldr(text):
    """(body_lines, tldr_lines) around the first 'TLDR' header line. If there is no
    TLDR header, everything is body and the TLDR side is empty."""
    lines = text.splitlines()
    idx = next(
        (i for i, l in enumerate(lines) if l.strip().lower().startswith("tldr")),
        None,
    )
    if idx is None:
        return lines, []
    return lines[:idx], lines[idx + 1:]


def _has_markers(text):
    return bool(_line_markers(text.splitlines()))


def _is_long(text):
    # Policy: anything past a quick point gets a TLDR. A short but STRUCTURED
    # reply (2+ bullet lines) also counts.
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
    return any(ln.strip().startswith("tldr") for ln in text.lower().splitlines())


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
    1..N index; None if the key is coherent OR the reply uses no key at all."""
    body_lines, tldr_lines = _split_body_tldr(text)
    body = _line_markers(body_lines)
    tl = _line_markers(tldr_lines)
    if not body and not tl:
        return None  # no key in use -> not this check's job
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
    return None  # structurally coherent


def _fmt(nums):
    inv = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
    return " ".join(inv[i - 1] if 1 <= i <= len(inv) else str(i) for i in nums) or "(none)"


def reason_for(text):
    """Pure classification (no I/O). Returns a nudge reason or None.
      - if the reply uses the circled key -> structural coherence check
      - else if long + no TLDR -> NO_TLDR
      - else if long + plain-digit TLDR -> NO_KEY (convert to circled)
      - else -> None (a long unnumbered prose reply; the NUMBER judge decides in main)
    Short unnumbered replies -> None (allowed)."""
    if _has_markers(text):
        return structural_reason(text)
    if not _is_long(text):
        return None
    if not _has_tldr(text):
        return NO_TLDR_REASON
    if _needs_key(text):
        return NO_KEY_REASON
    return None


# --- AI-judge layer (I/O; reached from main after the free checks) ---------------
def _env_file_key(env_path):
    """Read the judge key from a $STATE_DIR/.env line (ANTHROPIC_API_KEY_TLDR, else
    ANTHROPIC_API_KEY). Returns the value or None."""
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                for name in _ENV_KEY_NAMES:
                    if line.startswith(name + "="):
                        v = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if v:
                            return v
    except Exception:
        pass
    return None


def _api_key(state_dir):
    """Resolve the judge key, in order: env ANTHROPIC_API_KEY_TLDR -> $STATE_DIR/.env
    line -> macOS Keychain service 'nexera-telebots-judge' (darwin only). Returns
    the key or None (absent -> judge silently off). Never logs or echoes the key."""
    k = os.environ.get("ANTHROPIC_API_KEY_TLDR")
    if k and k.strip():
        return k.strip()
    k = _env_file_key(os.path.join(state_dir, ".env"))
    if k:
        return k
    if sys.platform.startswith("darwin"):
        try:
            r = subprocess.run(
                ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-w"],
                capture_output=True, text=True, timeout=_KEY_TIMEOUT,
            )
            out = r.stdout.strip()
            if out:
                return out
        except Exception:
            pass
    return None


def _config_tldr_judge():
    """Cached lookup of `fleet-config option tldr_judge`. True ONLY when the option
    resolves to the literal 'true'. Any error (config missing, not installed, timeout)
    -> False, so the judge stays OFF by default and fails open."""
    global _config_judge_cache
    if _config_judge_cache is not None:
        return _config_judge_cache
    val = False
    try:
        if os.path.exists(_FLEET_CONFIG):
            r = subprocess.run(
                [sys.executable, _FLEET_CONFIG, "option", "tldr_judge"],
                capture_output=True, text=True, timeout=_CONFIG_TIMEOUT,
            )
            val = (r.returncode == 0 and r.stdout.strip().lower() == "true")
    except Exception:
        val = False
    _config_judge_cache = val
    return val


def _judge_enabled(state_dir):
    """The AI judge runs only when it is NOT kill-switched for this bot AND the fleet
    config opts it in. Default OFF. The kill switch is checked first so it short-
    circuits before any config subprocess."""
    if _judge_off(state_dir):
        return False
    return _config_tldr_judge()


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
    return os.path.exists(os.path.join(state_dir, "tldr_judge_off"))


def number_reason(text, state_dir):
    """NUMBER judge: a substantive UNNUMBERED reply -> should it carry a numbered key?
    Aggressive (the user wants numbering by default), gated on _substantive so one-
    liners and quick acks are never judged. Judge is OFF unless config opts in and a
    key resolves. Fails OPEN."""
    if not _substantive(text) or _has_markers(text):
        return None
    if not _judge_enabled(state_dir):
        return None
    key = _api_key(state_dir)
    if not key:
        return None
    v = _judge(text, key, _NUMBER_SYSTEM)
    if not v or not v.get("number"):
        return None
    topics = [t for t in (v.get("topics") or []) if str(t).strip()]
    if len(topics) < 2:
        return None
    return _no_number_reason(topics)


def coherence_reason(text, state_dir):
    """COHERENCE judge: a structurally-sound NUMBERED reply -> do the TLDR numbers
    correctly correspond, in order, to the body blocks? Judge is OFF unless config opts
    in and a key resolves. Fails OPEN."""
    if not _has_markers(text):
        return None
    if not _judge_enabled(state_dir):
        return None
    key = _api_key(state_dir)
    if not key:
        return None
    v = _judge(text, key, _COHERENCE_SYSTEM)
    if not v or v.get("coherent") is not False:
        return None  # coherent, or unparseable/unknown -> allow (fail open)
    return _coherence_nudge(v.get("problem", ""))


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

    reason = reason_for(text)  # free: structural coherence + missing-TLDR / plain-digit
    if reason is None:
        if _has_markers(text):
            reason = coherence_reason(text, state_dir)  # numbered + sound -> verify map
        else:
            reason = number_reason(text, state_dir)     # unnumbered long -> should num?
    if reason is None:
        return  # allow

    # at-most-once (keyed on exact text): any unchanged resend always passes, and a
    # different fix is re-evaluated fresh. Delivery can never hard-block.
    h = hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:16]
    seen_path = os.path.join(state_dir, "tldr_seen.txt")
    seen = ""
    if os.path.exists(seen_path):
        with open(seen_path) as f:
            seen = f.read()
    if h in seen:
        return
    keep = (seen.split() + [h])[-200:]
    with open(seen_path, "w") as f:
        f.write("\n".join(keep))

    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # fail-open: never block the reply path on an error
    sys.exit(0)
