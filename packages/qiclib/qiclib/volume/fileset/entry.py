"""One entry of a backup volume, as every file-set parser reports it.

:class:`VolumeEntry` is the record ``qicsilver tar`` writes and ``qicsilver
inspect`` lists, whichever parser produced it (:mod:`qiclib.volume.fileset`): the
backup's own path and the tar member name, size, modification time, the
backup's attribute byte and the mode derived from it, where the file's
bytes start in the volume, how many of them fell in holes, and the backup
software's own error flag. Both consumers read the volume through the same
parser, so they can never disagree about which entries there are, how big
they are, or which are damaged.

It used to live in ``qicsilver.entries``; it moved here so parsers, which
are qiclib's, can produce it (STYLE.md §2: qicsilver depends on qiclib,
never the reverse). ``qicsilver.entries`` re-exports it.

TODO: parsers for the formats QICStreamReader documents will need more
(unix mode/uid/gid, Mac forks, symlink targets); add ``kind`` and an
``extras`` mapping for pax headers then, not before.
"""

from __future__ import annotations

import stat
from dataclasses import dataclass

#: Permission bits each entry gets in a tar (and shows in a listing). A
#: QIC-113 backup records no owner permissions, only "read-only" (Extended:
#: DOS bit 0) or "write access allowed" (Basic-DOS: QIC-113 bit 1), so files
#: are 0444 or 0644 and directories always 0755.
DIR_MODE, RW_MODE, RO_MODE = 0o755, 0o644, 0o444


def tar_name(path: str) -> str:
    """'C:/WINDOWS/x' -> 'C/WINDOWS/x' (no ':' or leading '/' in tar names)."""
    root, _, rest = path.partition("/")
    root = root.rstrip(":").replace(":", "") or "root"
    return f"{root}/{rest}" if rest else root


@dataclass(frozen=True)
class VolumeEntry:
    """One directory or file of a backup volume, as tar writes it and inspect lists it."""

    path: str  # the backup's own path ("C:/WINDOWS/WIN.INI"); damage reports use it
    tar_name: str  # the tar member name (Extended: drive colon dropped)
    is_dir: bool
    size: int  # bytes the backup recorded for the file (0 for a directory)
    mtime: int | None  # seconds since 1970 UTC; None = not recorded
    attrs: int  # the backup's attribute byte (DOS or QIC-113, per the parser)
    mode: int  # permission bits derived from attrs (0o755 / 0o644 / 0o444)
    data_offset: int | None  # where the file's bytes start in the volume; None = nowhere
    missing: int  # bytes of the file that fell in holes (zero-filled in a tar)
    error: bool  # the backup software's own "could not read this file" flag
    # Basic-DOS only: listed in the directory but its data entry was never
    # found, so all ``size`` bytes are missing and read back as zeros.
    lost: bool = False

    @property
    def damage(self) -> str | None:
        """'lost' (bytes in holes), 'error' (backup software flag only), or None.

        Directories are never damaged: tar only reports files, and inspect
        must count exactly what tar counts. A file both flagged and short of
        bytes is 'lost', the damage report's rule.
        """
        if self.is_dir or not (self.missing or self.error):
            return None
        return "error" if self.error and not self.missing else "lost"

    @property
    def mode_string(self) -> str:
        """``ls -l`` / ``tar tv`` type and mode, e.g. ``-r--r--r--``, ``drwxr-xr-x``."""
        kind = stat.S_IFDIR if self.is_dir else stat.S_IFREG
        return stat.filemode(kind | self.mode)

    @property
    def name(self) -> str:
        """The last path component: what the standard checks test for being text."""
        return self.path.rpartition("/")[2] or self.path
