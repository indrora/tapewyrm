"""Shared dataclasses and enums that cross module boundaries.

This is the contract layer (DESIGN.md §6A.11, §13.4). Nothing here imports from
the rest of the package, so every layer can depend on it without cycles. The
bit-level report decoders (``DriveStatus.decode`` etc.) are grounded in the
QIC-117 report payload tables (DESIGN.md §13.1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


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

    These mirror the codes in the generated ``link/protocol.py``; the dataclass
    ``Marker`` below carries one of these plus its decoded payload.
    """

    SESSION_START = 0
    SEGMENT = 1
    EVENT = 2
    END = 3
    HEARTBEAT = 4  # demoted / optional keepalive (DESIGN.md §7.2)


class SegmentStatus(Enum):
    """Outcome of RS erasure decode for one segment (DESIGN.md §6A.9)."""

    CLEAN = "clean"  # every sector CRC-good, no correction needed
    CORRECTED = "corrected"  # 1..3 sectors rebuilt by RS
    UNCORRECTABLE = "uncorrectable"  # > 3 erasures, partial data kept
    MISSING = "missing"  # segment never captured


# ---------------------------------------------------------------------------
# Device / link layer (DESIGN.md §6A.2, §13.3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeviceInfo:
    model: str
    mcu: str
    firmware: str
    serial: str
    usb_high_speed: bool
    sram_bytes: int
    qic_caps: frozenset[str]  # e.g. {"verbs", "capture", "markers"}
    proto_ver: int = 0


@dataclass(frozen=True)
class TimingParams:
    """QIC-117 pulse/report cadence (DESIGN.md §2.1, §5.3, §13.1).

    Defaults are the Rev J nominals; per-drive overrides come from DriveProfile.
    """

    pulse_us: int = 200
    inter_pulse_us: int = 2000  # ~2.0 ms STEP interval (0.9-2.1 ms)
    terminate_gap_us: int = 3000  # > 2.9 ms ends a command train (< -> Soft Reset)
    report_settle_us: int = 900  # report bit appears within 900 us of 2nd pulse
    motion_timeout_s: int = 20  # generic motion wait-ready ceiling (seeks 15s, stop 8s)


@dataclass(frozen=True)
class SelectHint:
    """Drive-select strategy hint passed to the device (DESIGN.md §5.2)."""

    unit: int = 0
    sticky: bool = False  # keep select asserted across back-to-back command txns


@dataclass(frozen=True)
class StopCond:
    """Capture stop condition (DESIGN.md §6A.2, §13.3)."""

    byte_budget: int | None = None  # stop after this many GW flux bytes
    max_duration_s: int | None = None  # watchdog ceiling
    stop_on_eot: bool = True


# ---------------------------------------------------------------------------
# QIC-117 report decoders (DESIGN.md §13.1 report payloads, LSB-first)
# ---------------------------------------------------------------------------


def _bit(value: int, n: int) -> bool:
    return bool((value >> n) & 1)


@dataclass(frozen=True)
class DriveStatus:
    """Report Drive Status (cmd 6), 8 bits. Bits 1,5,6,7 valid only when ready."""

    ready: bool
    error: bool
    cartridge_present: bool
    write_protect: bool
    new_cartridge: bool
    referenced: bool
    at_bot: bool
    at_eot: bool
    raw: int = 0

    @classmethod
    def decode(cls, b: int) -> DriveStatus:
        return cls(
            ready=_bit(b, 0),
            error=_bit(b, 1),
            cartridge_present=_bit(b, 2),
            write_protect=_bit(b, 3),
            new_cartridge=_bit(b, 4),
            referenced=_bit(b, 5),
            at_bot=_bit(b, 6),
            at_eot=_bit(b, 7),
            raw=b,
        )


@dataclass(frozen=True)
class ErrorCode:
    """Report Error Code (cmd 7), 16 bits: 0-7 code, 8-15 associated command.

    Errors latch (read to clear). ``fatal`` is filled by qic117's error table;
    broken-tape (10) is the canonical fatal case (DESIGN.md §6A.3).
    """

    code: int
    associated_command: int
    fatal: bool = False
    raw: int = 0

    @classmethod
    def decode(cls, w: int, fatal: bool = False) -> ErrorCode:
        return cls(code=w & 0xFF, associated_command=(w >> 8) & 0xFF, fatal=fatal, raw=w)


@dataclass(frozen=True)
class DriveConfig:
    """Report Drive Configuration (cmd 8), 8 bits.

    bits 3-4 rate (00=4M/250k, 01=2M, 10=500k, 11=1M); bit6 extra-length;
    bit7 QIC-80-mode.
    """

    rate_code: int
    extra_length: bool
    qic80_mode: bool
    raw: int = 0

    # rate code -> kbps. 00 is ambiguous (4 Mbps OR 250 kbit/s by drive type);
    # we surface 250 as the conservative default and flag the ambiguity.
    _RATE_KBPS = {0b00: 250, 0b01: 2000, 0b10: 500, 0b11: 1000}

    @classmethod
    def decode(cls, b: int) -> DriveConfig:
        return cls(
            rate_code=(b >> 3) & 0b11,
            extra_length=_bit(b, 6),
            qic80_mode=_bit(b, 7),
            raw=b,
        )

    @property
    def rate_kbps(self) -> int:
        return self._RATE_KBPS[self.rate_code]

    @property
    def rate_ambiguous(self) -> bool:
        return self.rate_code == 0b00


@dataclass(frozen=True)
class TapeStatus:
    """Report Tape Status (cmd 33), 8 bits: 0-3 format, 4-6 type, 7 wide."""

    format: TapeFormat
    tape_type: int
    wide: bool
    raw: int = 0

    @classmethod
    def decode(cls, b: int) -> TapeStatus:
        fmt_bits = b & 0x0F
        fmt = (
            TapeFormat(fmt_bits)
            if fmt_bits in TapeFormat._value2member_map_
            else TapeFormat.UNKNOWN
        )
        return cls(format=fmt, tape_type=(b >> 4) & 0b111, wide=_bit(b, 7), raw=b)


# ---------------------------------------------------------------------------
# Drive profile (DESIGN.md §6A.3) — data, not code; loaded from profiles/*.toml
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DriveProfile:
    name: str
    # (command name, arg|None, post-delay ms) tuples run on wake.
    wake_sequence: tuple[tuple[str, int | None, int], ...]
    timing: TimingParams
    bit_order: str = "lsb"  # "msb" | "lsb"
    report_strategy: str = "index_edge"  # "index_edge" | "fixed_settle"
    quirks: frozenset[str] = frozenset()

    @classmethod
    def default(cls) -> DriveProfile:
        return cls(name="default", wake_sequence=(), timing=TimingParams())


# ---------------------------------------------------------------------------
# Capture container metadata (DESIGN.md §7.1)
# ---------------------------------------------------------------------------


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


@dataclass(frozen=True)
class Marker:
    """A parsed in-stream tape marker (DESIGN.md §7.2)."""

    kind: MarkerKind
    fields: dict[str, int | str] = field(default_factory=dict)
    offset: int = 0  # byte offset within the flux run where the marker sat


# ---------------------------------------------------------------------------
# Codec data types (DESIGN.md §6A.11, §13.5)
# ---------------------------------------------------------------------------


@dataclass
class FluxStream:
    """Decoded inter-transition intervals in sample-clock ticks."""

    intervals: list[int]
    sample_clock_hz: int


@dataclass
class RawSector:
    """One recovered sector: its abused C/H/R is its tape coordinate.

    On tape the ID field byte order is (FTK, FSD, FSC, 03) but we name the
    fields by their QIC meaning (DESIGN.md §2.2, §7.3).
    """

    fsd: int  # head  -> floppy side
    ftk: int  # cylinder -> floppy track
    fsc: int  # record -> floppy sector (1-based)
    data: bytes  # 1024 bytes (may be zero-filled if data field unreadable)
    id_crc_ok: bool
    data_crc_ok: bool
    deleted: bool  # data address mark was F8 (format-time bad block)

    SIZE = 1024


@dataclass
class Segment:
    """A 32-slot bin keyed by tape (track, segment-relative-to-track).

    ``sectors[i]`` is the sector with segment-relative index i, or None if not
    yet recovered. Excluded (BSM) positions are tracked separately so the RS
    decoder can repack to codeword length N = 31 - bad_blocks (DESIGN.md §2.3).
    """

    tpt: int  # tape track
    tps: int  # segment relative to track
    seg: int  # absolute logical segment
    sectors: list[RawSector | None] = field(default_factory=lambda: [None] * 32)
    excluded: set[int] = field(default_factory=set)  # BSM-excluded slot indices

    SECTORS = 32
    DATA_ROWS = 29
    PARITY_ROWS = 3


@dataclass
class SegmentResult:
    status: SegmentStatus
    corrected_count: int = 0
    erasure_count: int = 0
    data: bytes = b""  # concatenated 29 data sectors (29 * 1024) after correction


# ---------------------------------------------------------------------------
# Volume / file-set outputs (DESIGN.md §7.3, §7.5)
# ---------------------------------------------------------------------------


@dataclass
class FileEntry:
    path: str  # full path within the file set
    size: int
    attrs: int
    mtime: int | None  # epoch seconds, or None if undefined
    data: bytes = b""
    is_dir: bool = False
    unreadable_at_backup: bool = False


@dataclass
class FileSet:
    name: str  # source device / volume description (e.g. "C:")
    files: list[FileEntry] = field(default_factory=list)
    compressed: bool = False
    extended_os: bool = False


@dataclass
class LogicalVolume:
    file_sets: list[FileSet] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Recovery report (DESIGN.md §6A.9) — the user-facing quality signal
# ---------------------------------------------------------------------------


@dataclass
class RecoveryReport:
    # keyed by (tpt, tps)
    segment_status: dict[tuple[int, int], SegmentStatus] = field(default_factory=dict)
    segments_corrected: dict[tuple[int, int], int] = field(default_factory=dict)
    expected_bad: int = 0  # BSM-mapped (by design)
    unexpected_bad: int = 0  # uncorrectable beyond the BSM
    recapture: list[tuple[int, int]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def track_coverage(self) -> dict[int, float]:
        """Fraction of each track's segments that decoded clean-or-corrected."""
        per_track_total: dict[int, int] = {}
        per_track_ok: dict[int, int] = {}
        for (tpt, _tps), st in self.segment_status.items():
            per_track_total[tpt] = per_track_total.get(tpt, 0) + 1
            if st in (SegmentStatus.CLEAN, SegmentStatus.CORRECTED):
                per_track_ok[tpt] = per_track_ok.get(tpt, 0) + 1
        return {t: per_track_ok.get(t, 0) / per_track_total[t] for t in per_track_total}

    def summary(self) -> str:
        n = len(self.segment_status)
        clean = sum(1 for s in self.segment_status.values() if s is SegmentStatus.CLEAN)
        corr = sum(1 for s in self.segment_status.values() if s is SegmentStatus.CORRECTED)
        bad = sum(1 for s in self.segment_status.values() if s is SegmentStatus.UNCORRECTABLE)
        return (
            f"{n} segments: {clean} clean, {corr} corrected, {bad} uncorrectable; "
            f"expected-bad {self.expected_bad}, unexpected-bad {self.unexpected_bad}"
        )
