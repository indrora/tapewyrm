"""`tw convert`: TWRF captures -> TWTI tape image.

Decodes each capture's flux to sectors (MFM, the physical layer this package
owns), merges passes, places and Reed-Solomon corrects every segment, and
writes the result with the TWTI format from ``tapewyrm_archive.twti``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from tapewyrm_archive.progress import NULL_PROGRESS, Progress
from tapewyrm_archive.twrf import header_to_dict, read_header
from tapewyrm_archive.twti import (
    SEGMENT_STRIDE,
    VERSION,
    SegmentEntry,
    SegmentState,
    TapeImage,
)

from tapewyrm.codec import gwstream, merge, mfm
from tapewyrm.codec import segment as seg_mod
from tapewyrm.codec import volume as volume_mod
from tapewyrm.tape.geometry import Geometry
from tapewyrm.types import RawSector, SegmentStatus

log = logging.getLogger(__name__)

# Bare device streams from before TWRF carry no header; they were all QIC-80.
LEGACY_RAW_RATE_KBPS = 500

_FROM_RS = {
    SegmentStatus.CLEAN: SegmentState.CLEAN,
    SegmentStatus.CORRECTED: SegmentState.CORRECTED,
    SegmentStatus.UNCORRECTABLE: SegmentState.UNCORRECTABLE,
    SegmentStatus.MISSING: SegmentState.MISSING,
}


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
    from tapewyrm.buildinfo import host_build

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
    log.debug("merging %d passes", len(passes))
    merged = list(merge.union(passes))
    log.debug("merged to %d sectors; locating the header segment", len(merged))

    # The header places right under any geometry; then the header's own
    # geometry places everything else (floppy tracks per side). See
    # volume.locate_header, shared with the pipeline and `tw identify`.
    located = volume_mod.locate_header(merged, Geometry(tracks=28, segments_per_track=207))
    if located is None:
        log.debug(
            "locate_header found no header segment in %d merged sectors; refusing", len(merged)
        )
        raise ValueError("no header segment in these captures")
    vol, bsm, geom, segs = located.vol, located.bsm, located.geometry, located.segs
    by_seg = {s.seg: s for s in segs.values()}

    total = geom.total_segments()
    log.debug(
        "header geometry %s: %d segments, %d bad per the bad-sector map, %d placed",
        geom,
        total,
        len(bsm.bad_segments),
        len(by_seg),
    )
    entries: list[SegmentEntry] = []
    data: dict[int, bytes] = {}
    # BAD/MISSING are common (whole unread tracks), so they're counted here
    # and summarized after the loop rather than logged per segment.
    n_bad = n_missing = 0
    with progress.task("correcting segments", total=total, unit="segments") as bar:
        for n in range(total):
            bar.advance()
            if n in bsm.bad_segments:
                n_bad += 1
                entries.append(SegmentEntry(SegmentState.BAD))
                continue
            seg = by_seg.get(n)
            if seg is None:
                n_missing += 1
                entries.append(SegmentEntry(SegmentState.MISSING))
                continue
            res = seg_mod.correct_segment(seg)
            mask = sum(1 << k for k in seg.excluded)
            data[n] = res.data
            entries.append(
                SegmentEntry(_FROM_RS[res.status], res.erasure_count, len(res.data), mask)
            )
    log.debug(
        "segment correction: %d bad-map segments skipped, %d missing, %d corrected/kept",
        n_bad,
        n_missing,
        len(data),
    )

    first: dict = next((m["twrf"] for m in source_meta if "drive_config" in m["twrf"]), {})
    header = {
        "format": "TWTI",
        "version": VERSION,
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "tw_commit": host_build().commit,
        "segment_count": total,
        "segment_stride": SEGMENT_STRIDE,
        "geometry": asdict(geom),
        "qic80_header": asdict(vol),
        "drive": {
            k: first.get(k)
            for k in (
                "device_serial",
                "drive_status",
                "drive_config",
                "drive_rom",
                "drive_vendor_id",
                "tape_status",
                "rate_kbps",
                "firmware_commit",
            )
        },  # fmt: skip
        "sources": source_meta,
    }
    if not first:
        log.debug("no source capture carries drive_config; drive identity left empty")
    img = TapeImage(header=header, entries=entries)
    log.info("writing %s...", out)
    img.save(out, lambda n: data.get(n, b""))
    log.info(f"wrote {out}: {total} segments {img.counts()}")
    return TapeImage.open(out)
