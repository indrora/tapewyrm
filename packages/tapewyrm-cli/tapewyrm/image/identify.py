"""Identify straight from captures: decode flux, then hand the sectors to qiclib.

qiclib only reads TWTI images; turning TWRF captures into sectors needs this
package's MFM decoder, so the capture path lives here.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

from qiclib import merge
from qiclib import tape_profile as tp
from qiclib.identify import TapeInfo, from_image, from_sectors
from qiclib.types import RawSector
from tapewyrm_archive import twti
from tapewyrm_archive.progress import NULL_PROGRESS, Progress

from tapewyrm.image import convert

log = logging.getLogger(__name__)


def identify(
    path: Path,
    *,
    tape_profile: str = tp.GUESS,
    progress: Progress = NULL_PROGRESS,
) -> TapeInfo:
    """A TWTI image (by magic), or TWRF/raw capture(s) or a dump directory."""
    if path.is_file():
        with path.open("rb") as f:
            magic = f.read(len(twti.MAGIC))
        if magic == twti.MAGIC:
            log.debug("%s: TWTI magic; identifying from the image", path)
            return from_image(twti.TapeImage.open(path), tape_profile=tape_profile)
        log.debug("%s: magic %r != %r; treating it as a capture", path, magic, twti.MAGIC)
    else:
        log.debug("%s: not a file; treating it as a dump directory", path)
    return from_captures([path], tape_profile=tape_profile, progress=progress)


def from_captures(
    sources: Iterable[Path],
    *,
    tape_profile: str = tp.GUESS,
    progress: Progress = NULL_PROGRESS,
) -> TapeInfo:
    """Decode capture flux to sectors (merging passes), then :func:`from_sectors`."""
    files = convert.capture_files(sources)
    if not files:
        log.debug("sources expanded to no track captures; refusing")
        raise ValueError("no track captures found")
    passes: list[list[RawSector]] = []
    drive: dict | None = None
    with progress.task("decoding captures", total=len(files), unit="captures") as bar:
        for path in files:
            log.info("decoding %s...", path.name)
            sectors, meta = convert.decode_capture(path)
            log.info(f"{path.name}: {meta['sectors']} sectors")
            passes.append(sectors)
            # The first capture that recorded the drive's reports speaks for it
            # (legacy .raw streams carry none).
            if drive is None and "tape_status" in meta["twrf"]:
                log.debug("%s: carries drive reports; using it for the drive identity", path.name)
                drive = meta["twrf"]
            bar.advance()
    if drive is None:
        log.debug("no capture carried drive reports (tape_status); drive identity unknown")
    log.debug("merging %d passes", len(passes))
    return from_sectors(merge.union(passes), tape_profile=tape_profile, drive=drive)
