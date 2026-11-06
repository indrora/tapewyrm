"""DriveProfile TOML loader (DESIGN.md §6A.3) — data, not code.

A new drive is a new ``profiles/drive/<name>.toml``, never a code change (the analogue
of ftape's ``vendors.h``). This loads a TOML file into the ``DriveProfile``
dataclass from ``tapewyrm.types``. A bare name resolves against the packaged
``tapewyrm/profiles/drive/<name>.toml``; a path is loaded directly. ``default`` falls
back to ``DriveProfile.default()`` (no wake sequence). ``auto`` is not a file: it
names the probe list ``AUTO_ORDER`` (see below and ``drive.auto_wake``).

TOML schema (all keys optional except ``name``)::

    name = "teac"
    inherits = "generic"         # another profile (bare name or path) to start from
    wakeup = "colorado"          # ftape wake-up method: "none" | "colorado" | "mountain" | "insight"
    vendor_ids = [0x01cc]        # Report Vendor ID words this profile is for
    bit_order = "lsb"            # "msb" | "lsb"
    report_strategy = "fixed_settle"   # "fixed_settle" | "index_edge"
    quirks = ["slow_wake"]

    [timing]
    pulse_us = 200
    inter_pulse_us = 1800
    terminate_gap_us = 3000
    tack_us = 10000
    report_settle_us = 900
    motion_timeout_s = 20

    # explicit wake steps, each a [[wake]] table of (cmd, arg, delay_ms);
    # when present they REPLACE whatever ``wakeup`` would have generated
    [[wake]]
    cmd = "soft reset"
    arg = 0            # optional; omit / null for no argument
    delay_ms = 1000

``inherits`` is resolved recursively (``generic`` is the packaged base with the
QIC-117 Rev J nominal timings and no wake). The child wins key by key:
``[timing]`` merges one value at a time, every other key -- ``wakeup``,
``wake``, ``vendor_ids``, ``quirks`` -- is replaced wholesale when the child
sets it. A cycle is a ``ProfileError``.

``wakeup`` names one of ftape's wake-up methods (include/linux/ftape-vendors.h
WAKEUP_METHODS) and expands to the step list in ``WAKEUPS`` below; a profile
that needs something else spells its own ``[[wake]]`` steps instead. ``cmd``
is a QIC-117 command name, or one of the line steps in ``drive.LINE_STEPS``:
``"delay"`` (nothing but its ``delay_ms``) and ``"motor on"`` (assert IBM PC
bus unit ``arg``'s select and motor-enable lines; ftape's Motor-on wake).

``vendor_ids`` lists the Report Vendor ID (32) words the profile was written
for, so ``tw drive status`` can name the matching profile and a future
auto-detect can pick it (ftape keys its whole vendor table on this word).
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


# ---------------------------------------------------------------------------
# `--profile auto`: which profiles the session may try, and in what order
# ---------------------------------------------------------------------------

#: The profile name that means "probe AUTO_ORDER at session time" (the default
#: when neither --profile nor a config file names one). It is not a file.
AUTO = "auto"

#: Profiles ``--profile auto`` tries, in this order, stopping at the first the
#: drive answers (``drive.auto_wake``).
#:
#: This is ftape's drive detection, method for method and in ftape's order:
#: ``ftape_activate_drive()`` in drivers/char/ftape/lowlevel/ftape-ctl.c walks
#: WAKEUP_METHODS from include/linux/ftape-vendors.h (Linux 2.6.19; the full
#: citation and what each method sends are in the comment block above
#: ``drive.auto_wake``). ftape is the safety precedent: these are the wakes it
#: sent, blind, to whatever was on the cable of every Linux box that loaded it.
#:
#: * ``default`` -- ftape "None" (no_wake_up): no wake steps, just ask for
#:   status. Answers if a drive is already listening (always-selected drives,
#:   or a phantom drive a previous session left selected).
#: * ``colorado`` -- ftape "Colorado": Phantom Select (46) + N+2 unit 0. tw
#:   adds Enter Primary Mode (30). Bench-verified on the Jumbo 350 and 1400.
#: * ``mountain`` -- ftape "Mountain": Soft Select (23) + its 20-pulse train.
#:   Conner, Archive, Summit, COREtape and PERTEC drives in ftape's vendor table.
#: * ``insight`` -- ftape "Motor-on": wait 100 ms, then assert the unit's
#:   motor-enable (and drive-select) line. Irwin/Insight 80 and early Iomega.
#:
#: Not here: ``colorado.1400`` (same wake as ``colorado``, so it could never
#: succeed where that failed), ``conner`` and ``iomega`` (unverified Soft Reset
#: placeholders; ftape never sends Soft Reset to wake a drive).
AUTO_ORDER: tuple[str, ...] = ("default", "colorado", "mountain", "insight")


#: ftape's wake-up methods by name (include/linux/ftape-vendors.h WAKEUP_METHODS:
#: None, Colorado, Mountain, Motor-on), as tw step lists. A profile's
#: ``wakeup = "<name>"`` expands to one of these; the step names are the ones
#: ``tw drive --unit`` rewrites, so unit addressing keeps working. What each
#: method sends, and why tw's colorado adds Enter Primary Mode, is in the
#: comment block above ``drive.auto_wake``.
WAKEUPS: dict[str, tuple[tuple[str, int | None, int], ...]] = {
    "none": (),
    "colorado": (("phantom select", 0, 50), ("enter primary mode", None, 50)),
    "mountain": (("soft select", 18, 0),),
    "insight": (("delay", None, 100), ("motor on", 0, 10)),
}


def auto_candidates() -> tuple[DriveProfile, ...]:
    """Load the ``AUTO_ORDER`` profiles, in order, for ``drive.auto_wake``."""
    log.debug("auto: candidate profiles in order: %s", ", ".join(AUTO_ORDER))
    return tuple(load_profile(name) for name in AUTO_ORDER)


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
        # The ACK window is terminate_gap + tack (firmware qic_report); see the
        # TimingParams.tack_us comment for why the default is generous.
        tack_us=int(d.get("tack_us", defaults.tack_us)),
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
    if name_or_path == AUTO:
        # "auto" is resolved per session by drive.auto_wake, not loaded here; a
        # caller that gets this far has skipped that branch.
        log.debug("profile 'auto' passed to load_profile; refusing")
        raise ProfileError("'auto' is not a profile file: probe auto_candidates() instead")

    data = _resolve_raw(name_or_path, seen=())
    name = data.get("name")
    if not isinstance(name, str) or not name:
        # Fall back to the file stem if `name` is absent.
        stem = _resolve_path(name_or_path).stem
        log.debug("profile `name` is %r; falling back to file stem %r", name, stem)
        name = stem

    timing = _parse_timing(data.get("timing", {}))
    wakeup = data.get("wakeup")
    if wakeup is not None and wakeup not in WAKEUPS:
        log.debug("profile %r wakeup %r is not an ftape method; refusing", name, wakeup)
        raise ProfileError(
            f"profile {name}: unknown wakeup {wakeup!r} (one of {', '.join(WAKEUPS)})"
        )
    if "wake" in data:
        # Explicit steps always win: they are how a profile says "ftape's
        # method, plus this" or "something ftape never did".
        if wakeup is not None:
            log.debug("profile %r has both wakeup=%r and [[wake]]; the steps win", name, wakeup)
        wake = _parse_wake(data.get("wake"))
    else:
        wake = WAKEUPS[wakeup or "none"]
    quirks = data.get("quirks", [])
    if not isinstance(quirks, list):
        log.debug("`quirks` is %s, not a list; refusing", type(quirks).__name__)
        raise ProfileError("`quirks` must be an array of strings")
    vendor_ids = data.get("vendor_ids", [])
    if not isinstance(vendor_ids, list) or not all(isinstance(v, int) for v in vendor_ids):
        log.debug("`vendor_ids` is %r, not a list of ints; refusing", vendor_ids)
        raise ProfileError("`vendor_ids` must be an array of integers")

    return DriveProfile(
        name=name,
        wake_sequence=wake,
        timing=timing,
        bit_order=str(data.get("bit_order", "lsb")),
        report_strategy=str(data.get("report_strategy", "fixed_settle")),
        quirks=frozenset(str(q) for q in quirks),
        wakeup=wakeup,
        vendor_ids=tuple(vendor_ids),
    )


def _read_toml(name_or_path: str) -> dict:
    """Read one profile file into a dict (no inheritance applied)."""
    path = _resolve_path(name_or_path)
    if not path.exists():
        log.debug("profile %r resolved to %s, which does not exist; refusing", name_or_path, path)
        raise ProfileError(f"profile not found: {name_or_path} (looked at {path})")
    log.debug("loading drive profile from %s", path)
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        log.debug("reading profile %s failed (%s); refusing", path, exc)
        raise ProfileError(f"could not read profile {path}: {exc}") from exc


def _resolve_raw(name_or_path: str, seen: tuple[str, ...]) -> dict:
    """Read a profile and fold its ``inherits`` chain into one dict.

    The merge is child-wins, key by key, except ``[timing]`` which merges one
    value at a time so a child can override a single number (teac sets only
    the pulse cadence and keeps generic's TACK). Lists are replaced, never
    concatenated: a child that says ``wake`` or ``vendor_ids`` means exactly
    that list. ``name`` is never inherited (a child without one gets its file
    stem, see ``load_profile``).
    """
    if name_or_path in seen:
        log.debug("profile inheritance cycle: %s -> %s; refusing", " -> ".join(seen), name_or_path)
        raise ProfileError(f"profile inheritance cycle: {' -> '.join((*seen, name_or_path))}")
    child = _read_toml(name_or_path)
    parent_name = child.get("inherits")
    if parent_name is None:
        return child
    if not isinstance(parent_name, str) or not parent_name:
        log.debug("profile %r `inherits` is %r, not a name; refusing", name_or_path, parent_name)
        raise ProfileError(f"profile {name_or_path}: `inherits` must be a profile name or path")
    log.debug("profile %r inherits %r; merging", name_or_path, parent_name)
    parent = _resolve_raw(parent_name, (*seen, name_or_path))
    merged = {k: v for k, v in parent.items() if k != "name"}
    for key, value in child.items():
        if key == "inherits":
            continue
        if key == "timing" and isinstance(value, dict) and isinstance(merged.get("timing"), dict):
            merged["timing"] = {**merged["timing"], **value}
        else:
            merged[key] = value
    return merged


def profiles_for_vendor(vendor_id: int) -> list[str]:
    """Names of the packaged profiles whose ``vendor_ids`` list ``vendor_id``.

    A bench aid for ``tw drive status`` (and the seed of a vendor-keyed
    auto-detect, which is how ftape picks its wake-up method): the drive just
    told us who it is, say which profile was written for it. Profiles that
    fail to load are skipped, not fatal -- this is informational.
    """
    names: list[str] = []
    for path in sorted(PROFILES_DIR.glob("*.toml")):
        try:
            ids = _resolve_raw(path.stem, seen=()).get("vendor_ids", [])
        except ProfileError as exc:
            log.debug("profiles_for_vendor: skipping %s (%s)", path.name, exc)
            continue
        if isinstance(ids, list) and vendor_id in ids:
            names.append(path.stem)
    return names
