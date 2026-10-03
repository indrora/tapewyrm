"""`qicsilver tar`: turn an extracted QIC-113 backup volume into a tar archive.

The last step after ``tw dump -> tw convert -> qicsilver extract``::

    qicsilver tar jc-1998/vol-00.twvl jc-1998.tar

The input is a TWVL volume written by ``qicsilver extract``. Every directory
and file of the backup goes into a POSIX (pax) tar:

* paths come from the backup's own directory (in the extended format, long
  Windows 95 names, and the root, e.g. ``C:``, becomes the top-level
  directory ``C``),
* modification times are the ones recorded in the backup,
* read-only files get mode 0444, other files 0644, directories 0755,
* the backup's attribute byte is kept as a pax header (which one depends on
  the directory format, below).

Files whose bytes fall partly in holes (tape segments that could not be
recovered) are still written, zero-filled, unless ``--skip-damaged`` is given.
Either way they are listed in the damage report (``OUTPUT.damaged.txt`` by
default), separately from files the original backup software could not read.

Both QIC-113 directory formats are supported (Extended: DOS attributes kept
as pax ``TAPEWYRM.dos_attributes``; Basic-DOS: QIC-113 attribute bits kept as
pax ``TAPEWYRM.qic113_attributes``). Reading the volume -- the header, the
directory walk, sizes, modes and damage -- is :mod:`qicsilver.entries`, shared
with ``qicsilver inspect`` so a listing and a tar can never disagree; this
module only writes what it yields.
"""

from __future__ import annotations

import io
import logging
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

from tapewyrm_archive.progress import NULL_PROGRESS, Progress

from qicsilver.entries import entry_data, read_volume

log = logging.getLogger(__name__)


@dataclass
class TarResult:
    """What :func:`write_tar` wrote."""

    out: Path
    report: Path
    files: int = 0
    dirs: int = 0
    # (path, missing bytes, size, backup-software error flag)
    damaged: list[tuple[str, int, int, bool]] = field(default_factory=list)


def default_report_path(out: Path) -> Path:
    """``OUT.damaged.txt`` next to the tar."""
    return out.with_suffix(out.suffix + ".damaged.txt")


def write_tar(
    volume_path: Path,
    out: Path,
    *,
    report_path: Path | None = None,
    skip_damaged: bool = False,
    progress: Progress = NULL_PROGRESS,
) -> TarResult:
    """Write every directory and file of a TWVL volume into a pax tar at ``out``.

    Raises ``ValueError`` for a volume this cannot read (no QIC-113 section
    sizes in its table entry). See the module docstring for the tar layout
    and the damage report.
    """
    report_path = report_path or default_report_path(out)
    listing = read_volume(volume_path)
    vol = listing.volume
    result = TarResult(out=out, report=report_path)

    log.info("writing %s...", out)
    with (
        tarfile.open(out, "w", format=tarfile.PAX_FORMAT) as tar,
        progress.task("writing tar", total=len(listing.entries), unit="entries") as bar,
    ):
        for entry in listing.entries:
            bar.advance()
            info = tarfile.TarInfo(entry.tar_name)
            info.mtime = entry.mtime or 0
            info.pax_headers = {listing.attribute_key: f"0x{entry.attrs:02x}"}
            if entry.is_dir:
                info.type, info.mode = tarfile.DIRTYPE, entry.mode
                tar.addfile(info)
                result.dirs += 1
                continue
            if entry.damage is not None:
                result.damaged.append((entry.path, entry.missing, entry.size, entry.error))
                if skip_damaged:
                    log.debug("%s: --skip-damaged; leaving it out", entry.path)
                    continue
            data = entry_data(vol, entry)
            info.size = len(data)
            info.mode = entry.mode
            tar.addfile(info, io.BytesIO(data))
            result.files += 1

    log.info("writing damage report %s...", report_path)
    with report_path.open("w", encoding="utf-8") as rep:
        rep.write(f"# qicsilver damage report for {volume_path}\n")
        rep.write(f"# tape {listing.tape_name!r}; volume {listing.description!r}\n")
        lost = listing.lost_segments
        rep.write(f"# lost tape segments: {len(lost)} {lost}\n")
        rep.write(
            "# missing_bytes / size   flag   path\n"
            "#   lost  = bytes fell in tape segments we could not recover (zero-filled)\n"
            "#   error = the original backup software itself could not read the file (QIC-113\n"
            "#           'file error' bit; often a locked file, sometimes a placeholder name)\n"
        )
        for path, missing, size, flagged in result.damaged:
            flag = "error" if flagged and not missing else "lost"
            rep.write(f"{missing:>10} / {size:<10} {flag:5}  {path}\n")

    log.info(
        f"wrote {out}: {result.files} files, {result.dirs} directories; "
        f"{len(result.damaged)} damaged ({'skipped' if skip_damaged else 'zero-filled'}), "
        f"see {report_path}"
    )
    return result
