"""qicsilver: what is on a QIC tape image, and getting the files back out.

Works on TWTI tape images (``tw convert`` output, or its zstd-compressed
.twtz); no hardware, no flux. The steps after ``tw dump -> tw convert``:

    identify  TWTI image -> cartridge, header, dates, bad sectors, volumes
    extract   TWTI image -> one TWVL file per backup volume (QIC-122 decoded,
              holes recorded)
    inspect   TWVL volume, or one volume of a TWTI image (--volume N) -> a
              tar-tv style listing: sizes, times, damage
    tar       TWVL volume, or one volume of a TWTI image (--volume N) -> pax
              tar of the backup's files + a damage report

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
from typing import TYPE_CHECKING

import rich_click as click
from click.core import ParameterSource
from qiclib.extract import DEFAULT_PREFIX
from rich.console import Console
from tapewyrm_archive.progress import Progress

from qicsilver.console import make_console, progress_display, setup_logging

if TYPE_CHECKING:
    # An annotation only: qicsilver.entries (and the QIC-113 readers behind
    # it) is imported when a command needs it, as the commands import theirs.
    from qicsilver.entries import VolumeListing

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
    """qicsilver: read QIC tape images (TWTI/TWTZ) and recover the files on them."""
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

    IMAGE is a TWTI tape image (or a .twtz). To identify raw captures, `tw convert` them
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
@click.argument(
    "image", metavar="IMAGE", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
# Optional, so it defaults in the body (None tells "not given" apart from ".").
@click.argument(
    "outdir", metavar="[OUTDIR]", required=False, type=click.Path(file_okay=False, path_type=Path)
)
@click.option(
    "--volumes", "volume_spec", metavar="LIST", default=None,
    help="volumes to extract, e.g. 0, 0,2, 1-3 or 0,2-4  [default: all]",
)  # fmt: skip
@click.option(
    "--prefix", default=DEFAULT_PREFIX, show_default=True,
    help="output file names: OUTDIR/PREFIXNN.twvl",
)  # fmt: skip
@click.option(
    "-o", "--outfile", type=click.Path(dir_okay=False, path_type=Path), default=None,
    help="write the one selected volume to this file instead (not with OUTDIR or --prefix)",
)  # fmt: skip
@click.option("--volume-profile", default="guess", show_default=True, help=_VOLUME_PROFILE_HELP)
@click.pass_context
def extract(
    ctx: click.Context,
    image: Path,
    outdir: Path | None,
    volume_spec: str | None,
    prefix: str,
    outfile: Path | None,
    volume_profile: str,
) -> None:
    """TWTI tape image -> one TWVL file per backup volume (OUTDIR/vol-NN.twvl).

    OUTDIR defaults to the current directory. --volumes picks volumes by
    their number in the volume table (`qicsilver identify` lists them); NN
    in the file name is that number. -o names the output file when exactly
    one volume is selected, by --volumes N or because the tape has only one.

    Reads the volume table through the volume profile `qicsilver identify` would
    pick, decompresses QIC-122 data and lays each volume out by its QIC-113
    offsets, recording the byte ranges that were lost. Then `qicsilver
    inspect` or `qicsilver tar`.
    """
    from qiclib.extract import extract as do_extract
    from qiclib.extract import parse_volume_list
    from qiclib.volume_profile import VolumeProfileError

    app: AppContext = ctx.obj
    # Argument combinations that need no volume table are refused here, before
    # the image is opened; the ones that do (does volume N exist? does a
    # one-volume tape make -o valid?) are qiclib.extract's.
    volumes = None
    if volume_spec is not None:
        try:
            volumes = parse_volume_list(volume_spec)
        except ValueError as exc:
            log.debug("--volumes %r: %s", volume_spec, exc)
            raise click.BadParameter(str(exc), param_hint="--volumes") from exc
    if outfile is not None:
        if outdir is not None:
            log.debug("both OUTDIR %s and -o %s given; refusing", outdir, outfile)
            raise click.UsageError("give OUTDIR or -o/--outfile, not both")
        if ctx.get_parameter_source("prefix") is not ParameterSource.DEFAULT:
            log.debug("--prefix %r with -o %s; refusing", prefix, outfile)
            raise click.UsageError("--prefix names files in OUTDIR; it does nothing with -o")
        if volumes is not None and len(volumes) != 1:
            log.debug("-o with volumes %s; refusing", volumes)
            raise click.UsageError(
                f"-o/--outfile names one volume, but --volumes selects {len(volumes)} volumes"
            )
    out_dir = outdir if outdir is not None else Path(".")
    log.debug(
        "extracting %s to %s (volumes %s, prefix %r, outfile %s, volume profile %r)",
        image,
        out_dir,
        volumes or "all",
        prefix,
        outfile,
        volume_profile,
    )
    try:
        with app.progress() as prog:
            written = do_extract(
                image,
                out_dir,
                volumes=volumes,
                prefix=prefix,
                outfile=outfile,
                volume_profile=volume_profile,
                progress=prog,
            )
    except (ValueError, VolumeProfileError) as exc:
        log.debug("extract failed: %r", exc)
        raise click.ClickException(str(exc)) from exc
    for path in written:
        click.echo(path)


def _open_volume(
    file: Path, volume_index: int | None, volume_profile: str, progress: Progress
) -> VolumeListing:
    """The backup volume in FILE, listed: a TWVL as it is, or one volume of a TWTI/TWTZ image.

    The one way `inspect` and `tar` open their VOLUME|IMAGE argument, so the
    two can never drift: the same files are accepted, told apart by magic
    (as `tw inspect` tells its files apart: a renamed file still opens, and
    a capture or a stranger is refused by what it is, not what it is called);
    --volume follows one rule (a TWVL is already one volume, so it is a
    usage error there; an image with one volume needs no number and one
    with several needs it, which is qiclib.extract's _pick_volume); and
    every refusal is worded once. An image's volume is built in memory
    exactly as `extract` would have written it; the only difference is that
    no .twvl lands on disk. ``progress`` is the caller's display, so a
    command that goes on to write (`tar`) shows the segment decode and the
    write under one set of bars. Raises click exceptions only: a UsageError
    for --volume on a TWVL (exit 2), a ClickException for everything else
    (exit 1).
    """
    from qiclib.extract import load_volume
    from qiclib.volume_profile import VolumeProfileError
    from tapewyrm_archive.inspect import sniff

    from qicsilver.entries import list_volume, read_volume

    kind = sniff(file)
    log.debug(
        "opening %s (%s; volume %r, profile %r)",
        file,
        kind or "unknown format",
        volume_index,
        volume_profile,
    )
    try:
        if kind == "TWVL":
            if volume_index is not None:
                log.debug("--volume %d given with a TWVL file; refusing", volume_index)
                raise click.UsageError(
                    "--volume picks a volume in a tape image; this file is already one volume"
                )
            return read_volume(file)
        if kind in ("TWTI", "TWTZ"):
            vol = load_volume(file, volume_index, volume_profile=volume_profile, progress=progress)
            return list_volume(vol, file)
        if kind == "TWRF":
            log.debug("%s is a raw capture; refusing", file)
            raise click.ClickException(
                f"{file} is a raw capture; `tw convert` it into a tape image first"
            )
        log.debug("%s is not a TWVL, TWTI or TWTZ; refusing", file)
        raise click.ClickException(f"{file}: not a TWVL volume or a TWTI/TWTZ tape image")
    except (ValueError, VolumeProfileError) as exc:
        log.debug("opening %s failed: %r", file, exc)
        raise click.ClickException(str(exc)) from exc


@cli.command()
@click.argument(
    "file", metavar="VOLUME|IMAGE", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.argument("paths", metavar="[PATH]...", nargs=-1)
@click.option(
    "--volume", "volume_index", metavar="N", type=click.IntRange(min=0), default=None,
    help="which volume of a tape image to list (required when it has more than one)",
)  # fmt: skip
@click.option("--volume-profile", default="guess", show_default=True, help=_VOLUME_PROFILE_HELP)
@click.option("--json", "as_json", is_flag=True, help="print machine-readable JSON")
@click.option("--damaged", is_flag=True, help="list only damaged files")
@click.pass_obj
def inspect(
    app: AppContext,
    file: Path,
    paths: tuple[str, ...],
    volume_index: int | None,
    volume_profile: str,
    as_json: bool,
    damaged: bool,
) -> None:
    """List what is in a backup volume, like `tar tv`, without extracting it.

    VOLUME|IMAGE is a TWVL volume (`qicsilver extract` output) or a TWTI/TWTZ
    tape image, told apart by their magic, not their names. An image is read
    the way `extract` reads it, volume table and all, but nothing is
    written: a tape with one volume lists it, a tape with several needs
    --volume N (the number `qicsilver identify` shows next to each volume).
    --volume-profile is `extract`'s and applies to an image only.

    First a summary: tape name, volume label, backup date, the directory
    format and the parser that read it, file, directory and damaged counts,
    missing bytes and lost segments, and the warnings the volume carries
    (low-confidence, directory-damaged, ...). Then one line per entry: type
    and mode, size in bytes, modification time (UTC), damage (`lost N` = N
    bytes fell in unrecovered tape; `error` = the backup software could not
    read the file) and the path. Each PATH is a glob matched against the
    whole path (quote it), e.g. '*.TXT' or 'C:/WINDOWS/*'. Reads the
    volume exactly as `qicsilver tar` does, so the two always agree.
    """
    import json

    from qicsilver.inspect import format_entries, format_summary, select_entries, to_dict

    log.debug("inspecting %s (paths %r, damaged only %s)", file, paths, damaged)
    with app.progress() as prog:
        listing = _open_volume(file, volume_index, volume_profile, prog)
    entries = select_entries(listing.entries, paths, damaged)
    if as_json:
        click.echo(json.dumps(to_dict(listing, entries), indent=1))
        return
    for line in format_summary(listing):
        click.echo(line)
    click.echo()
    for line in format_entries(entries):
        click.echo(line)


@cli.command()
@click.argument(
    "file", metavar="VOLUME|IMAGE", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.argument("output", metavar="OUTPUT", type=click.Path(dir_okay=False, path_type=Path))
@click.option(
    "--volume", "volume_index", metavar="N", type=click.IntRange(min=0), default=None,
    help="which volume of a tape image to tar (required when it has more than one)",
)  # fmt: skip
@click.option("--volume-profile", default="guess", show_default=True, help=_VOLUME_PROFILE_HELP)
@click.option(
    "--report", type=click.Path(dir_okay=False, path_type=Path), default=None,
    help="damage report (default: OUTPUT.damaged.txt)",
)  # fmt: skip
@click.option("--skip-damaged", is_flag=True, help="leave damaged files out of the tar")
@click.pass_obj
def tar(
    app: AppContext,
    file: Path,
    output: Path,
    volume_index: int | None,
    volume_profile: str,
    report: Path | None,
    skip_damaged: bool,
) -> None:
    """Backup volume -> pax tar OUTPUT of its files, plus a damage report.

    VOLUME|IMAGE is a TWVL volume (`qicsilver extract` output) or a TWTI/TWTZ
    tape image, told apart by their magic exactly as `qicsilver inspect`
    tells them apart: an image is read the way `extract` reads it (so
    --volume-profile applies) and its volume tarred without a .twvl landing
    on disk; a tape with one volume needs no --volume, a tape with several
    needs --volume N (the number `qicsilver identify` shows).

    Keeps the backup's own paths, modification times and attributes (pax
    header TAPEWYRM.dos_attributes, or TAPEWYRM.qic113_attributes for a
    Basic-DOS volume), and the volume's warnings in the pax global header
    TAPEWYRM.warnings. Files with bytes in unrecovered segments are written
    zero-filled unless --skip-damaged.
    """
    from qicsilver.tar import write_tar

    log.debug("tar %s -> %s", file, output)
    try:
        # One display for both steps: an image's segment decode, then the write.
        with app.progress() as prog:
            listing = _open_volume(file, volume_index, volume_profile, prog)
            res = write_tar(
                listing, output, report_path=report, skip_damaged=skip_damaged, progress=prog
            )
    except ValueError as exc:
        log.debug("tar failed: %r", exc)
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"{res.out}: {res.files} files, {res.dirs} directories, "
        f"{len(res.damaged)} damaged (report: {res.report})"
    )
