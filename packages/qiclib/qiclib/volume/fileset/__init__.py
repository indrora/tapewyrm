"""File-set parsers: the bytes of a backup volume -> its entries, with a verdict.

The third layer of a volume read (DESIGN.md §7.8; the other two are the
volume profile, which reads the volume table, and the extent decoder, which
turns a volume's segments into bytes). A volume's bytes are laid out by
whichever backup program wrote the tape: a QIC-113 Basic-DOS directory
(Conner Backup Basics, QICStream v1, the "MTN" tapes), a QIC-113 Extended
one (Colorado/HP CMS backup, Windows 95 Backup, Arcada), or one of the
proprietary catalogs QICStreamReader documents (QICStream v2's "FSET"
control codes, Microsoft Tape Format, NovaStor, Mountain FileSafe...). Each
layout is a parser here, named by a volume profile's ``[fileset] parser``
key, and every parser answers two questions:

* :meth:`~qiclib.volume.fileset.base.FileSetParser.probe`: *could this be right?*
  It parses, then scores what came out with the checks every backup catalog
  must pass (names that are text, sizes inside the volume, dates in the
  backup's era, data where the directory says) and its own. The
  :class:`~qiclib.volume.verdict.Probe` is the yea/nay a profile's fingerprint can
  only guess at: a layout whose fingerprint fits but whose parse reads 186
  nameless zero-byte files loses here.
* :meth:`~qiclib.volume.fileset.base.FileSetParser.entries`: the listing itself,
  one :class:`~qiclib.volume.fileset.entry.VolumeEntry` per directory or file, the
  record ``qicsilver tar`` writes and ``qicsilver inspect`` lists.

Warnings a parser raises (:class:`~qiclib.volume.verdict.VolumeWarning`) ride on
the probe: that the directory had holes, that entries carry what a tar
cannot hold. ``qiclib.extract`` writes the chosen parser and the warnings
into the TWVL header (TWS-3 section 3.4), so a volume carries its own
reading instructions; ``qicsilver.entries`` dispatches on them.

Built-in parsers register themselves when their module is imported, which
importing this package does. A parser is stdlib-only library code
(STYLE.md §2) and never prints.
"""

from __future__ import annotations

from qiclib.volume.fileset import (  # noqa: F401  (registers)
    none,
    qic113_basic,
    qic113_extended,
    qicstream_fset,
)
from qiclib.volume.fileset.base import (
    NONE,
    FileSetParser,
    Reading,
    get,
    listable,
    names,
    probe_all,
    register,
)
from qiclib.volume.fileset.common import directory_offset, header_member, header_vtbl, volume_name
from qiclib.volume.fileset.entry import DIR_MODE, RO_MODE, RW_MODE, VolumeEntry, tar_name

__all__ = [
    "DIR_MODE",
    "NONE",
    "RO_MODE",
    "RW_MODE",
    "FileSetParser",
    "Reading",
    "VolumeEntry",
    "directory_offset",
    "get",
    "header_member",
    "header_vtbl",
    "listable",
    "names",
    "probe_all",
    "register",
    "tar_name",
    "volume_name",
]
