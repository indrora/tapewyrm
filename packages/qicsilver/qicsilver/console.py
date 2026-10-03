"""Terminal presentation for ``qicsilver``: logging, progress bars (STYLE.md §2.5).

A deliberate copy of ``tapewyrm/console.py`` (tapewyrm-cli), so both CLIs look
and behave the same without either depending on the other. Keep the two in
step: a change here belongs there too, and vice versa. Only ``_OUR_LOGGERS``
differs.

This is the only module (with ``cli``) that imports ``rich``. The library logs
through :mod:`logging` and reports progress through :mod:`tapewyrm.progress`;
here both are pointed at one stderr :class:`rich.console.Console`.

Why one shared console: rich's live display (the progress bars) redraws the
bottom of the terminal. Anything printed through the *same* console while it
is live is inserted above the bars; anything printed around it (``print``,
another console) tears the display. So the log handler and the progress
renderer must share it.

Why stderr: stdout is the data channel -- ``qicsilver identify --json``, status
tables, summaries -- and must stay pipeable. Logs and bars are diagnostics.

Verbosity: ``-v`` / ``-q`` move the ``tapewyrm`` logger's level around INFO,
which is where the library's narrative ("track 3: capturing -> ...") lives.
Third-party loggers stay at WARNING so ``-v`` doesn't turn on their noise.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from rich.console import Console
from rich.filesize import decimal
from rich.logging import RichHandler
from rich.progress import (
    BarColumn,
    ProgressColumn,
    SpinnerColumn,
    Task,
    TaskID,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.progress import Progress as RichLive
from rich.text import Text
from tapewyrm_archive.progress import NULL_PROGRESS, Progress, ProgressTask

# Every package whose library code this CLI runs. Each logs under its own
# top-level name (logging.getLogger(__name__)), so each needs the handler: a
# package left out here goes silent below WARNING. tapewyrm-cli's copy of this
# module lists its own set.
_OUR_LOGGERS = ("qicsilver", "qiclib", "tapewyrm_archive")

# -v/-q steps, centred on INFO (index 2): -qq ERROR ... -v DEBUG.
_LEVELS = [logging.ERROR, logging.WARNING, logging.INFO, logging.DEBUG]


def make_console() -> Console:
    """The stderr console shared by the log handler and the progress bars."""
    return Console(stderr=True)


def setup_logging(console: Console, verbose: int, quiet: int) -> None:
    """Route our packages' loggers (``_OUR_LOGGERS``) to ``console`` at a level set by -v/-q.

    Only our own loggers are configured (not the root), so importing these
    packages as libraries never hijacks a host application's logging. At -v the
    handler also shows the emitting module, which is what you want when
    chasing a bench problem.
    """
    level = _LEVELS[max(0, min(len(_LEVELS) - 1, 2 + verbose - quiet))]
    handler = RichHandler(
        console=console,
        show_time=True,
        show_path=level <= logging.DEBUG,
        markup=False,  # log text is data (paths, tape names), never rich markup
        rich_tracebacks=True,
        log_time_format="[%X]",
    )
    for name in _OUR_LOGGERS:
        logger = logging.getLogger(name)
        logger.handlers[:] = [handler]
        logger.setLevel(level)
        logger.propagate = False


class _AmountColumn(ProgressColumn):
    """``completed/total unit``, with bytes as file sizes and unknown totals as a count.

    rich's stock columns pick one rendering for the whole display; tasks here
    mix units (tracks, bytes, seconds), so the unit rides on each task's
    ``fields`` and this column renders per task.
    """

    def render(self, task: Task) -> Text:
        unit = task.fields.get("unit", "")
        done, total = task.completed, task.total
        if unit == "bytes":
            text = decimal(int(done)) + (f"/{decimal(int(total))}" if total else "")
        elif unit == "s":
            text = f"{done:,.0f}" + (f"/{total:,.0f}" if total else "") + " s"
        else:
            text = (
                f"{done:,.0f}" + (f"/{total:,.0f}" if total else "") + (f" {unit}" if unit else "")
            )
        return Text(text, style="progress.download")


class _RichTask:
    """:class:`tapewyrm.progress.ProgressTask` over one rich task id."""

    def __init__(self, live: RichLive, task_id: TaskID) -> None:
        self._live = live
        self._id = task_id

    def advance(self, amount: float = 1) -> None:
        self._live.advance(self._id, amount)

    def update(self, completed: float) -> None:
        self._live.update(self._id, completed=completed)


class RichProgress:
    """:class:`tapewyrm.progress.Progress` drawn as rich bars on ``console``.

    Tasks are removed when their ``with`` block ends: nested per-item bars
    (one per track, one per volume) would otherwise pile up for the whole
    run. The log line each loop emits on completion is the permanent record.
    """

    def __init__(self, live: RichLive) -> None:
        self._live = live

    @contextmanager
    def task(
        self, description: str, total: float | None = None, unit: str = ""
    ) -> Iterator[ProgressTask]:
        task_id = self._live.add_task(description, total=total, unit=unit)
        try:
            yield _RichTask(self._live, task_id)
        finally:
            self._live.remove_task(task_id)


@contextmanager
def progress_display(console: Console, enabled: bool) -> Iterator[Progress]:
    """Yield a live :class:`RichProgress` if ``enabled``, else the no-op one.

    Bars are also suppressed when stderr is not a terminal (a log file, CI):
    rich would otherwise print one frame per refresh into it.
    """
    if not (enabled and console.is_terminal):
        yield NULL_PROGRESS
        return
    live = RichLive(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        _AmountColumn(),
        TimeElapsedColumn(),
        console=console,
    )
    with live:
        yield RichProgress(live)
