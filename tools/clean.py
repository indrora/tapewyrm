#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Remove build, package, and test/cache artifacts.

Cross-platform (the justfile runs under cmd.exe on Windows, where `rm -rf` does
not exist). Deliberately does NOT touch the uv virtualenv (`host/.venv`) — that is
expensive to recreate; use `uv sync` if you want to rebuild it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Whole directories to remove if present.
DIRS = [
    ROOT / "firmware" / "out",  # firmware build output
    ROOT / "dist",  # one-package output
    ROOT / "host" / "dist",  # host wheel / sdist
    ROOT / "host" / "htmlcov",  # coverage HTML
]
# Tool caches at the two dirs tooling runs from (repo root + host/).
for _base in (ROOT, ROOT / "host"):
    DIRS += [_base / ".ruff_cache", _base / ".mypy_cache", _base / ".pytest_cache"]

# Python bytecode / metadata, only within source trees (so we never walk .venv).
SRC_ROOTS = [
    ROOT / "host" / "tapewyrm",
    ROOT / "host" / "tests",
    ROOT / "tools",
    ROOT / "protocol",
]


def rm(path: Path) -> None:
    try:
        rel = path.relative_to(ROOT)
    except ValueError:
        rel = path
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
        print(f"removed {rel}/")
    elif path.exists():
        path.unlink()
        print(f"removed {rel}")


def main() -> int:
    for d in DIRS:
        rm(d)
    for src in SRC_ROOTS:
        if not src.exists():
            continue
        for p in src.rglob("__pycache__"):
            rm(p)
        for p in src.rglob("*.egg-info"):
            rm(p)
    for base in (ROOT, ROOT / "host"):
        for cov in base.glob(".coverage*"):
            rm(cov)
    print("clean complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
