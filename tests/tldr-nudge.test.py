#!/usr/bin/env python3
# Unit tests for tldr-nudge (pure classifiers + offline judge guards).
# Run: python3 tests/tldr-nudge.test.py   -- offline, never sends an API request.
# Loads the hook from ../plugin/hooks/tldr-nudge.py so it works from the repo root.
import importlib.util, os, sys, tempfile

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)
_hook = os.path.join(_root, "plugin", "hooks", "tldr-nudge.py")
_spec = importlib.util.spec_from_file_location("tldr_nudge", _hook)
tn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tn)

# ~1300 chars of plain prose (no enum markers, no "tldr" line) to make a reply LONG.
FILLER = ("This is a realistic long status update about the work that was done today "
          "on the account, written in ordinary prose so that the message clears the "
          "length threshold the way a genuine reply would, without introducing any "
          "list markers or headers that the classifier might key on by accident. ") * 4

def L(*parts):
    return FILLER + "\n" + "\n".join(parts)

NONE, KEY, TLDR, MAP = None, "KEY", "TLDR", "MAP"

def kind(reason):
    if reason is None: return NONE
    if reason is tn.NO_KEY_REASON: return KEY
    if reason is tn.NO_TLDR_REASON: return TLDR
    if isinstance(reason, str) and "does not line up" in reason: return MAP
    return "?"

cases = []
def case(name, text, expect): cases.append((name, text, expect))

# --- short / unnumbered: never nudge from the pure layer ---
case("short plain", "Tuesday.", NONE)
case("short two plain-digit lines (not long)", "1. do X\n2. do Y", NONE)

# --- long, unnumbered, missing TLDR -> NO_TLDR ---
case("long no tldr", L("Here is a lot of detail about the migration and the DNS."), TLDR)

# --- long, unnumbered, plain-digit TLDR (no circled key) -> NO_KEY (convert to circled) ---
case("plain-digit tldr, no circled key", L("Body about the deal and the change.",
     "", "TLDR", "1. Migration done, verify DNS tonight", "2. Invoice bot blocked, send the Stripe key"), KEY)

# --- long, unnumbered, plain-prose TLDR -> None from the PURE layer (the NUMBER judge
#     decides in main; not tested here since it needs the network) ---
case("single-topic prose tldr (pure=None)", L("The signup form now asks Name, Email, Company, Role.",
     "", "TLDR", "Signup now asks Name, Email, Company, Role only, nothing else."), NONE)
case("dash-bullet tldr (pure=None)", L("Single-topic ship report, all one deploy.",
     "", "TLDR", "- Refresh live and verified", "- Rollback tag saved"), NONE)

# --- body plain-digit numbering is NOT a circled marker; not this layer's trigger ---
case("body plain-digit steps, inline tldr (pure=None)", L(
     "Migration done, full sequence:",
     "1. Snapshotted the project", "2. Applied the migrations", "3. Backfilled the column",
     "", "TLDR: migration shipped clean, nothing needed from you."), NONE)
case("decimals at line start are not markers", L(
     "3.2 million is the figure", "4.5 percent is the fee",
     "", "TLDR", "Numbers look strong."), NONE)
case("circled chars MID-sentence are decoration, not markers", L(
     "Body with ① first thing and ② second thing spelled out inline.",
     "", "TLDR", "Both handled, details above."), NONE)

# ============================================================================
# STRUCTURAL coherence of the circled key (the mapping must be perfect). A reply
# that USES the key must have body markers and TLDR markers forming a correct,
# matching, ordered 1..N index.
# ============================================================================

# coherent: body ① ② ③ mirrored by TLDR ① ② ③ -> allow
case("coherent 3-topic key", L("Three separate things for you.",
     "① First item wants only Name/Email/Company/Role at signup",
     "② Automations read Name and Email only",
     "③ Add Role, untick the 8 fields, delete nothing",
     "", "TLDR",
     "① First item wants Name/Email/Company/Role only",
     "② Automations read Name and Email only",
     "③ Add Role, untick 8 fields, delete nothing"), NONE)

# coherent and SHORT (structural runs regardless of length) -> allow
case("coherent 2-topic short", "① DNS is cut over\n② Invoice bot still needs the key\n\nTLDR\n① DNS cut over\n② Invoice bot needs the key", NONE)

# TLDR has an extra ③ with no matching body block -> MAP (the old 'passes' case, now caught)
case("tldr numbers exceed body markers", L("Two separate things for you.",
     "① First item wants only Name/Email/Company/Role",
     "② Automations read Name and Email only",
     "", "TLDR",
     "① First item wants Name/Email/Company/Role",
     "② Automations read Name and Email only",
     "③ Add Role, untick 8 fields, delete nothing"), MAP)

# TLDR numbered but body topics unmarked (prose body) -> MAP
case("tldr-only key, prose body", L("Formation is done and everything is filed and tracked.",
     "", "TLDR", "① Entity formed", "② Papers filed in the vault", "③ Next deadline 2028"), MAP)

# body marked but TLDR not mirrored -> MAP
case("body-only key, prose tldr", L(
     "① Entity formed and publication-compliant",
     "② Papers filed under the company docs",
     "③ Next deadline is the 2028 biennial",
     "", "TLDR", "All done, only operational to-dos left, next filing 2028."), MAP)

# gap: TLDR ① ③ (missing ②) -> MAP
case("gap in the tldr key", L(
     "① first", "② second", "③ third",
     "", "TLDR", "① first thing", "③ third thing"), MAP)

# out of order in the TLDR -> MAP
case("out-of-order tldr key", L(
     "① first", "② second", "③ third",
     "", "TLDR", "② second thing", "① first thing", "③ third thing"), MAP)

fails = 0
for name, text, expect in cases:
    got = kind(tn.reason_for(text))
    ok = (got == expect)
    if not ok: fails += 1
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: expected={expect} got={got}")
print(f"\n{len(cases)-fails}/{len(cases)} passed (pure layer)")

# ============================================================================
# Offline guards for the two AI-judge calls (NO network: each returns before any
# config / Keychain / API call, because the judge is default-OFF and gated).
# ============================================================================
_jd = tempfile.mkdtemp(prefix="tldr_judge_test_")
LONG_PROSE = FILLER + "\n\nTLDR\nOne flowing prose takeaway, no circled key."
NUMBERED = "① a\n② b\n\nTLDR\n① a\n② b"

jfails = 0
def jcase(name, cond):
    global jfails
    if not cond: jfails += 1
    print(f"[{'PASS' if cond else 'FAIL'}] judge:{name}")

# NUMBER judge guards
jcase("number: short reply skips judge", tn.number_reason("Tuesday.", _jd) is None)
jcase("number: already-numbered reply skips judge", tn.number_reason(NUMBERED, _jd) is None)
# COHERENCE judge guards
jcase("coherence: unnumbered reply skips judge", tn.coherence_reason(LONG_PROSE, _jd) is None)
# kill switch disables both, no network
open(os.path.join(_jd, "tldr_judge_off"), "w").close()
jcase("number: tldr_judge_off skips judge", tn.number_reason(LONG_PROSE, _jd) is None)
jcase("coherence: tldr_judge_off skips judge", tn.coherence_reason(NUMBERED, _jd) is None)

_total = fails + jfails
print(f"\nTOTAL {(len(cases)+5)-_total}/{len(cases)+5} passed")
sys.exit(1 if _total else 0)
