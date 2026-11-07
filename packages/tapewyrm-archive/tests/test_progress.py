"""QueueProgress -> ProgressRelay: tasks opened in a worker, drawn by the parent.

The wire is the same tuples whatever queue carries them, so a plain
``queue.Queue`` stands in for the multiprocessing one, and a fake clock drives
the flush timer.
"""

from __future__ import annotations

import queue
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from tapewyrm_archive.progress import ProgressRelay, QueueProgress


@dataclass
class _Task:
    description: str
    total: float | None
    unit: str
    position: float = 0
    closed: bool = False

    def advance(self, amount: float = 1) -> None:
        self.position += amount

    def update(self, completed: float) -> None:
        self.position = completed


class _Recorder:
    """The parent's Progress: remembers every task it was asked to draw."""

    def __init__(self) -> None:
        self.tasks: list[_Task] = []

    @contextmanager
    def task(self, description: str, total: float | None = None, unit: str = "") -> Iterator[_Task]:
        rec = _Task(description, total, unit)
        self.tasks.append(rec)
        try:
            yield rec
        finally:
            rec.closed = True


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _relay_all(q: queue.Queue, relay: ProgressRelay) -> list[tuple]:
    """Feed every queued message to ``relay``; return them for inspection."""
    sent = []
    while not q.empty():
        msg = q.get_nowait()
        sent.append(msg)
        relay.handle(msg)
    return sent


def test_a_task_is_replayed_with_prefix_total_unit_and_final_position():
    """Open, advances inside one flush window, close: one bar, its position sent once at close."""
    q: queue.Queue = queue.Queue()
    worker = QueueProgress(q, origin=3, prefix="track-03.twrf: ", clock=_Clock())
    with worker.task("framing MFM bytes", total=10, unit="sync runs") as bar:
        for _ in range(7):
            bar.advance()
    parent = _Recorder()
    sent = _relay_all(q, ProgressRelay(parent))
    assert [m[0] for m in sent] == ["open", "update", "close"]
    (task,) = parent.tasks
    assert (task.description, task.total, task.unit) == (
        "track-03.twrf: framing MFM bytes",
        10,
        "sync runs",
    )
    assert task.position == 7 and task.closed


def test_positions_are_flushed_on_a_timer_and_once_more_at_close():
    """Fifty advances with the clock stepping every tenth: a flush per step, then the last."""
    q: queue.Queue = queue.Queue()
    clock = _Clock()
    worker = QueueProgress(q, clock=clock)
    with worker.task("scanning", total=50) as bar:
        for n in range(1, 51):
            bar.advance()
            if n % 10 == 0:
                clock.now += 1.0
    positions = [m[2] for m in _relay_all(q, ProgressRelay(_Recorder())) if m[0] == "update"]
    assert positions == [11, 21, 31, 41, 50]


def test_update_sets_an_absolute_position_that_later_advances_build_on():
    q: queue.Queue = queue.Queue()
    worker = QueueProgress(q, clock=_Clock())
    with worker.task("parsing", total=20) as bar:
        bar.advance(3)
        bar.update(10)
        bar.advance(2)
    positions = [m[2] for m in _relay_all(q, ProgressRelay(_Recorder())) if m[0] == "update"]
    assert positions == [12]


def test_two_workers_tasks_do_not_collide():
    """Each worker counts its own tasks from zero; the origin keeps the keys apart."""
    q: queue.Queue = queue.Queue()
    first, second = (
        QueueProgress(q, origin=0, clock=_Clock()),
        QueueProgress(q, origin=1, clock=_Clock()),
    )
    with first.task("a") as a, second.task("b") as b:
        a.advance(1)
        b.advance(2)
    parent = _Recorder()
    _relay_all(q, ProgressRelay(parent))
    assert [(t.description, t.position, t.closed) for t in parent.tasks] == [
        ("a", 1, True),
        ("b", 2, True),
    ]


def test_relay_ignores_messages_for_tasks_it_never_opened():
    relay = ProgressRelay(_Recorder())
    relay.handle(("update", (9, 9), 5))
    relay.handle(("close", (9, 9)))
    relay.handle(("bogus", (9, 9)))


def test_relay_close_ends_what_a_dead_worker_left_open():
    """No close message ever came (the worker died); the parent's close ends its bar."""
    parent = _Recorder()
    relay = ProgressRelay(parent)
    relay.handle(("open", (0, 0), "PLL", 100, "transitions"))
    relay.handle(("update", (0, 0), 40))
    assert parent.tasks[0].position == 40 and not parent.tasks[0].closed
    relay.close()
    assert parent.tasks[0].closed
    relay.close()  # idempotent
