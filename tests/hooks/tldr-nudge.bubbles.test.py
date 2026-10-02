#!/usr/bin/env python3
import importlib.util, json, os, subprocess, sys, tempfile

_here = os.path.dirname(os.path.abspath(__file__))
_HOOK = os.path.join(os.path.dirname(os.path.dirname(_here)), "plugin", "hooks", "tldr-nudge.py")

spec = importlib.util.spec_from_file_location("tldr_nudge_bubbles", _HOOK)
tn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tn)
tn._api_key = lambda: None
tn._judge = lambda *a, **k: None

MENU = 'Four options, ranked:\n\n① Run tests\nCheck the pending build.\n② Build a dashboard\nShow the latest local status.\n③ Fix the layout\nImprove the spacing.\n④ Prepare a draft\nWrite a short update.\n\nReply with the number that you want.\n\nTLDR\n① Test the build\n② Show status\n③ Improve spacing\n④ Write an update'

LONG_INFO = 'Both checks passed.\n\n① Worker health\nEach configured worker reported a fresh heartbeat during the local check.\n\n② Package parity\nThe two package copies are identical. The running sessions will load the new copy on their next restart, and that is harmless.\n\nTLDR\n① Workers reported fresh heartbeats\n② Package copies match'

LONG_INFO_WHICH = LONG_INFO.replace(", and that is harmless", ", which is harmless")

RESEND = 'Two ways to clear the account lock: update its credentials tonight, or wait for the support team to unlock it in the morning. Which one do you want?'

CODE_DELIVERABLE = 'Draft ready to copy.\n\n```\nHi,\n\nThe checks passed and the report is ready.\n\nBest,\nThe team\n```\n\nWant me to send it, or hold for your edits?\n\nTLDR Draft is ready, nothing sent yet.'

DENY, ALLOW = "DENY", "ALLOW"
CASES = [

    ("D1 ranked four-option reply, no bubbles", MENU, {}, DENY, "DENY(bubble)"),
    ("D2 short either/or question the ack exemption would have passed",
     "Two ways to do this: patch the poller now, or wait for the next natural restart. "
     "Which one do you want?", {}, DENY, "DENY(bubble)"),
    ("D3 email ready, asks whether to send",
     "Draft is ready for the team. Subject is account support recap and the body is below "
     "the fold. Should I send it now, or hold it until Monday morning?", {}, DENY, "DENY(bubble)"),
    ("D4 want-me-to with two paths",
     "Want me to dispatch the agents on this tonight, or park the whole thing until "
     "tomorrow morning?", {}, DENY, "DENY(bubble)"),
    ("D5 numbered menu, your call / which of these",
     "Three ways in:\n1. Patch the poller now\n2. Wait for the natural restart\n"
     "3. Skip it entirely\nYour call on which of these fits the evening.", {}, DENY, "DENY(bubble)"),
    ("D6 yes-or-no confirm on a deadline",
     "The account lockout tripwire fires Wednesday, September 16. Do you want me to "
     "clear it tonight? Yes or no.", {}, DENY, "DENY(bubble)"),

    ("A1 short progress ack",
     "On it. Dispatching the account sweep now, back in about ten minutes with the "
     "first result.", {}, ALLOW, "ALLOW"),
    ("A2 the same menu reply, this time WITH bubbles", MENU,
     {"buttons": ["① Sweep", "② Mini App", "③ Polish", "④ Warm list"]},
     ALLOW, "ALLOW"),
    ("A3 fenced deliverable (asks a question, but it is a copy box)", CODE_DELIVERABLE, {},
     ALLOW, "ALLOW"),
    ("A4 long informational reply, TLDR, nothing asked", LONG_INFO, {}, ALLOW, "ALLOW"),
    ("A5 sent confirmation",
     "Sent at 7:42pm ET to team@example.test. Subject: account support recap.",
     {}, ALLOW, "ALLOW"),
    ("A6 attachment, no question mark in the text",
     "Invoice attached for the team, September retainer, 750 dollars, due on the first. "
     "Want me to draft the covering email as well.",
     {"files": ["/tmp/example-invoice.pdf"]}, ALLOW, "ALLOW"),
    ("A7 question under 60 characters", "Patch it now or later?", {}, ALLOW, "ALLOW"),
    ("A8 rhetorical question only inside the TLDR",
     "The watchdog restarted Main twice at 6:58pm ET because its heartbeat file went "
     "stale while the plugin slot was being rewritten. Both restarts were clean and the "
     "conversation survived, so there is nothing to undo and nothing to decide.\n\n"
     "TLDR\nMain bounced twice, clean, nothing to do. Which is exactly what should "
     "happen?", {}, ALLOW, "ALLOW"),

    ("F1 A4 again with 'which is harmless' - a pronoun, not a question", LONG_INFO_WHICH,
     {}, ALLOW, "ALLOW"),
]

RESEND_CASE = ("R1 must-deny text sent twice unchanged: deny once, then allow",
               RESEND, {}, DENY, "DENY(bubble)", "ALLOW")

FIX_NOTE = (
    "FIXED Sunday, September 13, 2026 (both were shipped as specified the same evening):\n"
    "  - the enumerated-menu signal no longer counts the bare word 'which' (F1 above now\n"
    "    allows; 'which one', 'which do you', 'which of these', 'tell me which' and\n"
    "    'let me know which' remain separate signals, see D2 and D5)\n"
    "  - the bubble guard spends the same at-most-once memory as the TLDR nudges\n"
    "    (tldr_seen.txt, text hash, capped), so an unchanged resend goes through (R1)"
)

def hook_verdict(state_dir, text, extra):
    """Run the hook the way Claude Code does: fake PreToolUse JSON on stdin."""


    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "mcp__plugin_telegram_telegram__reply",
        "tool_input": dict({"chat_id": "__CHAT_ID__", "text": text}, **extra),
    }
    env = dict(os.environ, TELEGRAM_STATE_DIR=state_dir)
    r = subprocess.run([sys.executable, _HOOK], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=30, env=env)
    out = r.stdout.strip()
    if not out:
        return "ALLOW", ""
    reason = json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
    return ("DENY(bubble)" if reason == tn.BUBBLE_REASON else "DENY(tldr)"), reason

def main():
    state_dir = tempfile.mkdtemp(prefix="bubble_state_test_")
    if "/.claude/channels" in state_dir:
        print("REFUSING to run against a real channel state dir")
        return 2
    open(os.path.join(state_dir, "tldr_judge_off"), "w").close()

    fails = 0
    print("%-58s %-6s %-12s %s" % ("case", "BUBBLE", "HOOK", "result"))
    print("-" * 96)
    for name, text, extra, want_bubble, want_hook in CASES:
        got_bubble = DENY if tn.bubble_reason(text, dict({"text": text}, **extra)) else ALLOW
        got_hook, _reason = hook_verdict(state_dir, text, extra)
        ok = (got_bubble == want_bubble) and (got_hook == want_hook)
        if not ok:
            fails += 1
        print("%-58s %-6s %-12s %s" % (name[:58], got_bubble, got_hook,
                                       "PASS" if ok else "FAIL (wanted %s / %s)" % (want_bubble, want_hook)))


    name, text, extra, want_bubble, want_first, want_second = RESEND_CASE
    got_bubble = DENY if tn.bubble_reason(text, dict({"text": text}, **extra)) else ALLOW
    got_first, _r1 = hook_verdict(state_dir, text, extra)
    got_second, _r2 = hook_verdict(state_dir, text, extra)
    ok = (got_bubble == want_bubble) and (got_first == want_first) and (got_second == want_second)
    if not ok:
        fails += 1
    print("%-58s %-6s %-12s %s" % (name[:58], got_bubble, got_first + " then " + got_second,
                                   "PASS" if ok else "FAIL (wanted %s / %s then %s)"
                                   % (want_bubble, want_first, want_second)))
    total = len(CASES) + 1

    print("-" * 96)
    print(FIX_NOTE)
    print("")
    print("the deny reason the model receives:")
    print("  " + tn.BUBBLE_REASON)
    print("")
    print("%d/%d cases passed (%d must deny, %d must allow, 1 deny-then-allow)"
          % (total - fails, total,
             sum(1 for c in CASES if c[3] == DENY), sum(1 for c in CASES if c[3] == ALLOW)))
    return 1 if fails else 0

if __name__ == "__main__":
    sys.exit(main())
