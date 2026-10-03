#!/usr/bin/env python3
"""Isolated Mac installer tests and the 21-scenario integration manifest.

Phase 1 executes the installer tests and runs each missing-package scenario
through a sandbox stub. STUB is never counted as an integration pass.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile

from repo_files import copy_files, git_paths, read_manifest

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
STOCK_SHA = "d902d7195a527b579b889cfdbf251fc24fd224a68e319ac6b7c3b691515c3458"
SCENARIOS = [
    (1, "Install without a bot or watchdog", ("install.sh",)),
    (2, "Overlay gates, repeat install, restore and status tokens", ("poller/install-overlay.sh", "poller/restore-stock.sh")),
    (3, "Add a bot, protect its token and reject duplicates", ("engine/fleet-add-bot",)),
    (4, "Poller start, commands and heartbeat", ("tests/poller/mock-telegram.ts", "tests/poller/fake-claude.ts")),
    (5, "Pairing and enrollment", ("engine/fleet-pair",)),
    (6, "Text, reply layout and quick bar", ("tests/poller/fake-claude.ts",)),
    (7, "Tap access, stale messages and slash labels", ("tests/poller/fake-claude.ts",)),
    (8, "Send it swap without injection", ("tests/poller/fake-claude.ts",)),
    (9, "Reaction access and normalization", ("tests/poller/fake-claude.ts",)),
    (10, "Key legend and inbox setup hint", ("tests/poller/fake-claude.ts",)),
    (11, "Model and effort pickers", ("tests/poller/fake-claude.ts",)),
    (12, "Handoff picker and single-bot guardian", ("tests/poller/fake-claude.ts", "plugin/hooks/context-guardian.sh")),
    (13, "Command intercept engine arguments", ("tests/poller/fake-claude.ts",)),
    (14, "Voice transcription and ordered delivery", ("tests/poller/fake-claude.ts", "voice/transcribe-voice")),
    (15, "Email preview and one-time consent", ("tests/poller/fake-claude.ts", "engine/fleet-email")),
    (16, "Engine dry runs and fixture outputs", ("engine/fleet-update", "engine/fleet-compact")),
    (17, "Hook dry runs and rich fallback", ("plugin/hooks/telebots-rules.sh", "tests/hooks")),
    (18, "Watchdog enrollment and overlay drift", ("daemon/watchdog.sh", "tests/ops")),
    (19, "Snapshot and restore transaction", ("engine/fleet-snapshot", "engine/fleet-restore", "tests/ops")),
    (20, "Uninstall restores stock and removes voice plist", ("uninstall.sh",)),
    (21, "Non-Mac installer exits before any write", ("install.sh",)),
]


def sh(root: Path, *args: str, fake_uname: str = "Darwin") -> subprocess.CompletedProcess[str]:
    """Every executed test command goes through sbenv, never inherited env."""
    env = {
        "HOME": str(Path.home()),
        "PATH": "/usr/bin:/bin",
        "TELEBOTS_FAKE_UNAME": fake_uname,
    }
    return subprocess.run(
        ["/bin/bash", str(HERE / "sbenv"), str(root), *args],
        cwd=root / "repo",
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def write_stub_overlay(root: Path) -> None:
    """Phase 1 only: preserve interface names while WP1 builds the overlay."""
    poller = root / "repo/poller"
    poller.mkdir(exist_ok=True)
    if not (poller / "ux.json").exists():
        shutil.copy2(HERE / "fixtures/ux.json", poller / "ux.json")
    (poller / "install-overlay.sh").write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\n'
        'slot="$HOME/.claude/plugins/cache/claude-plugins-official/telegram/0.0.7"\n'
        'if [ -f "$slot/server.ts.stock-backup" ]; then exit 0; fi\n'
        'cp "$slot/server.ts" "$slot/server.ts.stock-backup"\n'
        'printf "sandbox overlay\\n" > "$slot/server.ts"\n'
    )
    (poller / "verify-overlay.sh").write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\n'
        'slot="$HOME/.claude/plugins/cache/claude-plugins-official/telegram/0.0.7"\n'
        'if [ -f "$slot/server.ts.stock-backup" ]; then printf "OK\\n"; '
        'else printf "STOCK\\n"; exit 1; fi\n'
    )
    (poller / "restore-stock.sh").write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\n'
        'slot="$HOME/.claude/plugins/cache/claude-plugins-official/telegram/0.0.7"\n'
        'cp "$slot/server.ts.stock-backup" "$slot/server.ts"\n'
    )


def sandbox(parent: Path, number: int, phase1: bool, stock: Path | None, bun: Path | None) -> Path:
    root = parent / f"scenario-{number:02d}"
    root.mkdir()
    (root / ".telebots-sandbox").write_text("sandbox only\n")
    for name in ("home", "tmp", "cache", "tools", "logs", "mock"):
        (root / name).mkdir()
    shutil.copytree(HERE / "shims", root / "shims", symlinks=True)
    unit_root = os.environ.get("TELEBOTS_SANDBOX_ROOT")
    manifest = Path(unit_root) / "inputs/repo-files.nul" if unit_root else None
    copy_files(REPO, root / "repo", read_manifest(manifest) if manifest else git_paths(REPO))
    slot = root / "home/.claude/plugins/cache/claude-plugins-official/telegram/0.0.7"
    (slot / ".claude-plugin").mkdir(parents=True)
    (slot / ".claude-plugin/plugin.json").write_text('{"version":"0.0.7"}\n')
    if phase1:
        (slot / "server.ts").write_text("stock sandbox fixture\n")
    else:
        assert stock is not None and bun is not None
        (root / "inputs").mkdir()
        shutil.copy2(stock, root / "inputs/stock-server.ts")
        shutil.copy2(stock, slot / "server.ts")
        (root / "shims/bun").unlink()
        (root / "tools/bun").symlink_to(bun)
    if phase1:
        write_stub_overlay(root)
    return root


def suite(root: Path, command: tuple[str, ...], name: str) -> str:
    result = sh(root, *command)
    # Outputs stay in the disposable sandbox. A token-shaped string is always
    # redacted before either stream is stored.
    import re
    redact = re.compile(r"[0-9]{8,10}:[A-Za-z0-9_-]{35,}")
    (root / "logs" / (name + ".stdout")).write_text(redact.sub("[redacted]", result.stdout))
    (root / "logs" / (name + ".stderr")).write_text(redact.sub("[redacted]", result.stderr))
    if result.returncode:
        raise AssertionError(f"{name} exited {result.returncode}; see sandbox logs")
    return result.stdout + result.stderr


def integration_suite(root: Path, name: str) -> str:
    if name == "overlay":
        return suite(root, ("bun", "test", "tests/poller/overlay.test.ts"), name)
    if name == "poller":
        suite(root, ("/bin/bash", "-c", "cd poller && bun install --frozen-lockfile"), "poller-deps")
        return suite(root, ("bun", "test", "tests/poller/integration.test.ts"), name)
    if name == "engine":
        tests = sorted(set((root / "repo/tests/engine").glob("*.test.py")) |
                       set((root / "repo/tests/engine").glob("test_*.py")))
        if not tests:
            raise AssertionError("tests/engine has no scenario tests")
        return suite(root, ("python3", "-m", "unittest", "discover", "-s", "tests/engine",
                            "-p", "test_*.py", "-v"), name)
    if name == "hooks":
        tests = sorted((root / "repo/tests/hooks").glob("*.test.py"))
        if not tests:
            raise AssertionError("tests/hooks has no scenario tests")
        output = ""
        for test in tests:
            args = ("--require-404-fallback",) if test.name == "streamer-smoke.test.py" else ()
            output += suite(root, ("python3", str(test), *args), "hooks-" + test.stem)
        live = root / "inputs/live-tg-stream.py"
        args = ("--live", str(live)) if live.is_file() else ()
        output += suite(root, ("python3", "tests/hooks/ast_equiv.py", *args, "--self-test"), "hooks-ast")
        return output
    if name == "ops":
        return suite(root, ("python3", "-m", "unittest", "discover", "-s", "tests/ops",
                            "-p", "test_*.py", "-v"), name)
    raise AssertionError("unknown integration suite")


def install(root: Path) -> None:
    result = sh(root, "/bin/bash", str(root / "repo/install.sh"), "--yes")
    if result.returncode:
        raise AssertionError(f"install exited {result.returncode}: {result.stderr[-500:]}")


def scenario_1(root: Path) -> str:
    uv_dir = root / "uv-tools"
    interpreter = uv_dir / "mlx-whisper/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\nexit 0\n")
    interpreter.chmod(0o700)
    uv = root / "tools/uv"
    uv.write_text('#!/bin/sh\nif [ "$1" = tool ] && [ "$2" = dir ]; then printf "%s\\n" "$TELEBOTS_SANDBOX_ROOT/uv-tools"; else exit 1; fi\n')
    uv.chmod(0o700)
    safe = sh(root, "/bin/bash", "-c",
              'test -z "${TELEGRAM_BOT_TOKEN+x}" && '
              'for value in "$HOME" "$FLEET_HOME" "$TELEGRAM_STATE_DIR" "$TMPDIR"; do '
              'case "$value" in "$TELEBOTS_SANDBOX_ROOT"/*) ;; *) exit 1 ;; esac; done')
    assert safe.returncode == 0, "scrubbed environment leaked a live path or token"
    dry = sh(root, "/bin/bash", str(root / "repo/install.sh"), "--dry-run")
    assert dry.returncode == 0, dry.stderr[-500:]
    for phrase in ("empty bots list", "required overlay", "ux.json", "No bot was started", "watchdog"):
        assert phrase.lower() in dry.stdout.lower(), f"dry-run omitted {phrase}"
    assert not (root / "home/.claude/telegram-fleet").exists(), "dry-run wrote fleet home"
    install(root)
    install(root)
    fleet = root / "home/.claude/telegram-fleet"
    data = json.loads((fleet / "fleet.json").read_text())
    assert data["version"] == 2 and data["bots"] == []
    assert (fleet / "ux.json").is_file()
    assert (fleet / "bin/fleet-config").is_file()
    for name in ("install-overlay.sh", "verify-overlay.sh", "restore-stock.sh",
                 "overlay-common.sh", "seam-0.0.7.patch", "telebots.ts",
                 "email.ts", "contracts.ts", "ux.json"):
        assert (fleet / "bin" / name).is_file(), f"installed overlay support missing: {name}"
    verified = sh(root, str(fleet / "bin/verify-overlay.sh"), "--status")
    assert verified.returncode == 0 and verified.stdout.strip() == "OK", verified.stderr
    reapplied = sh(root, str(fleet / "bin/install-overlay.sh"))
    assert reapplied.returncode == 0, reapplied.stderr
    assert "launchctl" not in (root / "logs/shims.log").read_text()
    doctor = sh(root, str(fleet / "bin/fleet-doctor"))
    assert doctor.returncode == 0, doctor.stdout + doctor.stderr
    assert "[ ok ]  overlay status" in doctor.stdout and "[warn]  bots" in doctor.stdout
    assert (fleet / "templates/com.claude-fleet.watchdog.plist.template").is_file()
    with (fleet / "templates/com.claude-fleet.voice.plist.template").open("rb") as stream:
        voice = plistlib.load(stream)
    assert voice["ProgramArguments"] == [str(interpreter), str(fleet / "bin/transcribe-voice-daemon")]
    assert voice["EnvironmentVariables"]["FLEET_HOME"] == str(fleet)
    assert not (root / "home/Library/LaunchAgents/com.claude-fleet.watchdog.plist").exists()
    return "empty fleet, installed overlay verifier, watchdog and rendered voice templates; no watchdog call"


EVIDENCE = {
    2: ("overlay", "applies stock, deploys all imports"),
    3: ("engine", "test_add_bot_validates_before_config_and_hides_token"),
    4: ("poller", "F2.4 starts the real patched poller"),
    5: ("poller", "F2.5 retains stock pairing"),
    6: ("poller", "F2.6-7 delivers text"),
    7: ("poller", "F2.7 checks private chat"),
    8: ("poller", "F2.8-9 swaps Send it"),
    9: ("poller", "F2.8-9 swaps Send it"),
    10: ("poller", "F2.10 renders the shared legend"),
    11: ("poller", "F2.11 walks model then effort"),
    12: ("poller", "F2.12 lists configured peers"),
    13: ("poller", "F2.13 spawns every intercept"),
    14: ("poller", "F2.14 downloads and transcribes voice"),
    15: ("poller", "F2.15 requires exact email"),
    16: ("engine", "test_compact_classifier_uses_fixture_without_osascript"),
    17: ("hooks", "PASS: HTTP 404 uses plain fallback"),
    18: ("ops", "test_watchdog_version_change_alerts_once_without_restarting"),
    19: ("ops", "test_snapshot_restore_transaction_and_health_gate_in_sandbox"),
}


def scenario_20(root: Path) -> str:
    install(root)
    prior_launchctl = (root / "logs/shims.log").read_text().count("launchctl")
    agents = root / "home/Library/LaunchAgents"
    agents.mkdir(parents=True)
    for name in ("com.claude-fleet.watchdog.plist", "com.claude-fleet.voice.plist"):
        (agents / name).write_text("sandbox plist\n")
    dry = sh(root, "/bin/bash", str(root / "repo/uninstall.sh"), "--dry-run")
    assert dry.returncode == 0, dry.stderr[-500:]
    assert len(list(agents.glob("*.plist"))) == 2, "dry run removed a plist"
    assert (root / "home/.claude/telegram-fleet").is_dir(), "dry run removed fleet home"
    real = sh(root, "/bin/bash", str(root / "repo/uninstall.sh"), "--yes")
    assert real.returncode == 0, real.stderr[-500:]
    assert not list(agents.glob("*.plist")), "uninstall left a plist"
    assert not (root / "home/.claude/telegram-fleet").exists()
    slot = root / "home/.claude/plugins/cache/claude-plugins-official/telegram/0.0.7"
    expected_stock = root / "inputs/stock-server.ts"
    if expected_stock.exists():
        assert (slot / "server.ts").read_bytes() == expected_stock.read_bytes()
    else:
        assert (slot / "server.ts").read_text() == "stock sandbox fixture\n"
    assert (root / "logs/shims.log").read_text().count("launchctl") - prior_launchctl == 2
    return "stock restored; watchdog and voice plists removed; state kept"


def scenario_21(root: Path) -> str:
    result = sh(root, "/bin/bash", str(root / "repo/install.sh"), "--dry-run", fake_uname="Linux")
    assert result.returncode == 1, f"expected exit 1, got {result.returncode}"
    assert "macOS only" in result.stderr
    assert not (root / "home/.claude/telegram-fleet").exists()
    assert "claude" not in (root / "logs/shims.log").read_text()
    return "exit 1; macOS-only message; no CLI or file write"


def scenario_stub(root: Path, number: int, title: str, required: tuple[str, ...]) -> str:
    missing = [name for name in required if not (root / "repo" / name).exists()]
    # The command is deliberately executed through sbenv. It exercises the
    # sandbox launcher and preserves a per-scenario receipt without claiming
    # that an absent package passed its behavior test.
    marker = root / "logs" / f"scenario-{number:02d}.stub"
    result = sh(root, "/bin/bash", "-c", 'printf "stub\\n" > "$1"', "_", str(marker))
    assert result.returncode == 0 and marker.read_text() == "stub\n"
    return "missing " + ", ".join(missing) if missing else "integration assertions pending"


def runtime_path(path: Path) -> bool:
    """Skip state written by active channels and plugin/runtime workers."""
    name = path.name
    return (any(part in {"channels", "sessions", "projects", "logs", "__pycache__"}
                for part in path.parts)
            or name == "menubar-windows.json"
            or name.endswith((".log", ".pyc", ".in_use", ".synced"))
            or name.startswith((".in_use", ".synced")))


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "absent"


def record_tree(found: dict[str, object], root: Path, *, hashes: bool,
                ignore_runtime: bool) -> None:
    """Hash hook contents or list file names, sizes and mtimes without directory mtimes."""
    found[str(root)] = "directory" if root.is_dir() else "absent"
    if not root.is_dir():
        return
    for dirname, dirs, files in os.walk(root):
        current = Path(dirname)
        keep_dirs = []
        for name in dirs:
            path = current / name
            if ignore_runtime and runtime_path(path.relative_to(root)):
                continue
            if path.is_symlink():
                found[str(path)] = ("link", os.readlink(path))
            else:
                found[str(path)] = "directory"
                keep_dirs.append(name)
        dirs[:] = keep_dirs
        for name in files:
            path = current / name
            if ignore_runtime and runtime_path(path.relative_to(root)):
                continue
            if hashes:
                digest = file_sha(path)
                found[str(path)] = ("link", os.readlink(path), digest) if path.is_symlink() else digest
            else:
                metadata = path.lstat()
                kind = "link" if path.is_symlink() else "file"
                found[str(path)] = (kind, metadata.st_size, metadata.st_mtime_ns)


def changed_items(before: dict[str, object], after: dict[str, object]) -> list[str]:
    """Return the exact protected paths or checks whose snapshots differ."""
    return sorted(key for key in before.keys() | after.keys() if before.get(key) != after.get(key))


def snapshot(real_home: Path | None, main: Path | None) -> dict[str, object]:
    """Record protected files and named daemon PIDs through read-only ps."""
    found: dict[str, object] = {}
    if real_home:
        cache = real_home / ".claude/plugins/cache/claude-plugins-official/telegram"
        for server in cache.glob("*/server.ts"):
            found[str(server)] = file_sha(server)
        marketplace = real_home / ".claude/plugins/marketplaces/claude-plugins-official/external_plugins/telegram/server.ts"
        found[str(marketplace)] = file_sha(marketplace)
        record_tree(found, real_home / "Library/LaunchAgents", hashes=False, ignore_runtime=False)
        record_tree(found, real_home / ".claude/telegram-fleet", hashes=False, ignore_runtime=True)
        record_tree(found, real_home / ".claude/hooks", hashes=True, ignore_runtime=True)
        settings = real_home / ".claude/settings.json"
        found[str(settings)] = file_sha(settings)
    if main:
        for label, verb in (("HEAD", ["rev-parse", "HEAD"]),
                            ("status --porcelain", ["status", "--porcelain"])):
            r = subprocess.run(["/usr/bin/git", "-C", str(main), *verb], text=True,
                               capture_output=True, check=False,
                               env={"HOME": str(real_home or Path.home()), "PATH": "/usr/bin:/bin",
                                    "GIT_OPTIONAL_LOCKS": "0"})
            found[f"git:{main}:{label}"] = (r.returncode, r.stdout, r.stderr)
    markers = {"com.nexera.channel-watchdog": "channel-watchdog-loop",
               "com.claude-fleet.menubar": "Telebots Bar.app/Contents/MacOS/Telebots Bar",
               "com.nexera.transcribe-voice-daemon": "transcribe-voice-daemon"}
    try:
        listed = subprocess.run(["/bin/ps", "-axo", "pid=,command="], text=True,
                                capture_output=True, check=False,
                                env={"HOME": str(real_home or Path.home()), "PATH": "/usr/bin:/bin"})
    except OSError:
        listed = None
    if listed is None or listed.returncode:
        found["live-daemon-labels"] = "unavailable"
    else:
        pids: dict[str, list[str]] = {label: [] for label in markers}
        for line in listed.stdout.splitlines():
            fields = line.strip().split(None, 1)
            if len(fields) != 2 or not fields[0].isdigit():
                continue
            for label, marker in markers.items():
                if marker in fields[1]:
                    pids[label].append(fields[0])
        for label in markers:
            found[f"live-daemon:{label}"] = tuple(sorted(pids[label]))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, type=Path, help="directory for test sandboxes")
    parser.add_argument("--phase1", action="store_true", help="allow honest STUB results while other packages build")
    parser.add_argument("--real-home", type=Path, help="read-only infrastructure snapshot target")
    parser.add_argument("--main-checkout", type=Path, help="read-only git snapshot target")
    parser.add_argument("--stock-server", type=Path, help="verified upstream 0.0.7 server.ts for integration")
    parser.add_argument("--live-streamer", type=Path, help="read-only pinned live v30.22 source for AST proof")
    args = parser.parse_args()
    if not args.real_home or not args.main_checkout:
        parser.error("--real-home and --main-checkout are required for the untouched check")
    stock = args.stock_server.resolve() if args.stock_server else None
    bun = Path(shutil.which("bun") or "").resolve() if shutil.which("bun") else None
    if not args.phase1:
        if not stock or not stock.is_file():
            parser.error("integration requires --stock-server")
        if hashlib.sha256(stock.read_bytes()).hexdigest() != STOCK_SHA:
            parser.error("stock server.ts does not match the 0.0.7 pin")
        if not bun or not bun.is_file():
            parser.error("integration requires Bun")
    base = args.base.expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)
    if args.real_home and (base == args.real_home.resolve() or args.real_home.resolve() in base.parents):
        parser.error("sandbox base cannot be inside the real home")
    before = snapshot(args.real_home, args.main_checkout)
    total = {"PASS": 0, "STUB": 0, "FAIL": 0}
    for turn in (1, 2):
        pass_dir = Path(tempfile.mkdtemp(prefix=f"wp5-pass-{turn}-", dir=base))
        results = []
        group_results: dict[str, tuple[str, str]] = {}
        root = sandbox(pass_dir, 0, args.phase1, stock, bun)
        if not args.phase1 and args.live_streamer and args.live_streamer.is_file():
            shutil.copy2(args.live_streamer, root / "inputs/live-tg-stream.py")
        print(f"PASS {turn} root={pass_dir}", flush=True)
        for number, title, required in SCENARIOS:
            current = sandbox(pass_dir, number, args.phase1, stock, bun) if number == 21 else root
            try:
                if number == 1:
                    detail = scenario_1(current)
                    status = "PASS"
                elif number == 20:
                    detail = scenario_20(current)
                    status = "PASS"
                elif number == 21:
                    detail = scenario_21(current)
                    status = "PASS"
                elif not args.phase1:
                    group, marker = EVIDENCE[number]
                    if group not in group_results:
                        try:
                            output = integration_suite(current, group)
                            group_results[group] = ("PASS", output)
                        except (AssertionError, OSError) as exc:
                            group_results[group] = ("FAIL", str(exc))
                    status, output = group_results[group]
                    if status == "PASS" and marker not in output:
                        status, detail = "FAIL", f"{group} suite lacks assertion: {marker}"
                    elif status == "PASS":
                        detail = f"{group} suite assertion: {marker}"
                    else:
                        detail = output
                    if status == "PASS" and number == 12:
                        guardian = suite(current, ("python3", "tests/hooks/hook-runtime.test.py", "-k", "guardian"), "guardian")
                        if "test_guardian_61_and_91_single_bot_cells" not in guardian:
                            status, detail = "FAIL", "single-bot guardian assertion missing"
                    if status == "PASS" and number == 15:
                        backend = suite(current, ("python3", "-m", "unittest", "discover", "-s", "tests/ops",
                                                  "-p", "test_*.py", "-v", "-k", "custom_adapter"), "email-backend")
                        if "test_custom_adapter_claims_its_own_attempt" not in backend:
                            status, detail = "FAIL", "email adapter contract assertion missing"
                else:
                    detail = scenario_stub(current, number, title, required)
                    status = "STUB"
            except (AssertionError, OSError) as exc:
                detail = str(exc)
                status = "FAIL"
            total[status] += 1
            results.append({"id": number, "title": title, "status": status, "detail": detail})
            print(f"  {number:02d} {status} {title}: {detail}", flush=True)
        (pass_dir / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    after = snapshot(args.real_home, args.main_checkout)
    changes = changed_items(before, after)
    files_untouched = not changes
    pid_verified = (before.get("live-daemon-labels") != "unavailable"
                    and after.get("live-daemon-labels") != "unavailable")
    state = "PASS" if files_untouched and pid_verified else "PARTIAL" if files_untouched else "FAIL"
    print(f"UNTOUCHED {state}", flush=True)
    if not pid_verified:
        print("  live daemon PID check unavailable in this execution sandbox", flush=True)
    for key in changes:
        print(f"  changed: {key}", flush=True)
        if key.startswith("live-daemon:"):
            print(f"  daemon PIDs before={before.get(key)} after={after.get(key)}", flush=True)
    print("TOTAL grouped-scenario-results=42 (one shared root for scenarios 1-20 and one Linux root per pass; suites reused within each pass) "
          + " ".join(f"{k}={v}" for k, v in total.items()), flush=True)
    return 0 if files_untouched and not total["FAIL"] and (args.phase1 or (not total["STUB"] and pid_verified)) else 1


if __name__ == "__main__":
    sys.exit(main())
