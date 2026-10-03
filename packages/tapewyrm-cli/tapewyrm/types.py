"""Shared dataclasses and enums that cross module boundaries.

This is the contract layer (DESIGN.md §6A.11, §13.4). The on-disk capture
types (``Direction``, ``TapeFormat``, ``MarkerKind``, ``CaptureHeader``,
``Marker``) live in :mod:`tapewyrm_archive.types` with the TWRF format; the QIC-117 report
decoders in :mod:`tapewyrm_archive.qic117`; the sector/segment/file types in
:mod:`qiclib.types`. Nothing here imports from
the rest of the package, so every layer can depend on it without cycles. The
bit-level report decoders (``DriveStatus.decode`` etc.) are grounded in the
QIC-117 report payload tables (DESIGN.md §13.1).
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The recovery report (pipeline) is built from qiclib segment statuses and file sets.
from qiclib.types import FileSet, SegmentStatus

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


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
    port: str = ""  # serial device path, e.g. /dev/cu.usbmodem121401
    sample_clock_hz: int = 0  # flux sample clock, from the INFO verb (0 = unknown)


@dataclass(frozen=True)
class TimingParams:
    """QIC-117 pulse/report cadence (DESIGN.md §2.1, §5.3, §13.1).

    Defaults are the Rev J nominals; per-drive overrides come from DriveProfile.
    """

    pulse_us: int = 200
    inter_pulse_us: int = 2000  # ~2.0 ms STEP interval (0.9-2.1 ms)
    terminate_gap_us: int = 3000  # > 2.9 ms ends a command train (< -> Soft Reset)
    report_settle_us: int = 900  # report bit appears within 900 us of 2nd pulse (fw TBIT)
    motion_timeout_s: int = 20  # generic motion wait-ready ceiling (seeks 15s, stop 8s)
    tack_us: int = 2500  # max wait for a report's ACK bit (QIC-117 TACK)
    report_on_index: bool = False  # fw waits for an INDEX cue per bit instead of a fixed settle


@dataclass(frozen=True)
class SelectHint:
    """Drive-select strategy hint passed to the device (DESIGN.md §5.2)."""

    unit: int = 0
    sticky: bool = False  # keep select asserted across back-to-back command txns
    bus: str = "shugart"  # GW bus type: "shugart" (DS0-3 lines) or "ibmpc" (A/B + motor)
    motor: bool = False  # also assert the unit's motor-enable line


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


# ---------------------------------------------------------------------------
# Drive profile (DESIGN.md §6A.3) — data, not code; loaded from profiles/drive/*.toml
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DriveProfile:
    name: str
    # (command name, arg|None, post-delay ms) tuples run on wake.
    wake_sequence: tuple[tuple[str, int | None, int], ...]
    timing: TimingParams
    bit_order: str = "lsb"  # "msb" | "lsb"
    report_strategy: str = "fixed_settle"  # "fixed_settle" (bench-proven) | "index_edge"
    quirks: frozenset[str] = frozenset()

    @classmethod
    def default(cls) -> DriveProfile:
        return cls(name="default", wake_sequence=(), timing=TimingParams())


# ---------------------------------------------------------------------------
# Capture container metadata (DESIGN.md §7.1)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Codec data types (DESIGN.md §6A.11, §13.5)
# ---------------------------------------------------------------------------


@dataclass
class FluxStream:
    """Decoded inter-transition intervals in sample-clock ticks."""

    intervals: list[int]
    sample_clock_hz: int


# ---------------------------------------------------------------------------
# Volume / file-set outputs (DESIGN.md §7.3, §7.5)
# ---------------------------------------------------------------------------


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
