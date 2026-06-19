"""DriveProfile TOML loader (DESIGN.md §6A.3) — data, not code.

A new drive is a new ``profiles/<name>.toml``, never a code change (the analogue
of ftape's ``vendors.h``). This loads a TOML file into the ``DriveProfile``
dataclass from ``tapewyrm.types``. A bare name resolves against the packaged
``tapewyrm/profiles/<name>.toml``; a path is loaded directly. ``default`` falls
back to ``DriveProfile.default()``.

TOML schema (all keys optional except ``name``)::

    name = "colorado"
    bit_order = "lsb"            # "msb" | "lsb"
    report_strategy = "index_edge"   # "index_edge" | "fixed_settle"
    quirks = ["slow_wake"]

    [timing]
    pulse_us = 200
    inter_pulse_us = 2000
    terminate_gap_us = 3000
    report_settle_us = 900
    motion_timeout_s = 20

    # each wake step: a [[wake]] table of (cmd, arg, delay_ms)
    [[wake]]
    cmd = "soft reset"
    arg = 0            # optional; omit / null for no argument
    delay_ms = 1000
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from tapewyrm.types import DriveProfile, TimingParams

PROFILES_DIR = Path(__file__).resolve().parent.parent / "profiles"


class ProfileError(Exception):
    """A drive profile could not be loaded or is malformed."""


def _resolve_path(name_or_path: str) -> Path:
    """Resolve a bare profile name or an explicit path to a TOML file."""
    p = Path(name_or_path)
    # Explicit path (has a separator or a .toml suffix or actually exists).
    if p.suffix == ".toml" or p.exists() or p.is_absolute() or len(p.parts) > 1:
        return p
    return PROFILES_DIR / f"{name_or_path}.toml"


def _parse_timing(d: dict) -> TimingParams:
    defaults = TimingParams()
    return TimingParams(
        pulse_us=int(d.get("pulse_us", defaults.pulse_us)),
        inter_pulse_us=int(d.get("inter_pulse_us", defaults.inter_pulse_us)),
        terminate_gap_us=int(d.get("terminate_gap_us", defaults.terminate_gap_us)),
        report_settle_us=int(d.get("report_settle_us", defaults.report_settle_us)),
        motion_timeout_s=int(d.get("motion_timeout_s", defaults.motion_timeout_s)),
    )


def _parse_wake(raw: object) -> tuple[tuple[str, int | None, int], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ProfileError("`wake` must be an array of tables")
    steps: list[tuple[str, int | None, int]] = []
    for i, step in enumerate(raw):
        if not isinstance(step, dict):
            raise ProfileError(f"wake step {i} must be a table")
        cmd = step.get("cmd")
        if not isinstance(cmd, str):
            raise ProfileError(f"wake step {i} missing string `cmd`")
        arg_raw = step.get("arg")
        arg = None if arg_raw is None else int(arg_raw)
        delay_ms = int(step.get("delay_ms", 0))
        steps.append((cmd, arg, delay_ms))
    return tuple(steps)


def load_profile(name_or_path: str) -> DriveProfile:
    """Load a DriveProfile by bare name or path; ``default`` -> built-in fallback."""
    if name_or_path == "default":
        return DriveProfile.default()

    path = _resolve_path(name_or_path)
    if not path.exists():
        raise ProfileError(f"profile not found: {name_or_path} (looked at {path})")
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ProfileError(f"could not read profile {path}: {exc}") from exc

    name = data.get("name")
    if not isinstance(name, str) or not name:
        # Fall back to the file stem if `name` is absent.
        name = path.stem

    timing = _parse_timing(data.get("timing", {}))
    wake = _parse_wake(data.get("wake"))
    quirks = data.get("quirks", [])
    if not isinstance(quirks, list):
        raise ProfileError("`quirks` must be an array of strings")

    return DriveProfile(
        name=name,
        wake_sequence=wake,
        timing=timing,
        bit_order=str(data.get("bit_order", "lsb")),
        report_strategy=str(data.get("report_strategy", "index_edge")),
        quirks=frozenset(str(q) for q in quirks),
    )
