"""`tw convert`: TWRF captures -> TWTI tape image.

Decodes each capture's flux to sectors (MFM, the physical layer this package
owns), then hands the passes to :func:`qiclib.build.build_image`, which merges,
places and Reed-Solomon corrects every segment and writes the TWTI file.
This is the one place tapewyrm-cli depends on qiclib (STYLE.md §2).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from pathlib import Path

from qiclib.build import build_image
from qiclib.types import RawSector
from tapewyrm_archive import qic117
from tapewyrm_archive.progress import NULL_PROGRESS, Progress
from tapewyrm_archive.provenance import provenance_path
from tapewyrm_archive.twrf import header_to_dict, read_header
from tapewyrm_archive.twti import TapeImage
from tapewyrm_archive.types import CaptureHeader, TapeFormat

from tapewyrm.codec import gwpll, gwstream, mfm

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


def decode_capture(
    path: Path, *, progress: Progress = NULL_PROGRESS
) -> tuple[list[RawSector], dict]:
    """Decode one capture's flux to sectors, reporting each stage as it goes.

    The stages are the ones :func:`tapewyrm.codec.mfm.recover_sectors_from_flux`
    chains, called one by one here so each can report what it produced
    (bitcells, sync runs, sector tallies): one INFO line per stage, every line
    led by the capture's name so interleaved passes stay attributable.
    ``progress`` gets one bar per long pass (parse, PLL, framing, scan).
    """
    started = time.perf_counter()
    name = path.name
    log.debug("reading capture %s", path)
    blob = path.read_bytes()
    if path.suffix == ".twrf":
        hdr, flux_at = read_header(path)
        flux, rate, meta = blob[flux_at:], hdr.rate_kbps, header_to_dict(hdr)
        log.info("%s: %s, %d kbps, %s of flux", name, describe_header(hdr), rate, _mb(len(flux)))
    else:
        log.debug(
            "%s: suffix %r is not .twrf; treating as headerless legacy stream at %d kbps",
            path,
            path.suffix,
            LEGACY_RAW_RATE_KBPS,
        )
        flux, rate, meta = blob, LEGACY_RAW_RATE_KBPS, {"rate_kbps": LEGACY_RAW_RATE_KBPS}
        log.info(
            "%s: headerless legacy stream, assuming %d kbps, %s of flux",
            name,
            rate,
            _mb(len(flux)),
        )
    log.debug("%s: parsing %d bytes of flux stream", name, len(flux))
    ps = gwstream.parse(flux, progress=progress)
    del blob, flux  # the parsed intervals are all later stages need
    if ps.verified:
        checked = "verified against the END marker"
    elif ps.end is None:
        log.debug("%s: no END marker in the stream; parse left unverified", name)
        checked = "unverified (no END marker)"
    else:
        log.debug("%s: parse disagrees with END marker %r", name, ps.end)
        checked = "MISMATCHES the END marker"
    log.info(
        "%s: %.1f s of tape, %s transitions, %s",
        name,
        ps.duration_s,
        f"{len(ps.intervals):,}",
        checked,
    )

    log.debug("%s: running the PLL at %d kbps (sample clock %d Hz)", name, rate, ps.sample_clock_hz)
    cells = gwpll.flux_to_bitcells(
        ps.intervals, ps.sample_clock_hz, mfm.bitcell_seconds(rate), progress=progress
    )
    n_cells = len(cells)
    decoded, sync_runs = mfm.frame_bitcells(cells, progress=progress)
    del cells
    log.info("%s: %s bitcells, %s sync marks", name, f"{n_cells:,}", f"{sync_runs:,}")

    scan = mfm.ScanStats()
    sectors = list(mfm.recover_sectors_from_bytes(decoded, progress=progress, stats=scan))
    log.info(
        "%s: %s sectors: %s CRC-clean, %s data-CRC bad, %s ID-only (%.1f s)",
        name,
        f"{scan.sectors:,}",
        f"{scan.data_crc_ok:,}",
        f"{scan.data_bad:,}",
        f"{scan.id_only:,}",
        time.perf_counter() - started,
    )
    return sectors, {
        # The capture's directory name and file name only, never the full
        # path: that would record the user's home directory (TWS-2 4.5).
        "file": provenance_path(path),
        "verified": ps.verified,
        "sectors": len(sectors),
        "twrf": meta,
    }


def describe_header(hdr: CaptureHeader) -> str:
    """One phrase for a TWRF header: track, direction, format and tape type.

    The format comes from the drive's Report Tape Status byte when the capture
    recorded one (it also names the tape type and width); otherwise from the
    header's own ``tape_format``, which the dump filled in from the same report
    or its default.
    """
    where = f"track {hdr.track} {hdr.direction.value}"
    if hdr.tape_status is None:
        log.debug("track %d: no tape status byte recorded; using header format", hdr.track)
        return f"{where}, {_format_name(hdr.tape_format)}"
    status = qic117.TapeStatus.decode(hdr.tape_status)
    text = f"{where}, {_format_name(status.format)}"
    tape_type = qic117.TAPE_TYPES.get(status.tape_type)
    if tape_type is None:
        log.debug("tape type code %d is not in TAPE_TYPES", status.tape_type)
        tape_type = f"tape type {status.tape_type}"
    text += f" on {tape_type}"
    if status.wide:
        text += ", wide"
    return text


def _format_name(fmt: TapeFormat) -> str:
    """``TapeFormat.QIC80`` -> ``"QIC-80"`` (the name the standards use)."""
    return fmt.name.replace("QIC", "QIC-") if fmt is not TapeFormat.UNKNOWN else "unknown format"


def _mb(size: int) -> str:
    """A byte count as decimal megabytes, the unit rich's file-size bars use."""
    return f"{size / 1e6:,.1f} MB"


def convert(sources: Iterable[Path], out: Path, *, progress: Progress = NULL_PROGRESS) -> TapeImage:
    """Build a TWTI image from one or more dumps (passes are merged).

    Progress: one "capture k/N <name>" bar per capture (total N, positioned at
    k-1 while that capture decodes, so it reads as a single outer bar whose
    label follows the current file), with the stage bars of
    :func:`decode_capture` under it; then :func:`qiclib.build.build_image`'s.
    """
    started = time.perf_counter()
    files = capture_files(sources)
    if not files:
        log.debug(
            "sources expanded to no track captures (no files, or directories without track-*); refusing"
        )
        raise ValueError("no track captures found")
    count = len(files)
    log.info("converting %d capture(s) into %s", count, out.name)
    passes, source_meta = [], []
    for k, path in enumerate(files, 1):
        with progress.task(f"capture {k}/{count} {path.name}", total=count, unit="captures") as bar:
            bar.update(k - 1)
            sectors, meta = decode_capture(path, progress=progress)
            bar.update(k)
        passes.append(sectors)
        source_meta.append(meta)
    from tapewyrm.buildinfo import host_build

    img = build_image(
        passes, out, sources=source_meta, tw_commit=host_build().commit, progress=progress
    )
    log.info("converted %d capture(s) in %.1f s total", count, time.perf_counter() - started)
    return img
