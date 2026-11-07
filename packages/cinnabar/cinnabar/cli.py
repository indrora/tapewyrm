"""cinnabar: look at a tape capture or image, lane by lane.

    cinnabar PATH            open a viewer window on PATH
    cinnabar PATH --dump     print one track's lanes as text instead (no Qt)

PATH is a TWRF flux capture, a directory of them (one per track), or a
TWTI/TWTZ tape image. Output channels and flags follow ``tw`` (STYLE.md
section 2, 2.5): ``--dump`` on stdout; logs and ``--progress`` bars on
stderr through one rich console (:mod:`cinnabar.console`); ``-v`` / ``-q``
move the log level. ``click`` here is ``rich_click``. Qt is imported only
when a window is actually opened, so ``--dump`` and ``--help`` stay quick.

The volume-read flags (``--volume-profile`` and the hand-tuning flags) are
``qicsilver``'s, by the same decorator, so the viewer can be pointed at a
tape exactly as ``qicsilver extract`` would be and label what it extracts.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import rich_click as click
from qiclib.volume import fileset as fs
from qiclib.volume.profile import FIELD_NAMES
from rich.console import Console
from tapewyrm_archive.progress import Progress

from cinnabar.annotate import ordered_rows
from cinnabar.console import make_console, progress_display, setup_logging
from cinnabar.model import VOLUME_BASE, Track, in_row, rows_of

if TYPE_CHECKING:
    from qiclib.volume.profile import Overrides

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


_VOLUME_PROFILE_HELP = "volume-table layout: a profile name or path, or 'guess' to try them all"


def _overrides(
    fileset_parser: str | None,
    fileset_param: tuple[str, ...],
    extent_offset_bytes: str | None,
    extent_frame_size_bytes: str | None,
    vtbl_field: tuple[str, ...],
) -> Overrides | None:
    """The hand-tuning flags as one ``qiclib.volume.profile.Overrides``; None when none was given.

    A value that is wrong on its face (a parser nobody registered, a field
    outside bytes 57-127, ``NAME=OFFSET`` with no length) is a usage error
    here, before any file is opened. One only the tape can judge (a
    parameter the chosen profile's parser does not take) surfaces as the
    command's VolumeProfileError, exit 1.
    """
    from qiclib.volume.profile import Overrides as _Overrides
    from qiclib.volume.profile import VolumeProfileError

    if not (
        fileset_parser
        or fileset_param
        or extent_offset_bytes
        or extent_frame_size_bytes
        or vtbl_field
    ):
        return None
    try:
        return _Overrides.from_strings(
            fileset_parser=fileset_parser,
            fileset_params=fileset_param,
            extent_offset_bytes=int(extent_offset_bytes) if extent_offset_bytes else None,
            extent_frame_size_bytes=int(extent_frame_size_bytes)
            if extent_frame_size_bytes
            else None,
            vtbl_fields=vtbl_field,
        )
    except VolumeProfileError as exc:
        log.debug("hand-tuning flags refused: %s", exc)
        raise click.UsageError(str(exc)) from exc


def volume_read_options(f: Callable[..., Any]) -> Callable[..., Any]:
    """``--volume-profile`` and the hand-tuning flags, delivered as ``volume_profile`` and ``overrides``.

    A copy of qicsilver's decorator of the same name, kept identical by
    hand like console.py (cinnabar may not import qicsilver, STYLE.md
    section 2): the same flags with the same words on both CLIs. The five
    override flags are folded into one ``qiclib.volume.profile.Overrides``
    (:func:`_overrides`) before the command runs; the command sees
    ``overrides`` (or None) next to the ``volume_profile`` string and hands
    both to the loaders, which lay the overrides on whichever profile reads
    the tape, forced or guessed (DESIGN.md section 7.8). A hand-tuned read
    is named ``<profile>+overrides`` and carries the ``manual`` warning
    wherever the profile's name goes: the tape's rows, the details panel's
    amber line, ``--dump``.
    """

    @click.option("--volume-profile", default="guess", show_default=True, help=_VOLUME_PROFILE_HELP)
    @click.option(
        "--fileset-parser", metavar="NAME", default=None,
        help=f"read the volumes with this file-set parser instead of the profile's "
        f"({', '.join(fs.names())})",
    )  # fmt: skip
    @click.option(
        "--fileset-param", metavar="KEY=VALUE", multiple=True,
        help="a parameter of that parser (repeatable)",
    )  # fmt: skip
    @click.option(
        "--extent-offset-bytes", type=click.Choice(["4", "8"]), default=None,
        help="width of the uncompressed-offset field heading each QIC-122 extent",
    )  # fmt: skip
    @click.option(
        "--extent-frame-size-bytes", type=click.Choice(["2", "4"]), default=None,
        help="width of each compression frame's size field",
    )  # fmt: skip
    @click.option(
        "--vtbl-field", metavar="NAME=OFFSET,LENGTH", multiple=True,
        help=f"place a VTBL field past byte 56 by hand (repeatable; "
        f"{', '.join(sorted(FIELD_NAMES))})",
    )  # fmt: skip
    @functools.wraps(f)
    def wrapper(
        *args: Any,
        fileset_parser: str | None,
        fileset_param: tuple[str, ...],
        extent_offset_bytes: str | None,
        extent_frame_size_bytes: str | None,
        vtbl_field: tuple[str, ...],
        **kwargs: Any,
    ) -> Any:
        kwargs["overrides"] = _overrides(
            fileset_parser, fileset_param, extent_offset_bytes, extent_frame_size_bytes, vtbl_field
        )
        return f(*args, **kwargs)

    return wrapper


def dump_track(track: Track, *, segment: int | None = None) -> str:
    """One track as text: every lane of every (or one) segment, one span per line."""
    lines = [f"{track.label}: {track.axis.kind.value} axis, {len(track.segments)} segments"]
    lines += [f"  {k}: {v}" for k, v in track.detail]
    fmt = track.axis.format
    for seg in track.segments:
        if segment is not None and seg.index != segment:
            continue
        lines.append(f"  {seg.title} [{fmt(seg.start)} .. {fmt(seg.end)}] {seg.state}")
        if track.flux is not None:
            transitions = track.flux.transition_count(seg.start, seg.end)
            cells = track.flux.cell_count_between(seg.start, seg.end)
            lines.append(f"    flux: {transitions:,} transitions, {cells:,} cells")
        spans = seg.records + seg.fields
        for row in ordered_rows(rows_of(spans)):
            for s in in_row(spans, row):
                lines.append(
                    f"    {row:24} [{fmt(s.start)} .. {fmt(s.end)}] {s.label} {s.sublabel}".rstrip()
                )
        total = sum(len(r.data) for r in seg.byte_runs)
        lines.append(f"    Bytes: {total:,} in {len(seg.byte_runs)} runs")
    return "\n".join(lines)


@click.command()
@click.version_option(package_name="cinnabar", prog_name="cinnabar")
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.option("--track", "track_number", type=int, default=None, help="track to open first")
@click.option(
    "--volume",
    "volume_index",
    type=int,
    default=None,
    help="open volume N's decompressed stream (its index in the volume table) instead of a track",
)
@volume_read_options
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
    volume_index: int | None,
    volume_profile: str,
    overrides: Overrides | None,
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
    """cinnabar: look at a QIC tape capture (TWRF) or image (TWTI/TWTZ), lane by lane.

    The volume table is read through the volume profile that fits best
    (--volume-profile forces one), as `qicsilver identify` reads it.
    --fileset-parser, --fileset-param, --extent-offset-bytes,
    --extent-frame-size-bytes and --vtbl-field override that profile's
    layers by hand, for a tape no profile fits yet: the volume lanes are then
    laid out as given, the tape's rows name the profile as NAME+overrides,
    and the warning `manual` shows on the details panel.
    """
    app = AppContext(show_progress=show_progress)
    setup_logging(app.console, verbose, quiet)
    from qiclib.volume.profile import VolumeProfileError

    from cinnabar.load import open_path

    log.debug("opening %s (volume profile %r, overrides %s)", path, volume_profile, overrides)
    try:
        with app.progress() as prog:
            tape = open_path(path, prog, volume_profile=volume_profile, overrides=overrides)
    except (ValueError, VolumeProfileError) as exc:
        log.debug("opening %s failed: %r", path, exc)
        raise click.ClickException(str(exc)) from exc
    first = track_number if track_number is not None else tape.track_numbers[0]
    if volume_index is not None:
        first = VOLUME_BASE + volume_index
        if first not in tape.track_numbers and tape.kind == "TWRF":
            # A flux tape's volumes appear once track 0 has decoded and identified it.
            with app.progress() as prog:
                track0 = tape.load(tape.track_numbers[0], prog)
                if track0.decoder is not None:
                    track0.decoder.wait()
    if first not in tape.track_numbers:
        raise click.BadParameter(f"{tape.label(first)} is not in {path}", param_hint="--track")
    if dump:
        with app.progress() as prog:
            track = tape.load(first, prog)
            if track.decoder is not None:
                track.decoder.wait()
        # The tape's rows first (a flux tape's are complete once track 0 has
        # decoded and identified it), then the track's and its lanes.
        click.echo("\n".join(f"{k}: {v}" for k, v in tape.detail))
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
