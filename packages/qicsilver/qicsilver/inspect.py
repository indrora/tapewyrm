"""`qicsilver inspect`: list a TWVL backup volume, like ``tar tv``, without extracting it.

The entries come from :func:`qicsilver.entries.read_volume`, the same walk
``qicsilver tar`` writes from, so the listing and the tar always agree on
which entries exist, their sizes and which are damaged. This module only
filters and formats them:

* a summary of the whole volume (tape name, volume label, backup date, the
  QIC-113 directory format, file / directory / damaged counts, missing
  bytes, lost segments), then
* one line per entry, plain aligned columns so it stays greppable::

      -r--r--r--     12  1998-12-23 05:13:20           C:/EXAMPLE.TXT
      -rw-r--r--    100  1998-12-23 05:13:20  lost 50  C:/BROKEN.DAT

  type+mode, size in bytes, modification time (UTC), damage (``lost N``:
  N bytes fell in holes; ``error``: the backup software's own error flag),
  and the backup's path last, so a path with spaces stays one field at the
  end of the line.

``to_dict`` is the ``--json`` document: ``{"summary": {...}, "entries": [...]}``.
The summary always describes the whole volume; PATH globs and ``--damaged``
only narrow the entries.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from typing import Any

from rich.filesize import decimal

from qicsilver.entries import VolumeEntry, VolumeListing

log = logging.getLogger(__name__)

_NO_TIME = "-"
_TIME_WIDTH = len("1998-12-23 05:13:20")


def select_entries(
    entries: Iterable[VolumeEntry], patterns: Sequence[str] = (), damaged_only: bool = False
) -> list[VolumeEntry]:
    """The entries matching any of ``patterns`` (all when none), damaged ones only if asked.

    A pattern is an ``fnmatch`` glob over the whole path, case-sensitive (as
    tar's are), so ``*`` crosses ``/``: ``*.TXT`` matches in every directory.
    It is tried against the backup's own path (``C:/DOCS/X``) and the tar
    member name (``C/DOCS/X``), so either spelling works.
    """
    out = []
    for entry in entries:
        if damaged_only and entry.damage is None:
            continue
        if patterns and not any(
            fnmatchcase(entry.path, pat) or fnmatchcase(entry.tar_name, pat) for pat in patterns
        ):
            continue
        out.append(entry)
    log.debug(
        "selected %d entries (patterns %r, damaged only %s)", len(out), patterns, damaged_only
    )
    return out


def _time(mtime: int | None) -> str:
    """Seconds since 1970 as ``YYYY-MM-DD HH:MM:SS`` UTC; '-' when not recorded."""
    if mtime is None:
        return _NO_TIME
    return datetime.fromtimestamp(mtime, UTC).strftime("%Y-%m-%d %H:%M:%S")


def _damage(entry: VolumeEntry) -> str:
    if entry.damage == "lost":
        return f"lost {entry.missing}"
    return entry.damage or ""


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def format_summary(listing: VolumeListing) -> list[str]:
    """The header lines: what the volume is and how much of it survived."""
    lost = len(listing.lost_segments)
    return [
        f"tape name     {listing.tape_name or '-'}",
        f"volume        {listing.description or '-'}",
        f"date          {listing.date or '-'}",
        f"directory     QIC-113 {listing.format_name}",
        f"entries       {_plural(listing.files, 'file')}, "
        f"{listing.dirs} {'directory' if listing.dirs == 1 else 'directories'}, "
        f"{listing.damaged} damaged",
        f"missing       {decimal(listing.missing_bytes)} missing of "
        f"{decimal(listing.volume_bytes)}; {_plural(lost, 'lost segment')}",
    ]


def format_entries(entries: Sequence[VolumeEntry]) -> list[str]:
    """One aligned line per entry (see the module docstring for the columns)."""
    size_width = max((len(str(e.size)) for e in entries), default=1)
    damage_width = max((len(_damage(e)) for e in entries), default=0)
    lines = []
    for e in entries:
        cols = [e.mode_string, f"{e.size:>{size_width}}", f"{_time(e.mtime):<{_TIME_WIDTH}}"]
        if damage_width:
            cols.append(f"{_damage(e):<{damage_width}}")
        cols.append(e.path)
        lines.append("  ".join(cols))
    return lines


def to_dict(listing: VolumeListing, entries: Sequence[VolumeEntry]) -> dict[str, Any]:
    """The ``--json`` document: the whole volume's summary and the selected entries."""
    return {
        "summary": {
            "volume": str(listing.path),
            "tape_name": listing.tape_name,
            "description": listing.description,
            "date": listing.date,
            "directory_format": listing.format_name,
            "files": listing.files,
            "directories": listing.dirs,
            "damaged": listing.damaged,
            "volume_bytes": listing.volume_bytes,
            "missing_bytes": listing.missing_bytes,
            "lost_segments": listing.lost_segments,
        },
        "entries": [
            {
                "path": e.path,
                "tar_name": e.tar_name,
                "type": "directory" if e.is_dir else "file",
                "mode": e.mode_string,
                "size": e.size,
                "mtime": e.mtime,
                "attributes": e.attrs,
                "data_offset": e.data_offset,
                "missing_bytes": e.missing,
                "error": e.error,
                "damage": e.damage,
            }
            for e in entries
        ],
    }
