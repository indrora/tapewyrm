"""qiclib.merge: the multi-pass union and the tallies convert reports from it."""

from __future__ import annotations

from contextlib import contextmanager

from qiclib import merge
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
