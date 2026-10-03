"""qicsilver: what is on a QIC tape image, and getting the files back out.

Works on TWTI tape images (``tw convert`` output); no hardware, no flux. The
steps after ``tw dump -> tw convert``:

    identify  TWTI image -> cartridge, header, dates, bad sectors, volumes
    extract   TWTI image -> one TWVL file per backup volume (QIC-122 decoded,
              holes recorded)
    tar       TWVL volume -> pax tar of the backup's files + a damage report

Output channels and flags follow ``tw`` exactly (STYLE.md §2, §2.5): results
and ``--json`` on stdout; logs and ``--progress`` bars on stderr through one
rich console (:mod:`qicsilver.console`); ``-v`` / ``-q`` move the log level.
``click`` here is ``rich_click``.
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

from qicsilver.console import make_console, progress_display, setup_logging

log = logging.getLogger(__name__)


@dataclass
class AppContext:
    """Per-run state shared by subcommands (the same shape as ``tw``'s)."""

    # stderr console shared by logging and the progress bars (qicsilver.console)
    console: Console = field(default_factory=make_console)
    show_progress: bool = False

    @contextmanager
    def progress(self) -> Iterator[Progress]:
        """Live progress bars for one command if ``--progress``, else a no-op."""
        with progress_display(self.console, self.show_progress) as prog:
            yield prog


@click.group()
@click.version_option(package_name="qicsilver", prog_name="qicsilver")
@click.option("--progress", "show_progress", is_flag=True, help="show progress bars (stderr)")
@click.option("-v", "--verbose", count=True, help="more log output (-v for debug)")
@click.option("-q", "--quiet", count=True, help="less log output (-q warnings, -qq errors)")
@click.pass_context
def cli(ctx: click.Context, show_progress: bool, verbose: int, quiet: int) -> None:
    """qicsilver: read QIC tape images (TWTI) and recover the files on them."""
    app = AppContext(show_progress=show_progress)
    setup_logging(app.console, verbose, quiet)
    ctx.obj = app


_VOLUME_PROFILE_HELP = "volume-table layout: a profile name or path, or 'guess' to try them all"


@cli.command()
@click.argument("image", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--json", "as_json", is_flag=True, help="print machine-readable JSON")
@click.option("--volume-profile", default="guess", show_default=True, help=_VOLUME_PROFILE_HELP)
# Not -v/--verbose: that is the global log-level flag on `qicsilver` itself.
@click.option("--raw", is_flag=True, help="also dump raw records and profile scoring")
@click.pass_obj
def identify(app: AppContext, image: Path, as_json: bool, volume_profile: str, raw: bool) -> None:
    """What is on a tape: cartridge, factory stamp, dates, bad sectors, volumes.

    IMAGE is a TWTI tape image. To identify raw captures, `tw convert` them
    first; only the header segment and the volume table at the start of
    track 0 are needed, so a short capture of BOT is enough.
    """
    import json

    from qiclib.identify import format_info, identify, to_dict
    from qiclib.volume_profile import VolumeProfileError

    log.debug("identifying %s (volume profile %r)", image, volume_profile)
    try:
        info = identify(image, volume_profile=volume_profile)
    except (ValueError, VolumeProfileError) as exc:
        log.debug("identify failed: %r", exc)
        raise click.ClickException(str(exc)) from exc
    if as_json:
        click.echo(json.dumps(to_dict(info), indent=1))
    else:
        for line in format_info(info, verbose=raw):
            click.echo(line)


@cli.command()
@click.argument("image", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "-o", "--out", "out", required=True, type=click.Path(file_okay=False, path_type=Path),
    help="directory for the volume files (vol-NN.twvl)",
)  # fmt: skip
@click.option("--volume-profile", default="guess", show_default=True, help=_VOLUME_PROFILE_HELP)
@click.pass_obj
def extract(app: AppContext, image: Path, out: Path, volume_profile: str) -> None:
    """TWTI tape image -> one TWVL file per backup volume.

    Reads the volume table through the volume profile `qicsilver identify` would
    pick, decompresses QIC-122 data and lays each volume out by its QIC-113
    offsets, recording the byte ranges that were lost. Then `qicsilver tar`.
    """
    from qiclib.extract import extract as do_extract
    from qiclib.volume_profile import VolumeProfileError

    log.debug("extracting %s to %s (volume profile %r)", image, out, volume_profile)
    try:
        with app.progress() as prog:
            written = do_extract(image, out, volume_profile=volume_profile, progress=prog)
    except (ValueError, VolumeProfileError) as exc:
        log.debug("extract failed: %r", exc)
        raise click.ClickException(str(exc)) from exc
    for path in written:
        click.echo(path)


@cli.command()
@click.argument("volume", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "-o", "--out", "out", required=True, type=click.Path(dir_okay=False, path_type=Path),
    help="tar file to write",
)  # fmt: skip
@click.option(
    "--report", type=click.Path(dir_okay=False, path_type=Path), default=None,
    help="damage report (default: OUT.damaged.txt)",
)  # fmt: skip
@click.option("--skip-damaged", is_flag=True, help="leave damaged files out of the tar")
@click.pass_obj
def tar(app: AppContext, volume: Path, out: Path, report: Path | None, skip_damaged: bool) -> None:
    """TWVL volume -> pax tar of the backup's files, plus a damage report.

    Keeps the backup's own paths (long Windows 95 names), modification times
    and DOS attributes (pax header TAPEWYRM.dos_attributes). Files with bytes
    in unrecovered segments are written zero-filled unless --skip-damaged.
    """
    from qicsilver.tar import write_tar

    log.debug("tar %s -> %s", volume, out)
    try:
        with app.progress() as prog:
            res = write_tar(
                volume, out, report_path=report, skip_damaged=skip_damaged, progress=prog
            )
    except ValueError as exc:
        log.debug("tar failed: %r", exc)
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"{res.out}: {res.files} files, {res.dirs} directories, "
        f"{len(res.damaged)} damaged (report: {res.report})"
    )
