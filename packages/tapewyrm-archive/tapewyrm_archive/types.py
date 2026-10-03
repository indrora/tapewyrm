"""Types the on-disk formats are made of (TWRF capture headers and markers).

These moved here from ``tapewyrm.types`` when the container formats became
their own package: a TWRF file stores a ``CaptureHeader`` and ``Marker``s, so
whoever reads the file needs the types without needing the hardware stack.
Nothing here imports anything else from the repo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum


class Direction(Enum):
    """Serpentine direction of a tape track (DESIGN.md §2.2, §7.3).

    Even tracks are recorded forward, odd tracks reverse. ``logical`` order is
    what Logical-Forward presents regardless of physical direction.
    """

    FORWARD = "forward"
    REVERSE = "reverse"

    @classmethod
    def for_track(cls, track: int) -> Direction:
        return cls.FORWARD if track % 2 == 0 else cls.REVERSE


class TapeFormat(IntEnum):
    """Recording format, as reported by Report Tape Status (cmd 33) bits 0-3."""

    UNKNOWN = 0
    QIC40 = 1
    QIC80 = 2
    QIC3020 = 3
    QIC3010 = 4

    @property
    def rate_kbps(self) -> int:
        """Nominal bitcell rate for this format (DESIGN.md §2.2, §7.3)."""
        return {
            TapeFormat.QIC40: 250,
            TapeFormat.QIC80: 500,
            TapeFormat.QIC3010: 1000,
            TapeFormat.QIC3020: 2000,
            TapeFormat.UNKNOWN: 500,
        }[self]


class MarkerKind(IntEnum):
    """In-flux-stream tape markers (DESIGN.md §7.2).

    These mirror the codes in :class:`tapewyrm_archive.twrf.WireMarker`; the dataclass
    ``Marker`` below carries one of these plus its decoded payload.
    """

    SESSION_START = 0
    SEGMENT = 1
    EVENT = 2
    END = 3
    HEARTBEAT = 4  # demoted / optional keepalive (DESIGN.md §7.2)


@dataclass(frozen=True)
class CaptureHeader:
    """The linearization key stamped on every RawFluxCapture (DESIGN.md §7.1)."""

    rate_kbps: int
    sample_clock_hz: int
    track: int  # TPT — the tape-track this run captured
    direction: Direction
    pass_id: int
    utc: str  # ISO-8601 capture start (passed in; never derived in-codec)
    tape_format: TapeFormat = TapeFormat.QIC80
    segments_per_track: int = 0
    tracks: int = 0
    sectors_per_segment: int = 32
    device_serial: str = ""
    physical_reverse: bool = False  # salvage pass; flux is time-reversed offline
    # --- TWRF v2: who read it, and what the drive said (all optional) ---------
    # Raw QIC-117 report bytes at capture time, so a capture is self-describing
    # and the decoder never has to assume the bit rate. None = not reported
    # (drive too old for that report, or a v1 file).
    drive_status: int | None = None  # Report Drive Status (6)
    drive_config: int | None = None  # Report Drive Configuration (8): rate bits 3-4
    drive_rom: int | None = None  # Report ROM Version (9)
    drive_vendor_id: int | None = None  # Report Vendor ID (32)
    tape_status: int | None = None  # Report Tape Status (33)
    tw_commit: str | None = None  # host build (tapewyrm.buildinfo)
    firmware_commit: str | None = None  # device build (BUILD_INFO verb)
    firmware_dirty: bool | None = None


@dataclass(frozen=True)
class Marker:
    """A parsed in-stream tape marker (DESIGN.md §7.2)."""

    kind: MarkerKind
    fields: dict[str, int | str] = field(default_factory=dict)
    offset: int = 0  # byte offset within the flux run where the marker sat
