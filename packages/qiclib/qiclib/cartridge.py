"""What kind of cartridge is this? A guess from the header's geometry.

The header segment records tracks per cartridge and segments per track, and
both are fixed by the recording standard and the tape's length, so together
they name the cartridge. The tables below come straight from the standards in
docs/qic-standards/:

* **QIC-40-MC Rev M** (qic40m.pdf, cover + §5.3): 20 tracks on 0.250 in tape,
  exactly 68 / 102 / 365 segments per track for 205 / 307.5 / 1,100 ft.
* **QIC-80-MC Rev N** (qic80n.pdf, cover + §5.3, §5.4.1): 28 tracks on
  0.250 in tape, 36 on 0.315 in. Format code 4 ("variable length") has no
  fixed count; §5.4.1 gives the *minimum*:

      min segments = int((length_in * (1 - 0.03) - 1.36 + 0.68) / 23.88)

  (207 for a 425 ft tape). Inverting it turns a segment count back into a
  length. Formats 2, 3 and 5 are Rev K "fixed" formats, whose counts we have no
  spec for; the bench tapes give 150 (3M DC2120, code 2) and 207 (Colorado,
  code 5), and the inverse equation lands on the right lengths for both
  (307.8 ft and 424.7 ft), so we use it for every QIC-80 format code.
* **QIC-3010-MC Rev H** (qic3010h.pdf, cover + §5.4.1) and **QIC-3020-MC
  Rev H** (qic3020h.pdf, cover + §5.4.1): both 40 tracks on 0.250 in tape and
  50 on 0.315 in, both variable length only, both 900 Oe. They differ in
  density (22,125 vs 44,250 bpi), so a segment takes about half as much tape on
  QIC-3020 and the same tape formats to about twice the segments. Their §5.4.1
  minimums keep QIC-80's shape with their own erase gap and segment length:

      QIC-3010: int((length_in * 0.97 - 1.36 + 0.452) / 15.936)   (219 for 300 ft)
      QIC-3020: int((length_in * 0.97 - 1.36 + 0.226) / 8.131)    (429 for 300 ft)

Because QIC-3010 and QIC-3020 share track counts, geometry alone names a
cartridge only when just one of them puts the segment count near a catalogued
length. :func:`guess` therefore takes an optional ``standard`` hint (the
drive's Report Tape Status format bits, or format code 6, which only
QIC-3020 defines); with no hint and two standards that both fit, it says so and
lists both rather than picking one.

The drive has an opinion too: Report Tape Status (QIC-117 cmd 33) bits 4-6
name a tape type (``tapewyrm_archive.qic117.TAPE_TYPES``). TWRF captures record it, and
:mod:`qiclib.identify` prints both so they can be compared.

The catalogue is data, not code: one TOML **cartridge profile** per tape in
``qiclib/profiles/cartridge/`` (one file per entry, so each tape can carry its
own provenance comment and be named on its own: ``qic-extra``, ``qic80-dc2120``).
A profile describes the physical cartridge -- standard, tracks, width, length,
cover capacity, coercivity, model, vendor, label. It is not a *volume profile*
(:mod:`qiclib.volume_profile`), which describes a backup program's volume-table
layout. The §5.4.1 equations stay here in code, keyed by standard: they are
spec arithmetic that every cartridge of a standard shares.

Models (DC2080, DC2120) are the ones the standards' covers use. The QIC-3020
1,000 ft x 0.250 in profile is ``qic-extra``, the Verbatim MC3020EX on the
bench (see that file).
"""

from __future__ import annotations

import functools
import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PROFILES_DIR = Path(__file__).resolve().parent / "profiles" / "cartridge"

# §5.4.1 constants shared by QIC-80-MC Rev N, QIC-3010-MC and QIC-3020-MC
# (inches): long-term speed variation and maximum beginning gap.
_SPEED_VARIATION = 0.03
_BEGINNING_GAP_MAX = 1.36

# How far (in feet) an estimated length may sit from a catalogued one and still
# be called that cartridge. Variable formats are allowed to exceed the minimum
# segment count a little (a long tape formats to more segments), so lengths
# come out slightly over nominal (the MC3020EX: ~1,030 ft for 1,000). Within one
# standard and width the closest catalogued lengths are 307.5 and 425 ft (38%
# apart) or 750 and 1,000 ft (33%), so 10% cannot confuse neighbours.
_LENGTH_TOLERANCE = 0.10


@dataclass(frozen=True)
class VariableFormat:
    """One standard's §5.4.1 minimum-segments equation (lengths in inches)."""

    spec: str  # the document the numbers come from, for report text
    erase_gap_in: float  # the formula's "+ x" term
    segment_in: float  # tape one segment takes: the formula's divisor


# Per standard: the variable-format equation, where the standard has one.
# QIC-40 fixes its counts outright (a profile's segments_per_track) instead.
VARIABLE_FORMATS: dict[str, VariableFormat] = {
    "QIC-80": VariableFormat("QIC-80-MC Rev N", 0.68, 23.88),
    "QIC-3010": VariableFormat("QIC-3010-MC Rev H", 0.452, 15.936),
    "QIC-3020": VariableFormat("QIC-3020-MC Rev H", 0.226, 8.131),
}

# The document each standard name refers to, for report lines that cite one.
SPECS: dict[str, str] = {"QIC-40": "QIC-40-MC Rev M"} | {
    std: fmt.spec for std, fmt in VARIABLE_FORMATS.items()
}


class CartridgeProfileError(Exception):
    """A cartridge profile could not be loaded or is malformed."""


@dataclass(frozen=True)
class Cartridge:
    """One cartridge profile: a standard on a tape of a given width and length."""

    profile: str  # profile name, "qic-extra"
    standard: str  # "QIC-40", "QIC-80", "QIC-3010", "QIC-3020"
    tracks: int
    width_in: float
    length_ft: float
    capacity_mb: int  # uncompressed, as printed on the standard's cover (1.7 GB = 1700)
    model: str = ""  # cartridge model: the standard's, or a bench tape's for its class
    coercivity: str = ""  # "550 Oe" where the standard says so
    segments_per_track: int | None = None  # exact count, where the standard fixes one
    vendor: str = ""  # "Verbatim"
    label: str = ""  # marketing name on the box, "QIC-Extra"
    label_capacity: str = ""  # capacity as the label prints it, "1.6 GB"
    description: str = ""
    path: str = ""  # the file it was loaded from


# ---------------------------------------------------------------------------
# Loading (same rules as volume_profile: a bare name is a packaged profile,
# anything path-like is a path)
# ---------------------------------------------------------------------------

# Profile keys and the types they must have. bool is excluded from the
# numbers by hand below (TOML true is a Python int).
_REQUIRED: dict[str, tuple[type, ...]] = {
    "name": (str,),
    "standard": (str,),
    "tracks": (int,),
    "width_in": (int, float),
    "length_ft": (int, float),
}
_OPTIONAL: dict[str, tuple[type, ...]] = {
    "capacity_mb": (int,),
    "coercivity": (str,),
    "model": (str,),
    "segments_per_track": (int,),
    "vendor": (str,),
    "label": (str,),
    "label_capacity": (str,),
    "description": (str,),
}


def _resolve_path(name_or_path: str) -> Path:
    """A bare name resolves in the packaged directory; anything path-like is a path."""
    p = Path(name_or_path)
    if p.suffix == ".toml" or p.exists() or p.is_absolute() or len(p.parts) > 1:
        log.debug("cartridge profile %r is path-like; using it as a path", name_or_path)
        return p
    log.debug("cartridge profile %r is a bare name; looking in %s", name_or_path, PROFILES_DIR)
    return PROFILES_DIR / f"{name_or_path}.toml"


def load(name_or_path: str) -> Cartridge:
    """Load one cartridge profile by packaged name or by path."""
    path = _resolve_path(name_or_path)
    if not path.is_file():
        log.debug("cartridge profile %s is not a file; raising", path)
        known = ", ".join(builtin_names())
        raise CartridgeProfileError(f"no cartridge profile {name_or_path!r} (built in: {known})")
    log.debug("reading cartridge profile %s", path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        log.debug("cartridge profile %s: TOML error %s; raising", path, exc)
        raise CartridgeProfileError(f"{path}: {exc}") from exc
    return _from_dict(data, path)


def _from_dict(data: dict[str, Any], path: Path) -> Cartridge:
    """Validate one profile's keys and build its :class:`Cartridge`."""
    unknown = sorted(set(data) - set(_REQUIRED) - set(_OPTIONAL))
    if unknown:
        log.debug("cartridge profile %s: unknown keys %s; raising", path, unknown)
        raise CartridgeProfileError(f"{path}: unknown key(s) {', '.join(unknown)}")
    for key, types in (_REQUIRED | _OPTIONAL).items():
        if key not in data:
            if key in _REQUIRED:
                log.debug("cartridge profile %s: missing %r; raising", path, key)
                raise CartridgeProfileError(f"{path}: missing {key!r}")
            continue
        value = data[key]
        if isinstance(value, bool) or not isinstance(value, types):
            log.debug("cartridge profile %s: %s = %r has the wrong type; raising", path, key, value)
            wanted = " or ".join(t.__name__ for t in types)
            raise CartridgeProfileError(f"{path}: {key} = {value!r} must be {wanted}")
    standard = data["standard"]
    if standard not in SPECS:
        log.debug("cartridge profile %s: standard %r unknown; raising", path, standard)
        raise CartridgeProfileError(
            f"{path}: standard {standard!r} is not one of {', '.join(SPECS)}"
        )
    spt = data.get("segments_per_track")
    if spt is None and standard not in VARIABLE_FORMATS:
        # Without an equation or a fixed count, guess() could never match it.
        log.debug("cartridge profile %s: %s needs segments_per_track; raising", path, standard)
        raise CartridgeProfileError(
            f"{path}: {standard} has no variable-format equation; give segments_per_track"
        )
    return Cartridge(
        profile=data["name"],
        standard=standard,
        tracks=data["tracks"],
        width_in=float(data["width_in"]),
        length_ft=float(data["length_ft"]),
        capacity_mb=data.get("capacity_mb", 0),
        model=data.get("model", ""),
        coercivity=data.get("coercivity", ""),
        segments_per_track=spt,
        vendor=data.get("vendor", ""),
        label=data.get("label", ""),
        label_capacity=data.get("label_capacity", ""),
        description=data.get("description", ""),
        path=str(path),
    )


def builtin_names() -> list[str]:
    if not PROFILES_DIR.is_dir():
        log.debug("profile directory %s missing; no built-in cartridge profiles", PROFILES_DIR)
        return []
    return sorted(p.stem for p in PROFILES_DIR.iterdir() if p.suffix == ".toml")


@functools.cache
def catalogue() -> tuple[Cartridge, ...]:
    """Every packaged cartridge profile, loaded once.

    Sorted by standard (in :data:`SPECS` order), tracks, then length, not by
    file name: guess() lists alternatives in this order, so it must not depend
    on how the files happen to be named.
    """
    log.debug("loading cartridge profiles from %s", PROFILES_DIR)
    order = list(SPECS)
    profiles = [load(name) for name in builtin_names()]
    return tuple(sorted(profiles, key=lambda c: (order.index(c.standard), c.tracks, c.length_ft)))


def _capacity(capacity_mb: int) -> str:
    """Cover capacities as the covers print them: "680 MB", "1.7 GB" (decimal)."""
    if capacity_mb >= 1000:
        return f"{capacity_mb / 1000:g} GB"
    return f"{capacity_mb} MB"


def _cartridge_text(c: Cartridge) -> str:
    """Like ``QIC-3020, 1,000 ft x 0.250 in (MC3020EX class, Verbatim QIC-Extra) 900 Oe``."""
    parts = [f"{c.standard}, {c.length_ft:,g} ft x {c.width_in:.3f} in"]
    names = [f"{c.model} class"] if c.model else []
    branded = " ".join(part for part in (c.vendor, c.label) if part)
    if branded:
        names.append(branded)
    if names:
        parts.append(f"({', '.join(names)})")
    if c.coercivity:
        parts.append(c.coercivity)
    return " ".join(parts)


@dataclass(frozen=True)
class CartridgeGuess:
    cartridge: Cartridge | None  # None: nothing (or more than one thing) fits
    estimated_ft: float | None  # length implied by segments per track (variable formats)
    exact: bool  # the segment count is exactly what the standard prescribes
    basis: str  # one line on how we got here, for the report
    # When geometry fits more than one standard and nothing said which: every
    # cartridge that fits. ``cartridge`` is then None.
    alternatives: tuple[Cartridge, ...] = ()

    @property
    def standard(self) -> str | None:
        """The recording standard, when the guess settled on one."""
        return self.cartridge.standard if self.cartridge else None

    def describe(self) -> str:
        c = self.cartridge
        if c is None:
            if self.alternatives:
                either = " or ".join(_cartridge_text(alt) for alt in self.alternatives)
                return f"{either}; {self.basis}"
            return f"unknown ({self.basis})"
        text = _cartridge_text(c)
        if c.capacity_mb:
            text += f", {_capacity(c.capacity_mb)} native"
        return f"{text}; {self.basis}"


def min_segments_per_track(length_ft: float, standard: str = "QIC-80") -> int:
    """§5.4.1 of ``standard``: the minimum segments per track for a tape length.

    Raises ``KeyError`` for a standard with no variable format (QIC-40).
    """
    fmt = VARIABLE_FORMATS[standard]
    length_in = length_ft * 12
    usable = length_in * (1 - _SPEED_VARIATION) - _BEGINNING_GAP_MAX + fmt.erase_gap_in
    # The standards truncate with int(). A length from length_from_segments
    # sits exactly on a segment boundary, where float error can land the
    # quotient at 428.99999999999994 and truncate a whole segment away; a
    # nanosegment of slack makes the inverse round-trip without moving any real
    # tape length across a boundary.
    return int(usable / fmt.segment_in + 1e-9)


def length_from_segments(segments_per_track: int, standard: str = "QIC-80") -> float:
    """Inverse of :func:`min_segments_per_track`: the tape length (ft) it implies."""
    fmt = VARIABLE_FORMATS[standard]
    usable = segments_per_track * fmt.segment_in
    return (usable + _BEGINNING_GAP_MAX - fmt.erase_gap_in) / (1 - _SPEED_VARIATION) / 12


def guess(
    tracks: int,
    segments_per_track: int,
    standard: str | None = None,
    standard_from: str = "the hint",
) -> CartridgeGuess:
    """Name the cartridge from the header's tracks and segments per track.

    ``standard`` ("QIC-3020", ...) is a hint from outside the geometry, and
    ``standard_from`` says where it came from, for the report ("the drive's
    tape status"). It only narrows the choice: a hint that names no standard
    using this many tracks (a QIC-80 drive report on a 40-track header) is
    ignored, since the header's track count is the harder fact.
    """
    candidates = [c for c in catalogue() if c.tracks == tracks]
    if not candidates:
        log.debug("no catalogue entry has %d tracks; guessing unknown", tracks)
        return CartridgeGuess(None, None, False, f"no standard here uses {tracks} tracks")

    hinted = False
    if standard is not None:
        narrowed = [c for c in candidates if c.standard == standard]
        if narrowed:
            log.debug("%s from %s: %d candidates", standard, standard_from, len(narrowed))
            candidates, hinted = narrowed, True
        else:
            log.debug(
                "%s from %s uses no %d-track cartridge; ignoring the hint",
                standard,
                standard_from,
                tracks,
            )

    # Standards that fix the count (QIC-40): exact match or nothing.
    for c in candidates:
        if c.segments_per_track == segments_per_track:
            log.debug(
                "%d segments/track exactly matches %s %g ft",
                segments_per_track,
                c.standard,
                c.length_ft,
            )
            return CartridgeGuess(
                c, None, True, f"{segments_per_track} segments/track is exactly {c.standard}'s"
            )
    if all(c.segments_per_track is not None for c in candidates):
        counts = ", ".join(str(c.segments_per_track) for c in candidates)
        log.debug(
            "%d tracks: all standards fix spt (%s), none is %d; guessing unknown",
            tracks,
            counts,
            segments_per_track,
        )
        return CartridgeGuess(
            None, None, False, f"{segments_per_track} segments/track; expected one of {counts}"
        )

    # Variable formats: each standard turns the count into a length with its
    # own §5.4.1, and keeps its nearest cartridge if that is close enough.
    fits: list[tuple[Cartridge, float]] = []
    misses: list[str] = []
    standards = dict.fromkeys(c.standard for c in candidates if c.standard in VARIABLE_FORMATS)
    for std in standards:
        est = length_from_segments(segments_per_track, std)
        best = min(
            (c for c in candidates if c.standard == std), key=lambda c: abs(c.length_ft - est)
        )
        if abs(best.length_ft - est) > best.length_ft * _LENGTH_TOLERANCE:
            log.debug(
                "%s: estimated %.1f ft is > %.0f%% from nearest %g ft; no fit",
                std,
                est,
                _LENGTH_TOLERANCE * 100,
                best.length_ft,
            )
            misses.append(f"{std} ~{est:,.0f} ft")
            continue
        log.debug("%s: estimated %.1f ft fits %g ft", std, est, best.length_ft)
        fits.append((best, est))

    if not fits:
        log.debug(
            "%d segments/track fits no catalogued length; guessing unknown", segments_per_track
        )
        if len(misses) == 1:
            # One standard in play: keep the original wording and estimate.
            est = length_from_segments(segments_per_track, next(iter(standards)))
            return CartridgeGuess(
                None,
                est,
                False,
                f"{segments_per_track} segments/track ~ {est:,.0f} ft, not a known length",
            )
        return CartridgeGuess(
            None,
            None,
            False,
            f"{segments_per_track} segments/track is not a known length ({', '.join(misses)})",
        )
    if len(fits) > 1:
        # Only reachable without a usable hint: two standards share the track
        # count and both land near a catalogued length. Don't pick; list both.
        log.debug(
            "%d x %d fits %s; no hint to choose, listing all",
            tracks,
            segments_per_track,
            ", ".join(c.standard for c, _ in fits),
        )
        alternatives = tuple(c for c, _ in fits)
        estimates = ", ".join(f"{c.standard} ~{est:,.0f} ft" for c, est in fits)
        return CartridgeGuess(
            None,
            None,
            False,
            f"{segments_per_track} segments/track fits more than one standard "
            f"({estimates}); the drive's tape status would settle it",
            alternatives,
        )

    best, est = fits[0]
    fmt = VARIABLE_FORMATS[best.standard]
    exact = min_segments_per_track(best.length_ft, best.standard) == segments_per_track
    basis = f"{segments_per_track} segments/track ~ {est:,.1f} ft"
    if exact:
        basis += f" (exactly the {fmt.spec} minimum for that length)"
    if hinted:
        basis += f"; {best.standard} per {standard_from}"
    elif len(standards) > 1:
        log.debug("%s is the only standard whose length fits", best.standard)
        basis += f"; only {best.standard} fits {tracks} tracks at that count"
    return CartridgeGuess(best, est, exact, basis)
