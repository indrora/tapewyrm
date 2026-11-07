"""The file-set parser base class and the registry the volume profiles name parsers in.

A parser turns a TWVL volume's bytes into :class:`~qiclib.fileset.entry.VolumeEntry`
records and says how sure it is. Subclasses implement :meth:`FileSetParser.read`
only: parse the volume into a :class:`Reading` (the entries, plus any checks
and warnings only that parser can make, such as "the data-entry signatures
are where the directory says"). :meth:`FileSetParser.probe` wraps that with
the standard checks every catalog must pass (:func:`qiclib.fileset.common.standard_checks`)
and turns an exception into a fatal verdict, so a parser never needs its
own scoring loop and can never crash a probe.

Why probe at all, when the profile already matched the tape: a fingerprint
says what the *writer* was; only parsing says whether *this* layout reads
*this* volume. The unk-co tape (2026-11-07) matched the Rev N layout and
the Basic-DOS parser then listed 186 nameless zero-byte files without a
complaint. With a probe, that reading scores below zero and the Extended
parser's, which lists a real tree, wins.

The registry maps the ``[fileset] parser`` key of a volume profile to a
parser instance; :func:`probe_all` tries every registered parser on a
volume, for a TWVL written before headers named their parser.
"""

from __future__ import annotations

import logging
import struct
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from tapewyrm_archive.errors import MalformedFileError
from tapewyrm_archive.twvl import Volume

from qiclib.fileset.common import standard_checks
from qiclib.fileset.entry import VolumeEntry
from qiclib.verdict import Check, Probe, VolumeWarning
from qiclib.volume import VtblEntry

log = logging.getLogger(__name__)

#: The parser name of a profile that recognises a format but cannot list it.
NONE = "none"


@dataclass
class Reading:
    """What one parser made of a volume: its entries and its own verdict on them."""

    entries: list[VolumeEntry]
    checks: list[Check] = field(default_factory=list)
    warnings: set[VolumeWarning] = field(default_factory=set)


class FileSetParser:
    """One backup catalog layout. Subclasses set the class attributes and implement :meth:`read`.

    ``params`` names the ``[fileset]`` keys a profile may give this parser,
    each with its default; the profile loader refuses any other key, so a
    typo in a TOML file fails at load. ``source`` in every method is the
    file the volume came from, named in messages only.
    """

    name: str = ""
    description: str = ""
    params: Mapping[str, Any] = {}

    def read(
        self, vol: Volume, vtbl: VtblEntry, source: Path, params: Mapping[str, Any]
    ) -> Reading:
        """Parse the volume. Raise ``ValueError`` when its layout cannot be this one."""
        raise NotImplementedError

    def entries(
        self,
        vol: Volume,
        vtbl: VtblEntry,
        source: Path,
        params: Mapping[str, Any] | None = None,
    ) -> list[VolumeEntry]:
        """The listing: every directory and file, in directory order."""
        return self.read(vol, vtbl, source, params or {}).entries

    def probe(
        self,
        vol: Volume,
        vtbl: VtblEntry,
        source: Path,
        params: Mapping[str, Any] | None = None,
        *,
        now: datetime | None = None,
    ) -> Probe:
        """Could this parser be the right one for the volume?

        Parses, then scores the entries with the standard checks and the
        parser's own. Anything the parse raises is the verdict ``fatal``:
        the exceptions listed are what a structure walk hits on bytes of
        the wrong shape (a size field past the buffer, a struct short, a
        name that is not UTF-16), and a parser bug would surface the same
        way, which is why the traceback is logged at debug. A malformed
        TWVL header is the one exception that is not about the layout at
        all, so it propagates: no parser could read that file.
        """
        log.debug("probe: %s on %s (%d bytes)", self.name, source, len(vol.data))
        try:
            reading = self.read(vol, vtbl, source, params or {})
        except MalformedFileError:
            log.debug("probe: %s on %s: the TWVL itself is malformed; raising", self.name, source)
            raise
        except (ValueError, struct.error, IndexError, KeyError, UnicodeError) as exc:
            log.debug("probe: %s on %s raised; fatal", self.name, source, exc_info=True)
            return Probe(fatal=f"{type(exc).__name__}: {exc}")
        checks = standard_checks(reading.entries, vol, vtbl, now=now) + reading.checks
        probe = Probe(checks, set(reading.warnings), len(reading.entries))
        log.debug(
            "probe: %s on %s: %d entries, score %d, %d of %d checks failed, warnings %s",
            self.name,
            source,
            probe.entries,
            probe.score,
            len(probe.failures),
            len(checks),
            sorted(w.value for w in probe.warnings),
        )
        return probe


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, FileSetParser] = {}


def register(parser: FileSetParser) -> FileSetParser:
    """Add a parser under its ``name``; a second parser of that name is a programming error."""
    if not parser.name:
        raise ValueError(f"{type(parser).__name__} has no name")
    if parser.name in _REGISTRY:
        raise ValueError(f"a file-set parser named {parser.name!r} is already registered")
    log.debug("fileset: registering parser %s", parser.name)
    _REGISTRY[parser.name] = parser
    return parser


def get(name: str) -> FileSetParser:
    """The parser named ``name``, or a ``ValueError`` listing the known names."""
    try:
        return _REGISTRY[name]
    except KeyError:
        log.debug("fileset: no parser %r (known: %s); raising", name, names())
        raise ValueError(
            f"no file-set parser named {name!r} (known: {', '.join(names())})"
        ) from None


def names() -> list[str]:
    return sorted(_REGISTRY)


def listable() -> list[FileSetParser]:
    """Every parser that can actually list a volume (all but ``none``)."""
    return [p for n, p in sorted(_REGISTRY.items()) if n != NONE]


def probe_all(
    vol: Volume, vtbl: VtblEntry, source: Path, *, now: datetime | None = None
) -> list[tuple[FileSetParser, Probe]]:
    """Every listable parser's probe of the volume, best first.

    For a TWVL whose header names no parser (written before TWS-3 section
    3.4, or by hand). A probe that passed beats one that did not; among
    those, the higher score. Ties keep registry order.
    """
    results = [(p, p.probe(vol, vtbl, source, now=now)) for p in listable()]
    results.sort(key=lambda pair: (pair[1].passed, pair[1].score), reverse=True)
    log.debug(
        "probe_all on %s: %s",
        source,
        ", ".join(f"{p.name} {'ok' if pr.passed else 'no'} {pr.score}" for p, pr in results),
    )
    return results
