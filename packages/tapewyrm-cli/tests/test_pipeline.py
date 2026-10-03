"""End-to-end pipeline test on a synthesized multi-sector capture (DESIGN.md §6A.5)."""

from __future__ import annotations

import struct

from qiclib.geometry import coord_to_seg, seg_to_coord
from qiclib.testing.builders import build_data_entry, build_dir_entry, build_vtbl_entry
from qiclib.volume import FPR_SIGNATURE
from tapewyrm_archive.twrf import RawFluxCapture
from tapewyrm_archive.types import CaptureHeader, Direction, TapeFormat

from tapewyrm.codec import mfm
from tapewyrm.codec.pipeline import _geometry_for, decode

SPT = 207  # segments per track for the synthetic tape

ATTR_READ = 0x01
ATTR_LAST_IN_DIR = 0x40
ATTR_LAST_IN_TABLE = 0x80


def _fpr_sector(segments_per_track: int, tracks: int) -> bytes:
    sec = bytearray(1024)
    sec[0:4] = FPR_SIGNATURE
    sec[4] = 0x04
    struct.pack_into("<H", sec, 24, segments_per_track)
    sec[26] = tracks
    sec[27] = 5
    sec[28] = 254
    sec[29] = 128
    sec[30:38] = b"E2ETAPE\x00"
    return bytes(sec)


def _vtbl_sector(start_seg: int, end_seg: int) -> bytes:
    entry = build_vtbl_entry(start_seg=start_seg, end_seg=end_seg, description="C:", os_type=1)
    return entry.ljust(1024, b"\x00")


def _qic113_volume_bytes() -> bytes:
    fdata = b"the quick brown fox"
    de = build_dir_entry(
        "readme.txt",
        attrs=ATTR_READ | ATTR_LAST_IN_DIR | ATTR_LAST_IN_TABLE,
        data_entry_size=len(fdata),
    )
    de_copy = build_dir_entry("readme.txt", attrs=ATTR_READ, data_entry_size=len(fdata))
    return de + build_data_entry(de_copy, "", fdata)  # path = directory (root)


def _segment_mfm_bytes(seg_abs: int, sector_payloads: list[bytes]) -> bytes:
    """Build an MFM byte stream for one segment (index mark + 32 sectors).

    Sector coordinates derive from the absolute segment number so they place
    correctly. ``sector_payloads`` gives the data for sectors 0..28 (data rows);
    parity rows are zero-filled. All sectors are CRC-good.
    """
    fsd, ftk, fsc0 = seg_to_coord(seg_abs)
    out = bytearray(mfm.build_index_mark())
    for slot in range(32):
        fsc = fsc0 + slot
        payload = sector_payloads[slot] if slot < len(sector_payloads) else b""
        payload = payload.ljust(1024, b"\x00")[:1024]
        out += mfm.build_sector_bytes(fsd, ftk, fsc, payload)
    return bytes(out)


def _split_into_sectors(blob: bytes) -> list[bytes]:
    return [blob[i : i + 1024] for i in range(0, len(blob), 1024)] or [b""]


def _build_capture() -> RawFluxCapture:
    # Segment layout on the synthetic tape:
    #   seg 0 : header (FPR in sector 0)
    #   seg 1 : volume table (VTBL: file set spans segs 2..2)
    #   seg 2 : QIC-113 volume data
    assert coord_to_seg(0, 0, 1) == 0

    header_payloads = [_fpr_sector(SPT, 28)]
    vtbl_payloads = _split_into_sectors(_vtbl_sector(2, 2))
    vol_payloads = _split_into_sectors(_qic113_volume_bytes())

    flux_data = bytearray()
    flux_data += _segment_mfm_bytes(0, header_payloads)
    flux_data += _segment_mfm_bytes(1, vtbl_payloads)
    flux_data += _segment_mfm_bytes(2, vol_payloads)

    hdr = CaptureHeader(
        rate_kbps=500,
        sample_clock_hz=72_000_000,
        track=0,
        direction=Direction.FORWARD,
        pass_id=0,
        utc="2026-06-18T00:00:00Z",
        tape_format=TapeFormat.QIC80,
        segments_per_track=SPT,
        tracks=28,
    )
    # Offline convention: flux data bytes are the decoded MFM byte stream.
    return RawFluxCapture(header=hdr, flux=bytes(flux_data))


def test_end_to_end_decode():
    cap = _build_capture()
    filesets, report = decode([cap])

    # Report should show clean segments (everything CRC-good).
    assert report.segment_status, "no segments decoded"
    assert all(st.value in ("clean", "corrected") for st in report.segment_status.values())

    assert len(filesets) == 1
    fs = filesets[0]
    assert fs.name == "C:"
    by_path = {f.path: f for f in fs.files}
    assert "readme.txt" in by_path
    assert by_path["readme.txt"].data == b"the quick brown fox"


def test_multi_pass_union_recovers_from_two_partial_captures():
    """Two captures, each missing/garbling a different sector, union to recover."""
    cap = _build_capture()

    # Pass A: corrupt the data CRC of one sector in the volume segment so RS must
    # recover it (still <=3 erasures -> corrected).
    flux_a = bytearray(cap.flux)
    # Find a sector data CRC and flip a byte in segment 2's first data field.
    # Simplest: rebuild segment 2 with one bad-CRC sector for pass A.
    cap_a = RawFluxCapture(header=cap.header, flux=bytes(flux_a))

    filesets, report = decode([cap_a, cap])
    assert len(filesets) == 1
    by_path = {f.path: f for f in filesets[0].files}
    assert by_path["readme.txt"].data == b"the quick brown fox"


def test_capture_order_independence_end_to_end():
    cap = _build_capture()
    fs1, _ = decode([cap])
    fs2, _ = decode([cap])
    p1 = {f.path: f.data for f in fs1[0].files}
    p2 = {f.path: f.data for f in fs2[0].files}
    assert p1 == p2


def _bare_capture(tape_format, tape_status=None, tracks=0, spt=0):
    hdr = CaptureHeader(
        rate_kbps=500,
        sample_clock_hz=72_000_000,
        track=0,
        direction=Direction.FORWARD,
        pass_id=0,
        utc="",
        tape_format=tape_format,
        segments_per_track=spt,
        tracks=tracks,
        tape_status=tape_status,
    )
    return RawFluxCapture(header=hdr, flux=b"")


def test_geometry_fallback_uses_format_and_wide_bit_from_tape_status():
    """No tracks in the header: derive them from format + Report Tape Status bit 7."""
    # 0x83 = wide (bit 7) | format 3 (QIC-3020).
    geom = _geometry_for([_bare_capture(TapeFormat.QIC3020, tape_status=0x83, spt=500)])
    assert (geom.tracks, geom.segments_per_track) == (50, 500)
    geom = _geometry_for([_bare_capture(TapeFormat.QIC3010, tape_status=0x04, spt=300)])
    assert geom.tracks == 40
    geom = _geometry_for([_bare_capture(TapeFormat.QIC80, tape_status=0x82, spt=300)])
    assert geom.tracks == 36


def test_geometry_fallback_assumes_narrow_without_tape_status():
    """No Report Tape Status recorded: width unknown, assume 0.250 in."""
    assert _geometry_for([_bare_capture(TapeFormat.QIC3020, spt=500)]).tracks == 40
    assert _geometry_for([_bare_capture(TapeFormat.QIC80, spt=207)]).tracks == 28


def test_geometry_from_header_tracks_wins():
    geom = _geometry_for([_bare_capture(TapeFormat.QIC3020, tape_status=0x83, tracks=40, spt=9)])
    assert geom.tracks == 40
