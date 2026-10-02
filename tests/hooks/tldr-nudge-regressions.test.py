#!/usr/bin/env python3
import subprocess
import contextlib, hashlib, importlib.util, io, json, threading, types, os, sys, tempfile, time

_here = os.path.dirname(os.path.abspath(__file__))
_HOOK = os.path.join(os.path.dirname(os.path.dirname(_here)), "plugin", "hooks", "tldr-nudge.py")
_TEST_FLEET = tempfile.mkdtemp(prefix="tldr-fleet-")
with open(os.path.join(_TEST_FLEET, "fleet.json"), "w") as handle:
    json.dump({"options": {"tldr_judge": True}}, handle)
os.environ["FLEET_HOME"] = _TEST_FLEET

def load_hook():
    spec = importlib.util.spec_from_file_location("tldr_nudge", _HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

tn = load_hook()

CALLS = {"judge": 0, "key": 0}
VERDICT = {"number": True, "topics": ["alpha", "beta"]}

def fake_api_key():
    CALLS["key"] += 1
    return "test-key-never-real"

def fake_judge(text, key, system):
    CALLS["judge"] += 1
    return VERDICT

tn._api_key = fake_api_key
tn._judge = fake_judge

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

case("short plain", "Tuesday.", NONE)
case("short two plain-digit lines (not long)", "1. do X\n2. do Y", NONE)

case("long no tldr", L("Here is a lot of detail about the migration and the DNS."), TLDR)

case("plain-digit tldr, no circled key", L("Body about the deal and the change.",
     "", "TLDR", "1. Migration done, verify DNS tonight", "2. Invoice bot blocked, send the Stripe key"), KEY)

case("single-topic prose tldr (pure=None)", L("The signup form now asks Name, Email, Company, Role.",
     "", "TLDR", "Signup now asks Name, Email, Company, Role only, nothing else."), NONE)
case("dash-bullet tldr (pure=None)", L("Single-topic ship report, all one deploy.",
     "", "TLDR", "- Refresh live and verified", "- Rollback tag saved"), NONE)

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

case("coherent 3-topic key", L("Three separate things for you.",
     "① The team wants only Name/Email/Company/Role at signup",
     "② Automations read Name and Email only",
     "③ Add Role, untick the 8 fields, delete nothing",
     "", "TLDR",
     "① The team wants Name/Email/Company/Role only",
     "② Automations read Name and Email only",
     "③ Add Role, untick 8 fields, delete nothing"), NONE)

case("coherent 2-topic short", "① DNS is cut over\n② Invoice bot still needs the key\n\nTLDR\n① DNS cut over\n② Invoice bot needs the key", NONE)

case("tldr numbers exceed body markers", L("Two separate things for you.",
     "① The team wants only Name/Email/Company/Role",
     "② Automations read Name and Email only",
     "", "TLDR",
     "① The team wants Name/Email/Company/Role",
     "② Automations read Name and Email only",
     "③ Add Role, untick 8 fields, delete nothing"), MAP)

case("tldr-only key, prose body", L("Formation is done and everything is filed and tracked.",
     "", "TLDR", "① LLC formed", "② Papers filed in the notes", "③ Next deadline 2028"), MAP)

case("body-only key, prose tldr", L(
     "① LLC formed and publication-compliant",
     "② Papers filed under company documents",
     "③ Next deadline is the 2028 biennial",
     "", "TLDR", "All done, only operational to-dos left, next filing 2028."), MAP)

case("gap in the tldr key", L(
     "① first", "② second", "③ third",
     "", "TLDR", "① first thing", "③ third thing"), MAP)

case("out-of-order tldr key", L(
     "① first", "② second", "③ third",
     "", "TLDR", "② second thing", "① first thing", "③ third thing"), MAP)

def run_pure_cases():
    fails = 0
    for name, text, expect in cases:
        got = kind(tn.reason_for(text))
        ok = (got == expect)
        if not ok: fails += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: expected={expect} got={got}")
    print(f"\n{len(cases)-fails}/{len(cases)} passed (pure layer)")
    return fails

LONG_PROSE = FILLER + "\n\nTLDR\nOne flowing prose takeaway, no circled key."
NUMBERED = "① a\n② b\n\nTLDR\n① a\n② b"

def run_judge_guards():
    jd = tempfile.mkdtemp(prefix="tldr_judge_test_")
    fails = 0

    def jcase(name, cond):
        nonlocal fails
        if not cond: fails += 1
        print(f"[{'PASS' if cond else 'FAIL'}] judge:{name}")


    jcase("number: short reply skips judge", tn.number_reason("Tuesday.", jd) is None)
    jcase("number: already-numbered reply skips judge", tn.number_reason(NUMBERED, jd) is None)

    jcase("coherence: unnumbered reply skips judge", tn.coherence_reason(LONG_PROSE, jd) is None)

    open(os.path.join(jd, "tldr_judge_off"), "w").close()
    jcase("number: tldr_judge_off skips judge", tn.number_reason(LONG_PROSE, jd) is None)
    jcase("coherence: tldr_judge_off skips judge", tn.coherence_reason(NUMBERED, jd) is None)
    return fails

RESULTS = {"pass": 0, "fail": 0}

def check(name, cond, detail=""):
    if cond:
        RESULTS["pass"] += 1
        print("[PASS] " + name)
    else:
        RESULTS["fail"] += 1
        print("[FAIL] " + name + ((" :: " + str(detail)) if detail else ""))

CIRC = "①②③④⑤⑥⑦⑧⑨⑩"

LONG_BODY = (
    "Pushed the poller fix and restarted the watchdog just now.\n"
    "The queue drained in about four minutes and nothing else was touched.\n"
    "Logs for the other five bots are clean, no orphans left behind.\n"
    "The Stripe key is still missing, which is the one open item on my side.\n"
    "Nothing else needs you tonight.\n"
)

T_OPENER = (
    "TLDR compliance is enabled on this channel, so here is the full rundown.\n"
    "\n"
    "① Migration\n"
    "The DNS cutover finished at 4pm and the old records are gone.\n"
    "\n"
    "② Invoice bot\n"
    "Still blocked on the Stripe key, nothing else outstanding.\n"
    "\n"
    "TLDR\n"
    "① Migration done - verify DNS tonight\n"
    "② Invoice bot blocked - send the Stripe key"
)

T_BARE_TLDR = LONG_BODY + "\nTLDR\n"
T_INLINE_TLDR = LONG_BODY + "\nTLDR Migration done - verify DNS tonight"
T_NO_TLDR = LONG_BODY + "\nThat is everything worth reporting from this pass.\n"
T_PLAIN_DIGITS = (
    LONG_BODY + "\nTLDR\n1. Poller fixed and watchdog back up\n2. Stripe key still needed"
)
T_SHORT = "Done, the watchdog is back up."
T_JUDGED = (
    "Pushed the poller fix and restarted the watchdog just now.\n"
    "The queue drained in four minutes and nothing else was touched.\n"
    "Logs for the other five bots are clean.\n"
    "The Stripe key is still missing, which is the one open item.\n"
    "\n"
    "TLDR\n"
    "Poller fixed, Stripe key still needed."
)

T_JUDGED += "\n" + FILLER
T_OPENER = FILLER + "\n" + T_OPENER

def numbered(n, tl=None):
    """A body carrying circled headers 1..n plus a terminal TLDR mirroring tl."""
    tl = list(range(1, n + 1)) if tl is None else tl
    body = []
    for i in range(1, n + 1):
        body.append(CIRC[i - 1] + " Topic " + str(i))
        body.append("Detail line for topic " + str(i) + ", long enough to read.")
        body.append("")
    return ("\n".join(body) + "TLDR\n"
            + "\n".join(CIRC[i - 1] + " Topic " + str(i) + " handled" for i in tl))

def run_main(text, state_dir):
    """Drive main() with a mocked stdin payload and a temp state dir. Returns stdout."""
    payload = json.dumps({
        "tool_name": "mcp__plugin_telegram_telegram__reply",
        "tool_input": {"text": text},
    })
    saved_stdin = sys.stdin
    saved_env = os.environ.get("TELEGRAM_STATE_DIR")
    sys.stdin = io.StringIO(payload)
    os.environ["TELEGRAM_STATE_DIR"] = state_dir
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            tn.main()
    finally:
        sys.stdin = saved_stdin
        if saved_env is None:
            os.environ.pop("TELEGRAM_STATE_DIR", None)
        else:
            os.environ["TELEGRAM_STATE_DIR"] = saved_env
    return buf.getvalue()

def verdict_reason(fn, verdict, text, state_dir):
    """Run one judge-backed check against a canned verdict."""
    saved = tn._judge
    tn._judge = lambda t, k, s: verdict
    try:
        return fn(text, state_dir)
    finally:
        tn._judge = saved

def _hammer(state_dir, prefix, count):
    """Concurrency worker: stays module level so spawn can pickle it."""
    mod = load_hook()
    for i in range(count):
        mod._seen_record(state_dir, "%s%012d" % (prefix, i))

def fix1_split_at_last_header(state_dir):
    body, tail = tn._split_body_tldr(T_OPENER)
    check("fix1: split takes the LAST TLDR header, not the opener",
          tail == ["① Migration done - verify DNS tonight",
                   "② Invoice bot blocked - send the Stripe key"], repr(tail))
    check("fix1: the body keeps its circled markers",
          tn._line_markers(body) == [1, 2], tn._line_markers(body))
    check("fix1: a reply OPENING with 'TLDR ...' is no longer bounced",
          tn.reason_for(T_OPENER) is None, tn.reason_for(T_OPENER))
    check("fix1: a lone loose 'tldr...' line is still the header",
          tn._split_body_tldr("Body line.\nTLDR: all done.")[0] == ["Body line."])

def fix2_empty_tldr(state_dir):
    check("fix2: a bare terminal TLDR reads as empty",
          tn._tldr_is_empty(T_BARE_TLDR) is True)
    check("fix2: a long reply with an empty TLDR gets the NO_TLDR nudge",
          tn.reason_for(T_BARE_TLDR) is tn.NO_TLDR_REASON, tn.reason_for(T_BARE_TLDR))
    check("fix2: a single-line TLDR is not empty",
          tn._tldr_is_empty(T_INLINE_TLDR) is False)
    check("fix2: the single-line TLDR style still passes",
          tn.reason_for(T_INLINE_TLDR) is None, tn.reason_for(T_INLINE_TLDR))

def fix3_cardinality(state_dir):
    one = tn.reason_for(numbered(1))
    check("fix3: a sound key of only ① is nudged",
          one is not None and "only ONE numbered point" in one, one)
    six = tn.reason_for(numbered(6))
    check("fix3: a coherent six-point key is allowed",
          six is None, six)
    check("fix3: a sound key of ① ② ③ passes", tn.reason_for(numbered(3)) is None,
          tn.reason_for(numbered(3)))
    check("fix3: a sound key of exactly 5 passes", tn.reason_for(numbered(5)) is None,
          tn.reason_for(numbered(5)))
    r = tn.reason_for(numbered(2, tl=[1, 3]))
    check("fix3: a broken mapping still gets its own specific message",
          r is not None and "the TLDR numbers are" in r, r)
    check("fix3: that message is not a cardinality message",
          r is not None and "caps it at 5" not in r and "only ONE" not in r, r)
    r2 = tn.reason_for("① Topic one\nDetail.\n② Topic two\nDetail.\n\nTLDR\nAll handled.")
    check("fix3: a numbered body with an unnumbered TLDR still nudges",
          r2 is not None and "no matching key" in r2, r2)

def fix456_memory(state_dir):
    CALLS["judge"] = 0
    out1 = run_main(T_JUDGED, state_dir)
    check("fix5: the first send is denied with a nudge",
          '"permissionDecision": "deny"' in out1, out1[:120])
    check("fix5: that deny cost exactly one judge call", CALLS["judge"] == 1,
          CALLS["judge"])
    h = hashlib.sha256(T_JUDGED.encode("utf-8", "ignore")).hexdigest()[:16]
    seen = open(os.path.join(state_dir, "tldr_seen.txt")).read().split()
    check("fix5: the deny recorded the text hash", seen == [h], seen)

    out2 = run_main(T_JUDGED, state_dir)
    check("fix5: the identical resend is allowed", out2 == "", out2[:120])
    check("fix5: the resend made ZERO judge calls", CALLS["judge"] == 1, CALLS["judge"])

    trim_dir = tempfile.mkdtemp(prefix="tldr_trim_test_")
    seed = ["%016x" % i for i in range(1200)]
    with open(os.path.join(trim_dir, "tldr_seen.txt"), "w") as f:
        f.write("\n".join(seed))
    tn._seen_record(trim_dir, "ffffffffffffffff")
    kept = open(os.path.join(trim_dir, "tldr_seen.txt")).read().split()
    check("fix6: the memory trims to 1000 entries", len(kept) == 1000, len(kept))
    check("fix6: the newest hash survives the trim", kept[-1] == "ffffffffffffffff",
          kept[-1])
    check("fix6: the kept window is the newest 1000",
          seed[201] in kept and seed[200] not in kept)

def fix4_concurrency(state_dir):
    cdir = tempfile.mkdtemp(prefix="tldr_lock_test_")
    ctx = types.SimpleNamespace(Process=threading.Thread, Barrier=threading.Barrier)
    procs = [ctx.Process(target=_hammer, args=(cdir, "a", 100)),
             ctx.Process(target=_hammer, args=(cdir, "b", 100))]
    for p in procs:
        p.daemon = True
        p.start()
    for p in procs:
        p.join(10)
    alive = [p for p in procs if p.is_alive()]
    if alive:
        raise AssertionError("concurrency workers did not finish")
    got = set(open(os.path.join(cdir, "tldr_seen.txt")).read().split())
    want = {"a%012d" % i for i in range(100)} | {"b%012d" % i for i in range(100)}
    check("fix4: both concurrent writers finished", not alive)
    check("fix4: no update was lost under concurrent writes", got == want,
          "missing " + str(len(want - got)) + ", extra " + str(len(got - want)))

def fix7_verdict_types(state_dir):
    r = verdict_reason(tn.number_reason, {"number": True, "topics": "alpha"},
                       T_JUDGED, state_dir)
    check("fix7: topics as a bare string fails open", r is None, r)
    r = verdict_reason(tn.number_reason, {"number": "false", "topics": ["a", "b"]},
                       T_JUDGED, state_dir)
    check("fix7: number as the string 'false' fails open", r is None, r)
    r = verdict_reason(tn.number_reason, ["number", True], T_JUDGED, state_dir)
    check("fix7: a bare-list verdict fails open (number judge)", r is None, r)
    r = verdict_reason(tn.number_reason, {"number": True, "topics": ["a", "b", "c"]},
                       T_JUDGED, state_dir)
    check("fix7: a well-formed yes still nudges and lists the topics",
          r is not None and "a; b; c" in r, r)
    r = verdict_reason(tn.coherence_reason, ["coherent", False], T_OPENER, state_dir)
    check("fix7: a bare-list verdict fails open (coherence judge)", r is None, r)
    r = verdict_reason(tn.coherence_reason, {"coherent": False, "problem": "swapped"},
                       T_OPENER, state_dir)
    check("fix7: the coherence judge still nudges on a real mismatch",
          r is not None and "swapped" in r, r)

ZW, ZWNJ, ZWJ, BOM = "\u200b", "\u200c", "\u200d", "\ufeff"

T_SEMI_TLDR = LONG_BODY + "\nTL;DR\nPoller fixed, Stripe key still needed."
T_SEMI_BARE = LONG_BODY + "\nTL;DR\n"
T_ZW_TLDR = LONG_BODY + "\nTLDR\n" + ZW + ZWNJ + ZWJ + BOM + "\n"
T_WS_TLDR = LONG_BODY + "\nTLDR\n   \t  \n"
T_ZW_INLINE = LONG_BODY + "\nTLDR" + ZW + " Migration done - verify DNS tonight"

def h_of(text):
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:16]

def unwritable_state_dirs():
    """Two state dirs whose tldr_seen.txt can never be opened for writing, both
    independent of the running uid (root included): a state dir that is itself a FILE,
    and a state dir holding a DIRECTORY where the seen file belongs. Temp space only."""
    base = tempfile.mkdtemp(prefix="tldr_badstate_test_")
    as_file = os.path.join(base, "state_is_a_file")
    open(as_file, "w").close()
    as_dir = os.path.join(base, "state_dir")
    os.makedirs(os.path.join(as_dir, "tldr_seen.txt"))
    return (("state dir is a file", as_file), ("seen path is a directory", as_dir))

def _race_worker(text, state_dir, out_path, barrier):
    """Same-text race worker: stays module level so spawn can pickle it. It drives this
    module's own hook object, whose _api_key and _judge are mocked at import, so the
    child cannot reach the Keychain or the network either. Writes 'deny|allow <judge
    calls>' to out_path."""
    try:
        barrier.wait(20)
    except Exception:
        pass
    out = run_main(text, state_dir)
    with open(out_path, "w") as f:
        f.write(("deny" if "deny" in out else "allow") + " " + str(CALLS["judge"]))

def d1_record_failure_allows(state_dir):
    """DEFECT 1: a seen file that cannot be written must not buy a deny. Recording is
    what makes a resend recognisable, so denying on a failed record denies that text
    forever - a hard delivery block."""
    h = h_of(T_NO_TLDR)
    for label, bad in unwritable_state_dirs():
        st = tn._seen_record(bad, h)
        check("d1: an unwritable memory reports failed (" + label + ")",
              st == "failed", st)
        out1 = run_main(T_NO_TLDR, bad)
        out2 = run_main(T_NO_TLDR, bad)
        check("d1: the first send is ALLOWED when the memory fails (" + label + ")",
              out1 == "", out1[:160])
        check("d1: the resend is allowed too, no endless denial (" + label + ")",
              out2 == "", out2[:160])

def d1_under_lock_recheck(state_dir):
    """DEFECT 1 + 3: the membership re-check under the lock. A second recorder of the
    same hash reports "seen", and the caller reads that as allow, so two racing copies
    of one text can only ever produce ONE deny."""
    d = tempfile.mkdtemp(prefix="tldr_recheck_test_")
    h = h_of(T_NO_TLDR)
    check("d1: the first record reports recorded", tn._seen_record(d, h) == "recorded")
    check("d1: recording the same hash again reports seen",
          tn._seen_record(d, h) == "seen")
    kept = open(os.path.join(d, "tldr_seen.txt")).read().split()
    check("d1: the re-check left a single entry", kept == [h], kept)



    saved = tn._seen_check
    tn._seen_check = lambda sd, hh: False
    try:
        out = run_main(T_NO_TLDR, d)
    finally:
        tn._seen_check = saved
    check("d1: a racer reaching the deny path on an already-seen hash is allowed",
          out == "", out[:160])
    kept2 = open(os.path.join(d, "tldr_seen.txt")).read().split()
    check("d1: that racer added no duplicate entry", kept2 == [h], kept2)

def d2_lock_bound(state_dir):
    """DEFECT 2: a wedged lock holder must never stall the reply path. The wait is
    bounded, then the write degrades to unlocked."""
    d = tempfile.mkdtemp(prefix="tldr_lockbound_test_")
    path = os.path.join(d, "tldr_seen.txt")
    h = "d2d2d2d2d2d2d2d2"
    if tn.fcntl is None:
        check("d2: without fcntl the write path is already the unlocked one", True)
        check("d2: the record still succeeds", tn._seen_record(d, h) == "recorded")
        return
    holder = open(path, "a+")
    tn.fcntl.flock(holder.fileno(), tn.fcntl.LOCK_EX)
    t0 = time.time()
    status = tn._seen_record(d, h)
    dt = time.time() - t0
    tn.fcntl.flock(holder.fileno(), tn.fcntl.LOCK_UN)
    holder.close()
    check("d2: a held lock is waited out in under 3s, not forever", dt < 3.0,
          "%.2fs" % dt)
    check("d2: the record still lands, degraded to unlocked",
          status == "recorded" and open(path).read().split() == [h], status)

def d3_concurrent_mains(state_dir):
    """DEFECT 3: two simultaneous invocations of the SAME text. At most one deny and
    one entry in the memory. The duplicate judge call is the accepted residue, so the
    count is asserted as a range, not a value."""
    cdir = tempfile.mkdtemp(prefix="tldr_race_test_")
    outs = [os.path.join(cdir, "r0.txt"), os.path.join(cdir, "r1.txt")]
    procs = [subprocess.Popen([sys.executable, __file__, "--race", cdir, out],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for out in outs]
    result = [proc.communicate() for proc in procs]
    check("d3: both racing invocations finished", all(proc.returncode == 0 for proc in procs), result)
    results = [open(o).read().split() for o in outs if os.path.exists(o)]
    check("d3: both racers reported a verdict", len(results) == 2, results)
    verdicts = [r[0] for r in results]
    check("d3: at most ONE deny across the two racers", verdicts.count("deny") <= 1,
          verdicts)
    check("d3: one denied and the other was allowed through",
          sorted(verdicts) == ["allow", "deny"], verdicts)
    kept = open(os.path.join(cdir, "tldr_seen.txt")).read().split()
    check("d3: the memory holds exactly one entry for the text", kept == [h_of(T_JUDGED)],
          kept)
    judged = sum(int(r[1]) for r in results)
    check("d3: the duplicate judge call is tolerated (1 or 2 across the race)",
          1 <= judged <= 2, judged)

def d4_tldr_spelling(state_dir):
    """DEFECT 4: TL;DR is the same header. _TLDR_HEAD already accepted that spelling,
    so presence had to agree or a real summary read as a missing one."""
    check("d4: TL;DR counts as a TLDR being present", tn._has_tldr(T_SEMI_TLDR) is True)
    check("d4: a long reply ending TL;DR + a real takeaway passes",
          tn.reason_for(T_SEMI_TLDR) is None, tn.reason_for(T_SEMI_TLDR))
    check("d4: a bare TL;DR is still an empty TLDR", tn._tldr_is_empty(T_SEMI_BARE) is True)
    check("d4: a bare TL;DR still gets the NO_TLDR nudge",
          tn.reason_for(T_SEMI_BARE) is tn.NO_TLDR_REASON, tn.reason_for(T_SEMI_BARE))
    check("d4: a reply with no TLDR in either spelling is still caught",
          tn._has_tldr(T_NO_TLDR) is False
          and tn.reason_for(T_NO_TLDR) is tn.NO_TLDR_REASON)
    check("d4: plain TLDR presence is unchanged", tn._has_tldr(T_INLINE_TLDR) is True)

def d5_zero_width(state_dir):
    """DEFECT 5: U+200B and friends survive str.strip(), so a TLDR section holding only
    them used to count as content - a summary the reader cannot see."""
    check("d5: a zero-width-only TLDR section is empty",
          tn._tldr_is_empty(T_ZW_TLDR) is True)
    check("d5: a zero-width-only TLDR gets the NO_TLDR nudge",
          tn.reason_for(T_ZW_TLDR) is tn.NO_TLDR_REASON, tn.reason_for(T_ZW_TLDR))
    check("d5: a spaces-and-tabs-only TLDR is still empty",
          tn._tldr_is_empty(T_WS_TLDR) is True)
    check("d5: the single-line 'TLDR <content>' style still passes",
          tn._tldr_is_empty(T_INLINE_TLDR) is False
          and tn.reason_for(T_INLINE_TLDR) is None)
    check("d5: a zero width inside the header line does not hide real content",
          tn._tldr_is_empty(T_ZW_INLINE) is False)
    check("d5: an ordinary TLDR is untouched by the normalisation",
          tn._tldr_is_empty(T_JUDGED) is False and tn.reason_for(T_JUDGED) is None)

def run_all():
    state_dir = tempfile.mkdtemp(prefix="tldr_state_test_")
    live = os.path.abspath(os.path.expanduser("~/.claude/channels"))
    if os.path.abspath(state_dir).startswith(live):
        raise SystemExit("refusing to run: temp state dir landed inside " + live)
    print("hook      : " + _HOOK)
    print("state dir : " + state_dir)
    print("")
    fails = run_pure_cases()
    print("")
    fails += run_judge_guards()
    print("")
    for fn in (fix1_split_at_last_header, fix2_empty_tldr, fix3_cardinality,
               fix456_memory, fix4_concurrency, fix7_verdict_types,
               d1_record_failure_allows, d1_under_lock_recheck, d2_lock_bound,
               d3_concurrent_mains, d4_tldr_spelling, d5_zero_width):
        fn(state_dir)
    fails += RESULTS["fail"]
    total = len(cases) + 5 + RESULTS["pass"] + RESULTS["fail"]
    print("")
    print("judge calls: %d (all mocked), keychain lookups: %d (all mocked)"
          % (CALLS["judge"], CALLS["key"]))
    print("TOTAL %d/%d passed" % (total - fails, total))
    return 1 if fails else 0

if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--race":
        original = tn._seen_check
        def racing_check(state_dir, digest):
            result = original(state_dir, digest)
            time.sleep(0.05)
            return result
        tn._seen_check = racing_check
        _race_worker(T_JUDGED, sys.argv[2], sys.argv[3], None)
        sys.exit(0)
    sys.exit(run_all())
