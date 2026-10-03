"""Place recovered sectors into 32-sector segment bins (DESIGN.md §6.4 step 2-3, §2.3).

Each :class:`RawSector` self-locates via its ``(fsd, ftk, fsc)`` -> the §7.3
algebra (``Geometry.place``) gives ``(SEG, TPT, TPS, sector-in-segment)``. We bin
each sector into the ``(tpt, tps)`` :class:`Segment` at slot ``sector-in-segment``.

Because sectors self-locate, **capture order is irrelevant** (DESIGN.md §2.3):
out-of-order, retried, or partial captures reassemble identically.

Deleted-data sectors (``F8`` mark) are format-time bad blocks — kept in place but
counted as erasures downstream (the RS layer treats ``deleted`` as an erasure).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from qiclib.geometry import Geometry
from qiclib.types import RawSector, Segment

log = logging.getLogger(__name__)


def place(sectors: Iterable[RawSector], geom: Geometry) -> dict[tuple[int, int], Segment]:
    """Bin sectors by tape coordinate into ``{(tpt, tps): Segment}``.

    Sectors with an unreadable ID (``id_crc_ok`` false and an all-zero/origin
    coordinate that is really an orphan) still place at whatever coordinate their
    ID decoded to; a genuinely unusable ID lands at (0,0,1) and is harmless (it
    will collide only with the true origin sector, which, if present and CRC-good,
    wins — see the slot-fill rule below).
    """
    segments: dict[tuple[int, int], Segment] = {}
    # Slot collisions are tallied and logged once after the loop.
    n_placed = n_replaced = n_kept = 0
    log.debug("placing sectors into segment bins")

    for sec in sectors:
        seg_abs, tpt, tps, slot = geom.place(sec.fsd, sec.ftk, sec.fsc)
        key = (tpt, tps)
        segment = segments.get(key)
        if segment is None:
            segment = Segment(tpt=tpt, tps=tps, seg=seg_abs)
            segments[key] = segment

        existing = segment.sectors[slot]
        if _prefer(sec, existing):
            if existing is not None:
                n_replaced += 1
            segment.sectors[slot] = sec
        else:
            n_kept += 1
        n_placed += 1

    log.debug(
        "placed %d sectors into %d segments; %d slot collisions replaced a CRC-bad copy, "
        "%d kept the existing copy",
        n_placed,
        len(segments),
        n_replaced,
        n_kept,
    )
    return segments


def _prefer(new: RawSector, existing: RawSector | None) -> bool:
    """Decide whether ``new`` should replace ``existing`` in a slot.

    A CRC-good data field always beats a CRC-bad one; among equals the first
    placed is kept (capture-order independence for clean sectors, since two
    CRC-good copies of the same coordinate are byte-identical).
    """
    if existing is None:
        return True
    if existing.data_crc_ok:
        return False  # keep the good copy
    # existing is bad: take new if it is good, else keep existing (stable).
    return new.data_crc_ok
