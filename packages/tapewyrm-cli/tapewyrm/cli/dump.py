"""``tw dump``: capture whole tracks to TWRF flux files (DESIGN.md §6A.7).

The first step of the recovery workflow; ``tw convert`` (tapewyrm/cli/convert.py)
is the second.
"""

from __future__ import annotations

import logging
from pathlib import Path

import rich_click as click

from tapewyrm.cli.app import AppContext, cli
from tapewyrm.cli.session import _drive_session

log = logging.getLogger(__name__)


def _parse_tracks(spec: str) -> list[int]:
    """'0-12' or '0,3,5-7' -> sorted unique track numbers (each 0..63)."""
    out: set[int] = set()
    for part in spec.split(","):
        a, _, b = part.strip().partition("-")
        lo, hi = int(a), int(b or a)
        if not 0 <= lo <= hi <= 63:
            log.debug("track range %r -> %d..%d outside 0..63 or reversed; refusing", part, lo, hi)
            raise click.BadParameter(f"bad track range {part!r}", param_hint="TRACKS")
        out.update(range(lo, hi + 1))
    return sorted(out)


# Positional-argument convention for every command (tw and qicsilver alike):
# required things are positional, inputs come first and the output comes last,
# like cp(1): `tw convert SOURCE... OUTPUT`, `qicsilver extract IMAGE OUTDIR`,
# `qicsilver tar VOLUME OUTPUT`. `tw dump` reads from the drive, so its only
# path is its output, and the optional TRACKS selector follows it (an optional
# positional can only come last). Options with defaults stay options.
@cli.command()
@click.argument("outdir", metavar="OUTDIR", type=click.Path(file_okay=False, path_type=Path))
@click.argument("tracks", metavar="[TRACKS]", required=False, default=None)
@click.option(
    "--check", is_flag=True, help="also decode each pass and stop if < 80% of sectors CRC-clean"
)
@click.option(
    "--retries",
    metavar="N",
    type=click.IntRange(min=0),
    default=0,
    show_default=True,
    help=(
        "re-read a track up to N more times when its pass failed: the capture raised a "
        "link or drive error, the pass did not end at EOT, the stream did not verify, "
        "the drive found fewer segments than the best track so far, or any segment was "
        "missed inside the pass (missing_est > 0); the best pass is kept"
    ),
)
@click.option(
    "--no-clobber",
    is_flag=True,
    help=(
        "resume a dump of OUTDIR: skip a track whose last dump.jsonl line is clean and "
        "whose file is whole; a track dumped with a failed pass is read again, and its "
        "earlier capture is replaced only by a better pass"
    ),
)
@click.pass_obj
def dump(
    app: AppContext,
    outdir: Path,
    tracks: str | None,
    check: bool,
    retries: int,
    no_clobber: bool,
) -> None:
    """Capture whole tracks to TWRF files in OUTDIR, one Logical Forward pass each.

    TRACKS is a list of tracks and ranges such as 0-12 or 0,2,5-7. Without it,
    every track of the tape is dumped: the count comes from the format the
    drive reports (QIC-80: 0-27), and the dump refuses to start if the drive
    can't say. Each pass winds to its track's starting end, then ends when the tape stops
    at logical EOT. Dump only reads transitions; `tw convert` judges the data.
    After every pass it checks, without decoding, that the pass ended cleanly
    and the drive found as many segments as before, and stops early if the
    tape looks unhealthy. --check also decodes and checks sector CRCs.
    --retries N re-reads a track whose pass failed those checks (or skipped
    any segment, or raised) up to N more times first, keeping the best pass;
    dump.jsonl records every attempt. --no-clobber resumes a dump that
    stopped: tracks already dumped clean into OUTDIR are skipped, and a
    track dumped with a failed pass is read again, its earlier capture
    kept unless the new pass is better.
    Serpentine order: run tracks in ascending order to avoid rewinds.
    """
    from tapewyrm.tape.dump import DumpStopped, dump_tracks

    # Parse TRACKS before touching the drive so a typo fails fast.
    track_list = _parse_tracks(tracks) if tracks is not None else None
    log.debug(
        "dumping tracks %s to %s (check=%s, retries=%d, no_clobber=%s)",
        track_list or "all",
        outdir,
        check,
        retries,
        no_clobber,
    )
    with _drive_session(app) as d, app.progress() as prog:
        try:
            results = dump_tracks(
                d,
                outdir,
                track_list,
                progress=prog,
                check=check,
                retries=retries,
                no_clobber=no_clobber,
            )
        except DumpStopped as exc:
            log.debug("dump stopped: %r", exc)
            raise click.ClickException(f"dump stopped: {exc}") from exc
    segments = sum(r.index_pulses for r in results)
    line = f"done: {len(results)} tracks, {segments} segments by INDEX"
    if check:
        good = sum(r.good or 0 for r in results)
        total = sum(r.sectors or 0 for r in results)
        line += f", {good}/{total} sectors CRC-clean"
    re_read = sum(1 for r in results if r.attempts > 1)
    if re_read:
        line += f", {re_read} re-read"
    click.echo(f"{line} -> {outdir}")
