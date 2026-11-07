"""The ``none`` parser: a format that is recognised but cannot be listed.

A volume profile whose ``[fileset] parser`` is ``none`` still earns its
keep: ``qicsilver identify`` can name the software that wrote the tape, and
``qicsilver extract`` can decode its extents into a TWVL for later, or for
another tool. What it cannot do is list or tar the volume, and the
``no-parser`` warning says so on every probe, in the TWVL header and in the
refusal ``inspect`` and ``tar`` give.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from tapewyrm_archive.twvl import Volume

from qiclib.volume.fileset.base import NONE, FileSetParser, Reading, register
from qiclib.volume.table import VtblEntry
from qiclib.volume.verdict import Probe, VolumeWarning

log = logging.getLogger(__name__)


class NoneParser(FileSetParser):
    name = NONE
    description = "no parser: the format is recognised, but its files cannot be listed yet"

    def read(
        self, vol: Volume, vtbl: VtblEntry, source: Path, params: Mapping[str, Any]
    ) -> Reading:
        log.debug("%s: the 'none' parser cannot list a volume; raising", source)
        raise ValueError(
            f"{source}: this volume's format has no file-set parser yet; the TWVL "
            "holds its bytes, but they cannot be listed or tarred"
        )

    def probe(
        self,
        vol: Volume,
        vtbl: VtblEntry,
        source: Path,
        params: Mapping[str, Any] | None = None,
        *,
        now: datetime | None = None,
    ) -> Probe:
        """Always fatal, and always says why: ``no-parser``."""
        log.debug("probe: 'none' parser on %s; fatal by definition", source)
        return Probe(fatal="no file-set parser for this format", warnings={VolumeWarning.NO_PARSER})


register(NoneParser())
