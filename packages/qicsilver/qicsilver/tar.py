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

Both QIC-113 directory formats are supported, chosen from the volume table
entry (the flag byte, VTBL byte 56, and the QIC-113 signature at 58-61, which
every tape profile reads the same way):

* **Extended** (Colorado/HP backup software): ``qiclib.qic113ext``; DOS
  attributes kept as pax ``TAPEWYRM.dos_attributes``.
* **Basic-DOS** (e.g. the "MTN" tapes): ``qiclib.qic113``; QIC-113 attribute
  bits kept as pax ``TAPEWYRM.qic113_attributes``. Files the directory lists
  whose data never made it off the tape are written zero-filled (or skipped)
  and reported as lost.

Where the sections are comes from the TWVL header, never from the raw VTBL
record (TWS-3 section 6.2, rule 9): ``data_section_size``,
``dir_section_size``, ``directory_offset``, ``vtbl.flags`` and
``vtbl.compressed`` were decoded by ``qicsilver extract`` through the volume
profile that fits the tape, while bytes 57-127 of the raw record mean
different things to different backup programs (an MTN tape keeps part of its
label where Rev N keeps the data section size). :func:`_header_vtbl` builds
the one :class:`~qiclib.volume.VtblEntry` both paths use from those members;
only the QIC-113 signature at bytes 58-61, which the header does not carry,
is read from ``vtbl.raw``.
"""

from __future__ import annotations

import io
import logging
import tarfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from qiclib import qic113
from qiclib import qic113ext as q
from qiclib.volume import VTBL_ENTRY_LEN, VtblEntry, parse_vtbl_base
from tapewyrm_archive.errors import MalformedFileError
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


# ---------------------------------------------------------------------------
# The volume table entry, from the TWVL header (TWS-3 sections 3, 6.2)
# ---------------------------------------------------------------------------

# What a member read by _member must hold, worded for the error message. The
# check uses type(), not isinstance: bool is an int subclass, and an integer
# member must not accept true/false.
_INT, _BOOL = "a non-negative integer", "true or false"


def _member(volume_path: Path, obj: dict, where: str, name: str, kind: str, nullable: bool) -> Any:
    """``obj[name]``, refused as malformed when absent or of the wrong JSON type.

    Every member read here is REQUIRED of a TWVL writer (TWS-3 section 3.1),
    so absent is malformed, not "unknown"; "unknown" is an explicit null,
    allowed only where the spec says "or null". Integers must be
    non-negative: they are sizes, offsets and flag bytes.
    """
    label = f"{where}{name}"
    if name not in obj:
        log.debug("%s: header lacks %s; refusing", volume_path, label)
        raise MalformedFileError(volume_path, "TWVL", f"the header lacks the member {label}")
    value = obj[name]
    if value is None and nullable:
        return None
    ok = (type(value) is int and value >= 0) if kind == _INT else type(value) is bool
    if not ok:
        log.debug("%s: %s is %r, not %s; refusing", volume_path, label, value, kind)
        expected = f"{kind} or null" if nullable else kind
        raise MalformedFileError(
            volume_path, "TWVL", f"{label} is {value!r}; it must be {expected}"
        )
    return value


def _header_vtbl(volume_path: Path, hdr: dict) -> VtblEntry:
    """The volume's VTBL entry as the TWVL header decoded it (TWS-3 6.2 rule 9).

    Bytes 0-56 of ``vtbl.raw`` are universal, so they seed the entry
    (:func:`parse_vtbl_base`), and the raw record stays on it for the QIC-113
    signature check in :func:`qiclib.qic113.is_extended_os`. Everything that
    sizes or places a section -- the flag byte, the compression flag and both
    section sizes -- is then taken from the header, which ``qicsilver
    extract`` filled through the right volume profile. Re-decoding bytes 57+
    of the raw record would apply one fixed layout to every tape, which is
    exactly the misreading volume profiles exist to avoid.
    """
    vtbl = hdr.get("vtbl")
    if not isinstance(vtbl, dict):
        log.debug("%s: vtbl is %r, not an object; refusing", volume_path, type(vtbl).__name__)
        raise MalformedFileError(volume_path, "TWVL", "the header's vtbl member is not an object")
    raw_hex = vtbl.get("raw")
    try:
        raw = bytes.fromhex(raw_hex) if isinstance(raw_hex, str) else None
    except ValueError:
        raw = None
    if raw is None or len(raw) != VTBL_ENTRY_LEN:
        log.debug("%s: vtbl.raw is %r; refusing", volume_path, raw_hex)
        raise MalformedFileError(
            volume_path,
            "TWVL",
            f"vtbl.raw must be the {VTBL_ENTRY_LEN}-byte record as "
            f"{2 * VTBL_ENTRY_LEN} hexadecimal digits",
        )
    flags = _member(volume_path, vtbl, "vtbl.", "flags", _INT, nullable=False)
    if flags > 0xFF:
        log.debug("%s: vtbl.flags %d is not a byte; refusing", volume_path, flags)
        raise MalformedFileError(volume_path, "TWVL", f"vtbl.flags is {flags}; it is one byte")
    entry = replace(
        parse_vtbl_base(raw),
        flags=flags,
        compressed=_member(volume_path, vtbl, "vtbl.", "compressed", _BOOL, nullable=True),
        # The top-level copies, which TWS-3 3.2 has writers keep equal to
        # vtbl's; rule 9 names these.
        data_section_size=_member(volume_path, hdr, "", "data_section_size", _INT, nullable=True),
        dir_section_size=_member(volume_path, hdr, "", "dir_section_size", _INT, nullable=True),
    )
    if entry.flags != raw[56]:
        log.debug(
            "%s: header flags 0x%02x differ from raw byte 56 0x%02x; using the header's",
            volume_path,
            entry.flags,
            raw[56],
        )
    log.debug(
        "%s: vtbl from header: flags 0x%02x, compressed %r, data %r, directory %r bytes",
        volume_path,
        entry.flags,
        entry.compressed,
        entry.data_section_size,
        entry.dir_section_size,
    )
    return entry


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
    result = TarResult(out=out, report=report_path)
    vtbl = _header_vtbl(volume_path, hdr)
    # null = the writer did not know it exactly; consumers then locate the
    # directory themselves (TWS-3 4.4, 6.2 rule 8).
    dir_at = _member(volume_path, hdr, "", "directory_offset", _INT, nullable=True)
    if qic113.is_extended_os(vtbl):
        log.info("QIC-113 extended directory format")
        _write_extended(vol, vtbl, dir_at, str(volume_path), out, result, skip_damaged, progress)
    else:
        log.info("QIC-113 Basic-DOS directory format")
        _write_basic(vol, vtbl, dir_at, out, result, skip_damaged, progress)

    log.info("writing damage report %s...", report_path)
    with report_path.open("w", encoding="utf-8") as rep:
        rep.write(f"# qicsilver damage report for {volume_path}\n")
        # Display-only members: .get, so a header that lacks one still tars.
        lost = hdr.get("lost_segments") or []
        rep.write(f"# tape {hdr.get('tape_name')!r}; volume {vtbl.description!r}\n")
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


def _write_extended(
    vol: Volume,
    vtbl: VtblEntry,
    dir_at: int | None,
    vol_name: str,
    out: Path,
    result: TarResult,
    skip_damaged: bool,
    progress: Progress,
) -> None:
    """Extended-format volume: directory section last, at ``data_section_size``.

    ``vtbl`` is :func:`_header_vtbl`'s, so both sizes are the header's.
    """
    hdr = vol.header
    data_size, dir_size = vtbl.data_section_size, vtbl.dir_section_size
    if data_size is None or dir_size is None:
        log.debug(
            "volume %s: data_section_size %r, dir_section_size %r; refusing",
            vol_name,
            data_size,
            dir_size,
        )
        raise ValueError(
            f"{vol_name}: the volume table entry has no QIC-113 section sizes; not supported yet"
        )
    # An uncompressed Directory-Last volume records the directory's exact
    # start (after the Segment Gap); otherwise it sits right after the data
    # (compressed volumes, whose layout has no gap: the bench "jc" tape).
    if dir_at is None:
        log.debug("no directory_offset in the header; directory follows the data section")
        dir_at = data_size
    log.debug("reading the directory section: %d bytes at offset %d", dir_size, dir_at)
    dir_bytes, dir_missing = vol.read(dir_at, dir_size)
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
        f"tape {hdr.get('tape_name')!r}, volume {vtbl.description!r} "
        f"({hdr['vtbl'].get('date_decoded')}): {len(entries)} entries; "
        f"{missing_total:,} of {len(vol.data):,} volume bytes missing"
    )

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


def _write_basic(
    vol: Volume,
    vtbl: VtblEntry,
    dir_at: int | None,
    out: Path,
    result: TarResult,
    skip_damaged: bool,
    progress: Progress,
) -> None:
    """Basic-DOS volume: ``qiclib.qic113.extract`` lists every file, found or lost.

    A found file's missing bytes come from the volume's hole map (its data
    ``offset`` is into the same byte stream); a lost one is all missing.
    ``vtbl`` is :func:`_header_vtbl`'s: its section sizes and compression
    flag, which place a Directory-Last directory when ``dir_at`` is null, are
    the header's.
    """
    log.debug("extracting the Basic-DOS file set (%d volume bytes)", len(vol.data))
    fileset = qic113.extract(vol.data, vtbl, dir_offset=dir_at)
    missing_total = sum(b - a for a, b in vol.holes)
    log.info(
        f"tape {vol.header.get('tape_name')!r}: {len(fileset.files)} entries "
        f"({sum(f.lost for f in fileset.files)} lost); "
        f"{missing_total:,} of {len(vol.data):,} volume bytes missing"
    )
    log.info("writing %s...", out)
    with (
        tarfile.open(out, "w", format=tarfile.PAX_FORMAT) as tar,
        progress.task("writing tar", total=len(fileset.files), unit="entries") as bar,
    ):
        for f in fileset.files:
            bar.advance()
            info = tarfile.TarInfo(f.path)
            info.mtime = f.mtime or 0
            info.pax_headers = {"TAPEWYRM.qic113_attributes": f"0x{f.attrs:02x}"}
            if f.is_dir:
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                tar.addfile(info)
                result.dirs += 1
                continue
            if f.lost or f.offset is None:
                log.debug("%s: lost (no data entry); %d bytes zero-filled", f.path, f.size)
                data, missing = bytes(f.size), f.size
            else:
                data, missing = vol.read(f.offset, f.size)
            if missing or f.unreadable_at_backup:
                log.debug(
                    "%s: damaged (%d of %d bytes missing, unreadable at backup %s)",
                    f.path,
                    missing,
                    f.size,
                    f.unreadable_at_backup,
                )
                result.damaged.append((f.path, missing, f.size, f.unreadable_at_backup))
                if skip_damaged:
                    log.debug("%s: --skip-damaged; leaving it out", f.path)
                    continue
            info.size = len(data)
            # QIC-113 attribute bit 1 is "write access allowed" (§7.1.3).
            info.mode = 0o644 if f.attrs & qic113.ATTR_WRITE else 0o444
            tar.addfile(info, io.BytesIO(data))
            result.files += 1
