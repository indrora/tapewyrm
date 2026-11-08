"""qiclib.merge: the multi-pass union, choosing whole reads first, and their tallies."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from qiclib import merge
from qiclib.geometry import Geometry
from qiclib.header import BadSectorMap
from qiclib.types import RawSector


def _sec(fsc: int, ok: bool = True) -> RawSector:
    return RawSector(
        fsd=0, ftk=0, fsc=fsc, data=bytes([fsc]) * 4, id_crc_ok=True, data_crc_ok=ok, deleted=False
    )


def test_union_stats_count_what_later_passes_filled_in():
    """Pass 2 adds sector 3 (new) and replaces CRC-bad sector 2 (upgrade)."""
    first = [_sec(1), _sec(2, ok=False)]
    second = [_sec(1), _sec(2), _sec(3)]
    stats = merge.UnionStats()
    out = list(merge.union([first, second], stats=stats))
    assert sorted(s.fsc for s in out) == [1, 2, 3]
    assert all(s.data_crc_ok for s in out)
    assert (stats.passes, stats.seen, stats.unique) == (2, 5, 3)
    assert (stats.filled_new, stats.filled_upgraded) == (1, 1)


def test_union_advances_one_step_per_pass():
    opened: list[tuple[str, float | None, str]] = []
    steps: list[float] = []

    class _Task:
        def advance(self, amount: float = 1) -> None:
            steps.append(amount)

        def update(self, completed: float) -> None:
            raise AssertionError("union advances; it never jumps")

    class _Progress:
        @contextmanager
        def task(self, description, total=None, unit=""):
            opened.append((description, total, unit))
            yield _Task()

    list(merge.union([[_sec(1)], [_sec(2)], []], progress=_Progress()))
    assert opened == [("merging passes", 3, "passes")]
    assert sum(steps) == 3


# ---------------------------------------------------------------------------
# select: the best read of each track or segment
# ---------------------------------------------------------------------------

GEOM = Geometry(tracks=28, segments_per_track=207)


def _segment(seg: int, bad: frozenset[int] = frozenset(), *, tag: int = 0) -> list[RawSector]:
    """Segment ``seg`` of floppy track 0 (FSC 32*seg+1 ...), CRC-bad at ``bad`` slots.

    ``tag`` marks the bytes, so a test can tell which pass a sector came from.
    """
    return [
        RawSector(
            fsd=0,
            ftk=0,
            fsc=32 * seg + slot + 1,
            data=bytes([tag]) * 4,
            id_crc_ok=True,
            data_crc_ok=slot not in bad,
            deleted=False,
        )  # fmt: skip
        for slot in range(32)
    ]


def _select(passes, best, tracks, **kw):
    stats = merge.SelectStats()
    out = merge.select(
        passes, best, tracks=tracks, geometry=GEOM, bsm=BadSectorMap(), stats=stats, **kw
    )
    return out, stats


def test_select_by_sector_hands_the_passes_to_the_union_as_they_are():
    a, b = _segment(0, frozenset({1})), _segment(0)
    out, stats = _select([a, b], merge.Best.SECTOR, [0, 0])
    assert out == [a, b] and stats.dropped == 0


def test_select_by_track_keeps_the_read_with_the_most_clean_sectors():
    """A: 30 + 32 clean; B: 32 + 31. B wins track 0 whole; track 1's only read stays."""
    a = [*_segment(0, frozenset({1, 2}), tag=1), *_segment(1, tag=1)]
    b = [*_segment(0, tag=2), *_segment(1, frozenset({5}), tag=2)]
    c = _segment(2, tag=3)
    out, stats = _select([a, b, c], merge.Best.TRACK, [0, 0, 1])
    assert out == [[], b, c]
    assert stats.winners == {0: 1, 1: 2}
    assert (stats.contested, stats.dropped, stats.won) == (1, 64, [0, 1, 1])


def test_select_by_track_gives_a_tie_to_the_earliest_read():
    a, b = _segment(0, frozenset({3})), _segment(0, frozenset({4}))
    out, _ = _select([a, b], merge.Best.TRACK, [0, 0])
    assert out == [a, []]


def test_select_by_track_never_groups_reads_without_a_track():
    a, b = _segment(0, frozenset({3})), _segment(0)
    out, _ = _select([a, b], merge.Best.TRACK, [-1, -1])
    assert out == [a, b]


def test_select_by_segment_takes_every_sector_of_a_segment_from_one_read():
    """The same two reads: segment 0 comes all from B, segment 1 all from A."""
    a = [*_segment(0, frozenset({1, 2}), tag=1), *_segment(1, tag=1)]
    b = [*_segment(0, tag=2), *_segment(1, frozenset({5}), tag=2)]
    out, stats = _select([a, b], merge.Best.SEGMENT, [0, 0])
    assert {s.data[0] for s in out[0]} == {1} and {(s.fsc - 1) // 32 for s in out[0]} == {1}
    assert {s.data[0] for s in out[1]} == {2} and {(s.fsc - 1) // 32 for s in out[1]} == {0}
    assert (stats.contested, stats.won, stats.dropped) == (2, [1, 1], 64)
    # What reaches the union is whole: 64 CRC-good sectors, no two from one slot.
    merged = list(merge.union(out))
    assert sorted(s.fsc for s in merged) == list(range(1, 65))
    assert all(s.data_crc_ok for s in merged)


def test_rank_rs_prefers_the_track_read_with_more_correctable_segments():
    """A: 28 + 32 = 60 clean, but its segment 0 has 4 erasures (> 3) and does not correct.
    B: 30 + 29 = 59 clean, fewer than A's, but both its segments correct."""
    a = [*_segment(0, frozenset({0, 1, 2, 3})), *_segment(1)]
    b = [*_segment(0, frozenset({0, 1})), *_segment(1, frozenset({0, 1, 2}))]
    assert _select([a, b], merge.Best.TRACK, [0, 0])[0] == [a, []]
    assert _select([a, b], merge.Best.TRACK, [0, 0], rank=merge.Rank.RS)[0] == [[], b]


def test_ranking_leaves_the_sectors_as_they_were_read():
    """Scoring must not repair the reads it compares (rs.correct writes into its input)."""
    a = _segment(0, frozenset({0, 1}))
    _select([a, _segment(0, frozenset({2}))], merge.Best.SEGMENT, [0, 0], rank=merge.Rank.RS)
    assert [s.data_crc_ok for s in a[:3]] == [False, False, True]


def test_rank_ignores_bad_sector_map_exclusions():
    """An excluded slot is no part of the codeword: neither clean nor an erasure."""
    a, b = _segment(0, frozenset({7})), _segment(0, frozenset({8}))
    bsm = BadSectorMap(bad_lsns={7})  # segment 0, slot 7: A's bad sector is excluded
    out = merge.select([a, b], merge.Best.TRACK, tracks=[0, 0], geometry=GEOM, bsm=bsm)
    assert out == [a, []]  # A: 31 of 31 clean; B: 30 of 31


def test_select_wants_one_track_per_pass():
    with pytest.raises(ValueError, match="one track per pass"):
        merge.select([[]], merge.Best.TRACK, tracks=[], geometry=GEOM, bsm=BadSectorMap())
