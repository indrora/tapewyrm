"""`tw convert`: TWRF captures -> TWTI tape image.

Decodes each capture's flux to sectors (MFM, the physical layer this package
owns), then hands the passes to :func:`qiclib.build.build_image`, which merges,
places and Reed-Solomon corrects every segment and writes the TWTI file.
This is the one place tapewyrm-cli depends on qiclib (STYLE.md §2).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

from qiclib.build import build_image
from qiclib.types import RawSector
from tapewyrm_archive.progress import NULL_PROGRESS, Progress
from tapewyrm_archive.twrf import header_to_dict, read_header
from tapewyrm_archive.twti import (
    TapeImage,
)

from tapewyrm.codec import gwstream, mfm

log = logging.getLogger(__name__)

# Bare device streams from before TWRF carry no header; they were all QIC-80.
LEGACY_RAW_RATE_KBPS = 500


def capture_files(sources: Iterable[Path]) -> list[Path]:
    """Expand dump directories into their track captures (TWRF, else legacy .raw)."""
    out: list[Path] = []
    for src in sources:
        if src.is_dir():
            twrf = sorted(src.glob("track-*.twrf"))
            if not twrf:
                log.debug("%s: no track-*.twrf captures; falling back to legacy track-*.raw", src)
            out += twrf or sorted(src.glob("track-*.raw"))
        else:
            log.debug("%s: not a directory; using it as a capture file", src)
            out.append(src)
    return out


def decode_capture(path: Path) -> tuple[list[RawSector], dict]:
    log.debug("reading capture %s", path)
    blob = path.read_bytes()
    if path.suffix == ".twrf":
        hdr, flux_at = read_header(path)
        flux, rate, meta = blob[flux_at:], hdr.rate_kbps, header_to_dict(hdr)
    else:
        log.debug(
            "%s: suffix %r is not .twrf; treating as headerless legacy stream at %d kbps",
            path,
            path.suffix,
            LEGACY_RAW_RATE_KBPS,
        )
        flux, rate, meta = blob, LEGACY_RAW_RATE_KBPS, {"rate_kbps": LEGACY_RAW_RATE_KBPS}
    log.debug("%s: parsing %d bytes of flux stream", path.name, len(flux))
    ps = gwstream.parse(flux)
    log.debug(
        "%s: %d flux intervals at %d Hz (verified=%s); recovering sectors at %d kbps",
        path.name,
        len(ps.intervals),
        ps.sample_clock_hz,
        ps.verified,
        rate,
    )
    sectors = mfm.recover_sectors_from_flux(ps.intervals, ps.sample_clock_hz, rate)
    return sectors, {
        "file": str(path),
        "verified": ps.verified,
        "sectors": len(sectors),
        "twrf": meta,
    }


def convert(sources: Iterable[Path], out: Path, *, progress: Progress = NULL_PROGRESS) -> TapeImage:
    """Build a TWTI image from one or more dumps (passes are merged)."""
    files = capture_files(sources)
    if not files:
        log.debug(
            "sources expanded to no track captures (no files, or directories without track-*); refusing"
        )
        raise ValueError("no track captures found")
    log.debug("converting %d capture files into %s", len(files), out)
    passes, source_meta = [], []
    with progress.task("decoding captures", total=len(files), unit="captures") as bar:
        for path in files:
            log.info("decoding %s...", path.name)
            sectors, meta = decode_capture(path)
            log.info(f"{path.name}: {meta['sectors']} sectors at {meta['twrf']['rate_kbps']} kbps")
            passes.append(sectors)
            source_meta.append(meta)
            bar.advance()
    from tapewyrm.buildinfo import host_build

    return build_image(
        passes, out, sources=source_meta, tw_commit=host_build().commit, progress=progress
    )
