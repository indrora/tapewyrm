"""Readers that turn a file on disk into a :class:`cinnabar.model.Tape`.

- :mod:`cinnabar.load.image`: TWTI / TWTZ tape images (TWS-2): byte axis,
  one track per geometry row, segments from the segment table.
- :mod:`cinnabar.load.flux`: TWRF raw flux captures (TWS-1): time axis,
  one track per header track, each capture of it one of its passes,
  segments from the INDEX pulses, records from the flux codec.

:func:`open_path` picks by the file's magic (never its name, TWS-2 section
8), or treats a directory as a set of flux captures. Opening one capture
opens its whole dump: ``tw dump`` writes a ``dump.jsonl`` sidecar beside
its captures, one line per track with the capture's path (TWS-1 appendix
B), and :func:`dump_captures` turns that into the track list, so the
window's track selector has every track and not just the file clicked.

With ``--keep-all`` the sidecar names only the read its rules picked for
each track; the others sit beside it as ``track-NN.P.twrf`` (TWS-1 section
8.1 rule 7), which is why :func:`dump_captures` also takes the folder's
``track-*.twrf`` files. Without them, opening one capture would show one
read per track and hide the rest, which the window's Pass selector exists
to show.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from qiclib.volume import profile as vp
from tapewyrm_archive import inspect as inspect_mod
from tapewyrm_archive.progress import NULL_PROGRESS, Progress

from cinnabar.model import Tape

log = logging.getLogger(__name__)

SIDECAR = "dump.jsonl"


def sidecar_listed(folder: Path) -> list[Path]:
    """The captures the ``dump.jsonl`` in ``folder`` names that are there, in line order.

    The sidecar's ``path`` members are as ``tw dump`` wrote them, relative
    to wherever it ran, so only their file names are trusted and looked for
    in ``folder``; a listed file that is not there is skipped. A file named
    by more than one line (a rerun without ``--keep-all`` rewrites
    ``track-NN.twrf``) takes the place of its *last* line, since the order
    is what ranks one run's pick over another's (:func:`open_captures
    <cinnabar.load.flux.open_captures>`). No sidecar, or an unreadable one,
    lists nothing.
    """
    sidecar = folder / SIDECAR
    if not sidecar.is_file():
        return []
    found: dict[Path, None] = {}  # an ordered set
    try:
        for line in sidecar.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            name = Path(str(entry.get("path", ""))).name
            candidate = folder / name
            if name and candidate.is_file():
                found.pop(candidate, None)  # re-inserted: the later line wins
                found[candidate] = None
    except (OSError, ValueError) as exc:
        log.warning("%s: unreadable sidecar, ignoring it: %s", sidecar, exc)
        return []
    return list(found)


def is_track_capture(path: Path) -> bool:
    """Whether ``path`` is named as ``tw dump`` names a track's capture: ``track-*.twrf`` (TWS-1 8.1 rule 7)."""
    return path.name.startswith("track-") and path.suffix == ".twrf"


def dump_captures(capture: Path) -> tuple[list[Path], list[Path]]:
    """Every read in the dump ``capture`` belongs to, and the ones its ``dump.jsonl`` names.

    Returns ``(captures, best)``: the sidecar's captures, then the folder's
    other ``track-*.twrf`` files (the reads ``--keep-all`` kept and did not
    pick), and the sidecar's list on its own (:func:`sidecar_listed`), which
    is ``best`` for :func:`cinnabar.load.flux.open_captures`.

    ``capture`` belongs to the dump if the sidecar names it, or if it is a
    ``track-*.twrf`` beside a sidecar that names something: a pass that was
    not picked is still part of the dump, and opening it should show every
    track. Otherwise (no sidecar, or one about other files) it opens alone,
    as it always has.
    """
    folder = capture.parent
    listed = sidecar_listed(folder)
    named = {p.resolve() for p in listed}
    if not listed or (capture.resolve() not in named and not is_track_capture(capture)):
        return [capture], []
    others = sorted(p for p in folder.iterdir() if is_track_capture(p) and p.resolve() not in named)
    log.info("%s lists %d captures; %d more reads beside it", SIDECAR, len(listed), len(others))
    return [*listed, *others], listed


def open_path(
    path: Path,
    progress: Progress = NULL_PROGRESS,
    *,
    volume_profile: str = vp.GUESS,
    overrides: vp.Overrides | None = None,
) -> Tape:
    """A :class:`Tape` for ``path``: an image, a capture, or a directory of captures.

    ``progress`` reports the opening (a TWTZ decompresses here); each
    track's own loading reports through the ``progress`` given to
    ``Tape.load``. ``volume_profile`` and ``overrides`` are ``qicsilver``'s:
    the profile the volume table is read through (a name, a path or
    ``"guess"``) and hand-given parameters laid on it
    (:func:`qiclib.volume.profile.override`); both reach
    :mod:`qiclib.identify` in each loader, so the viewer labels the tape
    exactly as ``qicsilver extract`` with the same flags would read it.
    """
    if path.is_dir():
        from cinnabar.load import flux

        return flux.open_directory(
            path, best=sidecar_listed(path), volume_profile=volume_profile, overrides=overrides
        )
    kind = inspect_mod.sniff(path)
    if kind in ("TWTI", "TWTZ"):
        from cinnabar.load import image

        return image.open_image(path, progress, volume_profile=volume_profile, overrides=overrides)
    if kind == "TWRF":
        from cinnabar.load import flux

        captures, best = dump_captures(path)
        return flux.open_captures(
            captures, best=best, volume_profile=volume_profile, overrides=overrides
        )
    raise ValueError(f"{path}: not a TWRF capture or a TWTI/TWTZ image")
