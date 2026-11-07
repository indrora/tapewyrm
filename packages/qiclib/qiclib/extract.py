"""`qicsilver extract`: TWTI/TWTZ tape image -> TWVL volume files.

Reads the volume table through a volume profile (:mod:`qiclib.identify`,
:mod:`qiclib.volume_profile`), then builds each volume's bytes: QIC-122
extents decoded and placed at their uncompressed offsets, or, for an
uncompressed volume, the segments' data laid end to end. Each volume is
written with the TWVL format from ``tapewyrm_archive.twvl`` (TWS-3), holes
and all; ``qicsilver tar`` reads it from there.

Which volumes are written, and under what names, is decided here too, so the
CLI only checks that its arguments make sense together: ``volumes`` picks
volume-table indexes (parsed from ``--volumes`` by :func:`parse_volume_list`),
each written as ``{out_dir}/{prefix}{NN}.twvl``, or ``outfile`` names the one
selected volume's file. Every check that needs the volume table (does volume
N exist? is exactly one selected for ``outfile``?) runs before anything is
written, so a typo never leaves half an output directory behind.

The same machinery serves a reader that wants one volume without a file in
between: :func:`load_volume` opens the image, reads the table
(:func:`read_volume_table`) and returns one volume as the in-memory
:class:`~tapewyrm_archive.twvl.Volume` :func:`build_volume` makes, exactly
the object ``extract`` would have saved. ``qicsilver inspect IMAGE`` lists a
tape that way. Its selection rule (:func:`_pick_volume`) is deliberately
stricter than ``extract``'s: a one-volume tape needs no choice, a tape with
several needs an explicit number, never a guess.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from pathlib import Path

from tapewyrm_archive.progress import NULL_PROGRESS, Progress
from tapewyrm_archive.provenance import provenance_path
from tapewyrm_archive.twti import SegmentState, TapeImage
from tapewyrm_archive.twvl import VERSION, SparseVolume, Volume, find_holes

from qiclib import fileset as fs
from qiclib import qic122
from qiclib import volume as volume_mod
from qiclib import volume_profile as vp
from qiclib.verdict import Probe, VolumeWarning, warning_names

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

    ``qiclib.build`` records the map's mask on MISSING entries too (TWS-2
    section 5.3), so this is exact; a mask of 0 means a full 29 KiB.
    """
    excluded = bin(excluded_mask & ((1 << _SECTORS_PER_SEGMENT) - 1)).count("1")
    return max(0, _SECTORS_PER_SEGMENT - excluded - _ECC_SECTORS) * _SECTOR_BYTES


def _segment_bounds(img: TapeImage, start: int, end: int) -> tuple[list[int], int]:
    """Stream offsets where each non-BAD segment of an uncompressed volume starts, and the total.

    Mirrors the layout loop in :func:`build_volume` exactly: BAD segments take
    no room, MISSING/UNCORRECTABLE ones their expected size, read ones their data.
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


# ---------------------------------------------------------------------------
# Volume selection and output naming
# ---------------------------------------------------------------------------

#: Output file prefix when none is given: ``vol-00.twvl``, ``vol-01.twvl``...
DEFAULT_PREFIX = "vol-"


def parse_volume_list(spec: str) -> list[int]:
    """'0', '0,2', '1-3' or '0,2-4' -> sorted, de-duplicated volume numbers.

    The same syntax as ``tw dump``'s TRACKS (``tapewyrm.cli.dump._parse_tracks``),
    minus its 0-63 bound: how many volumes there are is only known once the
    volume table is read, so :func:`extract` checks the upper end. Anything
    that is not a non-negative decimal number or an ascending ``a-b`` range is
    a ``ValueError`` naming the offending part.
    """
    out: set[int] = set()
    for part in spec.split(","):
        low_text, dash, high_text = part.strip().partition("-")
        if not dash:
            high_text = low_text  # a single number N is the range N-N
        # isdecimal, not a bare int(): int() also takes ' 1', '+1' and '1_0'.
        # An empty side ('1-', '-1', '') is not decimal either, so it fails here.
        if not (low_text.isdecimal() and high_text.isdecimal()):
            log.debug("volume list %r: part %r is not N or N-M; refusing", spec, part)
            raise ValueError(f"bad volume range {part!r}: use numbers and ranges like 0,2-4")
        low, high = int(low_text), int(high_text)
        if low > high:
            log.debug("volume list %r: range %d-%d is reversed; refusing", spec, low, high)
            raise ValueError(f"bad volume range {part!r}: the range is reversed")
        out.update(range(low, high + 1))
    return sorted(out)


def _select_volumes(volumes: list[int] | None, count: int, outfile: Path | None) -> list[int]:
    """The volume-table indexes to write, checked against the table's ``count``.

    Runs before any output is written. ``None`` selects every volume; a number
    the table does not have is a ``ValueError`` listing the valid range; and
    ``outfile`` names one file, so it needs exactly one volume selected.
    """
    if volumes is None:
        log.debug("no volume selection; taking all %d volumes", count)
        selected = list(range(count))
    else:
        selected = sorted(set(volumes))
        absent = [k for k in selected if not 0 <= k < count]
        if absent:
            have = "it has no volumes" if count == 0 else f"its volumes are 0-{count - 1}"
            if count == 1:
                have = "its only volume is 0"
            log.debug("selected volumes %s not in a %d-volume table; refusing", absent, count)
            raise ValueError(
                f"no volume {', '.join(map(str, absent))} on this tape; {have} "
                "(see `qicsilver identify`)"
            )
    if outfile is not None and len(selected) != 1:
        log.debug("outfile %s with %d volumes selected; refusing", outfile, len(selected))
        raise ValueError(
            f"an output file names one volume, but {len(selected)} volumes are selected; "
            "pick one with --volumes N, or give an output directory instead"
        )
    return selected


def _pick_volume(volume: int | None, count: int) -> int:
    """The one volume-table index :func:`load_volume` builds, or a ``ValueError`` saying how to pick.

    The rule ``qicsilver inspect IMAGE`` follows. A tape with exactly one
    volume needs no choice, so ``None`` picks it. A tape with several needs
    ``volume`` (``--volume N``): silently taking the first would list the
    wrong backup without a word, and the error names the count and the
    range instead. An explicit number is range-checked by
    :func:`_select_volumes`, so ``inspect`` and ``extract`` word a missing
    volume the same way ("no volume 7 on this tape; its volumes are 0-2").
    """
    if volume is not None:
        [picked] = _select_volumes([volume], count, None)
        return picked
    if count == 1:
        log.debug("no volume given and the table has one; taking volume 0")
        return 0
    if count == 0:
        log.debug("no volume given and the volume table is empty; refusing")
        raise ValueError("the volume table is empty; there is no volume to read")
    log.debug("no volume given and the table has %d volumes; refusing", count)
    raise ValueError(
        f"this tape has {count} volumes (0-{count - 1}); pick one with --volume N "
        "(`qicsilver identify` lists them)"
    )


# ---------------------------------------------------------------------------
# Reading the volume table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VolumeTable:
    """The volume table as the best-fitting volume profile read it.

    ``profile`` is None when the segment parsed to no records at all; the
    table is then empty and nothing can be extracted. ``verdict`` is that
    profile's full reading and ``alternatives`` the other survivors of the
    fingerprint gate, best first, which :func:`stamp_fileset` falls back to
    when the chosen profile's parser does not pass.
    """

    profile: vp.VolumeProfile | None
    entries: list[volume_mod.VtblEntry]
    verdict: vp.Verdict | None = None
    alternatives: tuple[vp.Verdict, ...] = ()

    @property
    def extent_offset_bytes(self) -> int:
        """Width of the uncompressed-offset field that heads each QIC-122 extent.

        The profile knows it (Rev G's 8-byte quadword, or MTN's 4). With no
        profile there is nothing to decode anyway, so the Rev G width stands.
        """
        return self.profile.extent_offset_bytes if self.profile is not None else 8

    @property
    def extent_frame_size_bytes(self) -> int:
        """Width of each compression frame's size field (Rev G's 2, or NovaStor's 4)."""
        return self.profile.extent_frame_size_bytes if self.profile is not None else 2


def read_volume_table(img: TapeImage, volume_profile: str = vp.GUESS) -> VolumeTable:
    """The image's volume table, read through a volume profile as ``qicsilver identify`` reads it.

    ``volume_profile`` is a name, a path, or ``"guess"``. Only bytes 0-56 of a
    VTBL entry are universal: section sizes, the compression flag and the
    extent offset width differ by the software that wrote the tape, and the
    plain Rev N layout misreads them on e.g. MTN tapes -- a size taken from
    the middle of the label, compression read as off. A table segment that
    was never recovered is a ``ValueError``; a partly rebuilt one is parsed
    for what it still holds.
    """
    # The FPR member extraction cannot do without, checked up front so a
    # malformed header is a MalformedFileError naming the member, not a
    # KeyError mid-way (TWS-2 sections 4.3 and 11.2; the checks live in
    # TapeImage, see the note on qic80_segment there).
    vt_seg = img.qic80_segment("first_data_seg")
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
        # Not refused: a partly rebuilt table may still read. One that reads
        # as garbage scores low with every volume profile, and a size it
        # claims beyond what its segments can hold is refused in build_volume.
        log.debug("volume table segment %d is UNCORRECTABLE; parsing partial data anyway", vt_seg)
    log.debug("reading volume table from segment %d with volume profile %r", vt_seg, volume_profile)
    # Deferred: identify pulls in build, segment and rs (the sector decode
    # path), which only this step of extraction needs.
    from qiclib.identify import from_image

    verdicts = from_image(img, volume_profile=volume_profile).verdicts
    if not verdicts:
        log.debug("no VTBL records in segment %d; no profile to apply", vt_seg)
        return VolumeTable(None, [])
    alive = vp.survivors(verdicts)
    if not alive:
        # Every fingerprint failed. A forced profile is the user's call and
        # stands (identify notes the mismatch); a guess with no survivor has
        # no business sizing a volume.
        if volume_profile == vp.GUESS:
            log.debug("every volume profile eliminated by its fingerprint; refusing")
            raise ValueError(
                "no volume profile's fingerprint matches this tape's volume table; pick "
                "one with --volume-profile (`qicsilver identify --raw` shows the scoring)"
            )
        log.warning(
            "volume profile %s was forced, but its fingerprint does not match this tape",
            verdicts[0].profile.name,
        )
        alive = verdicts
    best = alive[0]
    profile = best.profile
    log.info("volume table read with volume profile %s (score %d)", profile.name, best.score)
    rivals = vp.contenders(verdicts)  # the deferred cannot tie for the table
    if len(rivals) > 1 and rivals[1].score == best.score:
        log.warning(
            "volume profiles %s and %s fit equally well; using %s (pick one with --volume-profile)",
            profile.name,
            rivals[1].profile.name,
            profile.name,
        )
    table = VolumeTable(profile, best.entries, best, tuple(alive[1:]))
    log.debug(
        "volume table: %d entries; extent offsets are %d bytes",
        len(table.entries),
        table.extent_offset_bytes,
    )
    return table


# ---------------------------------------------------------------------------
# Building one volume
# ---------------------------------------------------------------------------


def build_volume(
    img: TapeImage,
    k: int,
    e: volume_mod.VtblEntry,
    *,
    offset_bytes: int,
    frame_size_bytes: int = 2,
    tape_name: str,
    image_path: Path,
    progress: Progress = NULL_PROGRESS,
) -> Volume:
    """Volume ``k`` of the image, table entry ``e``, as an in-memory TWVL volume.

    QIC-122 extents are decoded (``offset_bytes`` wide offsets,
    ``frame_size_bytes`` wide frame sizes, both the profile's) and placed at
    their uncompressed offsets, or an uncompressed volume's segments are laid
    end to end; segments that were not recovered become holes. ``tape_name``
    and ``image_path`` only fill in the header (TWS-3 section 3.1). A table
    entry that runs past the image, or claims more bytes than its segments
    could hold, is a ``ValueError`` before any segment is read.
    """
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
        log.debug("volume %d: compressed flag is None; treating segments as QIC-122 extents", k)
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
    with progress.task(f"volume {k}", total=e.end_seg + 1 - e.start_seg, unit="segments") as bar:
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
                ext = qic122.decode_extent(
                    data, offset_bytes=offset_bytes, frame_size_bytes=frame_size_bytes
                )
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
            "tape_name": tape_name,
            "vtbl": _vtbl_dict(e),
            "data_section_size": e.data_section_size,
            "dir_section_size": e.dir_section_size,
            # Where the directory section starts in the volume bytes,
            # when that is known exactly (uncompressed Directory-Last);
            # None = consumers locate it (qiclib.qic113 does).
            "directory_offset": directory_offset,
            # The length of the volume bytes, so a reader can tell a
            # truncated file from a short volume (TWS-3 3.1, 6.2).
            "volume_size": size,
            "holes": holes,
            "lost_segments": lost,
            # Only the image's directory name and file name: the
            # full path as given would put the user's home directory
            # (and user name) into every volume (TWS-3 8.2).
            "source_image": provenance_path(image_path),
            "drive": img.header.get("drive"),
        },
        data=body,
    )
    missing = sum(b - a for a, b in holes)
    log.info(
        f"volume {k}: {e.description!r}, {size:,} bytes, {missing:,} missing "
        f"({len(lost)} segments lost)"
    )
    return vol


# ---------------------------------------------------------------------------
# The file-set verdict: which parser reads the volume, stamped into its header
# ---------------------------------------------------------------------------


def _same_bytes(a: vp.Verdict, b: vp.Verdict, k: int) -> bool:
    """Would profiles ``a`` and ``b`` have built volume ``k`` from the same bytes?

    Only then can ``b``'s parser be tried on a volume built under ``a``: the
    extent widths must agree, and so must the section sizes and the
    compression flag the build took from the table entry.
    """
    pa, pb = a.profile, b.profile
    if (pa.extent_offset_bytes, pa.extent_frame_size_bytes) != (
        pb.extent_offset_bytes,
        pb.extent_frame_size_bytes,
    ):
        return False
    ea, eb = a.entries[k], b.entries[k]
    return (ea.data_section_size, ea.dir_section_size, ea.compressed) == (
        eb.data_section_size,
        eb.dir_section_size,
        eb.compressed,
    )


def _probe_with(
    vol: Volume, vtbl: volume_mod.VtblEntry, verdict: vp.Verdict, k: int, source: Path
) -> Probe:
    """One profile's parser probe of volume ``k``, its ``volume_bytes`` gate included.

    The gate is the one fingerprint the table could not answer (bytes at
    the start of the volume, e.g. QICStream's ``FSET``); it fails the probe
    outright, since a parser for a different format has nothing to say.
    """
    profile = verdict.profile
    head_len = max((off + len(sig) for off, sig in profile.volume_bytes), default=0)
    gate = vp.volume_gate(profile, bytes(vol.data[:head_len]))
    failed = [c for c in gate if not c.ok]
    if failed:
        log.debug("volume %d: %s fails its volume fingerprint; fatal", k, profile.name)
        return Probe(
            checks=gate,
            fatal="volume fingerprint failed: " + "; ".join(f"{c.name} {c.detail}" for c in failed),
        )
    parser = fs.get(profile.fileset_parser)
    log.debug("volume %d: probing with parser %s (profile %s)", k, parser.name, profile.name)
    probe = parser.probe(vol, vtbl, source, profile.fileset_params)
    probe.checks[:0] = gate
    return probe


def stamp_fileset(vol: Volume, table: VolumeTable, k: int, source: Path) -> Probe | None:
    """Probe volume ``k`` with its profile's parser and write the verdict into its header.

    The table said who wrote the tape; this says whether the volume's bytes
    read as that software's catalog (:mod:`qiclib.fileset`). The header
    gains ``fileset`` (profile, parser, parameters, status and the probe)
    and ``warnings`` (TWS-3 sections 3.4 and 3.5), which ``qicsilver
    inspect`` and ``tar`` dispatch on, so a volume carries its own reading
    instructions wherever it goes.

    When the chosen parser does not pass, the other survivors of the
    fingerprint gate that would have built the same bytes
    (:func:`_same_bytes`) are tried in rank order; the first whose parser
    passes takes over, with ``low-confidence``. None passing keeps the
    chosen profile's reading, failed probe and all: the TWVL is still worth
    keeping, and the warning travels with it. A probe that passed with a
    failed check is ``low-confidence`` too.
    """
    hdr = vol.header
    verdict = table.verdict
    if verdict is None:
        log.debug("volume %d: no profile; nothing to probe", k)
        hdr["fileset"] = None
        hdr["warnings"] = []
        return None
    vtbl = fs.header_vtbl(source, hdr)
    chosen, probe = verdict, _probe_with(vol, vtbl, verdict, k, source)
    if not probe.passed:
        why = probe.fatal or f"score {probe.score}, {len(probe.failures)} checks failed"
        for alt in table.alternatives:
            if not _same_bytes(verdict, alt, k):
                log.debug(
                    "volume %d: %s would have built different bytes; not tried", k, alt.profile.name
                )
                continue
            alt_probe = _probe_with(vol, vtbl, alt, k, source)
            if alt_probe.passed:
                log.warning(
                    "volume %d: the %s parser (profile %s) did not pass its probe (%s); "
                    "the %s parser (profile %s) does, using it",
                    k,
                    verdict.profile.fileset_parser,
                    verdict.profile.name,
                    why,
                    alt.profile.fileset_parser,
                    alt.profile.name,
                )
                chosen, probe = alt, alt_probe
                probe.warnings.add(VolumeWarning.LOW_CONFIDENCE)
                break
        else:
            log.warning(
                "volume %d: the %s parser (profile %s) did not pass its probe (%s); "
                "the volume may not list",
                k,
                verdict.profile.fileset_parser,
                verdict.profile.name,
                why,
            )
    elif probe.failures:
        log.debug(
            "volume %d: probe passed with %d failed checks; low-confidence", k, len(probe.failures)
        )
        probe.warnings.add(VolumeWarning.LOW_CONFIDENCE)
    profile = chosen.profile
    warnings = set(chosen.warnings) | probe.warnings
    hdr["fileset"] = {
        "profile": profile.name,
        "parser": profile.fileset_parser,
        "params": dict(profile.fileset_params),
        "status": profile.status,
        "probe": probe.to_dict(),
    }
    hdr["warnings"] = warning_names(warnings)
    log.info(
        "volume %d: file set %s via profile %s (%s%s)",
        k,
        profile.fileset_parser,
        profile.name,
        "probe passed" if probe.passed else "probe FAILED",
        f"; warnings: {', '.join(hdr['warnings'])}" if warnings else "",
    )
    return probe


# ---------------------------------------------------------------------------
# The two entry points: every selected volume to files, or one volume in memory
# ---------------------------------------------------------------------------


def extract(
    image_path: Path,
    out_dir: Path,
    *,
    volumes: list[int] | None = None,
    prefix: str = DEFAULT_PREFIX,
    outfile: Path | None = None,
    volume_profile: str = vp.GUESS,
    progress: Progress = NULL_PROGRESS,
) -> list[Path]:
    """Write the tape image's volumes as TWVL files; returns their paths in order.

    ``volumes`` lists volume-table indexes (default: all). Each is written as
    ``{out_dir}/{prefix}{NN}.twvl``, NN being its index in the volume table
    (two digits), not its position in the selection. ``outfile`` instead names
    the file for the one selected volume (``out_dir`` and ``prefix`` are then
    unused); several volumes selected with it, or a volume the table does not
    have, is a ``ValueError`` raised before anything is written.

    The volume table is read through a volume profile, exactly as ``qicsilver
    identify`` reads it (:func:`read_volume_table`, which says why that
    matters).
    """
    log.info("extracting volumes from %s...", image_path.name)
    # A TWTZ image is decompressed to a temp file here; its finalizer deletes it
    # when img goes out of scope at return.
    img = TapeImage.open(image_path, progress=progress)
    # The other FPR member extraction needs (first_data_seg is
    # read_volume_table's), checked up front for the same reason.
    tape_name = img.qic80_str("tape_name")
    table = read_volume_table(img, volume_profile)
    vtbl = table.entries
    if not vtbl:
        log.debug("volume table parsed to no entries; nothing to extract")
    selected = _select_volumes(volumes, len(vtbl), outfile)
    log.debug("writing volumes %s of %d", selected, len(vtbl))
    # Create the one directory the output goes into: -o's parent, or out_dir.
    target_dir = outfile.parent if outfile is not None else out_dir
    log.debug("creating output directory %s", target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    with progress.task("extracting volumes", total=len(selected), unit="volumes") as overall:
        for k in selected:
            vol = build_volume(
                img,
                k,
                vtbl[k],
                offset_bytes=table.extent_offset_bytes,
                frame_size_bytes=table.extent_frame_size_bytes,
                tape_name=tape_name,
                image_path=image_path,
                progress=progress,
            )
            stamp_fileset(vol, table, k, image_path)
            path = outfile if outfile is not None else out_dir / f"{prefix}{k:02d}.twvl"
            log.debug("volume %d: %d holes; saving to %s", k, len(vol.holes), path)
            vol.save(path)
            log.info("%s: volume %d written", path, k)
            written.append(path)
            overall.advance()
    return written


def load_volume(
    image_path: Path,
    volume: int | None = None,
    *,
    volume_profile: str = vp.GUESS,
    progress: Progress = NULL_PROGRESS,
) -> Volume:
    """One volume of the tape image, in memory: what ``extract`` would have saved for it.

    ``volume`` is its index in the volume table. ``None`` is allowed only when
    the tape has exactly one volume (:func:`_pick_volume`); several volumes,
    or an index the table lacks, is a ``ValueError`` that says how to pick.
    ``volume_profile`` and ``progress`` are :func:`extract`'s.
    """
    log.info(
        "reading volume %s from %s...",
        "(the only one)" if volume is None else volume,
        image_path.name,
    )
    # A TWTZ image is decompressed to a temp file; the context manager
    # deletes it as soon as the volume's bytes are in hand.
    with TapeImage.open(image_path, progress=progress) as img:
        tape_name = img.qic80_str("tape_name")
        table = read_volume_table(img, volume_profile)
        k = _pick_volume(volume, len(table.entries))
        vol = build_volume(
            img,
            k,
            table.entries[k],
            offset_bytes=table.extent_offset_bytes,
            frame_size_bytes=table.extent_frame_size_bytes,
            tape_name=tape_name,
            image_path=image_path,
            progress=progress,
        )
        stamp_fileset(vol, table, k, image_path)
        return vol
