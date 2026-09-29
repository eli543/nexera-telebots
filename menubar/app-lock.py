#!/usr/bin/env python3
"""Hold Telebots Bar's actual flock through replacement, including child copies."""
import ctypes
import plistlib
import fcntl
import os
import shutil
import subprocess
import sys
from pathlib import Path

lock_path = Path.home() / 'Library/Application Support/Telebots Bar/instance.lock'

def open_lock():
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    return os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)

def take_lock():
    fd = open_lock()
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise RuntimeError('Telebots Bar is running or another replacement holds its instance lock; nothing replaced')
    return fd

def verify_inherited():
    fd = int(os.environ.get('TELEBOTS_SWAP_LOCK_FD', '-1'))
    current = os.stat(lock_path, follow_symlinks=False)
    held = os.fstat(fd)
    if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
        raise RuntimeError('replacement lock descriptor does not name the app instance lock')
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd

def job_state():
    label = 'com.claude-fleet.menubar'
    result = subprocess.run(['/bin/launchctl', 'print', f'gui/{os.getuid()}/{label}'], capture_output=True, text=True)
    if result.returncode == 0:
        return 'loaded'
    if result.returncode == 113 and f'Could not find service "{label}"' in result.stderr:
        return 'unloaded'
    raise RuntimeError('could not inspect LaunchAgent state; replacement refused')

def ensure_no_copies():
    lib = ctypes.CDLL('/usr/lib/libproc.dylib')
    for item in subprocess.check_output(['/bin/ps', '-U', str(os.getuid()), '-o', 'pid='], text=True).split():
        buffer = ctypes.create_string_buffer(4096)
        if lib.proc_pidpath(int(item), buffer, len(buffer)) > 0 and buffer.value.decode().endswith('/Telebots Bar.app/Contents/MacOS/Telebots Bar'):
            raise RuntimeError(f'Telebots Bar copy is running (pid {item}); replacement refused')

def plist_command(path, expected_exe):
    with open(path, 'rb') as f:
        args = plistlib.load(f).get('ProgramArguments', [])
    if not args or args[0] != expected_exe or '--launchd' not in args:
        raise RuntimeError('previous plist does not launch the bundle this installer can restore; nothing stopped')
    return ' '.join(args)

def restore_files(previous, app, plist):
    # The caller has unloaded the service. The flock excludes manual/staged copies.
    verify_inherited()
    if job_state() != 'unloaded': raise RuntimeError('LaunchAgent is still loaded; rollback refused')
    ensure_no_copies()
    previous, app, plist = map(Path, (previous, app, plist))
    old_app, old_plist = previous / 'Telebots Bar.app', previous / 'agent.plist'
    if not (previous / 'complete').is_file():
        raise RuntimeError('rollback backup is incomplete; current files left in place')
    incoming = app.with_name('.Telebots Bar.rollback-' + str(os.getpid()))
    if old_app.exists():
        subprocess.run(['/usr/bin/ditto', str(old_app), str(incoming)], check=True)
        subprocess.run(['/usr/bin/codesign', '--verify', '--strict', str(incoming)], check=True)
    if app.exists():
        shutil.rmtree(app)
    if old_app.exists():
        os.rename(incoming, app)
    if old_plist.exists():
        plist.parent.mkdir(parents=True, exist_ok=True)
        temp = plist.with_name(plist.name + '.rollback-' + str(os.getpid()))
        shutil.copy2(old_plist, temp)
        os.replace(temp, plist)
    elif plist.exists():
        plist.unlink()

def verify_holder(pid, allow_legacy=False):
    fd = open_lock()
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise RuntimeError('the service has no held instance lock')
        lines = os.pread(fd, 256, 0).decode().splitlines()
        # Older installed builds wrote one line. Rollback also verifies launchd's
        # exact executable/arguments before admitting this compatibility path.
        if allow_legacy and lines == [f'{pid} launchd']:
            os.kill(int(pid), 0)
            return
        if len(lines) != 2 or lines[0] != f'{pid} launchd' or not lines[1].startswith('started '):
            raise RuntimeError('instance lock does not name the launched service')
        import datetime
        env = dict(os.environ, TZ='UTC', LC_ALL='C')
        stamp = subprocess.check_output(['/bin/ps', '-p', pid, '-o', 'lstart='], env=env, text=True).strip()
        actual = datetime.datetime.strptime(stamp, '%a %b %d %H:%M:%S %Y').replace(tzinfo=datetime.timezone.utc).timestamp()
        if int(float(lines[1][8:])) != int(actual):
            raise RuntimeError('service PID start time changed')
    finally:
        os.close(fd)

try:
    args = sys.argv[1:]
    if args[0] == '--job-state':
        print(job_state())
    elif args[0] == '--no-copies':
        ensure_no_copies()
    elif args[0] == '--plist-command':
        print(plist_command(*args[1:]))
    elif args[0] == '--verify':
        verify_inherited()
    elif args[0] == '--verify-holder':
        verify_holder(args[1])
    elif args[0] == '--verify-rollback-holder':
        verify_holder(args[1], allow_legacy=True)
    elif args[0] == '--restore-files':
        restore_files(*args[1:])
    else:
        fd = take_lock()
        try:
            env = dict(os.environ, TELEBOTS_SWAP_LOCK_FD=str(fd))
            result = subprocess.run(args, env=env, pass_fds=(fd,))
            sys.exit(result.returncode)
        finally:
            os.close(fd)
except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
    print('Telebots replacement: ' + str(error), file=sys.stderr)
    sys.exit(1)
