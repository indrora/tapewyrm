"""DriveProfile TOML loader (DESIGN.md §6A.3) — data, not code.

A new drive is a new ``profiles/drive/<name>.toml``, never a code change (the analogue
of ftape's ``vendors.h``). This loads a TOML file into the ``DriveProfile``
dataclass from ``tapewyrm.types``. A bare name resolves against the packaged
``tapewyrm/profiles/drive/<name>.toml``; a path is loaded directly. ``default`` falls
back to ``DriveProfile.default()``.

TOML schema (all keys optional except ``name``)::

    name = "colorado"
    bit_order = "lsb"            # "msb" | "lsb"
    report_strategy = "fixed_settle"   # "fixed_settle" | "index_edge"
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

import logging
import tomllib
from pathlib import Path

from tapewyrm.types import DriveProfile, TimingParams

log = logging.getLogger(__name__)

PROFILES_DIR = Path(__file__).resolve().parent.parent / "profiles" / "drive"


class ProfileError(Exception):
    """A drive profile could not be loaded or is malformed."""


def _resolve_path(name_or_path: str) -> Path:
    """Resolve a bare profile name or an explicit path to a TOML file."""
    p = Path(name_or_path)
    # Explicit path (has a separator or a .toml suffix or actually exists).
    if p.suffix == ".toml" or p.exists() or p.is_absolute() or len(p.parts) > 1:
        log.debug("profile %r looks like a path; using it directly", name_or_path)
        return p
    log.debug("profile %r is a bare name; resolving under %s", name_or_path, PROFILES_DIR)
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
        log.debug("profile has no [[wake]] steps; empty wake sequence")
        return ()
    if not isinstance(raw, list):
        log.debug("`wake` is %s, not a list; refusing", type(raw).__name__)
        raise ProfileError("`wake` must be an array of tables")
    steps: list[tuple[str, int | None, int]] = []
    for i, step in enumerate(raw):
        if not isinstance(step, dict):
            log.debug("wake step %d is %s, not a table; refusing", i, type(step).__name__)
            raise ProfileError(f"wake step {i} must be a table")
        cmd = step.get("cmd")
        if not isinstance(cmd, str):
            log.debug("wake step %d cmd is %r, not a string; refusing", i, cmd)
            raise ProfileError(f"wake step {i} missing string `cmd`")
        arg_raw = step.get("arg")
        arg = None if arg_raw is None else int(arg_raw)
        delay_ms = int(step.get("delay_ms", 0))
        steps.append((cmd, arg, delay_ms))
    return tuple(steps)


def load_profile(name_or_path: str) -> DriveProfile:
    """Load a DriveProfile by bare name or path; ``default`` -> built-in fallback."""
    if name_or_path == "default":
        log.debug("profile 'default' requested; using built-in DriveProfile.default()")
        return DriveProfile.default()

    path = _resolve_path(name_or_path)
    if not path.exists():
        log.debug("profile %r resolved to %s, which does not exist; refusing", name_or_path, path)
        raise ProfileError(f"profile not found: {name_or_path} (looked at {path})")
    log.debug("loading drive profile from %s", path)
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        log.debug("reading profile %s failed (%s); refusing", path, exc)
        raise ProfileError(f"could not read profile {path}: {exc}") from exc

    name = data.get("name")
    if not isinstance(name, str) or not name:
        # Fall back to the file stem if `name` is absent.
        log.debug("profile `name` is %r; falling back to file stem %r", name, path.stem)
        name = path.stem

    timing = _parse_timing(data.get("timing", {}))
    wake = _parse_wake(data.get("wake"))
    quirks = data.get("quirks", [])
    if not isinstance(quirks, list):
        log.debug("`quirks` is %s, not a list; refusing", type(quirks).__name__)
        raise ProfileError("`quirks` must be an array of strings")

    return DriveProfile(
        name=name,
        wake_sequence=wake,
        timing=timing,
        bit_order=str(data.get("bit_order", "lsb")),
        report_strategy=str(data.get("report_strategy", "fixed_settle")),
        quirks=frozenset(str(q) for q in quirks),
    )
