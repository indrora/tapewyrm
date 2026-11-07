"""What every file-set parser shares: the TWVL header's VTBL, and the standard checks.

**The VTBL from the header.** Where a volume's sections are comes from the
TWVL header, never from the raw VTBL record (TWS-3 section 6.2, rule 9):
``data_section_size``, ``dir_section_size``, ``directory_offset``,
``vtbl.flags`` and ``vtbl.compressed`` were decoded by ``qicsilver extract``
through the volume profile that fits the tape, while bytes 57-127 of the raw
record mean different things to different backup programs (an MTN tape
keeps part of its label where Rev N keeps the data section size).
:func:`header_vtbl` builds the one :class:`~qiclib.volume.VtblEntry` every
parser uses from those members; only the QIC-113 signature at bytes 58-61,
which the header does not carry, is read from ``vtbl.raw``.

**The standard checks.** Lifted from what QICStreamReader's readers test
before trusting a record (names printable, sizes nonzero and in bounds,
dates that decode) and from what the wrong parser gets wrong (the unk-co
tape under the Basic-DOS parser: every name empty, every size zero, every
date in 1970). Every parser's probe runs them; a parser adds its own in
its :class:`~qiclib.fileset.base.Reading`.

Library code (STYLE.md §2.5): it logs, raises ``ValueError`` /
``MalformedFileError``, and prints nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tapewyrm_archive.errors import MalformedFileError
from tapewyrm_archive.twvl import Volume

from qiclib.fileset.entry import VolumeEntry
from qiclib.verdict import Check
from qiclib.volume import VTBL_ENTRY_LEN, VtblEntry, decode_short_date, parse_vtbl_base

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# The volume table entry, from the TWVL header (TWS-3 sections 3, 6.2)
# ---------------------------------------------------------------------------

# What a member read by header_member must hold, worded for the error
# message. The check uses type(), not isinstance: bool is an int subclass,
# and an integer member must not accept true/false.
INT, BOOL = "a non-negative integer", "true or false"


def header_member(
    volume_path: Path, obj: dict, where: str, name: str, kind: str, nullable: bool
) -> Any:
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
    ok = (type(value) is int and value >= 0) if kind == INT else type(value) is bool
    if not ok:
        log.debug("%s: %s is %r, not %s; refusing", volume_path, label, value, kind)
        expected = f"{kind} or null" if nullable else kind
        raise MalformedFileError(
            volume_path, "TWVL", f"{label} is {value!r}; it must be {expected}"
        )
    return value


def header_vtbl(volume_path: Path, hdr: dict) -> VtblEntry:
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
    flags = header_member(volume_path, vtbl, "vtbl.", "flags", INT, nullable=False)
    if flags > 0xFF:
        log.debug("%s: vtbl.flags %d is not a byte; refusing", volume_path, flags)
        raise MalformedFileError(volume_path, "TWVL", f"vtbl.flags is {flags}; it is one byte")
    entry = replace(
        parse_vtbl_base(raw),
        flags=flags,
        compressed=header_member(volume_path, vtbl, "vtbl.", "compressed", BOOL, nullable=True),
        # The top-level copies, which TWS-3 3.2 has writers keep equal to
        # vtbl's; rule 9 names these.
        data_section_size=header_member(
            volume_path, hdr, "", "data_section_size", INT, nullable=True
        ),
        dir_section_size=header_member(
            volume_path, hdr, "", "dir_section_size", INT, nullable=True
        ),
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


def directory_offset(volume_path: Path, hdr: dict) -> int | None:
    """``directory_offset`` from the header: null = the writer did not know it exactly.

    Consumers then locate the directory themselves (TWS-3 4.4, 6.2 rule 8).
    """
    return header_member(volume_path, hdr, "", "directory_offset", INT, nullable=True)


def volume_name(vol: Volume, source: Path) -> str:
    """``source``, plus ``volume N`` when the header says which volume of the tape it is.

    For a message about a volume read out of an image, the image's name
    alone would not say which of its volumes was meant.
    """
    index = vol.header.get("volume_index")
    return str(source) if index is None else f"{source} volume {index}"


# ---------------------------------------------------------------------------
# The standard checks
# ---------------------------------------------------------------------------

#: No DOS-era backup holds a file older than the DOS epoch, 1980-01-01.
DOS_EPOCH = int(datetime(1980, 1, 1, tzinfo=UTC).timestamp())
#: Files cannot be newer than the backup that holds them, give or take a
#: day of clock skew between the backed-up machine and the backup's own clock.
BACKUP_SLACK = timedelta(days=1)
#: The share of dated entries that may fall outside the era and still pass,
#: never fewer than one entry. A file with a wild date (a 2038 timestamp, a
#: never-set DOS date, a root directory stamped 0) is normal in a real backup;
#: a layout read wrong gets most of them wrong.
DATE_TOLERANCE = 0.1


def is_text(name: str) -> bool:
    """Non-empty, no control characters, nothing the decoder replaced (U+FFFD)."""
    return bool(name) and all(0x20 <= ord(ch) != 0x7F for ch in name) and "�" not in name


def date_ceiling(vtbl: VtblEntry, now: datetime | None = None) -> int:
    """Seconds since 1970 of the latest believable file time: the backup's date, or now."""
    when = decode_short_date(vtbl.date)
    if when is None:
        log.debug("date ceiling: no backup date in the VTBL entry; using now")
        return int((now or datetime.now(UTC)).timestamp())
    return int((datetime(*when, tzinfo=UTC) + BACKUP_SLACK).timestamp())


def standard_checks(
    entries: Sequence[VolumeEntry],
    vol: Volume,
    vtbl: VtblEntry,
    *,
    now: datetime | None = None,
) -> list[Check]:
    """Is this listing believable? The tests every backup catalog passes when read right.

    Weights: *entries* and *names* are worth 2, since an empty listing or
    one of unprintable names is the classic sign of the wrong layout; the
    rest 1. ``now`` pins the date ceiling for tests.
    """
    checks: list[Check] = []
    total = len(entries)
    checks.append(Check("entries", total > 0, 2, f"{total} entries"))
    if not entries:
        log.debug("standard checks: no entries; the remaining checks have nothing to test")
        return checks
    unreadable = sum(not is_text(e.name) for e in entries)
    checks.append(
        Check("names are text", unreadable == 0, 2, f"{unreadable} of {total} names unprintable")
    )
    size = len(vol.data)
    placed = [e for e in entries if not e.is_dir and e.data_offset is not None]
    overrun = sum(e.data_offset + e.size > size for e in placed if e.data_offset is not None)
    checks.append(
        Check(
            "sizes fit",
            overrun == 0,
            detail=f"{overrun} of {len(placed)} placed files run past the {size}-byte volume",
        )
    )
    files = [e for e in entries if not e.is_dir]
    if len(files) >= 2:
        # One file may well be empty; a backup of nothing but empty files is
        # the wrong layout reading zeros as sizes.
        with_bytes = sum(e.size > 0 for e in files)
        checks.append(
            Check("files have data", with_bytes > 0, detail=f"{with_bytes} of {len(files)} files")
        )
    dated = [e.mtime for e in entries if e.mtime is not None]
    if dated:
        ceiling = date_ceiling(vtbl, now)
        in_era = sum(DOS_EPOCH <= t <= ceiling for t in dated)
        allowed = max(1, int(DATE_TOLERANCE * len(dated)))
        checks.append(
            Check(
                "dates in era",
                len(dated) - in_era <= allowed,
                detail=f"{in_era} of {len(dated)} between 1980 and "
                f"{datetime.fromtimestamp(ceiling, UTC):%Y-%m-%d}",
            )
        )
    return checks
