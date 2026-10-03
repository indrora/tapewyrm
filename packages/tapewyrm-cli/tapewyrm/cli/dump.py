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
@click.pass_obj
def dump(app: AppContext, outdir: Path, tracks: str | None, check: bool) -> None:
    """Capture whole tracks to TWRF files in OUTDIR, one Logical Forward pass each.

    TRACKS is a list of tracks and ranges such as 0-12 or 0,2,5-7. Without it,
    every track of the tape is dumped: the count comes from the format the
    drive reports (QIC-80: 0-27), and the dump refuses to start if the drive
    can't say. Each pass winds to its track's starting end, then ends when the tape stops
    at logical EOT. Dump only reads transitions; `tw convert` judges the data.
    After every pass it checks, without decoding, that the pass ended cleanly
    and the drive found as many segments as before, and stops early if the
    tape looks unhealthy. --check also decodes and checks sector CRCs.
    Serpentine order: run tracks in ascending order to avoid rewinds.
    """
    from tapewyrm.tape.dump import DumpStopped, dump_tracks

    # Parse TRACKS before touching the drive so a typo fails fast.
    track_list = _parse_tracks(tracks) if tracks is not None else None
    log.debug("dumping tracks %s to %s (check=%s)", track_list or "all", outdir, check)
    with _drive_session(app) as d, app.progress() as prog:
        try:
            results = dump_tracks(d, outdir, track_list, progress=prog, check=check)
        except DumpStopped as exc:
            log.debug("dump stopped: %r", exc)
            raise click.ClickException(f"dump stopped: {exc}") from exc
    segments = sum(r.index_pulses for r in results)
    line = f"done: {len(results)} tracks, {segments} segments by INDEX"
    if check:
        good = sum(r.good or 0 for r in results)
        total = sum(r.sectors or 0 for r in results)
        line += f", {good}/{total} sectors CRC-clean"
    click.echo(f"{line} -> {outdir}")
