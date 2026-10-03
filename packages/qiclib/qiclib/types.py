"""Types of the QIC layout layer: sectors, segments, and the files in a backup.

``RawSector`` is the hand-off between the physical layer and this one: the
MFM decoder (tapewyrm-cli) produces them, everything in qiclib consumes them.
Nothing here imports anything outside qiclib's own dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class SegmentStatus(Enum):
    """Outcome of RS erasure decode for one segment (DESIGN.md §6A.9)."""

    CLEAN = "clean"  # every sector CRC-good, no correction needed
    CORRECTED = "corrected"  # 1..3 sectors rebuilt by RS
    UNCORRECTABLE = "uncorrectable"  # > 3 erasures, partial data kept
    MISSING = "missing"  # segment never captured


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


@dataclass
class FileEntry:
    path: str  # full path within the file set
    size: int
    attrs: int
    mtime: int | None  # epoch seconds, or None if undefined
    data: bytes = b""
    is_dir: bool = False
    unreadable_at_backup: bool = False
    # Where ``data`` starts in the volume stream it came from (None = not known),
    # so a caller with the volume's hole map can count a file's missing bytes.
    offset: int | None = None
    # Listed in the directory, but its data entry was never found (it fell in
    # unrecovered tape): ``data`` is empty and ``size`` is what it should be.
    lost: bool = False


@dataclass
class FileSet:
    name: str  # source device / volume description (e.g. "C:")
    files: list[FileEntry] = field(default_factory=list)
    compressed: bool = False
    extended_os: bool = False
