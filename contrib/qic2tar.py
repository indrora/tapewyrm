"""qic2tar: turn an extracted QIC-113 backup volume into a tar archive.

The last step after ``tw dump -> tw convert -> tw extract``. Usage (from the
repository root)::

    uv run --project host python contrib/qic2tar.py jc-1998/vol-00.twvl -o jc-1998.tar

The input is a TWVL volume written by ``tw extract``. Every directory and file
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

import argparse
import io
import sys
import tarfile
from pathlib import Path

from tapewyrm.codec import qic113ext as q
from tapewyrm.image.twvl import Volume


def _tar_name(path: str) -> str:
    """'C:/WINDOWS/x' -> 'C/WINDOWS/x' (no ':' or leading '/' in tar names)."""
    root, _, rest = path.partition("/")
    root = root.rstrip(":").replace(":", "") or "root"
    return f"{root}/{rest}" if rest else root


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="qic2tar", description=__doc__.split("\n\n")[0])
    ap.add_argument("volume", type=Path, help="TWVL volume from `tw extract`")
    ap.add_argument(
        "-o", "--output", type=Path, required=True, help="tar file to write"
    )
    ap.add_argument(
        "--report", type=Path, help="damage report (default: OUTPUT.damaged.txt)"
    )
    ap.add_argument(
        "--skip-damaged", action="store_true", help="leave damaged files out"
    )
    args = ap.parse_args(argv)
    report_path = args.report or args.output.with_suffix(
        args.output.suffix + ".damaged.txt"
    )

    def log(msg: str) -> None:
        print(msg, file=sys.stderr, flush=True)

    vol = Volume.load(args.volume)
    hdr = vol.header
    data_size, dir_size = hdr["data_section_size"], hdr["dir_section_size"]
    if data_size is None or dir_size is None:
        log("this volume's table entry has no QIC-113 section sizes; not supported yet")
        return 2
    dir_bytes, dir_missing = vol.read(data_size, dir_size)
    if dir_missing:
        log(
            f"warning: {dir_missing:,} bytes of the directory are missing; listing may be cut short"
        )
    entries = q.parse_directory(dir_bytes)
    paths = q.directory_paths(entries)
    missing_total = sum(b - a for a, b in vol.holes)
    log(
        f"tape {hdr['tape_name']!r}, volume {hdr['vtbl']['description']!r} "
        f"({hdr['vtbl']['date_decoded']}): {len(entries)} entries; "
        f"{missing_total:,} of {len(vol.data):,} volume bytes missing"
    )

    damaged: list[tuple[str, int, int, bool]] = []
    files = dirs = 0
    with tarfile.open(args.output, "w", format=tarfile.PAX_FORMAT) as tar:
        for lay in q.layout(entries):
            e = entries[lay.index]
            info = tarfile.TarInfo(_tar_name(paths[lay.index]))
            mt = e.mtime
            info.mtime = int(mt.timestamp()) if mt else 0
            info.pax_headers = {"TAPEWYRM.dos_attributes": f"0x{e.attributes:02x}"}
            if e.is_dir:
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                tar.addfile(info)
                dirs += 1
                continue
            data, missing = (b"", 0)
            if lay.data_offset is not None and lay.data_size:
                data, missing = vol.read(lay.data_offset, lay.data_size)
            flagged = bool(e.traversal & q.T_ERROR)
            if missing or flagged:
                damaged.append((paths[lay.index], missing, lay.data_size, flagged))
                if args.skip_damaged:
                    continue
            info.size = len(data)
            info.mode = 0o444 if e.attributes & 0x01 else 0o644
            tar.addfile(info, io.BytesIO(data))
            files += 1

    with report_path.open("w", encoding="utf-8") as rep:
        rep.write(f"# qic2tar damage report for {args.volume}\n")
        rep.write(
            f"# tape {hdr['tape_name']!r}; volume {hdr['vtbl']['description']!r}\n"
        )
        rep.write(
            f"# lost tape segments: {len(hdr['lost_segments'])} {hdr['lost_segments']}\n"
        )
        rep.write(
            "# missing_bytes / size   flag   path\n"
            "#   lost  = bytes fell in tape segments we could not recover (zero-filled)\n"
            "#   error = the original backup software itself could not read the file (QIC-113\n"
            "#           'file error' bit; often a locked file, sometimes a placeholder name)\n"
        )
        for path, missing, size, flagged in damaged:
            flag = "error" if flagged and not missing else "lost"
            rep.write(f"{missing:>10} / {size:<10} {flag:5}  {path}\n")

    log(
        f"wrote {args.output}: {files} files, {dirs} directories; "
        f"{len(damaged)} damaged ({'skipped' if args.skip_damaged else 'zero-filled'}), "
        f"see {report_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
