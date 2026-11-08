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
@click.option(
    "--best",
    type=click.Choice(["track", "segment", "sector"]),
    default="sector",
    show_default=True,
    help=(
        "where several reads of one track compete (tw dump --keep-all/--passes, or several "
        "dumps): sector uses every CRC-good sector of every read; segment takes each segment "
        "whole from its best read; track takes each track whole from its best read"
    ),
)
@click.option(
    "--rank",
    type=click.Choice(["crc", "rs"]),
    default="crc",
    show_default=True,
    help=(
        "what makes a read best for --best=track/segment: crc counts CRC-clean sectors; rs "
        "first asks whether a segment corrects and its spare Reed-Solomon parity agrees (which "
        "catches a CRC-good sector that is wrong), then counts CRC-clean sectors"
    ),
)
@click.option(
    "--no-vote",
    is_flag=True,
    help=(
        "do not rebuild what no single read got right: by default a sector read CRC-bad 3+ "
        "times is majority-voted byte by byte and kept if its CRC then checks, and a segment "
        "that will not correct is solved one RS column at a time from the bytes its reads "
        "agree on, kept only if every column's spare parity checks"
    ),
)
@click.pass_obj
def convert(
    app: AppContext,
    sources: tuple[Path, ...],
    output: Path,
    jobs: int,
    best: str,
    rank: str,
    no_vote: bool,
) -> None:
    """TWRF dump(s) -> TWTI logical tape image OUTPUT (.twtz: zstd-compressed).

    Each SOURCE is a dump directory (or an individual track-NN.twrf file);
    OUTPUT comes last, as with cp, and its suffix picks the image kind: .twti
    (sparse) or .twtz (zstd-compressed). Every
    capture is decoded at its own recorded bit rate; when several dumps of the
    same tape are given, their sectors are merged so a re-read fills the gaps
    of an earlier pass. No hardware needed. The decode is one core per
    capture: -j N runs N at once, -j 0 as many as the cores and RAM allow.
    --best picks the grain at which several reads of a track compete: by
    sector (the default) every read fills the others' gaps; by segment or by
    track each one comes whole from its best read, ranked by --rank. Where
    several reads all got a sector wrong, a majority vote and a Reed-Solomon
    column solve rebuild it from the parts they got right, each checked
    before it is used (--no-vote turns that off). The log goes track by
    track: which reads each had, what each contributed, and every segment
    that needed a merge, a vote or a rescue, or failed.
    """
    from qiclib.merge import Best, Rank

    from tapewyrm.image.convert import convert as do_convert

    log.debug(
        "converting %d sources to %s with %d job(s), best by %s ranked by %s, vote=%s",
        len(sources),
        output,
        jobs,
        best,
        rank,
        not no_vote,
    )
    if rank != "crc" and best == "sector":
        log.warning("--rank does nothing with --best=sector: every CRC-good sector is used")
    try:
        with app.progress() as prog:
            do_convert(
                list(sources),
                output,
                progress=prog,
                jobs=jobs,
                best=Best(best),
                rank=Rank(rank),
                vote=not no_vote,
            )
    except ValueError as exc:
        log.debug("convert failed: %r", exc)
        raise click.ClickException(str(exc)) from exc
