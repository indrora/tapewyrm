"""Progress reporting hooks for long-running library loops (STYLE.md §2.5).

Library code (``twrf.parse_body``, ``TapeImage.save``, ``qiclib.build`` and
``qiclib.extract``, tapewyrm-cli's ``tape.dump``, ...) wants to say
"I am 40 of 207 segments into this" without knowing *how* that is shown. It
takes a ``progress: Progress = NULL_PROGRESS`` argument and opens tasks on it:

    with progress.task("correcting segments", total=total) as bar:
        for n in range(total):
            ...
            bar.advance()

The default, :data:`NULL_PROGRESS`, does nothing, so tests and scripts pay no
cost and need no terminal. The CLIs pass a ``rich``-backed implementation
(``RichProgress`` in ``tapewyrm.console`` and ``qicsilver.console``) when
``--progress`` is given. Keeping this a :class:`typing.Protocol` is what lets
the libraries stay free of any import of ``rich``: the presentation
dependency lives only in the CLIs.

Units: a task's ``unit`` is a display hint only -- ``"bytes"`` renders the
amount as a file size, ``"s"`` as seconds, anything else as ``M/N unit``.
``total=None`` means "unknown" (a capture runs until the tape stops), which a
renderer shows as an indeterminate bar with a running count.

Log lines (``logging``) and progress are separate channels: logs say what
happened, progress says how far along it is. A renderer is expected to keep
both legible on the same terminal (rich draws log lines above live bars).

Across processes: a loop that runs in a worker (``tw convert -j``) cannot
draw on the parent's terminal, and a renderer cannot be pickled into it. The
worker gets a :class:`QueueProgress`, whose tasks are messages on a
``multiprocessing`` queue, and the parent replays them onto its real
renderer with a :class:`ProgressRelay`; see those classes.
"""

from __future__ import annotations

import itertools
import logging
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from typing import Any, Protocol

log = logging.getLogger(__name__)


class ProgressTask(Protocol):
    """One bar: a single loop's position."""

    def advance(self, amount: float = 1) -> None:
        """Move forward by ``amount`` units."""
        ...

    def update(self, completed: float) -> None:
        """Set the absolute position (for time-driven loops)."""
        ...


class Progress(Protocol):
    """A sink for progress tasks; see the module docstring."""

    def task(
        self, description: str, total: float | None = None, unit: str = ""
    ) -> AbstractContextManager[ProgressTask]:
        """Open a task for the duration of a ``with`` block."""
        ...


class _NullTask:
    """A task that ignores everything."""

    def advance(self, amount: float = 1) -> None:
        pass

    def update(self, completed: float) -> None:
        pass


class NullProgress:
    """The default :class:`Progress`: shows nothing, costs nothing."""

    @contextmanager
    def task(
        self, description: str, total: float | None = None, unit: str = ""
    ) -> Iterator[ProgressTask]:
        yield _NullTask()


NULL_PROGRESS: Progress = NullProgress()


# ---------------------------------------------------------------------------
# Progress across processes
# ---------------------------------------------------------------------------
#
# The wire between a QueueProgress (worker) and a ProgressRelay (parent) is
# three kinds of tuple:
#
#     ("open",   key, description, total, unit)
#     ("update", key, completed)
#     ("close",  key)
#
# ``key`` is ``(origin, n)``: the worker's origin (a number the parent chose,
# so two workers never collide) and a per-worker counter. Positions travel
# as absolute values, never deltas, so the sender may coalesce as many
# advances as it likes and the receiver still lands on the right number.
#
# And coalesce it must: the MFM framing stage advances once per sync run,
# ~19,000 times a track, and each put on a multiprocessing queue is a pickle
# and a pipe write. A task's position is flushed at most every _FLUSH_S,
# which is already faster than rich redraws, plus once at close so the bar
# ends where the loop did.

_FLUSH_S = 0.1


class _QueueTask:
    """One task of a :class:`QueueProgress`: position kept here, flushed on a timer."""

    def __init__(self, queue: Any, key: tuple[int, int], clock: Callable[[], float]) -> None:
        self._queue = queue
        self._key = key
        self._clock = clock
        self._position = 0.0
        self._flushed_at = clock()
        self._dirty = False

    def advance(self, amount: float = 1) -> None:
        self._position += amount
        self._flush_if_due()

    def update(self, completed: float) -> None:
        self._position = completed
        self._flush_if_due()

    def _flush_if_due(self) -> None:
        now = self._clock()
        if now - self._flushed_at < _FLUSH_S:
            self._dirty = True
            return
        self._flush(now)

    def _flush(self, now: float) -> None:
        self._queue.put(("update", self._key, self._position))
        self._flushed_at = now
        self._dirty = False

    def close(self) -> None:
        """Send the final position if it moved since the last flush, then the close."""
        if self._dirty:
            self._flush(self._clock())
        self._queue.put(("close", self._key))


class QueueProgress:
    """A :class:`Progress` whose tasks are messages on ``queue`` (see the module docstring).

    ``origin`` tells the parent's relay which worker a task belongs to; give
    each worker its own. ``prefix`` is put in front of every description
    (the capture's file name, so bars from several workers stay
    attributable). ``clock`` is for tests; it is :func:`time.monotonic`.
    """

    def __init__(
        self,
        queue: Any,
        *,
        origin: int = 0,
        prefix: str = "",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._queue = queue
        self._origin = origin
        self._prefix = prefix
        self._clock = clock
        self._serial = itertools.count()

    @contextmanager
    def task(
        self, description: str, total: float | None = None, unit: str = ""
    ) -> Iterator[ProgressTask]:
        key = (self._origin, next(self._serial))
        self._queue.put(("open", key, self._prefix + description, total, unit))
        task = _QueueTask(self._queue, key, self._clock)
        try:
            yield task
        finally:
            task.close()


class ProgressRelay:
    """Replays :class:`QueueProgress` messages onto ``progress`` in this process.

    Feed it every message read from the queue with :meth:`handle`; each
    "open" opens a task on ``progress`` that stays open until its "close".
    Call :meth:`close` when the workers are gone: a worker that died
    mid-loop never sent its close, and its bar would otherwise stay up.
    """

    def __init__(self, progress: Progress) -> None:
        self._progress = progress
        self._open: dict[tuple[int, int], tuple[ExitStack, ProgressTask]] = {}

    def handle(self, message: tuple) -> None:
        kind, key = message[0], message[1]
        if kind == "open":
            _, _, description, total, unit = message
            if key in self._open:
                log.debug("progress task %r opened twice; closing the earlier one", key)
                self._close(key)
            stack = ExitStack()
            bar = stack.enter_context(self._progress.task(description, total=total, unit=unit))
            self._open[key] = (stack, bar)
        elif kind == "update":
            entry = self._open.get(key)
            if entry is None:
                log.debug("progress update for unknown task %r ignored", key)
                return
            entry[1].update(message[2])
        elif kind == "close":
            if key not in self._open:
                log.debug("progress close for unknown task %r ignored", key)
                return
            self._close(key)
        else:
            log.debug("progress message of unknown kind %r ignored", kind)

    def _close(self, key: tuple[int, int]) -> None:
        stack, _ = self._open.pop(key)
        stack.close()

    def close(self) -> None:
        """Close every task still open (none, unless a worker died mid-loop)."""
        if self._open:
            log.debug("%d progress tasks left open by their workers; closing them", len(self._open))
        for key in list(self._open):
            self._close(key)
