"""The entries of a TWVL backup volume: the one directory walk tar and inspect share.

``qicsilver tar`` writes these entries into a tar and ``qicsilver inspect``
lists them, so both read the volume through :func:`read_volume` /
:func:`list_volume` and can never disagree about which entries there are,
how big they are, or which are damaged. The walk itself is a file-set
parser (:mod:`qiclib.volume.fileset`), and which one is the TWVL header's to say:
``fileset.parser`` (TWS-3 section 3.4) was written by ``qicsilver extract``
from the volume profile that fit the tape and confirmed by that parser's
probe, so a volume carries its own reading instructions. The header's
``warnings`` ride along into the listing, where ``inspect`` prints them and
``tar`` keeps them in a pax global header.

A volume whose header names no parser (written before headers did, or by
hand) is probed with every parser and read with the best that passes; one
whose profile has no parser (``none``) is refused with the reason.

The entry record, :class:`~qiclib.volume.fileset.entry.VolumeEntry`, and the
header's VTBL (:func:`~qiclib.volume.fileset.common.header_vtbl`) live in qiclib,
where the parsers are, and are re-exported here for the two consumers.

Library code (STYLE.md §2.5): it logs, raises ``ValueError`` /
``MalformedFileError``, and prints nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from qiclib.volume import fileset as fs
from qiclib.volume.fileset import VolumeEntry, directory_offset, header_vtbl, tar_name
from qiclib.volume.table import VtblEntry
from qiclib.volume.verdict import VolumeWarning, parse_warning, warning_names
from tapewyrm_archive.errors import MalformedFileError
from tapewyrm_archive.qic80date import format_short_date
from tapewyrm_archive.twvl import Volume

log = logging.getLogger(__name__)

__all__ = [
    "VolumeEntry",
    "VolumeListing",
    "entry_data",
    "header_vtbl",
    "list_volume",
    "read_volume",
    "tar_name",
]

# The two QIC-113 parsers, as the listing has always named their formats.
_FORMAT_NAMES = {"qic113-extended": "Extended", "qic113-basic": "Basic-DOS"}


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass
class VolumeListing:
    """A TWVL volume and every entry of its backup, in directory order."""

    path: Path  # the file the volume came from: a TWVL, or the tape image it was read out of
    volume: Volume
    vtbl: VtblEntry
    parser: str  # the file-set parser that read it (a qiclib.volume.fileset name)
    entries: list[VolumeEntry]
    warnings: set[VolumeWarning] = field(default_factory=set)

    @property
    def extended(self) -> bool:
        """Read by the QIC-113 extended parser (long names, DOS attributes)."""
        return self.parser == "qic113-extended"

    @property
    def format_name(self) -> str:
        """The directory format: 'Extended' or 'Basic-DOS' for QIC-113, else the parser's name."""
        return _FORMAT_NAMES.get(self.parser, self.parser)

    @property
    def is_qic113(self) -> bool:
        return self.parser in _FORMAT_NAMES

    @property
    def attribute_key(self) -> str:
        """The pax header tar keeps the attribute byte in."""
        return "TAPEWYRM.dos_attributes" if self.extended else "TAPEWYRM.qic113_attributes"

    @property
    def warning_names(self) -> list[str]:
        """The warnings as sorted strings, for a summary line or a header."""
        return warning_names(self.warnings)

    # Display-only header members use .get: a header that lacks one still reads.
    @property
    def tape_name(self) -> str | None:
        return self.volume.header.get("tape_name")

    @property
    def volume_index(self) -> int | None:
        """The volume's number in the tape's volume table (``qicsilver identify`` order)."""
        return self.volume.header.get("volume_index")

    @property
    def profile(self) -> str | None:
        """The volume profile ``extract`` read the tape with, when the header says."""
        fileset = self.volume.header.get("fileset")
        return fileset.get("profile") if isinstance(fileset, dict) else None

    @property
    def description(self) -> str:
        return self.vtbl.description

    @property
    def date(self) -> str | None:
        """The volume's backup date, ``YYYY-MM-DD HH:MM:SS``, or None when undefined."""
        return format_short_date(self.vtbl.date)

    @property
    def lost_segments(self) -> list[int]:
        return list(self.volume.header.get("lost_segments") or [])

    @property
    def volume_bytes(self) -> int:
        return len(self.volume.data)

    @property
    def missing_bytes(self) -> int:
        """Volume bytes in holes (the whole volume, not only file data)."""
        return self.volume.missing(0, len(self.volume.data))

    @property
    def files(self) -> int:
        return sum(not e.is_dir for e in self.entries)

    @property
    def dirs(self) -> int:
        return sum(e.is_dir for e in self.entries)

    @property
    def damaged(self) -> int:
        return sum(e.damage is not None for e in self.entries)


# ---------------------------------------------------------------------------
# Reading a volume
# ---------------------------------------------------------------------------


def read_volume(volume_path: Path) -> VolumeListing:
    """Load a TWVL file and list every entry of its backup (:func:`list_volume`).

    Raises ``ValueError`` for a volume this cannot read (its format has no
    parser, or no parser passes on a header that names none), and
    ``MalformedFileError`` (a ``ValueError``) for a header member that is
    missing or mistyped.
    """
    log.info("reading volume %s...", volume_path)
    return list_volume(Volume.load(volume_path), volume_path)


def list_volume(vol: Volume, source: Path) -> VolumeListing:
    """List every entry of a volume that is already in memory.

    ``vol`` came from a TWVL file (:func:`read_volume`) or straight out of a
    tape image (``qiclib.extract.load_volume``, how ``qicsilver inspect
    IMAGE`` reads one). Both carry the same TWVL header, so a volume is
    listed identically whichever way it arrived. ``source`` is the file the
    bytes came from, named in messages and the listing. Raises as
    :func:`read_volume` does.
    """
    hdr = vol.header
    vtbl = header_vtbl(source, hdr)
    # Read once here so a malformed member is refused as such, before any
    # parser (whose probe would only report "could not read") sees it.
    directory_offset(source, hdr)
    warnings = _header_warnings(source, hdr)
    parser, params, chosen_warnings = _parser_for(vol, vtbl, source, hdr)
    warnings |= chosen_warnings
    log.info("file set: %s", parser.description)
    reading = parser.read(vol, vtbl, source, params)
    warnings |= reading.warnings
    if warnings:
        log.info("warnings: %s", ", ".join(warning_names(warnings)))
    return VolumeListing(source, vol, vtbl, parser.name, reading.entries, warnings)


def _header_warnings(source: Path, hdr: dict) -> set[VolumeWarning]:
    """The header's ``warnings`` (TWS-3 3.5): a list of warning names, absent on older files."""
    raw = hdr.get("warnings", [])
    if not isinstance(raw, list):
        log.debug("%s: warnings is %r, not a list; refusing", source, type(raw).__name__)
        raise MalformedFileError(source, "TWVL", "the header's warnings member is not an array")
    try:
        return {parse_warning(str(name)) for name in raw}
    except ValueError as exc:
        log.debug("%s: %s; refusing", source, exc)
        raise MalformedFileError(source, "TWVL", f"warnings: {exc}") from exc


def _parser_for(
    vol: Volume, vtbl: VtblEntry, source: Path, hdr: dict
) -> tuple[fs.FileSetParser, Mapping[str, Any], set[VolumeWarning]]:
    """The parser the header names, or the best one a probe of them all finds.

    Returns the parser, its parameters and the warnings choosing it raised
    (none when the header named it: ``extract`` already recorded those in
    the header's own ``warnings``).
    """
    fileset = hdr.get("fileset")
    if isinstance(fileset, dict) and fileset.get("parser"):
        name = str(fileset["parser"])
        try:
            parser = fs.get(name)
        except ValueError as exc:
            log.debug("%s: header names parser %r; refusing", source, name)
            raise MalformedFileError(source, "TWVL", f"fileset.parser: {exc}") from exc
        params = fileset.get("params") or {}
        if not isinstance(params, dict):
            log.debug("%s: fileset.params is %r; refusing", source, type(params).__name__)
            raise MalformedFileError(source, "TWVL", "fileset.params is not an object")
        log.debug(
            "%s: header names parser %s (profile %r)", source, parser.name, fileset.get("profile")
        )
        if parser.name == fs.NONE:
            log.debug("%s: the profile has no parser; refusing", source)
            raise ValueError(
                f"{source}: the volume profile {fileset.get('profile')!r} recognised this "
                "format but has no file-set parser for it; the volume cannot be listed yet"
            )
        return parser, params, set()
    if fileset is not None:
        log.debug("%s: fileset is %r; refusing", source, fileset)
        raise MalformedFileError(source, "TWVL", "the header's fileset member names no parser")
    # No parser named: a TWVL from before TWS-3 section 3.4, or one written
    # by hand. Every parser gets a go, and the best that passes reads it.
    log.info("%s: the header names no file-set parser; probing every parser", source)
    results = fs.probe_all(vol, vtbl, source)
    parser, probe = results[0]
    if not probe.passed:
        summary = "; ".join(f"{p.name}: {pr.fatal or f'score {pr.score}'}" for p, pr in results)
        log.debug("%s: no parser passed (%s); refusing", source, summary)
        raise ValueError(f"{source}: no file-set parser can read this volume ({summary})")
    log.info("probe chose the %s parser (score %d)", parser.name, probe.score)
    warnings = set(probe.warnings)
    if len(results) > 1 and results[1][1].passed:
        log.debug(
            "%s: %s also passed (score %d); low-confidence",
            source,
            results[1][0].name,
            results[1][1].score,
        )
        warnings.add(VolumeWarning.LOW_CONFIDENCE)
    return parser, {}, warnings


def entry_data(vol: Volume, entry: VolumeEntry) -> bytes:
    """The file's bytes as tar stores them: holes and lost files zero-filled.

    An Extended entry with no data area (or none of size) is empty, whatever
    its recorded size; a lost Basic-DOS file is ``size`` zeros.
    """
    if entry.lost:
        return bytes(entry.size)
    if entry.data_offset is None or not entry.size:
        return b""
    data, _ = vol.read(entry.data_offset, entry.size)
    return data
