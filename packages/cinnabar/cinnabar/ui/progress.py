"""A :class:`tapewyrm_archive.progress.Progress` that reports through Qt signals.

The libraries report long work through the ``Progress`` protocol (one
``task`` per stage: "parsing flux", "PLL", "decompressing"); the CLIs draw
those as rich bars. Here the same calls become signals, emitted from
whatever worker thread is doing the work and delivered to the main thread
by Qt's queued connections, so the status bar can say what is going on
and how far it has got without the window ever blocking.

One task at a time is shown (the stages are sequential); nested tasks
show the innermost.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from PySide6.QtCore import QObject, Signal
from tapewyrm_archive.progress import ProgressTask


class QtProgress(QObject):
    started = Signal(str, float, str)  # description, total (0 = unknown), unit
    advanced = Signal(float)  # completed so far
    finished = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._depth = 0

    @contextmanager
    def task(
        self, description: str, total: float | None = None, unit: str = ""
    ) -> Iterator[ProgressTask]:
        self._depth += 1
        self.started.emit(description, float(total or 0), unit)
        job = _Task(self)
        try:
            yield job
        finally:
            self._depth -= 1
            if self._depth == 0:
                self.finished.emit()


class _Task:
    def __init__(self, owner: QtProgress) -> None:
        self._owner = owner
        self._done = 0.0

    def advance(self, amount: float = 1) -> None:
        self._done += amount
        self._owner.advanced.emit(self._done)

    def update(self, completed: float) -> None:
        self._done = completed
        self._owner.advanced.emit(self._done)
