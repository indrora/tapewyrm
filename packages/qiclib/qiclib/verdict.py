"""Verdicts: the checks, warnings and probes every layer of a volume read reports.

Three layers turn a tape image into a listing (DESIGN.md §7.8): a volume
profile reads the volume table, the extent decoder turns a volume's
segments into bytes, and a file-set parser turns those bytes into entries.
Each can be wrong *quietly* -- a wrong VTBL layout reads a label as a size,
a wrong parser lists 186 nameless zero-byte files dated 1970 and calls none
of them damaged (the unk-co tape, 2026-11-07) -- so each layer says what it
checked and how that went, in one vocabulary:

* a :class:`Check` is one yes/no test with a weight. A reading's score is
  the sum of its points and the best score wins
  (:mod:`qiclib.volume_profile`); a failed check also names what it saw, so
  ``identify --raw`` can show why a layout lost.
* a :class:`VolumeWarning` is a fixed name for a caveat downstream tools
  must know about: that the profile has never met a real tape, that the
  entries carry things a DOS tar cannot hold, that the directory was
  reconstructed by scanning. They travel with the data (``identify --json``,
  the TWVL header, ``inspect``, the tar's pax headers) so nothing silently
  passes off a low-confidence read as a sure one.
* a :class:`Probe` is a file-set parser's verdict on a volume: its checks,
  its warnings, how many entries it would list, and ``fatal`` when the parse
  cannot be right at all (no directory section, an exception). A probe that
  did not pass is why a profile loses even after its VTBL reading won.

This module is a leaf (it imports nothing of qiclib), so the profile loader,
the parsers, ``identify``/``extract`` and the CLIs all share it without
cycles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


@dataclass(frozen=True)
class Check:
    """One yes/no plausibility test: ``weight`` points when it holds, minus when not."""

    name: str
    ok: bool
    weight: int = 1
    detail: str = ""

    @property
    def points(self) -> int:
        return self.weight if self.ok else -self.weight

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "points": self.points, "detail": self.detail}


class VolumeWarning(StrEnum):
    """A caveat about how a volume was read, by a fixed name.

    A closed set: a profile's TOML may only name members (a typo fails at
    load, not in a tar six months later), and every consumer compares
    against members, never strings it made up. The string value is what
    appears in JSON, TWVL headers and pax headers.
    """

    # About the profile itself (qiclib.volume_profile, from its status).
    UNTESTED = "untested"  # written from a spec or someone else's reader; no tape here has run it
    SINGLE_TAPE = "single-tape"  # confirmed on exactly one bench tape
    GENERIC = "generic"  # a default layout won because nothing positive matched the tape
    # About the choice between profiles.
    LOW_CONFIDENCE = "low-confidence"  # runner-up within 2 points, or a probe passed with failures
    VTBL_AMBIGUOUS = "vtbl-ambiguous"  # two profiles read the volume table identically
    MANUAL = "manual"  # the operator overrode a layer (parser, extent width, VTBL field) by hand
    # About what can be done with the volume.
    NO_PARSER = "no-parser"  # identified and extractable to TWVL, but nothing can list it
    SPECIAL_FS = "special-fs"  # resource forks, unix owner/mode, symlinks, devices, NT streams...
    LOSSY_NAMES = "lossy-names"  # names went through a lossy codec (Mac Roman as ASCII, 8.3 only)
    SPANS_CARTRIDGES = "spans-cartridges"  # the volume continues on another cartridge
    COMPRESSION_UNKNOWN = "compression-unknown"  # compressed with something we cannot expand
    DIRECTORY_DAMAGED = "directory-damaged"  # the catalog had holes; the listing may be short
    DIRECTORY_RECONSTRUCTED = "directory-reconstructed"  # catalog lost; entries found by scanning


#: One line per warning, for ``--help`` text and the spec.
WARNING_MEANINGS: dict[VolumeWarning, str] = {
    VolumeWarning.UNTESTED: "the profile comes from a spec or another reader; no tape here has exercised it",
    VolumeWarning.SINGLE_TAPE: "the profile's layout was confirmed on exactly one tape",
    VolumeWarning.GENERIC: "a default layout was chosen because nothing positive matched",
    VolumeWarning.LOW_CONFIDENCE: "another reading came within 2 points, or the parser passed with failures",
    VolumeWarning.VTBL_AMBIGUOUS: "two profiles read the volume table identically",
    VolumeWarning.MANUAL: "the read was hand-tuned: a parser, extent width or VTBL field was given on the command line",
    VolumeWarning.NO_PARSER: "the format is recognised but no parser can list its files",
    VolumeWarning.SPECIAL_FS: "entries carry what a DOS tar cannot: forks, unix ownership, symlinks, devices, streams",
    VolumeWarning.LOSSY_NAMES: "file names went through a lossy encoding",
    VolumeWarning.SPANS_CARTRIDGES: "the volume continues on another cartridge",
    VolumeWarning.COMPRESSION_UNKNOWN: "the data is compressed with a scheme that cannot be expanded",
    VolumeWarning.DIRECTORY_DAMAGED: "the directory section had holes; the listing may be short",
    VolumeWarning.DIRECTORY_RECONSTRUCTED: "the directory was lost; entries were recovered by scanning",
}


def parse_warning(name: str) -> VolumeWarning:
    """The member named ``name``, or a ``ValueError`` listing the valid names."""
    try:
        return VolumeWarning(name)
    except ValueError:
        known = ", ".join(w.value for w in VolumeWarning)
        raise ValueError(f"unknown warning {name!r} (known: {known})") from None


def warning_names(warnings: set[VolumeWarning] | frozenset[VolumeWarning]) -> list[str]:
    """The warnings as sorted strings, the form every JSON document and header uses."""
    return sorted(w.value for w in warnings)


# Two readings within this many points of each other are not told apart with
# confidence: one plausibility check (+1/-1 = 2 points) is all that separates
# them.
CLOSE_MARGIN = 2


@dataclass
class Probe:
    """A file-set parser's verdict on one volume.

    ``fatal`` set means the parse cannot be right (the parser raised, or found
    no directory at all): the profile that named this parser is out, whatever
    its checks say. Otherwise the checks score it like a VTBL reading, and
    ``passed`` is a positive score with at least one entry. ``entries`` is how
    many the parser would list, including lost or damaged ones.
    """

    checks: list[Check] = field(default_factory=list)
    warnings: set[VolumeWarning] = field(default_factory=set)
    entries: int = 0
    fatal: str | None = None

    @property
    def score(self) -> int:
        return sum(c.points for c in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    @property
    def passed(self) -> bool:
        return self.fatal is None and self.entries > 0 and self.score > 0

    def to_dict(self) -> dict[str, Any]:
        """JSON form for ``identify --json``, the TWVL header and ``inspect --json``."""
        return {
            "passed": self.passed,
            "score": self.score,
            "entries": self.entries,
            "fatal": self.fatal,
            "warnings": warning_names(self.warnings),
            "checks": [c.to_dict() for c in self.checks],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Probe:
        """The inverse of :meth:`to_dict`, for a probe read back out of a TWVL header."""
        return cls(
            checks=[
                Check(c["name"], bool(c["ok"]), abs(int(c["points"])), str(c.get("detail", "")))
                for c in data.get("checks", [])
            ],
            warnings={parse_warning(w) for w in data.get("warnings", [])},
            entries=int(data.get("entries", 0)),
            fatal=data.get("fatal"),
        )
