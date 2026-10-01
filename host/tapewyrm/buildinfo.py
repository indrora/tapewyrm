"""Which source this ``tw`` was built from (for ``tw info``).

Two ways to know, in order of preference:

1. **Running from a git checkout** (``uv run tw``, editable installs): ask git
   directly, so the answer is never stale. We first check that this package's
   own files are *tracked* in the repo -- a wheel installed into a virtualenv
   that happens to live inside the repo (``host/.venv``) would otherwise report
   the repo's commit for code it does not contain.
2. **An installed wheel**: ``hatch_build.py`` stamps ``tapewyrm/_build_stamp.py``
   into the wheel at build time with the commit and dirty flag.

Otherwise the commit is unknown (e.g. a wheel built from an sdist, where there
is no git history to read).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path


@dataclass(frozen=True)
class HostBuild:
    version: str
    commit: str | None  # 40-char hex SHA
    dirty: bool  # host/ had uncommitted changes (checkout) / at build (wheel)
    source: str  # "git checkout" | "build stamp" | "unknown"


def _git(cwd: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip()


def host_build() -> HostBuild:
    try:
        version = metadata.version("tapewyrm")
    except metadata.PackageNotFoundError:
        version = "unknown"

    pkg = Path(__file__).resolve().parent
    # Only trust git if this package is tracked here (see module docstring).
    # __init__.py, not this file: a brand-new untracked module would fail it.
    if _git(pkg, "ls-files", "--error-unmatch", "__init__.py") is not None:
        commit = _git(pkg, "rev-parse", "HEAD")
        if commit:
            # Dirty = tracked changes anywhere in the host project (host/).
            status = _git(pkg, "status", "--porcelain", "--untracked-files=no", "--", "..")
            return HostBuild(version, commit, bool(status), "git checkout")

    try:
        from tapewyrm import _build_stamp  # type: ignore[attr-defined]
    except ImportError:
        return HostBuild(version, None, False, "unknown")
    return HostBuild(version, _build_stamp.COMMIT or None, bool(_build_stamp.DIRTY), "build stamp")
