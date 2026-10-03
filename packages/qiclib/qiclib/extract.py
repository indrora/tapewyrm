"""`qicsilver extract`: TWTI tape image -> TWVL volume files.

Reads the volume table through a volume profile, decodes QIC-122 extents and
lays each volume out with the TWVL format from ``tapewyrm_archive.twvl``.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from pathlib import Path

from tapewyrm_archive.progress import NULL_PROGRESS, Progress
from tapewyrm_archive.twti import SegmentState, TapeImage
from tapewyrm_archive.twvl import VERSION, SparseVolume, Volume, find_holes

from qiclib import qic122
from qiclib import volume as volume_mod
from qiclib import volume_profile as vp

log = logging.getLogger(__name__)

# A QIC-80 segment is 32 sectors, the last 3 of the non-excluded ones being
# Reed-Solomon ECC; the rest (29 when nothing is excluded) carry data.
_SECTORS_PER_SEGMENT = 32
_ECC_SECTORS = 3
_SECTOR_BYTES = 1024


def _expected_data_len(excluded_mask: int) -> int:
    """Bytes of data a segment holds given the sectors its bad-sector map excludes.

    Used to size the hole a MISSING or UNCORRECTABLE segment leaves in an
    uncompressed volume. ``data_len`` cannot be used for that: a MISSING
    segment records 0, and an UNCORRECTABLE one records whatever partial data
    survived. The bad-sector map is the one thing that fixes a segment's size
    independently of whether it was read, so it is what the writing software
    saw too: 32 sectors, minus the excluded ones, minus 3 for ECC.

    ``qiclib.build`` records the map's mask on MISSING entries too, so this is
    exact for images it wrote. Images from before that (mask 0 on MISSING
    entries) get a full 29 KB hole, which drifts by any excluded sectors.
    """
    excluded = bin(excluded_mask & ((1 << _SECTORS_PER_SEGMENT) - 1)).count("1")
    return max(0, _SECTORS_PER_SEGMENT - excluded - _ECC_SECTORS) * _SECTOR_BYTES


def _segment_bounds(img: TapeImage, start: int, end: int) -> tuple[list[int], int]:
    """Stream offsets where each non-BAD segment of an uncompressed volume starts, and the total.

    Mirrors the layout loop in :func:`extract` exactly: BAD segments take no
    room, MISSING/UNCORRECTABLE ones their expected size, read ones their data.
    """
    bounds: list[int] = []
    pos = 0
    for n in range(start, end + 1):
        ent = img.entries[n]
        if ent.state is SegmentState.BAD:
            continue
        bounds.append(pos)
        if ent.state in (SegmentState.MISSING, SegmentState.UNCORRECTABLE):
            pos += _expected_data_len(ent.excluded_mask)
        else:
            pos += ent.data_len
    return bounds, pos


def _vtbl_dict(e: volume_mod.VtblEntry) -> dict:
    d = asdict(e)
    d["signature"] = e.signature.decode("ascii", "replace")
    d["raw"] = e.raw.hex()
    d["date_decoded"] = volume_mod.decode_short_date(e.date)
    return d


def extract(
    image_path: Path,
    out_dir: Path,
    *,
    volume_profile: str = vp.GUESS,
    progress: Progress = NULL_PROGRESS,
) -> list[Path]:
    """Write every volume on the tape image as ``vol-NN.twvl`` in ``out_dir``.

    The volume table is read through a volume profile, exactly as ``qicsilver identify``
    reads it (``volume_profile`` is a name, a path, or ``"guess"``). Only bytes
    0-56 of a VTBL entry are universal: section sizes, the compression flag and
    the extent offset width differ by the software that wrote the tape, and the
    plain Rev N layout misreads them on e.g. MTN tapes -- a size taken from the
    middle of the label, compression read as off.
    """
    log.info("extracting volumes from %s...", image_path.name)
    img = TapeImage.open(image_path)
    q80 = img.header["qic80_header"]
    vt_seg = q80["first_data_seg"]
    vt_entry = img.entries[vt_seg]
    log.debug(
        "volume table at segment %d (first_data_seg): state %s, %d erasures, data_len %d",
        vt_seg,
        vt_entry.state.name,
        vt_entry.erasures,
        vt_entry.data_len,
    )
    if vt_entry.state in (SegmentState.MISSING, SegmentState.BAD):
        log.debug("volume table segment %d is %s; refusing", vt_seg, vt_entry.state.name)
        raise ValueError(f"volume table segment {vt_seg} was not recovered")
    if vt_entry.state is SegmentState.UNCORRECTABLE:
        # Not refused (behaviour unchanged), but the table may be garbage.
        log.debug("volume table segment %d is UNCORRECTABLE; parsing partial data anyway", vt_seg)
    log.debug("reading volume table from segment %d with volume profile %r", vt_seg, volume_profile)
    # Deferred: identify imports twti and the codec stack; keep this module light.
    from qiclib.identify import from_image

    verdicts = from_image(img, volume_profile=volume_profile).verdicts
    if verdicts:
        best = verdicts[0]
        profile = best.profile
        vtbl = best.entries
        log.info("volume table read with volume profile %s (score %d)", profile.name, best.score)
        if len(verdicts) > 1 and verdicts[1].score == best.score:
            log.warning(
                "volume profiles %s and %s fit equally well; using %s (pick one with --volume-profile)",
                profile.name,
                verdicts[1].profile.name,
                profile.name,
            )
    else:
        log.debug("no VTBL records in segment %d; no profile to apply", vt_seg)
        profile, vtbl = None, []
    offset_bytes = profile.extent_offset_bytes if profile is not None else 8
    log.debug("volume table: %d entries; extent offsets are %d bytes", len(vtbl), offset_bytes)
    if not vtbl:
        log.debug("volume table segment %d parsed to no entries; nothing to extract", vt_seg)
    log.debug("creating output directory %s", out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    with progress.task("extracting volumes", total=len(vtbl), unit="volumes") as overall:
        for k, e in enumerate(vtbl):
            size = (e.data_section_size or 0) + (e.dir_section_size or 0)
            log.debug(
                "volume %d %r: segments %d..%d, compressed=%r, data_section_size=%r, "
                "dir_section_size=%r -> %d bytes",
                k,
                e.description,
                e.start_seg,
                e.end_seg,
                e.compressed,
                e.data_section_size,
                e.dir_section_size,
                size,
            )
            if e.end_seg < e.start_seg:
                log.debug(
                    "volume %d: end_seg %d < start_seg %d; no segments will be read",
                    k,
                    e.end_seg,
                    e.start_seg,
                )
            elif e.end_seg >= len(img.entries):
                log.debug(
                    "volume %d: end_seg %d is past the image's %d segments; refusing",
                    k,
                    e.end_seg,
                    len(img.entries),
                )
                raise ValueError(
                    f"volume {k}: the volume table says it ends at segment {e.end_seg}, "
                    f"past the end of the image ({len(img.entries)} segments, last is "
                    f"{len(img.entries) - 1}); the image is truncated or the table is read "
                    "with the wrong layout -- try another --volume-profile"
                )
            if size == 0:
                log.debug(
                    "volume %d: section sizes give 0 bytes (missing/None in the table?); "
                    "volume body will be empty",
                    k,
                )
            if e.compressed is None:
                log.debug(
                    "volume %d: compressed flag is None; treating segments as QIC-122 extents", k
                )
            # The whole volume is allocated up front (SparseVolume.read), so a
            # misread size is a MemoryError, not a bad file. Refuse anything
            # the volume's segments could not hold even at the best plausible
            # compression ratio -- the same bound volume_profile's size check uses.
            span = max(0, e.end_seg - e.start_seg + 1)
            ratio = 1 if e.compressed is False else vp.MAX_COMPRESSION_RATIO
            max_size = span * vp.SEGMENT_DATA_BYTES * ratio
            if size > max_size:
                log.debug(
                    "volume %d: %d bytes > %d (%d segments x %d x %d); refusing",
                    k,
                    size,
                    max_size,
                    span,
                    vp.SEGMENT_DATA_BYTES,
                    ratio,
                )
                raise ValueError(
                    f"volume {k}: the volume table claims {size:,} bytes, but its {span} "
                    f"segments hold at most {max_size:,}; the table is probably read with the "
                    "wrong layout -- try another --volume-profile"
                )
            # Uncompressed volumes are the segments' usable data laid end to
            # end, so their real length -- and every segment boundary -- is
            # known before reading a byte. The table's data + directory sizes
            # undercount it: QIC-113 Rev G §7 puts a Segment Gap between the
            # sections (§7.1: the directory starts on a segment boundary), so
            # sizing from the table cut the end off a Directory-Last directory.
            # The boundaries also pin that directory exactly: the first one at
            # or after the data section's end. (Compressed volumes are laid out
            # by their extents' offsets, where the gap takes no room.)
            directory_offset = None
            if e.compressed is False:
                bounds, total = _segment_bounds(img, e.start_seg, e.end_seg)
                log.debug(
                    "volume %d: uncompressed; segments hold %d bytes (table says %d)",
                    k,
                    total,
                    size,
                )
                size = total
                if e.directory_last and e.data_section_size is not None:
                    directory_offset = next((b for b in bounds if b >= e.data_section_size), None)
                    log.debug(
                        "volume %d: directory-last; directory at %r (first segment boundary "
                        "at or after the %d-byte data section)",
                        k,
                        directory_offset,
                        e.data_section_size,
                    )
            stream = SparseVolume(size=size)
            lost: list[int] = []
            n_bad = 0
            # Uncompressed volumes only: where the next segment's data goes.
            # The volume is the concatenation of each segment's usable data in
            # order, and segments differ in length (the bad-sector map shortens
            # some, BAD ones hold nothing), so the offset is a running sum, not
            # (n - start_seg) * segment size.
            pos = 0
            with progress.task(
                f"volume {k}", total=e.end_seg + 1 - e.start_seg, unit="segments"
            ) as bar:
                for n in range(e.start_seg, e.end_seg + 1):
                    bar.advance()
                    st = img.entries[n].state
                    if st is SegmentState.BAD:
                        # Mapped out before the backup was written: the
                        # software skipped it, so it takes no room in the volume.
                        log.debug("segment %d (volume %d): BAD per bad-sector map; skipping", n, k)
                        n_bad += 1
                        continue
                    if st in (SegmentState.MISSING, SegmentState.UNCORRECTABLE):
                        hole = _expected_data_len(img.entries[n].excluded_mask)
                        log.debug(
                            "segment %d (volume %d): state %s; marking lost (%d-byte hole "
                            "at %d if uncompressed)",
                            n,
                            k,
                            st.name,
                            hole,
                            pos,
                        )
                        lost.append(n)
                        pos += hole
                        continue
                    data = img.segment(n)
                    if e.compressed is False:
                        # Uncompressed volume: usable data laid end to end.
                        stream.add(pos, data)
                        pos += len(data)
                        continue
                    try:
                        ext = qic122.decode_extent(data, offset_bytes=offset_bytes)
                    except qic122.Qic122Error as exc:
                        log.debug(
                            "segment %d (volume %d): QIC-122 decode failed (state %s, "
                            "%d bytes): %s; marking lost",
                            n,
                            k,
                            st.name,
                            len(data),
                            exc,
                        )
                        lost.append(n)
                        continue
                    if ext.uncompressed_offset + len(ext.data) > size:
                        log.debug(
                            "segment %d (volume %d): extent at offset %d + %d bytes runs "
                            "past volume size %d; bytes beyond it are dropped",
                            n,
                            k,
                            ext.uncompressed_offset,
                            len(ext.data),
                            size,
                        )
                    stream.add(ext.uncompressed_offset, ext.data)
            log.debug(
                "volume %d: %d segments skipped (BAD), %d lost, %d of %d bytes covered",
                k,
                n_bad,
                len(lost),
                stream.coverage(),
                size,
            )
            body, _ = stream.read(0, size)
            holes = find_holes(stream, size)
            vol = Volume(
                header={
                    "format": "TWVL",
                    "version": VERSION,
                    "volume_index": k,
                    "tape_name": q80["tape_name"],
                    "vtbl": _vtbl_dict(e),
                    "data_section_size": e.data_section_size,
                    "dir_section_size": e.dir_section_size,
                    # Where the directory section starts in the volume bytes,
                    # when that is known exactly (uncompressed Directory-Last);
                    # None = consumers locate it (qiclib.qic113 does).
                    "directory_offset": directory_offset,
                    "holes": holes,
                    "lost_segments": lost,
                    "source_image": str(image_path),
                    "drive": img.header.get("drive"),
                },
                data=body,
            )
            path = out_dir / f"vol-{k:02d}.twvl"
            log.debug("volume %d: %d holes; saving to %s", k, len(holes), path)
            vol.save(path)
            missing = sum(b - a for a, b in holes)
            log.info(
                f"{path}: {e.description!r}, {size:,} bytes, {missing:,} missing "
                f"({len(lost)} segments lost)"
            )
            written.append(path)
            overall.advance()
    return written
