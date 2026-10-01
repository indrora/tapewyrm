"""qic2tar: turn a dumped QIC-80 backup tape into a tar archive.

Usage (from the repository root)::

    uv run --project host python contrib/qic2tar.py captures/jc-1998 -o jc-1998.tar

``DUMP_DIR`` is a directory of ``track-NN.raw`` captures written by
``tw dump``. The tool decodes them (flux -> sectors -> Reed-Solomon ->
QIC-122 decompression -> QIC-113 volume), then writes every directory and file
of the backup into a POSIX (pax) tar archive:

* paths come from the backup's own directory (long Windows 95 names; the root,
  e.g. ``C:``, becomes the top-level directory ``C``),
* modification times are the ones recorded in the backup,
* read-only files get mode 0444, other files 0644, directories 0755,
* DOS/Windows attributes are kept as a pax header ``TAPEWYRM.dos_attributes``.

Files whose bytes fall partly in tape segments that could not be recovered are
still written, with the missing ranges zero-filled, unless ``--skip-damaged``
is given. Either way they are listed in the damage report
(``OUT.damaged.txt`` by default) so nothing is silently wrong.

Supported today: QIC-113 extended-format volumes (the kind Colorado/HP backup
software wrote), QIC-122 compressed or not. Only the first volume on the tape
is extracted.
"""

from __future__ import annotations

import argparse
import io
import sys
import tarfile
from pathlib import Path

from tapewyrm.codec import qic113ext as q
from tapewyrm.codec.recover import recover
from tapewyrm.codec.volume import decode_short_date


def _tar_name(path: str) -> str:
    """'C:/WINDOWS/x' -> 'C/WINDOWS/x' (no ':' or leading '/' in tar names)."""
    root, _, rest = path.partition("/")
    root = root.rstrip(":").replace(":", "") or "root"
    return f"{root}/{rest}" if rest else root


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="qic2tar", description=__doc__.split("\n\n")[0])
    ap.add_argument("dump_dir", type=Path, help="directory of track-NN.raw captures")
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

    rv = recover(args.dump_dir, log)
    vt = rv.vtbl
    if vt.data_section_size is None or vt.dir_section_size is None:
        log("this volume's table entry has no QIC-113 section sizes; not supported yet")
        return 2
    dir_bytes, dir_missing = rv.stream.read(vt.data_section_size, vt.dir_section_size)
    if dir_missing:
        log(
            f"warning: {dir_missing:,} bytes of the directory are missing; listing may be cut short"
        )
    entries = q.parse_directory(dir_bytes)
    paths = q.directory_paths(entries)
    stamp = decode_short_date(vt.date)
    log(
        f"tape {rv.header.tape_name!r}, volume {vt.description!r} ({stamp}): {len(entries)} "
        f"entries; volume bytes recovered {rv.stream.coverage():,}/{rv.stream.size:,}"
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
                data, missing = rv.stream.read(lay.data_offset, lay.data_size)
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
        rep.write(f"# qic2tar damage report for {args.dump_dir}\n")
        rep.write(f"# tape {rv.header.tape_name!r}; volume {vt.description!r}\n")
        rep.write(
            f"# segments: {len(rv.missing_segments)} missing {rv.missing_segments}, "
            f"{len(rv.uncorrectable_segments)} uncorrectable {rv.uncorrectable_segments}\n"
        )
        rep.write(
            "# missing_bytes / size   flag   path\n"
            "#   lost  = bytes fell in tape segments we could not recover (zero-filled)\n"
            "#   error = the 1998 backup software itself could not read the file (QIC-113\n"
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
