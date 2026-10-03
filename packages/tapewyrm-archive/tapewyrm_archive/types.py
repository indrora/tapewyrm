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
        """The standard's intended transfer rate, in kbit/s (each spec's §3.4).

        QIC-40-MC Rev M: 250 kb/s (25 ips; 500 kb/s at 50 ips is also given).
        QIC-80-MC Rev N: 500 kb/s at 34 ips. QIC-3010-MC Rev H: 500 kb/s and
        QIC-3020-MC Rev H: 1 Mb/s, both at 22.6 ips. Every one adds "other
        speeds and compatible transfer rates are possible", and drives use
        that: this used to say 1000 / 2000 for 3010 / 3020, which are faster
        drive modes, not the standards' rates. The bench QIC-Extra tape (3020)
        was read at 1000 kb/s. For a capture, trust the drive's configuration
        report (TWRF ``rate_kbps``), not this.
        """
        return {
            TapeFormat.QIC40: 250,
            TapeFormat.QIC80: 500,
            TapeFormat.QIC3010: 500,
            TapeFormat.QIC3020: 1000,
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
    """The linearization key stamped on every TWRF capture (DESIGN.md §7.1, TWS-1 §4)."""

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
    # Salvage pass taken in Physical Reverse: the flux is in time-reversed
    # order. Recorded only; `tw convert` does not yet reverse it (TWS-1 8.2).
    physical_reverse: bool = False
    # --- TWRF v2: who read it, and what the drive said (all optional) ---------
    # Raw QIC-117 report bytes at capture time, so a capture is self-describing
    # and the decoder never has to assume the bit rate. None = the drive did not
    # answer that report. The reader requires every member (TWS-1 section 8.2),
    # so None never means "absent from the file".
    drive_status: int | None = None  # Report Drive Status (6)
    drive_config: int | None = None  # Report Drive Configuration (8): rate bits 3-4
    drive_rom: int | None = None  # Report ROM Version (9)
    drive_vendor_id: int | None = None  # Report Vendor ID (32)
    tape_status: int | None = None  # Report Tape Status (33)
    tw_commit: str | None = None  # tw build (tapewyrm-cli's tapewyrm.buildinfo)
    firmware_commit: str | None = None  # device build (BUILD_INFO verb)
    firmware_dirty: bool | None = None


@dataclass(frozen=True)
class Marker:
    """A parsed in-stream tape marker (DESIGN.md §7.2)."""

    kind: MarkerKind
    fields: dict[str, int | str] = field(default_factory=dict)
    offset: int = 0  # byte offset within the flux run where the marker sat
