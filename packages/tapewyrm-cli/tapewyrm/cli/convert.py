"""``tw convert``: TWRF captures -> TWTI logical tape image. No hardware needed."""

from __future__ import annotations

import logging
from pathlib import Path

import rich_click as click

from tapewyrm.cli.app import AppContext, cli

log = logging.getLogger(__name__)


@cli.command()
@click.argument(
    "sources", metavar="SOURCE...", nargs=-1, required=True,
    type=click.Path(exists=True, path_type=Path),
)  # fmt: skip
@click.argument("output", metavar="OUTPUT", type=click.Path(dir_okay=False, path_type=Path))
@click.option(
    "-j",
    "--jobs",
    metavar="N",
    type=click.IntRange(min=0),
    default=1,
    show_default=True,
    help=(
        "decode N captures at once, each in its own process; 0 picks N from the cores "
        "and the RAM (a worker needs about 25 bytes per byte of capture: 2.5 GB for a "
        "100 MB track)"
    ),
)
@click.pass_obj
def convert(app: AppContext, sources: tuple[Path, ...], output: Path, jobs: int) -> None:
    """TWRF dump(s) -> TWTI logical tape image OUTPUT (.twtz: zstd-compressed).

    Each SOURCE is a dump directory (or an individual track-NN.twrf file);
    OUTPUT comes last, as with cp, and its suffix picks the image kind: .twti
    (sparse) or .twtz (zstd-compressed). Every
    capture is decoded at its own recorded bit rate; when several dumps of the
    same tape are given, their sectors are merged so a re-read fills the gaps
    of an earlier pass. No hardware needed. The decode is one core per
    capture: -j N runs N at once, -j 0 as many as the cores and RAM allow.
    """
    from tapewyrm.image.convert import convert as do_convert

    log.debug("converting %d sources to %s with %d job(s)", len(sources), output, jobs)
    try:
        with app.progress() as prog:
            do_convert(list(sources), output, progress=prog, jobs=jobs)
    except ValueError as exc:
        log.debug("convert failed: %r", exc)
        raise click.ClickException(str(exc)) from exc
