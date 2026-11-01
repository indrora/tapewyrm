"""cinnabar: look at a tape capture or image, lane by lane.

    cinnabar PATH            open a viewer window on PATH
    cinnabar PATH --dump     print one track's lanes as text instead (no Qt)

PATH is a TWRF flux capture, a directory of them (one per track), or a
TWTI/TWTZ tape image. Output channels and flags follow ``tw`` (STYLE.md
section 2, 2.5): ``--dump`` on stdout; logs and ``--progress`` bars on
stderr through one rich console (:mod:`cinnabar.console`); ``-v`` / ``-q``
move the log level. ``click`` here is ``rich_click``. Qt is imported only
when a window is actually opened, so ``--dump`` and ``--help`` stay quick.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import rich_click as click
from rich.console import Console
from tapewyrm_archive.progress import Progress

from cinnabar.console import make_console, progress_display, setup_logging
from cinnabar.model import Track, depth, level

log = logging.getLogger(__name__)


@dataclass
class AppContext:
    """Per-run state (the same shape as ``tw``'s and ``qicsilver``'s)."""

    console: Console = field(default_factory=make_console)
    show_progress: bool = False

    @contextmanager
    def progress(self) -> Iterator[Progress]:
        with progress_display(self.console, self.show_progress) as prog:
            yield prog


def dump_track(track: Track, *, segment: int | None = None) -> str:
    """One track as text: every lane of every (or one) segment, one span per line."""
    lines = [f"{track.label}: {track.axis.kind.value} axis, {len(track.segments)} segments"]
    fmt = track.axis.format
    for seg in track.segments:
        if segment is not None and seg.index != segment:
            continue
        lines.append(f"  {seg.title} [{fmt(seg.start)} .. {fmt(seg.end)}] {seg.state}")
        if track.flux is not None:
            transitions = track.flux.transition_count(seg.start, seg.end)
            cells = track.flux.cell_count_between(seg.start, seg.end)
            lines.append(f"    flux: {transitions:,} transitions, {cells:,} cells")
        for lvl in range(depth(seg.records)):
            name = f"{track.physical} Records" if lvl == 0 else f"{track.physical} Fields"
            for s in level(seg.records, lvl):
                lines.append(
                    f"    {name:22} [{fmt(s.start)} .. {fmt(s.end)}] {s.label} {s.sublabel}".rstrip()
                )
        for lvl in range(depth(seg.fields)):
            for s in level(seg.fields, lvl):
                lines.append(
                    f"    Fields {lvl:<15} [{fmt(s.start)} .. {fmt(s.end)}] {s.label} {s.sublabel}".rstrip()
                )
        total = sum(len(r.data) for r in seg.byte_runs)
        lines.append(f"    Bytes: {total:,} in {len(seg.byte_runs)} runs")
    return "\n".join(lines)


@click.command()
@click.version_option(package_name="cinnabar", prog_name="cinnabar")
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.option("--track", "track_number", type=int, default=None, help="track to open first")
@click.option("--segment", type=int, default=None, help="with --dump: only this segment")
@click.option("--dump", is_flag=True, help="print the track's lanes as text and exit (no window)")
@click.option(
    "--screenshot",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="render the window offscreen to this PNG and exit",
)
@click.option(
    "--theme",
    type=click.Choice(["sketch", "paper", "dark", "contrast"]),
    default="sketch",
    show_default=True,
    help="colour scheme",
)
@click.option(
    "--window",
    type=str,
    default=None,
    help="with --screenshot: the view range to show, as START:END in seconds or bytes",
)
@click.option(
    "--click",
    "click_at",
    type=float,
    default=None,
    help="with --screenshot: open the details panel on the record at this axis position",
)
@click.option("--progress", "show_progress", is_flag=True, help="show progress bars (stderr)")
@click.option("-v", "--verbose", count=True, help="more log output (-v for debug)")
@click.option("-q", "--quiet", count=True, help="less log output (-q warnings, -qq errors)")
def cli(
    path: Path,
    track_number: int | None,
    segment: int | None,
    dump: bool,
    screenshot: Path | None,
    theme: str,
    window: str | None,
    click_at: float | None,
    show_progress: bool,
    verbose: int,
    quiet: int,
) -> None:
    """cinnabar: look at a QIC tape capture (TWRF) or image (TWTI/TWTZ), lane by lane."""
    app = AppContext(show_progress=show_progress)
    setup_logging(app.console, verbose, quiet)
    from cinnabar.load import open_path

    tape = open_path(path)
    first = track_number if track_number is not None else tape.track_numbers[0]
    if first not in tape.track_numbers:
        raise click.BadParameter(f"track {first} is not in {path}", param_hint="--track")
    if dump:
        with app.progress():
            track = tape.load(first)
            if track.decoder is not None:
                track.decoder.wait()
        click.echo(dump_track(track, segment=segment))
        return
    if screenshot is not None:
        from cinnabar.ui.app import screenshot as render

        rng = None
        if window:
            a, b = window.split(":")
            rng = (float(a), float(b))
        render(
            tape, first, screenshot, segment=segment or 0, theme=theme, window=rng, click=click_at
        )
        return
    from cinnabar.ui.app import run

    run(tape, first, theme=theme)
