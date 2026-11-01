"""Readers that turn a file on disk into a :class:`cinnabar.model.Tape`.

- :mod:`cinnabar.load.image`: TWTI / TWTZ tape images (TWS-2): byte axis,
  one track per geometry row, segments from the segment table.
- :mod:`cinnabar.load.flux`: TWRF raw flux captures (TWS-1): time axis,
  one track per capture file, segments from the INDEX pulses, records from
  the flux codec.

:func:`open_path` picks by the file's magic (never its name, TWS-2 section
8), or treats a directory as a set of flux captures. Opening one capture
opens its whole dump: ``tw dump`` writes a ``dump.jsonl`` sidecar beside
its captures, one line per track with the capture's path (TWS-1 appendix
B), and :func:`sidecar_captures` turns that into the track list, so the
window's track selector has every track and not just the file clicked.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from tapewyrm_archive import inspect as inspect_mod
from tapewyrm_archive.progress import NULL_PROGRESS, Progress

from cinnabar.model import Tape

log = logging.getLogger(__name__)

SIDECAR = "dump.jsonl"


def sidecar_captures(capture: Path) -> list[Path]:
    """The captures listed by the ``dump.jsonl`` beside ``capture``, ``capture`` among them.

    The sidecar's ``path`` members are as ``tw dump`` wrote them, relative
    to wherever it ran, so only their file names are trusted and looked for
    in the capture's own directory; a listed file that is not there is
    skipped. Without a sidecar, or with one that does not mention the
    capture, the list is the capture alone.
    """
    sidecar = capture.parent / SIDECAR
    if not sidecar.is_file():
        return [capture]
    found: list[Path] = []
    try:
        for line in sidecar.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            name = Path(str(entry.get("path", ""))).name
            candidate = capture.parent / name
            if name and candidate.is_file() and candidate not in found:
                found.append(candidate)
    except (OSError, ValueError) as exc:
        log.warning("%s: unreadable sidecar, opening %s alone: %s", sidecar, capture.name, exc)
        return [capture]
    if capture.resolve() not in {p.resolve() for p in found}:
        return [capture]
    log.info("%s lists %d captures", sidecar.name, len(found))
    return found


def open_path(path: Path, progress: Progress = NULL_PROGRESS) -> Tape:
    """A :class:`Tape` for ``path``: an image, a capture, or a directory of captures.

    ``progress`` reports the opening (a TWTZ decompresses here); each
    track's own loading reports through the ``progress`` given to
    ``Tape.load``.
    """
    if path.is_dir():
        from cinnabar.load import flux

        return flux.open_directory(path)
    kind = inspect_mod.sniff(path)
    if kind in ("TWTI", "TWTZ"):
        from cinnabar.load import image

        return image.open_image(path, progress)
    if kind == "TWRF":
        from cinnabar.load import flux

        return flux.open_captures(sidecar_captures(path))
    raise ValueError(f"{path}: not a TWRF capture or a TWTI/TWTZ image")
