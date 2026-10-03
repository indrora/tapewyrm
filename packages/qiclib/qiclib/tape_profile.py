"""Tape profiles: per-tape quirks of the volume table, as data (TOML).

Drive profiles (``qic117.profile``) capture how a *drive* misbehaves; tape
profiles capture how the *software that wrote a tape* laid out its volume table.
QIC-80-MC Rev N §8 only pins down bytes 0-56 of a 128-byte ``VTBL`` record
(signature, segment range, description, date, flags). Everything after that --
directory/data section sizes, source label, compression, OS type -- depends on
who wrote it:

* Rev N itself puts them at 92/96/106/124/125 (``qic80-rev-n``);
* Colorado's CMS backup sets the vendor bit and writes the QIC-113 signature
  (113 at byte 58), with the same offsets (``cms-qic113``);
* the 3M DC2120 bench tape's software wrote ``MTN`` at byte 58, left the
  vendor bit clear, and packed the label 4 bytes earlier (``mtn``);
* any other vendor-specific entry: only bytes 0-56 mean anything
  (``vendor-unknown``).

A profile is a TOML file in ``tapewyrm/profiles/tape/`` (or any path)::

    name = "mtn"
    description = "..."

    [match]                      # hints for --tape-profile guess (all optional)
    vendor_bit = false           # VTBL byte 56 bit 0
    vtbl_bytes = [[58, "4d544e"]]  # [offset, hex] that must appear in the record
    format_codes = [2]           # header format codes this software used

    [vtbl]                       # [offset, length]; a field left out is unknown
    multi_cartridge_seq = [57, 1]
    dir_section_size = [92, 4]
    data_section_size = [96, 4]
    source_label = [102, 16]
    compression = [120, 1]       # bit 7 = compressed, bits 0-5 = QIC-123 code
    os_type = [125, 1]

**Guessing.** ``guess`` decodes every ``VTBL`` record with every profile and
scores each reading with plausibility checks: does the segment range sit in the
data area, is the date sane, is the label printable text, is the compression
code one we know, does the claimed size fit in the segments it occupies. Each
``[match]`` hint that holds is worth +2 and each that fails -2; each other
check +1 / -1. The best total wins. A wrong layout reads neighbouring fields as
sizes and labels, which fail these checks loudly (the 3M tape under Rev N
claims a 4.9-billion-GB volume), so the margin is usually wide.

Scope: ``qicsilver identify`` and ``qicsilver extract`` both read volume
tables through this (``qiclib.identify``, ``qiclib.extract``). The fixed Rev N
/ QIC-113 parser in ``qiclib.volume`` remains as the reference;
tests/test_tape_profile.py checks that the ``qic80-rev-n`` and ``cms-qic113``
profiles agree with it.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from qiclib.volume import (
    DATA_SECTORS_PER_SEGMENT,
    VolumeInfo,
    VtblEntry,
    decode_short_date,
    parse_vtbl_base,
)

log = logging.getLogger(__name__)

PROFILES_DIR = Path(__file__).resolve().parent / "profiles" / "tape"
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

# QIC-123 compression codes we can name: 0 = none, 1 = QIC-122 compliant.
KNOWN_COMPRESSION_CODES = frozenset({0, 1})
# Upper bound on a believable compression ratio when checking sizes. QIC-122
# (Stac LZS) on 1990s files is typically 1.5-2.5:1; 4 is generous on purpose,
# so the check only catches nonsense like a size read from the wrong bytes.
MAX_COMPRESSION_RATIO = 4
SEGMENT_DATA_BYTES = DATA_SECTORS_PER_SEGMENT * 1024
HINT_WEIGHT = 2


class TapeProfileError(Exception):
    """A tape profile could not be loaded or is malformed."""


@dataclass(frozen=True)
class FieldSpec:
    offset: int
    length: int


@dataclass(frozen=True)
class TapeProfile:
    name: str
    description: str = ""
    vendor_bit: bool | None = None  # [match] hint; None = no opinion
    vtbl_bytes: tuple[tuple[int, bytes], ...] = ()  # [match] hint
    format_codes: frozenset[int] = frozenset()  # [match] hint; empty = no opinion
    fields: dict[str, FieldSpec] = field(default_factory=dict)
    path: str = ""
    # [extent] offset_bytes: width of each data segment's QIC-113 extent
    # offset field (qic122.decode_extent). 8 per Rev G; 4 on older software.
    extent_offset_bytes: int = 8


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _resolve_path(name_or_path: str) -> Path:
    """A bare name resolves in the packaged directory; anything path-like is a path.

    Same rule as ``qic117.profile`` so the two flags behave alike.
    """
    p = Path(name_or_path)
    if p.suffix == ".toml" or p.exists() or p.is_absolute() or len(p.parts) > 1:
        log.debug("tape profile %r is path-like; using it as a path", name_or_path)
        return p
    log.debug("tape profile %r is a bare name; looking in %s", name_or_path, PROFILES_DIR)
    return PROFILES_DIR / f"{name_or_path}.toml"


def load(name_or_path: str) -> TapeProfile:
    path = _resolve_path(name_or_path)
    if not path.is_file():
        log.debug("tape profile %s is not a file; raising", path)
        known = ", ".join(builtin_names())
        raise TapeProfileError(f"no tape profile {name_or_path!r} (built in: {known})")
    log.debug("reading tape profile %s", path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        log.debug("tape profile %s: TOML error %s; raising", path, exc)
        raise TapeProfileError(f"{path}: {exc}") from exc
    return _from_dict(data, path)


def _from_dict(data: dict, path: Path) -> TapeProfile:
    if "name" not in data:
        log.debug("tape profile %s has no 'name' key (keys %s); raising", path, sorted(data))
        raise TapeProfileError(f"{path}: missing 'name'")
    match = data.get("match", {})
    fields: dict[str, FieldSpec] = {}
    for key, value in data.get("vtbl", {}).items():
        if key not in FIELD_NAMES:
            log.debug("tape profile %s: [vtbl] key %r not a known field; raising", path, key)
            raise TapeProfileError(
                f"{path}: unknown [vtbl] field {key!r} (known: {', '.join(sorted(FIELD_NAMES))})"
            )
        offset, length = value
        if not (57 <= offset and offset + length <= 128 and length > 0):
            # Bytes 0-56 are fixed by Rev N for everyone; profiles only place
            # what comes after.
            log.debug(
                "tape profile %s: [vtbl] %s offset %s length %s outside 57-127; raising",
                path,
                key,
                offset,
                length,
            )
            raise TapeProfileError(f"{path}: [vtbl] {key} = {value} is outside bytes 57-127")
        fields[key] = FieldSpec(int(offset), int(length))
    vtbl_bytes = tuple(
        (int(off), bytes.fromhex(hexstr)) for off, hexstr in match.get("vtbl_bytes", [])
    )
    extent = data.get("extent", {})
    unknown = sorted(set(extent) - {"offset_bytes"})
    if unknown:
        log.debug("tape profile %s: [extent] keys %s not known; raising", path, unknown)
        raise TapeProfileError(f"{path}: unknown [extent] key(s) {', '.join(unknown)}")
    offset_bytes = extent.get("offset_bytes", 8)
    if offset_bytes not in (4, 8):
        log.debug(
            "tape profile %s: [extent] offset_bytes %r not 4 or 8; raising", path, offset_bytes
        )
        raise TapeProfileError(f"{path}: [extent] offset_bytes = {offset_bytes!r} must be 4 or 8")
    return TapeProfile(
        name=str(data["name"]),
        description=str(data.get("description", "")),
        vendor_bit=match.get("vendor_bit"),
        vtbl_bytes=vtbl_bytes,
        format_codes=frozenset(int(c) for c in match.get("format_codes", [])),
        fields=fields,
        path=str(path),
        extent_offset_bytes=int(offset_bytes),
    )


def builtin_names() -> list[str]:
    if not PROFILES_DIR.is_dir():
        log.debug("profile directory %s missing; no built-in tape profiles", PROFILES_DIR)
        return []
    return sorted(p.stem for p in PROFILES_DIR.iterdir() if p.suffix == ".toml")


def load_builtin() -> list[TapeProfile]:
    return [load(name) for name in builtin_names()]


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------


def _int(rec: bytes, spec: FieldSpec) -> int:
    return int.from_bytes(rec[spec.offset : spec.offset + spec.length], "little")


def decode_entry(rec: bytes, profile: TapeProfile) -> VtblEntry:
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
# Plausibility checks and guessing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    weight: int = 1
    detail: str = ""

    @property
    def points(self) -> int:
        return self.weight if self.ok else -self.weight


@dataclass
class Verdict:
    """One profile's reading of the whole volume table, and how believable it is."""

    profile: TapeProfile
    entries: list[VtblEntry]
    checks: list[Check]

    @property
    def score(self) -> int:
        return sum(c.points for c in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]


def _printable(text: str) -> bool:
    return all(32 <= ord(ch) < 127 for ch in text)


def match_checks(rec: bytes, vol: VolumeInfo, profile: TapeProfile) -> list[Check]:
    """The profile's own ``[match]`` hints against one record."""
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
    records: list[bytes], vol: VolumeInfo, profile: TapeProfile, now: datetime | None = None
) -> Verdict:
    entries: list[VtblEntry] = []
    checks: list[Check] = []
    log.debug("tape profile %s: evaluating %d VTBL records", profile.name, len(records))
    for rec in records:
        entry = decode_entry(rec, profile)
        entries.append(entry)
        checks += match_checks(rec, vol, profile)
        checks += entry_checks(entry, vol, now)
    verdict = Verdict(profile, entries, checks)
    # score/failures walk the checks, so only pay for them when DEBUG is on.
    if log.isEnabledFor(logging.DEBUG):
        log.debug(
            "tape profile %s: score %d, %d of %d checks failed",
            profile.name,
            verdict.score,
            len(verdict.failures),
            len(checks),
        )
    return verdict


def guess(
    records: list[bytes],
    vol: VolumeInfo,
    profiles: list[TapeProfile] | None = None,
    now: datetime | None = None,
) -> list[Verdict]:
    """Every profile's verdict, best first (ties keep catalogue order)."""
    if profiles is None:
        log.debug("guess: no profiles given; loading the built-in ones")
    candidates = profiles if profiles is not None else load_builtin()
    log.debug("guess: scoring %d tape profiles against %d records", len(candidates), len(records))
    verdicts = [evaluate(records, vol, p, now) for p in candidates]
    return sorted(verdicts, key=lambda v: v.score, reverse=True)
