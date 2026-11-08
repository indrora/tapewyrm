"""Multi-pass union of recovered sectors (DESIGN.md §6.4, §6A.5).

Across multiple capture passes, take any sector whose ``data_crc_ok`` is True in
*any* pass; this runs **before** RS so the erasure decoder is handed the smallest
possible erasure set. Sectors are identified by their self-locating coordinate
``(fsd, ftk, fsc)`` (DESIGN.md §2.3).

The union is capture-order independent and idempotent: two CRC-good copies of the
same coordinate are byte-identical, so whichever is seen first wins and the other
is a no-op.

That is the finest grain there is, and the default. :func:`select` runs before
the union and narrows the passes to coarser units first (``tw convert
--best``): one read per *track* (the capture that holds the most of it), or one
read per *segment* (every sector of a segment from the same pass). Coarser
choices give up sectors the union would have rescued; what they buy is an
image whose every track, or every Reed-Solomon codeword, came off the tape in
one go, which is the thing to compare when the passes disagree or a CRC-16
collision is suspected. The winner is ranked by CRC-clean sectors (``crc``,
the default) or by whether its segments Reed-Solomon correct first (``rs``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Sequence, Sized
from dataclasses import dataclass, field
from enum import StrEnum

from tapewyrm_archive.progress import NULL_PROGRESS, Progress

from qiclib import header as header_mod
from qiclib import place, rs
from qiclib import segment as seg_mod
from qiclib.geometry import Geometry
from qiclib.types import RawSector, Segment

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
    origin: dict[tuple[int, int, int], int] | None = None,
) -> Iterator[RawSector]:
    """Union sectors across passes by ``(fsd, ftk, fsc)``, preferring CRC-good.

    ``passes`` is an iterable of per-pass sector iterables. Yields one sector per
    distinct coordinate: the first CRC-good copy seen, or (if none was good) the
    first copy seen at all (so even uncorrectable coordinates survive into RS as
    erasures rather than vanishing).

    ``progress`` gets one task counting passes (the total when ``passes`` has
    a length); ``stats``, when given, receives the tallies at the end.
    ``origin``, when given, is filled with the index of the pass each kept
    copy came from, by coordinate: what ``tw convert``'s per-track log
    names as "from" (:mod:`qiclib.mergelog`).
    """
    best: dict[tuple[int, int, int], RawSector] = {}
    # Per-sector outcomes are tallied and logged once (per-sector logging would
    # be thousands of lines per track).
    n_passes = n_seen = n_upgraded = n_new_later = n_upgraded_later = 0
    total = len(passes) if isinstance(passes, Sized) else None
    log.debug("unioning sectors across %s capture passes", total or "a stream of")
    with progress.task("merging passes", total=total, unit="passes") as bar:
        for index, sectors in enumerate(passes):
            n_passes += 1
            for sec in sectors:
                n_seen += 1
                key = (sec.fsd, sec.ftk, sec.fsc)
                current = best.get(key)
                if current is None:
                    if n_passes > 1:
                        n_new_later += 1
                    best[key] = sec
                    if origin is not None:
                        origin[key] = index
                elif not current.data_crc_ok and sec.data_crc_ok:
                    n_upgraded += 1
                    if n_passes > 1:
                        n_upgraded_later += 1
                    best[key] = sec
                    if origin is not None:
                        origin[key] = index
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


# ---------------------------------------------------------------------------
# Choosing whole reads before the union (tw convert --best)
# ---------------------------------------------------------------------------


class Best(StrEnum):
    """The unit :func:`select` keeps whole: the grain at which the best read wins."""

    TRACK = "track"
    SEGMENT = "segment"
    SECTOR = "sector"


class Rank(StrEnum):
    """What makes one read of a track or segment better than another (:func:`select`)."""

    CRC = "crc"
    RS = "rs"


# A segment's score under each rank; bigger is better, compared as tuples.
# crc: (CRC-clean sectors in the codeword,)
# rs:  (its parity tier, _PARITY_TIER, then CRC-clean sectors in the codeword)
Score = tuple[int, ...]

# How much a segment's spare-parity verdict (rs.verify) is worth under
# --rank rs. Verified beats corrects-but-unchecked beats everything else; an
# INCONSISTENT segment holds a CRC-good sector that is wrong, which is no
# more use than one with too many erasures (both need another read), so the
# two tie and the CRC-clean count decides between them.
_PARITY_TIER = {
    rs.Parity.CONSISTENT: 2,
    rs.Parity.UNVERIFIED: 1,
    rs.Parity.INCONSISTENT: 0,
    rs.Parity.UNCORRECTABLE: 0,
    rs.Parity.MISSING: 0,
}


@dataclass
class SelectStats:
    """Tallies from one :func:`select`.

    ``contested`` counts the units (tracks or segments) more than one pass
    read, the only ones where a choice was made; ``won[i]`` how many units
    pass ``i`` supplied, contested or not (sectors, at ``Best.SECTOR``);
    ``dropped`` the sectors left out, every one of which the plain union
    would have looked at: the losing reads', and at ``Best.SEGMENT`` also a
    pass's second copy of a slot and IDs outside QIC's coordinate space,
    which placement drops anyway. ``winners`` maps a track to the pass that
    won it (``Best.TRACK`` only), for the caller to name in its log.
    """

    contested: int = 0
    won: list[int] = field(default_factory=list)
    dropped: int = 0
    winners: dict[int, int] = field(default_factory=dict)


def _clean_slots(seg: Segment) -> int:
    """Codeword slots of ``seg`` that hold a CRC-clean sector: not excluded, not erased."""
    erased = seg_mod.erasure_mask(seg)
    return sum(1 for i in range(Segment.SECTORS) if i not in seg.excluded and not erased[i])


def _score(seg: Segment, rank: Rank) -> Score:
    """``seg``'s score as one read of it, under ``rank`` (see ``Score``).

    ``rs`` asks :func:`qiclib.rs.verify` first: does the segment correct, and
    does the parity the solve leaves over agree with the rows taken as good?
    That is what tells two reads apart when both have few enough erasures
    to correct but one holds a CRC-good sector that is wrong (a CRC-16
    collision, ~1 burst in 65,536), which counting CRC-clean sectors cannot
    see. ``verify`` never writes into the segment (``rs.correct`` would:
    it scatters the solved rows into the sectors it is given, which here
    are the caller's passes).
    """
    clean = _clean_slots(seg)
    if rank is Rank.RS:
        return (_PARITY_TIER[rs.verify(seg)], clean)
    return (clean,)


def _placed(
    sectors: list[RawSector], geometry: Geometry, bsm: header_mod.BadSectorMap
) -> dict[tuple[int, int], Segment]:
    """One pass's sectors binned into segments under the tape's own geometry and map.

    The same two steps the image build takes after it has found the header
    (:func:`qiclib.header.locate_header`), so a segment is scored here as it
    will be corrected there: BSM-excluded slots are no part of the codeword,
    neither erasures nor clean.
    """
    segs = place.place(sectors, geometry)
    header_mod.apply_bsm(segs, bsm)
    return segs


def select(
    passes: Sequence[list[RawSector]],
    best: Best,
    *,
    tracks: Sequence[int],
    geometry: Geometry,
    bsm: header_mod.BadSectorMap,
    rank: Rank = Rank.CRC,
    progress: Progress = NULL_PROGRESS,
    stats: SelectStats | None = None,
) -> list[list[RawSector]]:
    """Narrow ``passes`` to the best read of each track or segment, for :func:`union`.

    ``tracks[i]`` is the track pass ``i`` was captured from (its TWRF
    header's ``track``); ``geometry`` and ``bsm`` are the tape's, from its
    header, which a caller finds first by locating it in the union of every
    pass (a segment's sectors cannot be told apart from its neighbours'
    without the floppy tracks per side, nor its codeword sized without the
    map).

    - ``Best.SECTOR`` returns the passes as they are: the union already keeps
      the best copy of every sector.
    - ``Best.TRACK`` keeps, of the passes captured from one track, the one
      whose segments score highest in total, and drops the others whole.
      A pass with no track (negative) is a group of its own and always stays.
    - ``Best.SEGMENT`` keeps, for each segment any pass read, the sectors of
      the pass whose copy of that segment scores highest, and drops every
      other pass's sectors of it. Segments are compared across all passes,
      whatever track header they came under: sectors self-locate.

    The score is :class:`Rank`: ``crc`` counts the CRC-clean sectors of the
    codeword; ``rs`` first asks whether the segment Reed-Solomon corrects
    (a track's ``rs`` score: how many of its segments do), then counts. Ties
    go to the earliest pass, so the choice follows source order as the union
    does. The result has one list per pass, in order, some of them emptied.
    """
    if len(tracks) != len(passes):
        log.debug("select: %d passes but %d track numbers; refusing", len(passes), len(tracks))
        raise ValueError(f"select needs one track per pass: {len(passes)} passes, {len(tracks)}")
    best, rank = Best(best), Rank(rank)
    tally = stats if stats is not None else SelectStats()
    tally.won = [0] * len(passes)
    if best is Best.SECTOR:
        log.debug("select: by sector, %d passes go to the union as they are", len(passes))
        if rank is not Rank.CRC:
            log.warning("ranking by %s has no effect when choosing by sector", rank.value)
        tally.won = [len(p) for p in passes]
        return [list(p) for p in passes]

    placed: list[dict[tuple[int, int], Segment]] = []
    scores: list[dict[tuple[int, int], Score]] = []
    with progress.task("scoring reads", total=len(passes), unit="passes") as bar:
        for sectors in passes:
            segs = _placed(sectors, geometry, bsm)
            placed.append(segs)
            scores.append({key: _score(seg, rank) for key, seg in segs.items()})
            bar.advance()

    if best is Best.TRACK:
        return _by_track(passes, tracks, scores, tally)
    return _by_segment(passes, placed, scores, tally)


def _by_track(
    passes: Sequence[list[RawSector]],
    tracks: Sequence[int],
    scores: list[dict[tuple[int, int], Score]],
    tally: SelectStats,
) -> list[list[RawSector]]:
    """:func:`select` at ``Best.TRACK``: one whole pass per track number."""

    def total(i: int) -> Score:
        # Elementwise sum of the pass's segment scores: (clean,) for crc,
        # (segments that correct, clean) for rs.
        per_seg = list(scores[i].values())
        width = len(per_seg[0]) if per_seg else 1
        return tuple(sum(s[k] for s in per_seg) for k in range(width))

    groups: dict[int, list[int]] = {}
    for i, track in enumerate(tracks):
        # A pass with no track number cannot compete with anything.
        groups.setdefault(track if track >= 0 else -1 - i, []).append(i)
    keep: set[int] = set()
    for track, members in groups.items():
        winner = members[0]
        for i in members[1:]:
            if total(i) > total(winner):  # strictly: ties go to the earliest
                winner = i
        keep.add(winner)
        if track >= 0:
            tally.winners[track] = winner
        if len(members) > 1:
            tally.contested += 1
            log.debug(
                "select: track %d: pass %d wins with %s over %s",
                track,
                winner,
                total(winner),
                {i: total(i) for i in members if i != winner},
            )
    out: list[list[RawSector]] = []
    for i, sectors in enumerate(passes):
        if i in keep:
            tally.won[i] = 1
            out.append(list(sectors))
        else:
            tally.dropped += len(sectors)
            out.append([])
    log.debug(
        "select by track: %d tracks, %d read more than once, %d sectors dropped",
        len(groups),
        tally.contested,
        tally.dropped,
    )
    return out


def _by_segment(
    passes: Sequence[list[RawSector]],
    placed: list[dict[tuple[int, int], Segment]],
    scores: list[dict[tuple[int, int], Score]],
    tally: SelectStats,
) -> list[list[RawSector]]:
    """:func:`select` at ``Best.SEGMENT``: every segment's sectors from one pass."""
    winner: dict[tuple[int, int], int] = {}
    readers: dict[tuple[int, int], int] = {}
    for i, per_seg in enumerate(scores):
        for key, score in per_seg.items():
            readers[key] = readers.get(key, 0) + 1
            if key not in winner or score > scores[winner[key]][key]:
                winner[key] = i  # strictly greater: ties go to the earliest
    tally.contested = sum(1 for n in readers.values() if n > 1)
    out: list[list[RawSector]] = [[] for _ in passes]
    for key, i in winner.items():
        tally.won[i] += 1
        # The placed segment's sectors, not the pass's raw list: place()
        # already settled slot collisions inside the pass (CRC-good first)
        # and dropped IDs outside QIC's coordinate space.
        out[i].extend(sec for sec in placed[i][key].sectors if sec is not None)
    tally.dropped = sum(len(p) for p in passes) - sum(len(p) for p in out)
    log.debug(
        "select by segment: %d segments, %d read more than once, %d sectors dropped",
        len(winner),
        tally.contested,
        tally.dropped,
    )
    return out
