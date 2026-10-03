#!/usr/bin/env bash
# Shared implementation for the version-pinned local overlay scripts.
# This file performs no action when sourced; entry points call overlay_run.

overlay_run() {
  python3 - "$1" "$SCRIPT_DIR" "${HOME:?HOME must be set}" "$STOCK_SHA" "$PATCHED_SHA" "$EXPECT_VERSION" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

operation, source_arg, home, stock_sha, patched_sha, expected_version = sys.argv[1:]
source = Path(source_arg)
cache = Path(home) / '.claude/plugins/cache/claude-plugins-official/telegram'
modules = ('telebots.ts', 'email.ts', 'contracts.ts', 'ux.json')
marker_name = '.telebots-overlay.json'
backup_name = 'server.ts.stock-backup'
status_codes = {'OK': 0, 'STOCK': 10, 'MISSING_MODULE': 11, 'TAMPERED': 12,
                'VERSION_MISMATCH': 13, 'NOT_INSTALLED': 14}

class Refusal(Exception):
    def __init__(self, message, code):
        self.message, self.code = message, code

def sha(data):
    return hashlib.sha256(data).hexdigest()

def regular(path):
    return path.is_file() and not path.is_symlink()

def digest(path):
    return sha(path.read_bytes()) if regular(path) else None

def version(slot):
    try:
        value = json.loads((slot / '.claude-plugin/plugin.json').read_text())['version']
        if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9.+_-]+', value):
            return value
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return 'unknown'

def select_slot():
    if not cache.is_dir():
        return None
    slots = [p for p in cache.iterdir() if p.is_dir() and not p.is_symlink()]
    if not slots:
        return None
    # A newer version wins even when an older slot was just edited. A slot with
    # a missing server still wins so an older version cannot be patched by mistake.
    def order(path):
        numbers = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)', path.name)
        return (tuple(map(int, numbers.groups())) if numbers else (-1, -1, -1),
                path.stat().st_mtime_ns, path.name)
    return max(slots, key=order)

def pinned_slot():
    slot = cache / expected_version
    return slot if slot.is_dir() and not slot.is_symlink() else None

def restore_plan(slot, newest):
    if slot is None:
        return 'OTHER_VERSION:' + version(newest) if newest is not None else 'ABSENT'
    if version(slot) != expected_version:
        raise Refusal('The pinned plugin slot has invalid version metadata; no files changed.', 13)
    server = slot / 'server.ts'
    if not server.exists() and not server.is_symlink():
        return 'ABSENT'
    current = digest(server)
    if current == stock_sha:
        return 'STOCK'
    if current != patched_sha:
        raise Refusal('Unknown server.ts hash; no installed files changed.', 12)
    checked_backup(slot, required=True)
    if (slot / '.telebots-overlay.lock').exists():
        raise Refusal('Another overlay operation is active; no installed files changed.', 24)
    return 'RESTORE'

def source_payloads():
    if any(not regular(source / name) for name in modules):
        raise Refusal('The overlay package is incomplete; no installed files changed.', 21)
    data = {name: (source / name).read_bytes() for name in modules}
    try:
        json.loads(data['ux.json'])
    except (ValueError, UnicodeError):
        raise Refusal('The packaged ux.json is invalid; no installed files changed.', 21)
    return data

def manifest(payloads):
    return {'schema': 1, 'plugin_version': expected_version, 'stock_sha256': stock_sha,
            'server_sha256': patched_sha,
            'modules': {name: sha(data) for name, data in payloads.items()}}

def status(slot):
    if slot is None:
        return 'NOT_INSTALLED'
    ver = version(slot)
    if ver != expected_version:
        return 'VERSION_MISMATCH:' + ver
    server = slot / 'server.ts'
    if not server.exists() and not server.is_symlink():
        return 'NOT_INSTALLED'
    current = digest(server)
    if current == stock_sha:
        return 'STOCK'
    if current != patched_sha:
        return 'TAMPERED'
    if any(not (slot / name).exists() and not (slot / name).is_symlink() for name in modules):
        return 'MISSING_MODULE'
    if any(not regular(slot / name) for name in modules):
        return 'TAMPERED'
    try:
        expected = manifest(source_payloads())
        marker = slot / marker_name
        if not regular(marker) or json.loads(marker.read_text()) != expected:
            return 'TAMPERED'
        if any(digest(slot / name) != value for name, value in expected['modules'].items()):
            return 'TAMPERED'
        if digest(slot / backup_name) != stock_sha:
            return 'TAMPERED'
    except (OSError, ValueError, UnicodeError, Refusal):
        return 'TAMPERED'
    return 'OK'

def require_slot(slot):
    if slot is None:
        raise Refusal('Official Telegram plugin is not installed.', 14)
    ver = version(slot)
    if ver != expected_version:
        raise Refusal('Unsupported plugin version: ' + ver + '. Expected ' + expected_version + '.', 13)
    server = slot / 'server.ts'
    if not server.exists() and not server.is_symlink():
        raise Refusal('Official Telegram server.ts is missing.', 14)
    if digest(server) not in (stock_sha, patched_sha):
        raise Refusal('Unknown server.ts hash; no installed files changed.', 12)

def checked_backup(slot, required=False):
    backup = slot / backup_name
    if backup.exists() or backup.is_symlink():
        if digest(backup) != stock_sha:
            raise Refusal('Stock backup is not the pinned upstream file; no installed files changed.', 22)
        return backup.read_bytes()
    if required:
        raise Refusal('Verified stock backup is missing; no installed files changed.', 22)
    return None

def publish(slot, stage, changes, original_server):
    # Preflight every destination and snapshot bytes before the first replacement.
    before = {}
    metadata = {}
    for name in changes:
        target = slot / name
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise Refusal('An overlay destination is not a regular file; no installed files changed.', 12)
        before[name] = target.read_bytes() if target.exists() else None
        metadata[name] = target.stat() if target.exists() else None
    if (slot / 'server.ts').read_bytes() != original_server or version(slot) != expected_version:
        raise Refusal('Plugin changed during preparation; no installed files changed.', 24)
    for name, data in changes.items():
        if data is not None:
            (stage / ('next-' + name)).write_bytes(data)
            (stage / ('next-' + name)).chmod(0o644)
    for name, data in before.items():
        target = slot / name
        current = target.read_bytes() if regular(target) else None
        if current != data or target.is_symlink():
            raise Refusal('An overlay file changed during preparation; no installed files changed.', 24)
    applied = []
    try:
        for name, data in changes.items():
            target = slot / name
            if data is None:
                target.unlink(missing_ok=True)
            else:
                os.replace(stage / ('next-' + name), target)
            applied.append(name)
    except OSError:
        for name in reversed(applied):
            target = slot / name
            if before[name] is None:
                target.unlink(missing_ok=True)
            else:
                rollback = stage / ('rollback-' + name)
                rollback.write_bytes(before[name])
                rollback.chmod(metadata[name].st_mode & 0o777)
                os.utime(rollback, ns=(metadata[name].st_atime_ns, metadata[name].st_mtime_ns))
                os.replace(rollback, target)
        raise Refusal('Overlay write failed and previous bytes were restored.', 25)

def mutate(slot):
    require_slot(slot)
    original = (slot / 'server.ts').read_bytes()
    was_stock = sha(original) == stock_sha
    backup = checked_backup(slot, required=not was_stock or operation == 'restore')
    payloads = source_payloads() if operation == 'install' else {}
    if operation == 'install' and status(slot) == 'OK':
        print('Telebots overlay already verified; no files changed.')
        return
    lock = slot / '.telebots-overlay.lock'
    try:
        lock.mkdir(mode=0o700)
    except FileExistsError:
        raise Refusal('Another overlay operation is active; no installed files changed.', 24)
    try:
        checked_backup(slot, required=not was_stock or operation == 'restore')
        with tempfile.TemporaryDirectory(prefix='.telebots-stage-', dir=slot) as tmp:
            stage = Path(tmp)
            if operation == 'install':
                patch = source / 'seam-0.0.7.patch'
                if not regular(patch):
                    raise Refusal('Seam patch is missing; no installed files changed.', 21)
                pristine = original if was_stock else backup
                (stage / 'server.ts').write_bytes(pristine)
                for dry in (True, False):
                    argv = ['patch', '--batch', '--forward', '-p1', '-d', str(stage)]
                    if dry:
                        argv.append('--dry-run')
                    result = subprocess.run(argv, input=patch.read_bytes(), capture_output=True)
                    if result.returncode:
                        raise Refusal('Seam patch did not apply; no installed files changed.', 23)
                patched = (stage / 'server.ts').read_bytes()
                if sha(patched) != patched_sha:
                    raise Refusal('Patched hash differs from the pin; no installed files changed.', 23)
                changes = {}
                if backup is None:
                    changes[backup_name] = pristine
                changes.update(payloads)
                changes[marker_name] = (json.dumps(manifest(payloads), indent=2, sort_keys=True) + '\n').encode()
                # server.ts becomes visible only after all its dependencies exist.
                changes['server.ts'] = patched
                publish(slot, stage, changes, original)
                print('Installed the verified Telebots overlay. Restart bots to load it.')
            else:
                changes = {'server.ts': backup}
                # Remove only files whose hashes match the installed marker.
                # Modified support files are retained, inert under stock server.ts.
                try:
                    saved = json.loads((slot / marker_name).read_text())
                    for name in modules:
                        if digest(slot / name) == saved.get('modules', {}).get(name):
                            if regular(slot / name):
                                changes[name] = None
                    if regular(slot / marker_name):
                        changes[marker_name] = None
                except (OSError, ValueError, UnicodeError, AttributeError):
                    pass
                publish(slot, stage, changes, original)
                print('Restored verified stock server.ts. The stock backup is retained.')
    finally:
        lock.rmdir()

try:
    newest = select_slot()
    slot = pinned_slot() if operation in ('restore', 'restore-plan') else newest
    if operation in ('status', 'verify'):
        value = status(slot)
        print(value if operation == 'status' else 'Overlay status: ' + value)
        sys.exit(status_codes[value.split(':', 1)[0]])
    if operation in ('restore', 'restore-plan'):
        plan = restore_plan(slot, newest)
        if operation == 'restore-plan':
            print(plan)
            sys.exit(0)
        if plan != 'RESTORE':
            print('No modified 0.0.7 plugin slot to restore (' + plan + ').')
            sys.exit(0)
    mutate(slot)
except Refusal as error:
    print('error: ' + error.message, file=sys.stderr)
    sys.exit(error.code)
except (OSError, ValueError) as error:
    if operation in ('status', 'verify'):
        print('TAMPERED' if operation == 'status' else 'Overlay status: TAMPERED')
        sys.exit(12)
    print('error: Overlay file operation failed (' + type(error).__name__ + ').', file=sys.stderr)
    sys.exit(25)
PY
}
