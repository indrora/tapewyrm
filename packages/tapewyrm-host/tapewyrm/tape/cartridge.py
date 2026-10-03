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
  (307.8 ft and 424.7 ft), so we use it for every format code.

The drive has an opinion too: Report Tape Status (QIC-117 cmd 33) bits 4-6
name a tape type (``qic117.status.TAPE_TYPES``). TWRF captures record it, and
:mod:`tapewyrm.image.identify` prints both so they can be compared.

Cartridge names (DC2080, DC2120) are the ones the standards' covers use.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

# QIC-80-MC Rev N §5.4.1 constants (inches): long-term speed variation, max
# beginning gap, erase gap, and the length of one floppy track's 4 segments.
_SPEED_VARIATION = 0.03
_BEGINNING_GAP_MAX = 1.36
_ERASE_GAP = 0.68
_FLOPPY_TRACK_SEGMENTS = 23.88

# How far (in feet) an estimated length may sit from a catalogued one and still
# be called that cartridge. Variable formats are allowed to exceed the minimum
# segment count a little (a long tape formats to more segments), so lengths
# come out slightly over nominal. Within one width the closest catalogued
# lengths are 307.5 and 425 ft (38% apart), so 10% cannot confuse neighbours.
_LENGTH_TOLERANCE = 0.10


@dataclass(frozen=True)
class Cartridge:
    """One catalogue entry: a standard on a tape of a given width and length."""

    standard: str  # "QIC-40", "QIC-80"
    tracks: int
    width_in: float
    length_ft: float
    capacity_mb: int  # uncompressed, as printed on the standard's cover
    name: str = ""  # cartridge model, when the standard names one
    coercivity: str = ""  # "550 Oe" where the standard says so
    segments_per_track: int | None = None  # exact count, where the standard fixes one


CATALOGUE: tuple[Cartridge, ...] = (
    # QIC-40-MC Rev M: cover (40 MB, DC2000) and §5.3 (sectors per track / 32).
    Cartridge("QIC-40", 20, 0.250, 205, 40, "DC2000", segments_per_track=68),
    # The cover only states 40 MB (205 ft); the longer tapes' capacities are not
    # in our copy of the spec, so they stay 0 (unknown) rather than guessed.
    Cartridge("QIC-40", 20, 0.250, 307.5, 0, segments_per_track=102),
    Cartridge("QIC-40", 20, 0.250, 1100, 0, segments_per_track=365),
    # QIC-80-MC Rev N cover.
    Cartridge("QIC-80", 28, 0.250, 205, 80, "DC2080"),
    Cartridge("QIC-80", 28, 0.250, 307.5, 120, "DC2120"),
    Cartridge("QIC-80", 28, 0.250, 425, 172, coercivity="550 Oe"),
    Cartridge("QIC-80", 28, 0.250, 1000, 400, coercivity="550 Oe"),
    Cartridge("QIC-80", 36, 0.315, 400, 200, coercivity="550 Oe"),
    Cartridge("QIC-80", 36, 0.315, 750, 400, coercivity="550 Oe"),
    Cartridge("QIC-80", 36, 0.315, 1000, 500, coercivity="550 Oe"),
)


@dataclass(frozen=True)
class CartridgeGuess:
    cartridge: Cartridge | None  # None: nothing in the catalogue is close enough
    estimated_ft: float | None  # length implied by segments per track (QIC-80 only)
    exact: bool  # the segment count is exactly what the standard prescribes
    basis: str  # one line on how we got here, for the report

    def describe(self) -> str:
        c = self.cartridge
        if c is None:
            return f"unknown ({self.basis})"
        parts = [f"{c.standard}, {c.length_ft:g} ft x {c.width_in:.3f} in"]
        if c.name:
            parts.append(f"({c.name} class)")
        if c.coercivity:
            parts.append(c.coercivity)
        text = " ".join(parts)
        if c.capacity_mb:
            text += f", {c.capacity_mb} MB native"
        return f"{text}; {self.basis}"


def min_segments_per_track(length_ft: float) -> int:
    """QIC-80-MC Rev N §5.4.1: the minimum segments per track for a tape length."""
    length_in = length_ft * 12
    usable = length_in * (1 - _SPEED_VARIATION) - _BEGINNING_GAP_MAX + _ERASE_GAP
    return int(usable / _FLOPPY_TRACK_SEGMENTS)


def length_from_segments(segments_per_track: int) -> float:
    """Inverse of :func:`min_segments_per_track`: the tape length (ft) it implies."""
    usable = segments_per_track * _FLOPPY_TRACK_SEGMENTS
    return (usable + _BEGINNING_GAP_MAX - _ERASE_GAP) / (1 - _SPEED_VARIATION) / 12


def guess(tracks: int, segments_per_track: int) -> CartridgeGuess:
    """Name the cartridge from the header's tracks and segments per track."""
    candidates = [c for c in CATALOGUE if c.tracks == tracks]
    if not candidates:
        log.debug("no catalogue entry has %d tracks; guessing unknown", tracks)
        return CartridgeGuess(None, None, False, f"no standard here uses {tracks} tracks")

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

    est = length_from_segments(segments_per_track)
    best = min(candidates, key=lambda c: abs(c.length_ft - est))
    if abs(best.length_ft - est) > best.length_ft * _LENGTH_TOLERANCE:
        log.debug(
            "estimated %.1f ft is > %.0f%% from nearest %g ft; guessing unknown",
            est,
            _LENGTH_TOLERANCE * 100,
            best.length_ft,
        )
        return CartridgeGuess(
            None,
            est,
            False,
            f"{segments_per_track} segments/track ~ {est:.0f} ft, not a known length",
        )
    exact = min_segments_per_track(best.length_ft) == segments_per_track
    basis = f"{segments_per_track} segments/track ~ {est:.1f} ft"
    if exact:
        basis += " (exactly the Rev N minimum for that length)"
    return CartridgeGuess(best, est, exact, basis)
