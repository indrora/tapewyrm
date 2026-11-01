"""Readers that turn a file on disk into a :class:`cinnabar.model.Tape`.

- :mod:`cinnabar.load.image`: TWTI / TWTZ tape images (TWS-2): byte axis,
  one track per geometry row, segments from the segment table.
- :mod:`cinnabar.load.flux`: TWRF raw flux captures (TWS-1): time axis,
  one track per capture file, segments from the INDEX pulses, records from
  the flux codec.

:func:`open_path` picks by the file's magic (never its name, TWS-2 section
8), or treats a directory as a set of flux captures.
"""

from __future__ import annotations

from pathlib import Path

from tapewyrm_archive import inspect as inspect_mod

from cinnabar.model import Tape


def open_path(path: Path) -> Tape:
    """A :class:`Tape` for ``path``: an image, a capture, or a directory of captures."""
    if path.is_dir():
        from cinnabar.load import flux

        return flux.open_directory(path)
    kind = inspect_mod.sniff(path)
    if kind in ("TWTI", "TWTZ"):
        from cinnabar.load import image

        return image.open_image(path)
    if kind == "TWRF":
        from cinnabar.load import flux

        return flux.open_captures([path])
    raise ValueError(f"{path}: not a TWRF capture or a TWTI/TWTZ image")
