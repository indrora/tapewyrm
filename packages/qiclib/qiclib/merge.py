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

import logging
from collections.abc import Iterable, Iterator, Sized
from dataclasses import dataclass

from tapewyrm_archive.progress import NULL_PROGRESS, Progress

from qiclib.types import RawSector

log = logging.getLogger(__name__)


@dataclass
class UnionStats:
    """Tallies from one :func:`union`, filled in once it has been consumed.

    ``filled_new`` counts coordinates a later pass read that no earlier pass
    had at all; ``filled_upgraded`` counts coordinates a later pass replaced a
    CRC-bad copy of with a CRC-good one. Their sum is what re-reading bought.
    """

    passes: int = 0
    seen: int = 0
    unique: int = 0
    filled_new: int = 0
    filled_upgraded: int = 0


def union(
    passes: Iterable[Iterable[RawSector]],
    *,
    progress: Progress = NULL_PROGRESS,
    stats: UnionStats | None = None,
) -> Iterator[RawSector]:
    """Union sectors across passes by ``(fsd, ftk, fsc)``, preferring CRC-good.

    ``passes`` is an iterable of per-pass sector iterables. Yields one sector per
    distinct coordinate: the first CRC-good copy seen, or (if none was good) the
    first copy seen at all (so even uncorrectable coordinates survive into RS as
    erasures rather than vanishing).

    ``progress`` gets one task counting passes (the total when ``passes`` has
    a length); ``stats``, when given, receives the tallies at the end.
    """
    best: dict[tuple[int, int, int], RawSector] = {}
    # Per-sector outcomes are tallied and logged once (per-sector logging would
    # be thousands of lines per track).
    n_passes = n_seen = n_upgraded = n_new_later = n_upgraded_later = 0
    total = len(passes) if isinstance(passes, Sized) else None
    log.debug("unioning sectors across %s capture passes", total or "a stream of")
    with progress.task("merging passes", total=total, unit="passes") as bar:
        for sectors in passes:
            n_passes += 1
            for sec in sectors:
                n_seen += 1
                key = (sec.fsd, sec.ftk, sec.fsc)
                current = best.get(key)
                if current is None:
                    if n_passes > 1:
                        n_new_later += 1
                    best[key] = sec
                elif not current.data_crc_ok and sec.data_crc_ok:
                    n_upgraded += 1
                    if n_passes > 1:
                        n_upgraded_later += 1
                    best[key] = sec
            bar.advance()
    if stats is not None:
        stats.passes = n_passes
        stats.seen = n_seen
        stats.unique = len(best)
        stats.filled_new = n_new_later
        stats.filled_upgraded = n_upgraded_later
    log.debug(
        "union: %d passes, %d sectors seen -> %d unique coordinates "
        "(%d CRC-bad copies replaced by a CRC-good one, %d duplicates dropped)",
        n_passes,
        n_seen,
        len(best),
        n_upgraded,
        n_seen - len(best) - n_upgraded,
    )
    yield from best.values()
