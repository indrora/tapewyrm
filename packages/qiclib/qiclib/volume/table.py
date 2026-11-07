"""The volume table: 128-byte ``VTBL`` records and the volumes they describe (DESIGN.md §7.3, §7.5).

The volume table is the first segment of the logical area, a run of 128-byte
records: ``VTBL`` for each file set (its segment range, description and date
in bytes 0-56, which every writer agrees on, and a writer-specific layout in
57-127), ``XTBL`` / ``UTID`` for unicode names and ``EXVT`` to chain the
table into another segment.

``vtbl_records`` walks the table and ``parse_vtbl_base`` decodes the
universal bytes 0-56. ``_parse_vtbl_entry`` is the fixed Rev N / QIC-113
reading of the rest: the reference the ``qic80-rev-n`` and ``qic113-rev-g``
volume profiles (:mod:`qiclib.volume.profile`) are tested against.
``qicsilver identify`` and ``extract`` read through the profiles instead,
because the layout of bytes 57-127 is the writer's, not the standard's.

``volume_streams`` concatenates, for each VTBL entry, the *data* sectors of
its segment range (the 3 ECC sectors dropped) in logical-segment order into
one in-memory byte stream (DESIGN.md §7.5 input). It is the simple path used
by tests and by tapewyrm-cli's ``codec.pipeline``; the real one is
``qiclib.extract``, which reads the table through a volume profile,
decompresses QIC-122 extents and writes TWVL files with their holes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from qiclib import segment as seg_mod
from qiclib.header import BadSectorMap, VolumeInfo, ascii_field
from qiclib.types import Segment

log = logging.getLogger(__name__)

# Volume table entry signatures (DESIGN.md §7.3).
SIG_VTBL = b"VTBL"
SIG_XTBL = b"XTBL"  # unicode
SIG_UTID = b"UTID"  # unicode tape name
SIG_EXVT = b"EXVT"  # overflow to another segment
VTBL_ENTRY_LEN = 128

DATA_SECTORS_PER_SEGMENT = Segment.DATA_ROWS  # 29
#: Volume bytes one segment carries: its 29 data sectors (no ECC, nothing
#: excluded). The volume profile's size checks, the extract layout and the
#: QIC-113 segment-gap arithmetic all count in these.
SEGMENT_DATA_BYTES = DATA_SECTORS_PER_SEGMENT * 1024


@dataclass
class VtblEntry:
    """One 128-byte ``VTBL`` volume-table entry, QIC-80-MC Rev N §8.

    Bytes 0-56 are always defined. When the vendor-specific bit (byte 56 bit 0)
    is set, "only this bit and the previous bytes (0-55) are defined" and the
    volume is not QIC-80-MC compliant -- so every later field is ``None``. (The
    bench tape's CMS backup is such a volume.) The raw record is kept so a
    vendor-specific decoder can read the rest.
    """

    signature: bytes
    start_seg: int  # 4-5 (word)
    end_seg: int  # 6-7 (word)
    description: str  # 8-51
    flags: int  # 56
    os_type: int | None  # 125: 1 = DOS (not what marks an extended volume; see
    # qic113.is_extended_os)
    compressed: bool | None  # 124 bit 7
    dir_section_size: int | None  # 92-95
    raw: bytes = b""
    date: int = 0  # 52-55, packed (decode_short_date)
    multi_cartridge_seq: int | None = None  # 57
    data_section_size: int | None = None  # 96-103 (quadword)
    compression_code: int | None = None  # 124 bits 0-5 (QIC-123; 0x01 compliant)
    source_label: str | None = None  # 106-121

    # --- derived flag accessors (DESIGN.md §7.5) ---
    @property
    def vendor_specific(self) -> bool:
        return bool(self.flags & 0x01)  # byte 56 bit 0

    @property
    def segment_spanning(self) -> bool:
        return bool(self.flags & 0x10)  # byte 56 bit 4

    @property
    def directory_last(self) -> bool:
        return bool(self.flags & 0x20)  # byte 56 bit 5


# QIC-113 Rev G section 6: a QIC-113 extended-OS volume on a QIC-40/80 tape sets
# the vendor-specific bit and writes 113 / <revision> at VTBL offsets 58 / 60.
QIC113_SIGNATURE = 113


def parse_volume_table(seg: Segment) -> list[VtblEntry]:
    """Parse a volume-table :class:`Segment` (see :func:`parse_volume_table_data`)."""
    log.debug("vtbl: parsing volume table from segment %d", seg.seg)
    return parse_volume_table_data(seg_mod.segment_data(seg))


def parse_volume_table_data(data: bytes) -> list[VtblEntry]:
    """Parse 128-byte ``VTBL``/``XTBL``/``UTID``/``EXVT`` entries from a segment.

    The volume table is the first segment of the logical area. We scan its data
    area in 128-byte records, recognizing the four signatures; ``UTID`` (tape
    name) and ``EXVT`` (overflow) are recognized but yield no file-set range.
    """
    return [_parse_vtbl_entry(rec) for rec in vtbl_records(data)]


def vtbl_records(data: bytes) -> list[bytes]:
    """The raw 128-byte ``VTBL`` records of a volume-table data area, in order.

    Volume profiles (``qiclib.volume.profile``) decode these themselves, so this is
    the one place that knows how the table is walked.
    """
    records: list[bytes] = []
    off = 0
    n = len(data)
    while off + VTBL_ENTRY_LEN <= n:
        rec = data[off : off + VTBL_ENTRY_LEN]
        sig = rec[0:4]
        if sig == SIG_VTBL:
            records.append(rec)
        elif sig in (SIG_XTBL, SIG_UTID, SIG_EXVT):
            log.debug("vtbl: %r record at %d is not a file set; skipping", sig, off)
            # Rev N §8.1-8.3: XTBL extends the preceding VTBL (unicode name and
            # password), UTID is a unicode tape name, EXVT chains the table into
            # another segment. None of them is a file set of its own (XTBL used to
            # be parsed as one, producing a bogus entry).
            # TODO: follow EXVT (bytes 6-7 = child segment) for long tables.
            pass
        elif sig == b"\x00\x00\x00\x00":
            log.debug("vtbl: empty record at %d; end of table", off)
            break  # empty record => end of table
        else:
            log.debug("vtbl: unknown signature %r at %d; skipping record", sig, off)
        # Unknown 4cc: skip this record and continue scanning.
        off += VTBL_ENTRY_LEN
    log.debug("vtbl: %d VTBL records", len(records))
    return records


def parse_vtbl_base(rec: bytes) -> VtblEntry:
    """Decode only bytes 0-56 of a VTBL record, which every layout agrees on.

    QIC-80-MC Rev N §8: bytes 0-56 are always defined, even for vendor-specific
    entries. Everything later (sizes, label, compression, OS type) is left
    ``None`` for the caller -- the built-in Rev N / QIC-113 logic below, or a
    volume profile -- to fill in.
    """
    return VtblEntry(
        signature=rec[0:4],
        # Words, not doublewords: Rev N §8 (the old 4-byte read at 4/8 gave
        # 172425219 -> 1701603654 on the bench tape instead of 3 -> 2631).
        start_seg=int.from_bytes(rec[4:6], "little"),
        end_seg=int.from_bytes(rec[6:8], "little"),
        description=ascii_field(rec[8:52]),
        flags=rec[56],
        date=int.from_bytes(rec[52:56], "little"),
        os_type=None,
        compressed=None,
        dir_section_size=None,
        raw=rec,
    )


def _parse_vtbl_entry(rec: bytes) -> VtblEntry:
    """Decode one 128-byte VTBL entry (DESIGN.md §7.3, §7.5).

    The fixed Rev N / QIC-113 interpretation, used by
    :func:`parse_volume_table_data`; ``qicsilver identify`` and ``extract``
    read through volume profiles instead. The ``qic80-rev-n`` and
    ``cms-qic113`` profiles encode the same offsets as data
    (tests/test_volume_profile.py keeps the two in step).
    """
    entry = parse_vtbl_base(rec)
    if entry.flags & 0x01 and int.from_bytes(rec[58:60], "little") != QIC113_SIGNATURE:
        # Vendor specific and NOT a QIC-113 volume: per QIC-80 Rev N nothing
        # past byte 56 is defined.
        log.debug(
            "vtbl %r: vendor-specific, signature %d != %d; leaving bytes 57+ undecoded",
            entry.description,
            int.from_bytes(rec[58:60], "little"),
            QIC113_SIGNATURE,
        )
        return entry
    # Either a plain QIC-80 entry, or a vendor-specific one carrying the QIC-113
    # signature (58/59 = 113, 60/61 = QIC-113 revision: F = 6, G = 7), whose
    # bytes 84-127 QIC-113 Rev G section 6 defines with the same layout. The
    # bench tape is the latter (113, 6: QIC-113 Rev F, DOS extended format).
    return replace(
        entry,
        os_type=rec[125],
        compressed=bool(rec[124] & 0x80),
        dir_section_size=int.from_bytes(rec[92:96], "little"),
        multi_cartridge_seq=rec[57],
        data_section_size=int.from_bytes(rec[96:104], "little"),
        compression_code=rec[124] & 0x3F,
        source_label=ascii_field(rec[106:122]),
    )


# ---------------------------------------------------------------------------
# Per-file-set Volume Data Area reassembly
# ---------------------------------------------------------------------------


def volume_streams(
    segs: dict[tuple[int, int], Segment],
    vol: VolumeInfo,
    bsm: BadSectorMap,
) -> list[tuple[VtblEntry, bytes]]:
    """Build each file set's Volume Data Area byte stream.

    For each VTBL entry, concatenate the **data** sectors (the 3 ECC sectors
    dropped) of its segment range ``[start_seg, end_seg]`` in logical-segment
    order. BSM-flagged whole-bad segments are skipped (they hold no logical data).

    Requires the volume-table segment to be locatable: the table is the first
    segment of the logical area. We find it by scanning segments for a
    ``VTBL``/``XTBL`` signature in their (uncorrected) data rows.

    A missing segment is zero-filled at a full 29 sectors, and compressed
    volumes come out still compressed: this is not the TWVL path (see the
    module docstring).
    """
    spt = vol.segments_per_track or 1
    by_abs = _segments_by_abs(segs)

    log.debug("volume_streams: looking for the volume table among %d segments", len(segs))
    vtbl_seg = find_volume_table_segment(segs)
    if vtbl_seg is None:
        log.debug("volume_streams: no volume table segment; returning no streams")
        return []
    entries = parse_volume_table(vtbl_seg)

    streams: list[tuple[VtblEntry, bytes]] = []
    for entry in entries:
        log.debug(
            "volume_streams: assembling %r from segments %d-%d",
            entry.description,
            entry.start_seg,
            entry.end_seg,
        )
        out = bytearray()
        # Per-segment skips are counted and summarized after the loop.
        bad_skipped = 0
        missing = 0
        for seg_abs in range(entry.start_seg, entry.end_seg + 1):
            if bsm.is_segment_bad(seg_abs):
                bad_skipped += 1
                continue
            tpt, tps = divmod(seg_abs, spt)
            seg = by_abs.get(seg_abs) or segs.get((tpt, tps))
            if seg is None:
                # Missing segment: emit zero-filled data area to preserve offsets.
                missing += 1
                out.extend(bytes(DATA_SECTORS_PER_SEGMENT * 1024))
                continue
            out.extend(seg_mod.segment_data(seg))
        if bad_skipped or missing:
            log.debug(
                "volume_streams: %r skipped %d BSM-bad segments, zero-filled %d missing",
                entry.description,
                bad_skipped,
                missing,
            )
        streams.append((entry, bytes(out)))
    return streams


def _segments_by_abs(segs: dict[tuple[int, int], Segment]) -> dict[int, Segment]:
    return {s.seg: s for s in segs.values()}


def find_volume_table_segment(segs: dict[tuple[int, int], Segment]) -> Segment | None:
    """Find the segment whose data area begins with a VTBL/XTBL signature."""
    # Prefer the lowest absolute segment that looks like a volume table.
    candidates = sorted(segs.values(), key=lambda s: s.seg)
    for seg in candidates:
        data = seg_mod.segment_data(seg)
        if data[:4] in (SIG_VTBL, SIG_XTBL):
            log.debug("vtbl: volume table signature %r in segment %d", data[:4], seg.seg)
            return seg
    log.debug("vtbl: no VTBL/XTBL signature in %d segments; returning None", len(candidates))
    return None
