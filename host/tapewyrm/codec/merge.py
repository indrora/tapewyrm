"""Multi-pass union of recovered sectors (DESIGN.md §6.4, §6A.5).

Across multiple capture passes, take any sector whose ``data_crc_ok`` is True in
*any* pass; this runs **before** RS so the erasure decoder is handed the smallest
possible erasure set. Sectors are identified by their self-locating coordinate
``(fsd, ftk, fsc)`` (DESIGN.md §2.3).

The union is capture-order independent and idempotent: two CRC-good copies of the
same coordinate are byte-identical, so whichever is seen first wins and the other
is a no-op.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from tapewyrm.types import RawSector


def union(passes: Iterable[Iterable[RawSector]]) -> Iterator[RawSector]:
    """Union sectors across passes by ``(fsd, ftk, fsc)``, preferring CRC-good.

    ``passes`` is an iterable of per-pass sector iterables. Yields one sector per
    distinct coordinate: the first CRC-good copy seen, or (if none was good) the
    first copy seen at all (so even uncorrectable coordinates survive into RS as
    erasures rather than vanishing).
    """
    best: dict[tuple[int, int, int], RawSector] = {}
    for sectors in passes:
        for sec in sectors:
            key = (sec.fsd, sec.ftk, sec.fsc)
            current = best.get(key)
            if current is None:
                best[key] = sec
            elif not current.data_crc_ok and sec.data_crc_ok:
                best[key] = sec
    yield from best.values()
