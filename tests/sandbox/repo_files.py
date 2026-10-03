"""List and copy repository files that Git tracks or would track."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess


def _paths(data: bytes) -> list[Path]:
    paths = [Path(os.fsdecode(name)) for name in data.split(b"\0") if name]
    if any(path.is_absolute() or ".." in path.parts for path in paths):
        raise ValueError("Git file list contains a path outside the repository")
    return paths


def git_paths(repo: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        check=True,
        capture_output=True,
    )
    return _paths(result.stdout)


def read_manifest(path: Path) -> list[Path]:
    return _paths(path.read_bytes())


def write_manifest(path: Path, paths: list[Path]) -> None:
    path.write_bytes(b"".join(os.fsencode(path.as_posix()) + b"\0" for path in paths))


def copy_files(source: Path, destination: Path, paths: list[Path]) -> list[Path]:
    destination.mkdir()
    copied = []
    for relative in paths:
        original = source / relative
        target = destination / relative
        if original.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(os.readlink(original))
        elif original.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, target)
        else:
            # A tracked file may have been deleted in the working tree.
            continue
        copied.append(relative)
    return copied
