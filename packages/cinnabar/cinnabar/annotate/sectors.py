"""The QIC-80 records lane: MFM sectors, on tape and in an image.

QIC-80-MC Rev N section 5 records a segment as 32 floppy-style MFM sectors.
Each sector on tape is (DESIGN.md 2.2, ``tapewyrm.codec.mfm``):

    gap  | 3x A1 sync | FE | FTK FSD FSC 03 | CRC | gap | 3x A1 sync | FB/F8 | 1024 data | CRC

An ID field names the sector by its abused floppy coordinates (FSD, FTK,
FSC); the data field holds 1,024 bytes; each has a CRC-CCITT. The last 3
sectors of the 32 are Reed-Solomon parity (DESIGN.md 2.3). So:

- :func:`sector_spans` draws sectors recovered from flux: one chevron per
  sector with its fields as children, positioned in seconds from the
  codec's position channel (``tapewyrm.codec.mfm`` ``positions``).
- :func:`slot_spans` draws the 32 slots of an image segment, where there is
  no tape structure left to see: a block per slot coloured by what the
  segment table says happened to it (TWS-2 section 5).
"""

from __future__ import annotations

from collections.abc import Sequence

from qiclib import geometry as geometry_mod
from qiclib.types import RawSector
from tapewyrm_archive.twti import SegmentEntry, SegmentState

from cinnabar.model import SECTOR_BYTES, Shape, Span, assign_rows

SECTORS = 32
DATA_SECTORS = 29
SLOT_ROW = "TWTI: Sectors"
SECTOR_ROWS = ("MFM: Sectors", "MFM: Fields")


def slot_spans(entry: SegmentEntry, base: int = 0) -> list[Span]:
    """The sector slots of one image segment, as records with a data child each.

    Slots are drawn at 1,024 bytes each over the segment's 29,696-byte data
    slot, so the lane lines up with the Bytes lane below it: slot k's data
    is at ``base + 1024 k``. Only the 29 data slots are drawn: the image
    does not store the 3 parity sectors (TWS-2 section 6), and on the byte
    axis there is no room for them before the next segment starts. Flux
    captures, which have the parity sectors on tape, show them.
    """
    state = entry.state
    out: list[Span] = []
    for k in range(DATA_SECTORS):
        start = base + k * SECTOR_BYTES
        end = start + SECTOR_BYTES
        excluded = bool(entry.excluded_mask >> k & 1)
        parity = k >= DATA_SECTORS
        if excluded:
            style, sub = "slot-excluded", "excluded"
        elif state is SegmentState.MISSING:
            style, sub = "slot-missing", "missing"
        elif state is SegmentState.BAD:
            style, sub = "slot-bad", "bad segment"
        elif parity:
            style, sub = "ecc", "ECC"
        elif state is SegmentState.UNCORRECTABLE:
            style, sub = "slot-uncorrectable", "uncorrectable"
        elif state is SegmentState.CORRECTED:
            style, sub = "slot-corrected", "corrected"
        else:
            style, sub = "data", "clean"
        out.append(
            Span(
                start,
                end,
                f"Sector {k}",
                sub,
                style,
                Shape.CHEVRON,
                (
                    ("slot", str(k)),
                    ("state", state.name),
                    ("excluded", "yes" if excluded else "no"),
                ),
                row=SLOT_ROW,
            )
        )
    return out


def sector_spans(
    sectors: Sequence[RawSector],
    positions: Sequence[tuple[float, float, float, float]],
    holds: Sequence[str] = (),
) -> list[Span]:
    """Records for sectors recovered from flux.

    ``positions[i]`` is ``(id_start, id_end, data_start, data_end)`` in
    seconds for ``sectors[i]``: the ID field from its first sync byte to the
    end of its CRC, the data field likewise. ``holds[i]`` is an optional
    second line saying what the sector contains ("Volume Table", "Header").
    """
    out: list[Span] = []
    for i, (sec, (id0, id1, d0, d1)) in enumerate(zip(sectors, positions, strict=True)):
        bad = not sec.data_crc_ok
        id_bad = not sec.id_crc_ok
        sync_w = (id1 - id0) * 3 / 10  # 3 sync bytes of a 10-byte ID field (3+1+4+2)
        d_sync_w = (d1 - d0) * 3 / 1030  # 3 sync bytes of a 1030-byte data field
        d_mark_w = (d1 - d0) / 1030
        d_crc_w = (d1 - d0) * 2 / 1030
        fields = (
            Span(id0, id0 + sync_w, "sync", "A1 A1 A1", "mark", Shape.HEX),
            Span(id0 + sync_w, id0 + sync_w + (id1 - id0) / 10, "FE", "ID mark", "mark", Shape.HEX),
            Span(
                id0 + sync_w + (id1 - id0) / 10,
                id1 - (id1 - id0) * 2 / 10,
                "ID",
                f"{sec.fsd}/{sec.ftk}/{sec.fsc}",
                "idfield" if not id_bad else "crc-bad",
                Shape.BLOCK,
                (("fsd", str(sec.fsd)), ("ftk", str(sec.ftk)), ("fsc", str(sec.fsc))),
            ),
            Span(
                id1 - (id1 - id0) * 2 / 10,
                id1,
                "crc",
                "ok" if not id_bad else "BAD",
                "crc" if not id_bad else "crc-bad",
                Shape.HEX,
            ),
            Span(id1, d0, "gap", "", "gap", Shape.BLOCK),
            Span(d0, d0 + d_sync_w, "sync", "A1 A1 A1", "mark", Shape.HEX),
            Span(
                d0 + d_sync_w,
                d0 + d_sync_w + d_mark_w,
                "F8" if sec.deleted else "FB",
                "data mark",
                "mark",
                Shape.HEX,
            ),
            Span(
                d0 + d_sync_w + d_mark_w,
                d1 - d_crc_w,
                "data",
                "1,024 B",
                "data" if not bad else "data-bad",
                Shape.BLOCK,
            ),
            Span(
                d1 - d_crc_w,
                d1,
                "crc",
                "ok" if not bad else "BAD",
                "crc" if not bad else "crc-bad",
                Shape.HEX,
            ),
        )
        label = f"id={sec.fsd}/{sec.ftk}/{sec.fsc}"
        # The last three sectors of a segment are its Reed-Solomon parity
        # (DESIGN.md 2.3), not data: say so, as the image's slots do, rather
        # than "Header" or a volume's name like their neighbours, which made
        # them look like data that had not decoded.
        parity = geometry_mod.sector_in_segment(sec.fsc) >= DATA_SECTORS
        if parity:
            sub = "ECC" if not bad else "ECC, CRC BAD"
        else:
            sub = holds[i] if i < len(holds) and holds[i] else ("CRC BAD" if bad else "")
        style = "record-bad" if (bad or id_bad) else ("ecc" if parity else "record")
        out.append(
            Span(
                id0,
                d1,
                label,
                sub,
                style,
                Shape.CHEVRON,
                (
                    ("fsd/ftk/fsc", f"{sec.fsd}/{sec.ftk}/{sec.fsc}"),
                    (
                        "holds",
                        "RS parity for the segment"
                        if parity
                        else (holds[i] if i < len(holds) and holds[i] else "data"),
                    ),
                    ("id crc", "ok" if sec.id_crc_ok else "bad"),
                    ("data crc", "ok" if sec.data_crc_ok else "bad"),
                    ("deleted mark", "yes" if sec.deleted else "no"),
                ),
                fields,
            )
        )
    return [assign_rows(s, SECTOR_ROWS) for s in out]
