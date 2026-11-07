"""Volume profiles: which software wrote a tape, as data (TOML), and how to read what it wrote.

Drive profiles (``tapewyrm.qic117.profile``, tapewyrm-cli) capture how a
*drive* misbehaves; cartridge profiles (:mod:`qiclib.cartridge`) describe
the physical tape; volume profiles capture how the *backup program that
wrote a tape* laid its data out. One profile answers three questions, one
per layer of a volume read (DESIGN.md §7.8):

1. **``[vtbl]``: how are VTBL bytes 57-127 laid out?** QIC-80-MC Rev N §8
   only pins down bytes 0-56 of a 128-byte ``VTBL`` record (signature,
   segment range, description, date, flags). Section sizes, source label,
   compression and OS type move with the writer: Rev N itself puts them at
   92/96/106/124/125; QIC-113 Rev G §6 keeps those offsets behind a ``113``
   signature at byte 58 (Colorado/HP CMS backup adds ``CMS`` at 62); the
   software behind the "MTN" tapes packed the label 4 bytes earlier.
2. **``[extent]``: how do the volume's segments become bytes?** The width
   of each QIC-122 extent's uncompressed-offset field (8 per Rev G, 4 on
   the MTN tapes) and of each frame's size field (2 per Rev G, 4 on
   NovaStor tapes), see :mod:`qiclib.qic122`.
3. **``[fileset]``: which parser reads those bytes, with what parameters?**
   A :mod:`qiclib.fileset` parser by name: ``qic113-basic``,
   ``qic113-extended``, or ``none`` for a format that is recognised but
   cannot be listed yet.

A profile is a TOML file in ``qiclib/profiles/volume/`` (or any path)::

    name = "cms-qic113"
    description = "..."
    status = "bench"                 # "spec" | "bench" | "untested"
    tapes = ["jc", "unk-co"]         # bench tapes it was confirmed on
    warnings = []                    # static warnings (qiclib.verdict.VolumeWarning names)

    [match]                          # the fingerprint: a GATE, every item must hold
    vtbl_bytes = [[58, "7100"], [62, "434d53"]]   # [offset, hex] in the VTBL record
    vendor_bit = false               # VTBL byte 56 bit 0 (only when the layout needs it)
    format_codes = [2]               # header format codes this software used
    volume_bytes = [[4, "46534554"]] # [offset, hex] at the start of the VOLUME bytes

    [vtbl]                           # [offset, length]; a field left out is unknown
    multi_cartridge_seq = [57, 1]
    dir_section_size = [92, 4]
    data_section_size = [96, 8]
    source_label = [106, 16]
    compression = [124, 1]           # bit 7 = compressed, bits 0-5 = QIC-123 code
    os_type = [125, 1]

    [extent]                         # optional
    offset_bytes = 8                 # 4 | 8; 8 when absent
    frame_size_bytes = 2             # 2 | 4; 2 when absent

    [fileset]                        # optional; "none" when absent
    parser = "qic113-extended"       # a qiclib.fileset parser name
    # any further key is a parameter of that parser

**Fingerprints are gates, not hints.** A ``[match]`` item is something the
writer *wrote* (``113`` at 58, ``CMS`` at 62, ``MTN`` at 58): a record that
lacks it was not written by that software, so the profile is *eliminated*
for the tape. They used to be worth +/-2 and a profile could win with one
failed, which is how the Rev N layout beat the CMS layout on the unk-co
tape (2026-11-07): CMS's hint was the vendor-specific bit, a flag the same
software set on a QIC-80 cartridge and not on a QIC-3010 one. The rule
since: match on what the software wrote, never on a flag; and a failed
match is final.

**Scoring.** Every surviving profile decodes every ``VTBL`` record and the
reading is scored with plausibility checks: does the segment range sit in
the data area, is the date sane, is the label printable text, is the
compression code one we know, does the claimed size fit in the segments it
occupies. Each check is +1 / -1 (the gate items +2 / -2, for the ``--raw``
display); the best total wins. A wrong layout reads neighbouring fields as
sizes and labels, which fail these checks loudly (the 3M tape under Rev N
claims a 4.9-billion-GB volume), so the margin is usually wide; when it is
not (:data:`~qiclib.verdict.CLOSE_MARGIN`), the winner carries
``low-confidence``, and two profiles that read the table *identically* make
it ``vtbl-ambiguous``.

**Then the parser decides.** The table only says who wrote the tape;
whether the volume's bytes read as that software's catalog is the
``[fileset]`` parser's probe, run by ``qiclib.extract`` once the volume is
built (:mod:`qiclib.fileset`). A profile whose parser fails there loses
even after winning here.

**Warnings.** A profile's ``status`` says how much it has been exercised:
``spec`` (from a standard), ``bench`` (confirmed on the tapes listed) or
``untested`` (written from a spec or someone else's reader; raises
``untested``). A ``bench`` profile with one tape raises ``single-tape``; a
profile with no positive fingerprint (``qic80-rev-n``, ``vendor-unknown``)
raises ``generic`` because it only ever wins by default. Static
``warnings`` are the profile author's own (``special-fs``, say). All of
them travel with the verdict into ``identify --json`` and the TWVL header.

Scope: ``qicsilver identify``, ``extract``, ``inspect`` and ``tar`` all read
volume tables through this (:mod:`qiclib.identify`, :mod:`qiclib.extract`).
The fixed Rev N / QIC-113 parser in :mod:`qiclib.volume` remains the
reference; tests/test_volume_profile.py checks that the ``qic80-rev-n`` and
``qic113-rev-g`` profiles agree with it.
"""

from __future__ import annotations

import logging
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from qiclib import fileset as fs
from qiclib.verdict import CLOSE_MARGIN, Check, Probe, VolumeWarning, parse_warning
from qiclib.volume import (
    SEGMENT_DATA_BYTES,  # noqa: F401  (re-exported: extract and the tests read it here)
    VolumeInfo,
    VtblEntry,
    decode_short_date,
    parse_vtbl_base,
)

log = logging.getLogger(__name__)

PROFILES_DIR = Path(__file__).resolve().parent / "profiles" / "volume"
GUESS = "guess"

# The extended VTBL fields a profile may place. Numeric fields are little
# endian; ``source_label`` is space-filled ASCII; ``compression`` is one byte.
FIELD_NAMES = frozenset(
    {
        "multi_cartridge_seq",
        "dir_section_size",
        "data_section_size",
        "source_label",
        "compression",
        "os_type",
    }
)
#: How far a profile has been exercised (the ``status`` key).
STATUSES = ("spec", "bench", "untested")

# QIC-123 compression codes we can name: 0 = none, 1 = QIC-122 compliant.
KNOWN_COMPRESSION_CODES = frozenset({0, 1})
# Upper bound on a believable compression ratio when checking sizes. QIC-122
# (Stac LZS) on 1990s files is typically 1.5-2.5:1; 4 is generous on purpose,
# so the check only catches nonsense like a size read from the wrong bytes.
MAX_COMPRESSION_RATIO = 4
HINT_WEIGHT = 2


class VolumeProfileError(Exception):
    """A volume profile could not be loaded or is malformed."""


@dataclass(frozen=True)
class FieldSpec:
    offset: int
    length: int


@dataclass(frozen=True)
class VolumeProfile:
    name: str
    description: str = ""
    status: str = "untested"
    tapes: tuple[str, ...] = ()  # bench tapes a "bench" profile was confirmed on
    warnings: frozenset[VolumeWarning] = frozenset()  # the author's static warnings
    vendor_bit: bool | None = None  # [match] gate; None = no opinion
    vtbl_bytes: tuple[tuple[int, bytes], ...] = ()  # [match] gate: bytes in the VTBL record
    format_codes: frozenset[int] = frozenset()  # [match] gate; empty = no opinion
    volume_bytes: tuple[tuple[int, bytes], ...] = ()  # [match] gate: bytes in the volume
    fields: dict[str, FieldSpec] = field(default_factory=dict)
    path: str = ""
    # [extent] offset_bytes: width of each data segment's QIC-113 extent
    # offset field (qic122.decode_extent). 8 per Rev G; 4 on older software.
    extent_offset_bytes: int = 8
    # [extent] frame_size_bytes: width of each frame's size field. 2 per Rev
    # G; 4 on NovaStor tapes.
    extent_frame_size_bytes: int = 2
    # [fileset] parser and its parameters (the parser's defaults filled in).
    fileset_parser: str = fs.NONE
    fileset_params: Mapping[str, Any] = field(default_factory=dict)

    @property
    def positive_match(self) -> bool:
        """Does the fingerprint name anything the writer wrote (not just a flag)?"""
        return bool(self.vtbl_bytes or self.volume_bytes or self.format_codes)

    @property
    def static_warnings(self) -> frozenset[VolumeWarning]:
        """The warnings this profile raises whenever it is chosen."""
        out = set(self.warnings)
        if self.status == "untested":
            out.add(VolumeWarning.UNTESTED)
        elif self.status == "bench" and len(self.tapes) == 1:
            out.add(VolumeWarning.SINGLE_TAPE)
        if not self.positive_match:
            out.add(VolumeWarning.GENERIC)
        return frozenset(out)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _resolve_path(name_or_path: str) -> Path:
    """A bare name resolves in the packaged directory; anything path-like is a path.

    Same rule as ``tapewyrm.qic117.profile`` so the two flags behave alike.
    """
    p = Path(name_or_path)
    if p.suffix == ".toml" or p.exists() or p.is_absolute() or len(p.parts) > 1:
        log.debug("volume profile %r is path-like; using it as a path", name_or_path)
        return p
    log.debug("volume profile %r is a bare name; looking in %s", name_or_path, PROFILES_DIR)
    return PROFILES_DIR / f"{name_or_path}.toml"


def load(name_or_path: str) -> VolumeProfile:
    path = _resolve_path(name_or_path)
    if not path.is_file():
        log.debug("volume profile %s is not a file; raising", path)
        known = ", ".join(builtin_names())
        raise VolumeProfileError(f"no volume profile {name_or_path!r} (built in: {known})")
    log.debug("reading volume profile %s", path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        log.debug("volume profile %s: TOML error %s; raising", path, exc)
        raise VolumeProfileError(f"{path}: {exc}") from exc
    return _from_dict(data, path)


def _byte_pairs(path: Path, section: str, key: str, items: Any, limit: int | None) -> tuple:
    """``[[offset, hex], ...]`` -> ((offset, bytes), ...), each inside ``limit`` when given."""
    out = []
    for item in items:
        try:
            offset, hexstr = item
            expected = bytes.fromhex(hexstr)
            offset = int(offset)
        except (TypeError, ValueError) as exc:
            log.debug(
                "volume profile %s: [%s] %s item %r malformed; raising", path, section, key, item
            )
            raise VolumeProfileError(
                f"{path}: [{section}] {key} items are [offset, hex], not {item!r}"
            ) from exc
        if not expected or offset < 0 or (limit is not None and offset + len(expected) > limit):
            log.debug(
                "volume profile %s: [%s] %s item %r out of range (limit %s); raising",
                path,
                section,
                key,
                item,
                limit,
            )
            raise VolumeProfileError(f"{path}: [{section}] {key} item {item!r} is out of range")
        out.append((offset, expected))
    return tuple(out)


def _fileset(path: Path, data: dict) -> tuple[str, dict[str, Any]]:
    """The ``[fileset]`` section: a registered parser and its parameters, defaults filled in."""
    section = data.get("fileset")
    if section is None:
        log.debug("volume profile %s: no [fileset]; parser %r", path, fs.NONE)
        return fs.NONE, {}
    if not isinstance(section, dict) or "parser" not in section:
        log.debug("volume profile %s: [fileset] %r has no parser key; raising", path, section)
        raise VolumeProfileError(f"{path}: [fileset] needs a parser key (one of {fs.names()})")
    name = str(section["parser"])
    try:
        parser = fs.get(name)
    except ValueError as exc:
        log.debug("volume profile %s: [fileset] parser %r unknown; raising", path, name)
        raise VolumeProfileError(f"{path}: [fileset] {exc}") from exc
    params = dict(parser.params)
    unknown = sorted(set(section) - {"parser"} - set(parser.params))
    if unknown:
        log.debug(
            "volume profile %s: [fileset] keys %s not parameters of %s; raising",
            path,
            unknown,
            name,
        )
        accepted = ", ".join(sorted(parser.params)) or "none"
        raise VolumeProfileError(
            f"{path}: [fileset] key(s) {', '.join(unknown)} are not parameters of "
            f"the {name} parser (accepted: {accepted})"
        )
    params.update({k: v for k, v in section.items() if k != "parser"})
    return name, params


def _from_dict(data: dict, path: Path) -> VolumeProfile:
    if "name" not in data:
        log.debug("volume profile %s has no 'name' key (keys %s); raising", path, sorted(data))
        raise VolumeProfileError(f"{path}: missing 'name'")
    status = str(data.get("status", "untested"))
    if status not in STATUSES:
        log.debug("volume profile %s: status %r not one of %s; raising", path, status, STATUSES)
        raise VolumeProfileError(f"{path}: status = {status!r}; it is one of {', '.join(STATUSES)}")
    tapes = data.get("tapes", [])
    if not isinstance(tapes, list) or not all(isinstance(t, str) for t in tapes):
        log.debug("volume profile %s: tapes %r is not a list of strings; raising", path, tapes)
        raise VolumeProfileError(f"{path}: tapes must be a list of tape names")
    try:
        warnings = frozenset(parse_warning(str(w)) for w in data.get("warnings", []))
    except ValueError as exc:
        log.debug("volume profile %s: warnings %r; raising", path, data.get("warnings"))
        raise VolumeProfileError(f"{path}: {exc}") from exc
    match = data.get("match", {})
    unknown = sorted(set(match) - {"vendor_bit", "vtbl_bytes", "format_codes", "volume_bytes"})
    if unknown:
        log.debug("volume profile %s: [match] keys %s not known; raising", path, unknown)
        raise VolumeProfileError(f"{path}: unknown [match] key(s) {', '.join(unknown)}")
    fields: dict[str, FieldSpec] = {}
    for key, value in data.get("vtbl", {}).items():
        if key not in FIELD_NAMES:
            log.debug("volume profile %s: [vtbl] key %r not a known field; raising", path, key)
            raise VolumeProfileError(
                f"{path}: unknown [vtbl] field {key!r} (known: {', '.join(sorted(FIELD_NAMES))})"
            )
        offset, length = value
        if not (57 <= offset and offset + length <= 128 and length > 0):
            # Bytes 0-56 are fixed by Rev N for everyone; profiles only place
            # what comes after.
            log.debug(
                "volume profile %s: [vtbl] %s offset %s length %s outside 57-127; raising",
                path,
                key,
                offset,
                length,
            )
            raise VolumeProfileError(f"{path}: [vtbl] {key} = {value} is outside bytes 57-127")
        fields[key] = FieldSpec(int(offset), int(length))
    extent = data.get("extent", {})
    unknown = sorted(set(extent) - {"offset_bytes", "frame_size_bytes"})
    if unknown:
        log.debug("volume profile %s: [extent] keys %s not known; raising", path, unknown)
        raise VolumeProfileError(f"{path}: unknown [extent] key(s) {', '.join(unknown)}")
    offset_bytes = extent.get("offset_bytes", 8)
    if offset_bytes not in (4, 8):
        log.debug(
            "volume profile %s: [extent] offset_bytes %r not 4 or 8; raising", path, offset_bytes
        )
        raise VolumeProfileError(f"{path}: [extent] offset_bytes = {offset_bytes!r} must be 4 or 8")
    frame_size_bytes = extent.get("frame_size_bytes", 2)
    if frame_size_bytes not in (2, 4):
        log.debug(
            "volume profile %s: [extent] frame_size_bytes %r not 2 or 4; raising",
            path,
            frame_size_bytes,
        )
        raise VolumeProfileError(
            f"{path}: [extent] frame_size_bytes = {frame_size_bytes!r} must be 2 or 4"
        )
    parser, params = _fileset(path, data)
    return VolumeProfile(
        name=str(data["name"]),
        description=str(data.get("description", "")),
        status=status,
        tapes=tuple(tapes),
        warnings=warnings,
        vendor_bit=match.get("vendor_bit"),
        vtbl_bytes=_byte_pairs(path, "match", "vtbl_bytes", match.get("vtbl_bytes", []), 128),
        format_codes=frozenset(int(c) for c in match.get("format_codes", [])),
        volume_bytes=_byte_pairs(
            path, "match", "volume_bytes", match.get("volume_bytes", []), None
        ),
        fields=fields,
        path=str(path),
        extent_offset_bytes=int(offset_bytes),
        extent_frame_size_bytes=int(frame_size_bytes),
        fileset_parser=parser,
        fileset_params=params,
    )


def builtin_names() -> list[str]:
    if not PROFILES_DIR.is_dir():
        log.debug("profile directory %s missing; no built-in volume profiles", PROFILES_DIR)
        return []
    return sorted(p.stem for p in PROFILES_DIR.iterdir() if p.suffix == ".toml")


def load_builtin() -> list[VolumeProfile]:
    return [load(name) for name in builtin_names()]


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------


def _int(rec: bytes, spec: FieldSpec) -> int:
    return int.from_bytes(rec[spec.offset : spec.offset + spec.length], "little")


def decode_entry(rec: bytes, profile: VolumeProfile) -> VtblEntry:
    """Decode one 128-byte VTBL record using ``profile``'s field layout."""
    entry = parse_vtbl_base(rec)
    f = profile.fields
    updates: dict = {}
    if "multi_cartridge_seq" in f:
        updates["multi_cartridge_seq"] = _int(rec, f["multi_cartridge_seq"])
    if "dir_section_size" in f:
        updates["dir_section_size"] = _int(rec, f["dir_section_size"])
    if "data_section_size" in f:
        updates["data_section_size"] = _int(rec, f["data_section_size"])
    if "source_label" in f:
        spec = f["source_label"]
        raw = rec[spec.offset : spec.offset + spec.length]
        # Keep control characters visible to the checks: a label read from the
        # wrong offset usually picks up binary bytes, and that is the tell.
        updates["source_label"] = raw.split(b"\x00", 1)[0].decode("latin-1").rstrip(" ")
    if "compression" in f:
        byte = _int(rec, f["compression"])
        updates["compressed"] = bool(byte & 0x80)
        updates["compression_code"] = byte & 0x3F
    if "os_type" in f:
        updates["os_type"] = _int(rec, f["os_type"])
    return replace(entry, **updates)


# ---------------------------------------------------------------------------
# Gates, plausibility checks and guessing
# ---------------------------------------------------------------------------


@dataclass
class Verdict:
    """One profile's reading of the whole volume table, and how believable it is.

    ``checks`` holds every check, the gate items (``[match]``) first;
    ``gate`` is just those. A verdict with a failed gate item is
    ``eliminated``: the tape was not written by this profile's software,
    whatever the rest scores. ``warnings`` starts as the profile's own and
    gains the selection's (``low-confidence``, ``vtbl-ambiguous``) in
    :func:`guess`; ``probe`` is filled in by ``qiclib.extract`` once the
    volume is built and its parser has had its say.
    """

    profile: VolumeProfile
    entries: list[VtblEntry]
    checks: list[Check]
    gate: list[Check] = field(default_factory=list)
    warnings: set[VolumeWarning] = field(default_factory=set)
    probe: Probe | None = None

    @property
    def score(self) -> int:
        return sum(c.points for c in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    @property
    def eliminated(self) -> bool:
        return any(not c.ok for c in self.gate)

    def to_dict(self) -> dict[str, Any]:
        """The ``identify --json`` row for this profile."""
        return {
            "name": self.profile.name,
            "score": self.score,
            "eliminated": self.eliminated,
            "warnings": sorted(w.value for w in self.warnings),
            "checks": [c.to_dict() for c in self.checks],
            "probe": self.probe.to_dict() if self.probe is not None else None,
        }


def _printable(text: str) -> bool:
    return all(32 <= ord(ch) < 127 for ch in text)


def match_checks(rec: bytes, vol: VolumeInfo, profile: VolumeProfile) -> list[Check]:
    """The profile's ``[match]`` gate against one record: every one must hold."""
    checks: list[Check] = []
    if profile.vendor_bit is not None:
        actual = bool(rec[56] & 0x01)
        checks.append(
            Check(
                "vendor bit",
                actual == profile.vendor_bit,
                HINT_WEIGHT,
                f"byte 56 bit 0 = {int(actual)}",
            )
        )
    for offset, expected in profile.vtbl_bytes:
        actual_bytes = rec[offset : offset + len(expected)]
        checks.append(
            Check(
                f"bytes {offset}-{offset + len(expected) - 1}",
                actual_bytes == expected,
                HINT_WEIGHT,
                f"{actual_bytes.hex()} vs {expected.hex()}",
            )
        )
    if profile.format_codes:
        checks.append(
            Check(
                "format code",
                vol.format_code in profile.format_codes,
                HINT_WEIGHT,
                f"{vol.format_code} vs {sorted(profile.format_codes)}",
            )
        )
    return checks


def volume_gate(profile: VolumeProfile, head: bytes) -> list[Check]:
    """The profile's ``[match] volume_bytes`` against the start of a built volume.

    The one gate the table cannot answer, so ``qiclib.extract`` runs it once
    the volume's first bytes exist. ``head`` is as much of the volume as the
    caller has; a signature past its end fails.
    """
    checks: list[Check] = []
    for offset, expected in profile.volume_bytes:
        actual = head[offset : offset + len(expected)]
        checks.append(
            Check(
                f"volume bytes {offset}-{offset + len(expected) - 1}",
                actual == expected,
                HINT_WEIGHT,
                f"{actual.hex()} vs {expected.hex()}",
            )
        )
    return checks


def entry_checks(entry: VtblEntry, vol: VolumeInfo, now: datetime | None = None) -> list[Check]:
    """Is this decoded entry believable? Only fields the profile defined are checked."""
    checks: list[Check] = []
    first = vol.first_data_seg
    last = vol.last_data_seg or 0xFFFF
    checks.append(
        Check(
            "segment range",
            first <= entry.start_seg <= entry.end_seg <= last,
            detail=f"{entry.start_seg}-{entry.end_seg} in data area {first}-{last}",
        )
    )
    when = decode_short_date(entry.date)
    if when is not None:
        stamp = datetime(*when, tzinfo=UTC)
        earliest_packed = vol.initial_format_date or vol.format_date
        earliest = decode_short_date(earliest_packed) if earliest_packed else None
        floor = datetime(*earliest, tzinfo=UTC) if earliest else datetime(1985, 1, 1, tzinfo=UTC)
        ceiling = now or datetime.now(UTC)
        checks.append(
            Check(
                "date", floor <= stamp <= ceiling, detail=f"{stamp:%Y-%m-%d} after {floor:%Y-%m-%d}"
            )
        )
    if entry.source_label is not None:
        checks.append(
            Check("label is text", _printable(entry.source_label), detail=repr(entry.source_label))
        )
    if entry.compression_code is not None:
        code = entry.compression_code
        known = code in KNOWN_COMPRESSION_CODES and (entry.compressed or code == 0)
        checks.append(
            Check("compression code", known, detail=f"compressed={entry.compressed} code={code}")
        )
    if entry.multi_cartridge_seq is not None:
        checks.append(
            Check(
                "cartridge sequence",
                entry.multi_cartridge_seq <= 16,
                detail=str(entry.multi_cartridge_seq),
            )
        )
    if entry.data_section_size is not None or entry.dir_section_size is not None:
        total = (entry.data_section_size or 0) + (entry.dir_section_size or 0)
        span = max(0, entry.end_seg - entry.start_seg + 1) * SEGMENT_DATA_BYTES
        ratio = 1 if entry.compressed is False else MAX_COMPRESSION_RATIO
        checks.append(
            Check(
                "size fits",
                0 < total <= span * ratio,
                detail=f"{total} bytes in {span} bytes of segments (x{ratio})",
            )
        )
    return checks


def evaluate(
    records: list[bytes], vol: VolumeInfo, profile: VolumeProfile, now: datetime | None = None
) -> Verdict:
    entries: list[VtblEntry] = []
    checks: list[Check] = []
    gate: list[Check] = []
    log.debug("volume profile %s: evaluating %d VTBL records", profile.name, len(records))
    for rec in records:
        entry = decode_entry(rec, profile)
        entries.append(entry)
        hints = match_checks(rec, vol, profile)
        gate += hints
        checks += hints
        checks += entry_checks(entry, vol, now)
    verdict = Verdict(profile, entries, checks, gate, set(profile.static_warnings))
    # score/failures walk the checks, so only pay for them when DEBUG is on.
    if log.isEnabledFor(logging.DEBUG):
        log.debug(
            "volume profile %s: score %d, %d of %d checks failed%s",
            profile.name,
            verdict.score,
            len(verdict.failures),
            len(checks),
            "; ELIMINATED by its fingerprint" if verdict.eliminated else "",
        )
    return verdict


def rank(verdicts: list[Verdict]) -> list[Verdict]:
    """Survivors first by score, then the eliminated by score (ties keep the given order)."""
    return sorted(verdicts, key=lambda v: (not v.eliminated, v.score), reverse=True)


def survivors(verdicts: list[Verdict]) -> list[Verdict]:
    """The verdicts whose fingerprint held, in the order given."""
    return [v for v in verdicts if not v.eliminated]


def guess(
    records: list[bytes],
    vol: VolumeInfo,
    profiles: list[VolumeProfile] | None = None,
    now: datetime | None = None,
) -> list[Verdict]:
    """Every profile's verdict, best first: survivors of the gate by score, then the eliminated.

    The winner (``[0]``, when it survived) carries the selection's warnings:
    ``vtbl-ambiguous`` when the runner-up read the table identically *and*
    scored the same, ``low-confidence`` when a runner-up that read it
    differently came within :data:`~qiclib.verdict.CLOSE_MARGIN` points. A
    runner-up with the same reading but a lower score is no competitor: it
    is the winner's own generalisation (``qic113-rev-g`` under
    ``cms-qic113``), beaten by exactly the hint that makes the winner
    specific. With no survivor at all the list is just the eliminated, best
    first, and the caller decides what to make of that (``identify`` shows
    it; ``extract`` refuses).
    """
    if profiles is None:
        log.debug("guess: no profiles given; loading the built-in ones")
    candidates = profiles if profiles is not None else load_builtin()
    log.debug("guess: scoring %d volume profiles against %d records", len(candidates), len(records))
    ranked = rank([evaluate(records, vol, p, now) for p in candidates])
    alive = survivors(ranked)
    if len(alive) >= 2:
        best, runner = alive[0], alive[1]
        if runner.entries == best.entries:
            if runner.score == best.score:
                log.debug(
                    "guess: %s and %s read the table identically at %d; vtbl-ambiguous",
                    best.profile.name,
                    runner.profile.name,
                    best.score,
                )
                best.warnings.add(VolumeWarning.VTBL_AMBIGUOUS)
            else:
                log.debug(
                    "guess: %s reads the table as %s does, a hint less specific; no warning",
                    runner.profile.name,
                    best.profile.name,
                )
        elif best.score - runner.score <= CLOSE_MARGIN:
            log.debug(
                "guess: %s %d vs %s %d is within %d; low-confidence",
                best.profile.name,
                best.score,
                runner.profile.name,
                runner.score,
                CLOSE_MARGIN,
            )
            best.warnings.add(VolumeWarning.LOW_CONFIDENCE)
    elif not alive:
        log.debug("guess: every profile was eliminated by its fingerprint")
    return ranked
