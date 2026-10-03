"""`tw identify`: header + volume table from the start of track 0 (image/identify.py)."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest
from tapewyrm_archive.twti import SEGMENT_STRIDE, SegmentEntry, SegmentState, TapeImage

from qiclib import identify as ident
from qiclib import segment as seg_mod
from qiclib.testing import bench_3m
from qiclib.testing.builders import (
    build_header_segment,
    build_volume_table_segment,
    build_vtbl_entry,
    make_segment_from_sectors,
    make_short_date,
    segment_raw_sectors,
    with_parity,
)
from qiclib.types import RawSector, Segment
from qiclib.volume import SIG_EXVT, VTBL_ENTRY_LEN
from tests.test_volume_bench import HEADER as BENCH_HEADER
from tests.test_volume_bench import VTBL as BENCH_VTBL

FORMAT_DATE = make_short_date(1998, 3, 14, 12, 30, 0)


def _header(seg_abs: int, **kw) -> Segment:
    return build_header_segment(seg_abs, tape_name="BACKUP", format_date=FORMAT_DATE, **kw)


def _vtbl(*extra: bytes) -> Segment:
    entries = [
        build_vtbl_entry(start_seg=3, end_seg=40, description="C: full"),
        build_vtbl_entry(start_seg=41, end_seg=60, description="D: docs", compressed=True),
        *extra,
    ]
    return build_volume_table_segment(2, entries)


def _sectors(*segs: Segment) -> list[RawSector]:
    out: list[RawSector] = []
    for seg in segs:
        out += segment_raw_sectors(seg)
    return out


def test_clean_tape_reports_header_and_volumes():
    info = ident.from_sectors(_sectors(_header(0), _header(1), _vtbl()))
    assert info.vol.tape_name == "BACKUP"
    assert (info.header_seg, info.header_state) == (0, "clean")
    assert (info.vtbl_seg, info.vtbl_state) == (2, "clean")
    assert [v.description for v in info.volumes] == ["C: full", "D: docs"]
    assert [v.compressed for v in info.volumes] == [False, True]
    assert info.notes == []
    text = "\n".join(ident.format_info(info))
    assert "BACKUP" in text and "1998-03-14 12:30:00" in text and "D: docs" in text


def test_unreadable_first_header_falls_back_to_duplicate():
    """Five lost sectors is past RS's 3; the walk must move on to the copy."""
    first = _header(0)
    for slot in range(5):
        first.sectors[slot] = None
    info = ident.from_sectors(_sectors(first, _header(1), _vtbl()))
    assert info.header_seg == 1
    assert info.vol.tape_name == "BACKUP"
    assert any("first copy (segment 0)" in n for n in info.notes)
    assert len(info.volumes) == 2


def test_header_with_bad_sector0_is_rebuilt_by_rs():
    """Sector 0 failed its CRC but the segment is correctable: still the header."""
    first = with_parity(_header(0))
    sector0 = first.sectors[0]
    assert sector0 is not None
    first.sectors[0] = RawSector(
        fsd=0, ftk=0, fsc=1, data=bytes(1024), id_crc_ok=True, data_crc_ok=False, deleted=False
    )
    assert seg_mod.segment_data(first)[:4] != sector0.data[:4]  # signature really gone
    info = ident.from_sectors(_sectors(first, _vtbl()))
    assert (info.header_seg, info.header_state) == (0, "corrected")
    assert info.vol.tape_name == "BACKUP"


def test_volume_table_not_captured():
    info = ident.from_sectors(_sectors(_header(0), _header(1)))
    assert info.volumes == []
    assert info.vtbl_state == "missing"
    assert any("segment 2 was not captured" in n for n in info.notes)


def test_exvt_record_is_reported():
    exvt = bytearray(VTBL_ENTRY_LEN)
    exvt[0:4] = SIG_EXVT
    struct.pack_into("<H", exvt, 6, 77)
    info = ident.from_sectors(_sectors(_header(0), _vtbl(bytes(exvt))))
    assert len(info.volumes) == 2  # EXVT is not a file set
    assert any("continues in segment 77" in n for n in info.notes)


def test_bad_sector_map_counts():
    info = ident.from_sectors(_sectors(_header(0, bad_lsns=[100, 101], bad_segments=[9]), _vtbl()))
    assert (len(info.bsm.bad_lsns), len(info.bsm.bad_segments)) == (2, 1)
    assert "2 sectors + 1 whole segments" in "\n".join(ident.format_info(info))


def test_no_header_is_an_error():
    with pytest.raises(ValueError, match="no header segment"):
        ident.from_sectors(_sectors(_vtbl()))


def test_bench_tape_bytes():
    """Real header + VTBL bytes off the bench Colorado tape (test_volume_bench)."""
    header = make_segment_from_sectors(0, 0, 0, [BENCH_HEADER])
    vtbl = make_segment_from_sectors(0, 2, 2, [BENCH_VTBL])
    info = ident.from_sectors(_sectors(header, vtbl))
    assert info.vol.format_code == 5
    assert (info.vol.segments_per_track, info.vol.tracks) == (207, 28)
    assert len(info.volumes) == 1
    assert info.volumes[0].description.startswith("Files from Disk1_vol1")


# ---------------------------------------------------------------------------
# TWTI images and the CLI
# ---------------------------------------------------------------------------


def _write_image(path: Path, segs: dict[int, Segment | None], count: int = 4) -> Path:
    """Save a small TWTI whose segment n holds ``segs[n]``'s data (None = missing)."""
    data: dict[int, bytes] = {}
    entries: list[SegmentEntry] = []
    for n in range(count):
        seg = segs.get(n)
        if seg is None:
            entries.append(SegmentEntry(SegmentState.MISSING))
            continue
        data[n] = seg_mod.segment_data(seg)
        entries.append(SegmentEntry(SegmentState.CLEAN, 0, len(data[n])))
    header = {
        "format": "TWTI",
        "segment_count": count,
        "segment_stride": SEGMENT_STRIDE,
        "qic80_header": {"header_seg": 0, "dup_header_seg": 1},
    }
    TapeImage(header=header, entries=entries).save(path, lambda n: data.get(n, b""))
    return path


def test_image_source(tmp_path):
    path = _write_image(tmp_path / "t.twti", {0: _header(0), 1: _header(1), 2: _vtbl()})
    info = ident.identify(path)
    assert info.vol.tape_name == "BACKUP"
    assert (info.vtbl_seg, info.vtbl_state) == (2, "clean")
    assert len(info.volumes) == 2


def test_image_with_missing_first_header(tmp_path):
    path = _write_image(tmp_path / "t.twti", {1: _header(1), 2: _vtbl()})
    info = ident.identify(path)
    assert info.header_seg == 1
    assert any("first copy (segment 0)" in n for n in info.notes)


def test_image_detected_by_magic_not_suffix(tmp_path):
    path = _write_image(tmp_path / "renamed.bin", {0: _header(0), 2: _vtbl()})
    assert ident.identify(path).vol.tape_name == "BACKUP"


# ---------------------------------------------------------------------------
# The 3M DC2120 bench tape: factory stamp, cartridge, tape profile
# ---------------------------------------------------------------------------


def _3m_sectors() -> list[RawSector]:
    data = bench_3m.header_data()
    header = make_segment_from_sectors(
        0, 0, 0, [data[k * 1024 : (k + 1) * 1024] for k in range(29)]
    )
    vtbl = make_segment_from_sectors(0, 2, 2, [bench_3m.VTBL])
    return _sectors(header, vtbl)


def test_3m_tape_end_to_end():
    info = ident.from_sectors(_3m_sectors())
    assert info.vol.manufacturer == "3M     QIC80-I IO80Fi@68IPS V1.11.22A ID5"
    assert info.vol.lot_code == "0001"
    assert info.cartridge.cartridge is not None
    assert info.cartridge.cartridge.name == "DC2120"
    assert info.profile == "mtn"
    assert info.volumes[0].source_label == "DISK1_VOL1"
    assert (len(info.bsm.bad_lsns), info.notes) == (2, [])
    text = "\n".join(ident.format_info(info))
    assert "factory pre-formatted" in text and "DC2120 class" in text
    assert "QIC-122" in text and "164.2 MB" in text


def test_forced_profile_is_used_even_when_it_scores_badly():
    info = ident.from_sectors(_3m_sectors(), tape_profile="qic80-rev-n")
    assert info.profile == "qic80-rev-n" and len(info.verdicts) == 1
    assert info.verdicts[0].failures


def test_verbose_dumps_raw_bytes_and_unused_fields():
    info = ident.from_sectors(_3m_sectors())
    text = "\n".join(ident.format_info(info, verbose=True))
    assert "78-127: 03" in text and "144-145: 02" in text  # Rev N "unused", set here
    assert "DISK1_VOL1" in text and "profile mtn: score" in text
    assert "FAIL" in text  # the losing profiles show why they lost


def test_owner_formatted_tape_has_no_stamp():
    info = ident.from_sectors(_sectors(_header(0), _vtbl()))
    assert info.vol.manufacturer == ""
    assert "no factory stamp" in "\n".join(ident.format_info(info))


def test_drive_reports_are_shown():
    drive = {"tape_status": 0x22, "drive_config": 0xD0, "drive_vendor_id": 71}
    info = ident.from_sectors(_3m_sectors(), drive=drive)
    line = next(x for x in ident.format_info(info) if x.startswith("drive saw"))
    assert "307.5 ft" in line and "extra-length" in line
