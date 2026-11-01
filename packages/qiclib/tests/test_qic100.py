"""qiclib.qic100: GCR table, CRCC, ID field, record framing round-trips (synthetic only)."""

from __future__ import annotations

import pytest

from qiclib import qic100 as q


def test_gcr_table_is_the_standards_and_a_bijection():
    # QIC-100-MC Rev F Table 1, a few spot values, and the 0,2 constraint.
    assert q.GCR_ENCODE[0x0] == 0x19
    assert q.GCR_ENCODE[0x9] == 0x09
    assert q.GCR_ENCODE[0xC] == 0x1E
    assert q.GCR_ENCODE[0xF] == 0x0F
    assert len(set(q.GCR_ENCODE)) == 16
    for g in q.GCR_ENCODE:
        assert "000" not in f"{g:05b}"
        assert q.GCR_DECODE[g] == q.GCR_ENCODE.index(g)


def test_crcc_matches_the_standards_worked_example():
    # Section 9.1.2: processing the byte 01 leaves the polynomial's tap
    # positions set: X34 X33 X27 X25 X23 X22 X09 X00 = 06 0A C0 02 01.
    assert q.crcc_bytes(bytes([1])) == bytes.fromhex("060ac00201")
    assert q.crcc_bytes(b"") == bytes(5)


def test_id_field_round_trip_non_id():
    raw = q.build_id_field(
        q.RecordType.DATA,
        track=5,
        frame=0x0123,
        thread=3,
        date=(16 << 9) | (12 << 5) | 23,  # 1996-12-23
        time=(5 << 11) | (13 << 5) | 10,  # 05:13:20
        manufacturer="QI",
    )
    f = q.parse_id_field(raw, q.RecordType.DATA)
    assert (f.track, f.thread, f.frame) == (5, 3, 0x0123)
    assert raw[2:4] == bytes([0x01, 0x23])  # frame LSB in bits 8-15 (6.7.3.2.2)
    assert f.data_field_length == 127
    assert f.frames_per_track == 618 and f.tracks_per_tape == 24
    assert f.date_tuple == (1996, 12, 23)
    assert f.time_tuple == (5, 13, 20)
    assert f.manufacturer == "QI"


def test_id_field_of_id_record_has_zero_bytes():
    raw = q.build_id_field(q.RecordType.ID, track=1, frame=7)
    assert raw[1] == 0 and raw[4:9] == bytes(5)
    f = q.parse_id_field(raw, q.RecordType.ID)
    assert f.thread is None and f.error_control is None
    assert f.frame == 7 and f.time_tuple is None and f.date_tuple is None


def _frame_cells(frame: int, payload: bytes, *, gap_cells: int = 100) -> bytes:
    id_rec = q.encode_record(
        q.RecordType.ID, None, q.build_id_field(q.RecordType.ID, track=0, frame=frame)
    )
    data = payload.ljust(q.DATA_FIELD_BYTES, b"\0")
    data_rec = q.encode_record(
        q.RecordType.DATA, data, q.build_id_field(q.RecordType.DATA, track=0, frame=frame)
    )
    gap = bytes([0, 1, 0, 1]) * (gap_cells // 4)  # erased-ish filler, never a preamble
    return id_rec + gap + data_rec + gap


def test_frame_records_round_trip():
    cells = _frame_cells(3, b"hello tape") + _frame_cells(4, b"second")
    recs = q.frame_records(cells)
    assert [r.record_type for r in recs] == [1, 0, 1, 0]
    assert [r.frame for r in recs] == [3, 3, 4, 4]
    assert all(r.crcc_ok for r in recs)
    assert recs[1].data_field is not None and recs[1].data_field[:10] == b"hello tape"
    assert recs[0].data_field is None
    # Positions: the ID record is preamble + SOD + (1 + 26 + 5) bytes + postamble.
    r = recs[0]
    assert r.sod_at == r.preamble_at + 310
    assert r.type_at == r.sod_at + 10
    assert r.id_at == r.type_at + 10
    assert r.crcc_at == r.id_at + 26 * 10
    assert r.postamble_at == r.crcc_at + 5 * 10
    assert r.end == r.postamble_at + 20
    # The data record's data field starts right after its type marker.
    d = recs[1]
    assert d.data_at == d.type_at + 10 and d.id_at == d.data_at + q.DATA_FIELD_BYTES * 10


def test_bad_crcc_is_flagged_not_dropped():
    raw = q.build_id_field(q.RecordType.ID, track=0, frame=9)
    cells = q.encode_record(q.RecordType.ID, None, raw, corrupt_crcc=True)
    (rec,) = q.frame_records(cells)
    assert rec.crcc_ok is False and rec.frame == 9


def test_short_preamble_is_not_a_record():
    raw = q.build_id_field(q.RecordType.ID, track=0, frame=1)
    cells = q.encode_record(q.RecordType.ID, None, raw, preamble=50)
    assert q.frame_records(cells) == []
    assert len(q.frame_records(cells, min_preamble=40)) == 1


def test_cells_to_intervals_and_bitcell_estimate():
    cells = q.encode_record(
        q.RecordType.ID, None, q.build_id_field(q.RecordType.ID, track=0, frame=1)
    )
    intervals = q.cells_to_intervals(cells, 72)
    assert sum(intervals) == 72 * (len(cells) - cells.index(1))  # from the first transition
    assert q.estimate_bitcell_ticks(intervals) == 72


def test_encode_rejects_wrong_shapes():
    raw = q.build_id_field(q.RecordType.ID, track=0, frame=1)
    with pytest.raises(ValueError):
        q.encode_record(q.RecordType.ID, b"x" * 4160, raw)
    with pytest.raises(ValueError):
        q.encode_record(q.RecordType.DATA, b"short", raw)
