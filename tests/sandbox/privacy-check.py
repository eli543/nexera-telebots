#!/usr/bin/env python3
"""Reject private identifiers and bot tokens before a public commit.

Only file and line numbers are printed. Source text is never echoed.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".next"}
SKIP_FILES = {"privacy-check.py", "privacy-allowlist.txt"}
TEXT_SUFFIXES = {".md", ".sh", ".py", ".ts", ".tsx", ".js", ".json", ".yml", ".yaml", ".swift", ".plist", ".txt", ".patch"}
TOKEN = re.compile(r"[0-9]{8,10}:[A-Za-z0-9_-]{35}")
WORDS = re.compile(r"[A-Za-z0-9_]+")

# Hashes keep private identifiers out of the public source while still making
# a direct identifier match fail. The first two are account identifiers, the
# next five are private bot names, then weekday, owner name and chat identifier.
DENIED = {
    "6607307e5db74f8cc0c5961d311b0aaaf86ab03c73f30baacf3a9277709f8bc7": "account",
    "abf4ed63f70a1c775f1885e96a0b72a9dded3c95803f972d5de482adbeb49500": "account",
    "4e71081de38adddc8127b4afb165ceb6c7c9e11440ea8c0353c940ef9855c72f": "private bot",
    "37b976e5022fba5b5060ab31321a610d454eaa6b0f154354f75bb076f2cd92f3": "private bot",
    "d3ba9ece936f9dceced2c91292170194c9abb8f2fd061a6920de605bd32a9265": "private bot",
    "6189f7898bc697c916b7f9aa516861a4f8c2f071cfac6f58e1c81be141226703": "private bot",
    "79403a38ef252306a966db0b3b1ec65a034e2980db1427dfda8dcadf9df0d926": "private bot",
    "e21f3f372a46d4be7b49f6809fc91baefeadae004af06485518e124a142c0271": "weekday or private bot",
    "8d6d4f52ee2db64142fab11d1044eaeb7573b009f7bd404923326bc8c0e7ee06": "owner name",
    "d48d5bdcb4e42b468f60a622bdd45d82da6ba5ea443f4de8444bb33a694c165e": "chat identifier",
}
MONTHS = "January February March April May June July August September October November December".split()
DATE = re.compile(r",?\s+(?:" + "|".join(MONTHS) + r")\s+\d{1,2}\b")
ALLOWLIST = {
    tuple(row.split("|", 1))
    for row in (Path(__file__).with_name("privacy-allowlist.txt")).read_text().splitlines()
    if row and not row.startswith("#")
}


def allowed(path: Path, kind: str, line: str) -> bool:
    if (path.as_posix(), kind) in ALLOWLIST:
        return True
    if kind == "weekday or private bot" and DATE.search(line):
        return True
    return False


def scan(path: Path) -> list[tuple[int, str]]:
    relative = path.relative_to(ROOT)
    try:
        data = path.read_text(encoding="utf-8")
    except (UnicodeError, OSError):
        return []
    issues: list[tuple[int, str]] = []
    for number, line in enumerate(data.splitlines(), 1):
        if TOKEN.search(line):
            issues.append((number, "bot token shape"))
        if "/Users/" in line and "/Users/example" not in line:
            issues.append((number, "home path"))
        if "~/.local/bin/claude-channels" in line:
            issues.append((number, "private engine path"))
        if not relative.parts[0] == "docs" and ("Knowledge/" in line or "Obsidian" in line):
            issues.append((number, "private knowledge reference"))
        for word in WORDS.findall(line):
            kind = DENIED.get(hashlib.sha256(word.encode()).hexdigest())
            if kind and not allowed(relative, kind, line):
                issues.append((number, kind))
    return issues


def main() -> int:
    findings: list[tuple[str, int, str]] = []
    for path in ROOT.rglob("*"):
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        if not path.is_file() or path.name in SKIP_FILES:
            continue
        if path.suffix not in TEXT_SUFFIXES and path.name not in {"install.sh", "uninstall.sh"}:
            continue
        for number, kind in scan(path):
            findings.append((str(path.relative_to(ROOT)), number, kind))
    if findings:
        for name, number, kind in findings[:40]:
            print(f"{name}:{number}: {kind}")
        print(f"Privacy gate failed: {len(findings)} finding(s).")
        return 1
    print("Privacy gate passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
