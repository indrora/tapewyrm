"""`qicsilver tar`: turn an extracted QIC-113 backup volume into a tar archive.

The last step after ``tw dump -> tw convert -> qicsilver extract``::

    qicsilver tar jc-1998/vol-00.twvl -o jc-1998.tar

(This was ``contrib/qic2tar.py`` before qicsilver existed.) The input is a TWVL
volume written by ``qicsilver extract``. Every directory and file
of the backup goes into a POSIX (pax) tar:

* paths come from the backup's own directory (long Windows 95 names; the root,
  e.g. ``C:``, becomes the top-level directory ``C``),
* modification times are the ones recorded in the backup,
* read-only files get mode 0444, other files 0644, directories 0755,
* DOS/Windows attributes are kept as a pax header ``TAPEWYRM.dos_attributes``.

Files whose bytes fall partly in holes (tape segments that could not be
recovered) are still written, zero-filled, unless ``--skip-damaged`` is given.
Either way they are listed in the damage report (``OUT.damaged.txt`` by
default), separately from files the original backup software could not read.

Supports QIC-113 extended-format volumes (as written by Colorado/HP backup
software).
"""

from __future__ import annotations

import io
import logging
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

from qiclib import qic113ext as q
from tapewyrm_archive.progress import NULL_PROGRESS, Progress
from tapewyrm_archive.twvl import Volume

log = logging.getLogger(__name__)


def _tar_name(path: str) -> str:
    """'C:/WINDOWS/x' -> 'C/WINDOWS/x' (no ':' or leading '/' in tar names)."""
    root, _, rest = path.partition("/")
    root = root.rstrip(":").replace(":", "") or "root"
    return f"{root}/{rest}" if rest else root


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
    log.info("reading volume %s...", volume_path)
    vol = Volume.load(volume_path)
    hdr = vol.header
    data_size, dir_size = hdr["data_section_size"], hdr["dir_section_size"]
    if data_size is None or dir_size is None:
        log.debug(
            "volume %s: data_section_size %r, dir_section_size %r; refusing",
            volume_path,
            data_size,
            dir_size,
        )
        raise ValueError(
            f"{volume_path}: the volume table entry has no QIC-113 section sizes; not supported yet"
        )
    log.debug("reading the directory section: %d bytes at offset %d", dir_size, data_size)
    dir_bytes, dir_missing = vol.read(data_size, dir_size)
    if dir_missing:
        log.warning(
            "%s bytes of the directory are missing; the listing may be cut short",
            f"{dir_missing:,}",
        )
    log.debug("parsing the QIC-113 directory")
    entries = q.parse_directory(dir_bytes)
    paths = q.directory_paths(entries)
    missing_total = sum(b - a for a, b in vol.holes)
    log.info(
        f"tape {hdr['tape_name']!r}, volume {hdr['vtbl']['description']!r} "
        f"({hdr['vtbl']['date_decoded']}): {len(entries)} entries; "
        f"{missing_total:,} of {len(vol.data):,} volume bytes missing"
    )

    result = TarResult(out=out, report=report_path)
    layout = list(q.layout(entries))
    log.info("writing %s...", out)
    with (
        tarfile.open(out, "w", format=tarfile.PAX_FORMAT) as tar,
        progress.task("writing tar", total=len(layout), unit="entries") as bar,
    ):
        for lay in layout:
            bar.advance()
            e = entries[lay.index]
            info = tarfile.TarInfo(_tar_name(paths[lay.index]))
            mt = e.mtime
            if mt is None:
                log.debug("%s: no modification time in the directory; using 0", paths[lay.index])
            info.mtime = int(mt.timestamp()) if mt else 0
            info.pax_headers = {"TAPEWYRM.dos_attributes": f"0x{e.attributes:02x}"}
            if e.is_dir:
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                tar.addfile(info)
                result.dirs += 1
                continue
            data, missing = (b"", 0)
            if lay.data_offset is not None and lay.data_size:
                data, missing = vol.read(lay.data_offset, lay.data_size)
            else:
                log.debug(
                    "%s: no data (offset %r, size %r); writing an empty file",
                    paths[lay.index],
                    lay.data_offset,
                    lay.data_size,
                )
            flagged = bool(e.traversal & q.T_ERROR)
            if missing or flagged:
                log.debug(
                    "%s: damaged (%d of %d bytes missing, backup error flag %s)",
                    paths[lay.index],
                    missing,
                    lay.data_size,
                    flagged,
                )
                result.damaged.append((paths[lay.index], missing, lay.data_size, flagged))
                if skip_damaged:
                    log.debug("%s: --skip-damaged; leaving it out", paths[lay.index])
                    continue
            info.size = len(data)
            info.mode = 0o444 if e.attributes & 0x01 else 0o644
            tar.addfile(info, io.BytesIO(data))
            result.files += 1

    log.info("writing damage report %s...", report_path)
    with report_path.open("w", encoding="utf-8") as rep:
        rep.write(f"# qicsilver damage report for {volume_path}\n")
        rep.write(f"# tape {hdr['tape_name']!r}; volume {hdr['vtbl']['description']!r}\n")
        rep.write(f"# lost tape segments: {len(hdr['lost_segments'])} {hdr['lost_segments']}\n")
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
