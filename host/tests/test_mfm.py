"""MFM framing + CRC tests: encode->decode round-trip, CRC pass/fail, F8."""

from __future__ import annotations

from tapewyrm.codec import mfm
from tapewyrm.codec.mfm import (
    build_index_mark,
    build_sector_bytes,
    build_segment_bytes,
    crc_ccitt,
    recover_sectors_from_bytes,
)
from tapewyrm.types import FluxStream


def _data(seed: int) -> bytes:
    return bytes((seed + i) & 0xFF for i in range(1024))


def test_crc_ccitt_preset_ones():
    # Empty input with preset 0xFFFF returns the preset (no bytes processed).
    assert crc_ccitt(b"") == 0xFFFF
    # Deterministic / stable for a fixed input.
    v = crc_ccitt(b"\x12\x34\x56")
    assert isinstance(v, int) and 0 <= v <= 0xFFFF
    # Same input -> same CRC.
    assert crc_ccitt(b"\x12\x34\x56") == v


def test_single_sector_round_trip():
    raw = build_sector_bytes(fsd=1, ftk=2, fsc=3, data=_data(0))
    sectors = list(recover_sectors_from_bytes(raw))
    assert len(sectors) == 1
    s = sectors[0]
    assert (s.fsd, s.ftk, s.fsc) == (1, 2, 3)
    assert s.data == _data(0)
    assert s.id_crc_ok is True
    assert s.data_crc_ok is True
    assert s.deleted is False


def test_bad_id_crc_flagged():
    raw = build_sector_bytes(1, 2, 3, _data(1), bad_id_crc=True)
    s = list(recover_sectors_from_bytes(raw))[0]
    assert s.id_crc_ok is False
    assert s.data_crc_ok is True


def test_bad_data_crc_flagged():
    raw = build_sector_bytes(1, 2, 3, _data(2), bad_data_crc=True)
    s = list(recover_sectors_from_bytes(raw))[0]
    assert s.id_crc_ok is True
    assert s.data_crc_ok is False


def test_deleted_block_f8():
    raw = build_sector_bytes(4, 5, 6, _data(3), deleted=True)
    s = list(recover_sectors_from_bytes(raw))[0]
    assert s.deleted is True
    assert s.data_crc_ok is True  # CRC still valid even for a deleted block


def test_segment_with_index_mark_and_many_sectors():
    sectors = [(0, 0, i + 1, _data(i)) for i in range(5)]
    seg_bytes = build_segment_bytes(sectors)
    # Index mark present at the head.
    assert seg_bytes.startswith(build_index_mark())
    recovered = list(recover_sectors_from_bytes(seg_bytes))
    assert len(recovered) == 5
    for i, s in enumerate(recovered):
        assert s.fsc == i + 1
        assert s.data == _data(i)
        assert s.data_crc_ok and s.id_crc_ok


def test_recover_sectors_via_fluxstream_adapter():
    raw = build_segment_bytes([(0, 0, 1, _data(7)), (0, 0, 2, _data(8))])
    # Offline convention: flux intervals carry decoded MFM byte values verbatim.
    fs = FluxStream(intervals=list(raw), sample_clock_hz=72_000_000)
    recovered = list(mfm.recover_sectors(fs, rate_kbps=500))
    assert len(recovered) == 2
    assert recovered[0].data == _data(7)
    assert recovered[1].data == _data(8)


def test_short_data_is_padded_on_encode():
    s = build_sector_bytes(0, 0, 1, b"\x01\x02\x03")
    rec = list(recover_sectors_from_bytes(s))[0]
    assert len(rec.data) == 1024
    assert rec.data[:3] == b"\x01\x02\x03"
    assert rec.data_crc_ok
