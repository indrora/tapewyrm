"""Progress reporting hooks for long-running library loops (STYLE.md §2.5).

Library code (``tape.dump``, ``image.twti``, ``image.twvl``, ...) wants to say
"I am 40 of 207 segments into this" without knowing *how* that is shown. It
takes a ``progress: Progress = NULL_PROGRESS`` argument and opens tasks on it:

    with progress.task("correcting segments", total=total) as bar:
        for n in range(total):
            ...
            bar.advance()

The default, :data:`NULL_PROGRESS`, does nothing, so tests and scripts pay no
cost and need no terminal. The CLI passes a ``rich``-backed implementation
(``tapewyrm.console.RichProgress``) when ``tw --progress`` is given. Keeping this
a :class:`typing.Protocol` is what lets the library stay free of any import of
``rich``: the presentation dependency lives only in the CLI.

Units: a task's ``unit`` is a display hint only -- ``"bytes"`` renders the
amount as a file size, ``"s"`` as seconds, anything else as ``M/N unit``.
``total=None`` means "unknown" (a capture runs until the tape stops), which a
renderer shows as an indeterminate bar with a running count.

Log lines (``logging``) and progress are separate channels: logs say what
happened, progress says how far along it is. A renderer is expected to keep
both legible on the same terminal (rich draws log lines above live bars).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Protocol


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
