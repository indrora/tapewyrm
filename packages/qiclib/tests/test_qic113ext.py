"""QIC-113 Rev G section 8 (extended volumes) and the sparse volume stream."""

import struct

from tapewyrm_archive.twvl import SparseVolume

from qiclib import qic113ext as q


def _dd(ddid: int, area: int, st: bytes = b"", name: str = "") -> bytes:
    raw = name.encode("utf-16-le")
    return struct.pack("<HQH", ddid, area, len(st)) + st + struct.pack("<H", len(raw)) + raw


def _win95(mtime: int, attrs: int = 0) -> bytes:
    return struct.pack("<I", attrs) + b"\xff" * 16 + struct.pack("<II", mtime, 0)


def _entry(name: str, traversal: int, size: int = 0, path_size: int = 0, mtime: int = 0) -> bytes:
    """A Directory Entry with Data (7), Win95 (10) and DOS (2) descriptions."""
    dds = (
        _dd(q.DD_DATA, size)
        + _dd(q.DD_WIN95, 0, _win95(mtime), name)
        + _dd(q.DD_DOS, 0, b"\x00" * 9, name)
    )
    body = struct.pack("<QHHB", 0, path_size, q.DD_WIN95, traversal) + dds
    return struct.pack("<H", len(body)) + body


def _with_data_entry_size(entry: bytes, data_entry_size: int) -> bytes:
    return entry[:2] + struct.pack("<Q", data_entry_size) + entry[10:]


ROOT = q.T_ROOT | q.T_DIR | q.T_LAST_IN_DIR


def test_directory_parse_names_sizes_times_and_paths():
    entries_raw = [
        _entry("C:", ROOT),  # root
        _entry("WINDOWS", q.T_DIR, mtime=0xFFFFFFFF),  # spec: all ones = unknown
        _entry("AUTOEXEC.BAT", q.T_LAST_IN_DIR, size=120, mtime=914_390_000),
        _entry("Long Name.txt", q.T_LAST_IN_DIR | q.T_LAST_IN_SET, size=5),  # inside WINDOWS
    ]
    entries = q.parse_directory(b"".join(entries_raw) + b"\x00\x00")
    assert [e.name for e in entries] == ["C:", "WINDOWS", "AUTOEXEC.BAT", "Long Name.txt"]
    assert entries[2].size == 120 and entries[2].mtime is not None
    assert entries[2].mtime.year == 1998
    assert entries[1].mtime is None  # 0xFFFFFFFF = unknown (QIC-113 2.2); 0 is the epoch
    assert q.directory_paths(entries) == [
        "C:",
        "C:/WINDOWS",
        "C:/AUTOEXEC.BAT",
        "C:/WINDOWS/Long Name.txt",
    ]


def test_path_entry_components():
    raw = struct.pack("<H", 10) + "WINDOWS".encode("utf-16-le") + b"\x00\x00"
    raw += struct.pack("<H", 10) + "SYSTEM".encode("utf-16-le")
    assert q.decode_path_entry(raw) == ["WINDOWS", "SYSTEM"]


def test_layout_uses_directory_sizes_and_skips_null_areas():
    # One file: sig + entry copy + path + DATA area (DOS/UNIX areas take no space).
    path = struct.pack("<H", 10) + "X".encode("utf-16-le")
    e_raw = _entry("F", q.T_LAST_IN_DIR | q.T_LAST_IN_SET, size=4, path_size=len(path))
    data_area = q.DATA_AREA_SIG + struct.pack("<H", q.DD_DATA) + b"DATA"
    win_area = q.DATA_AREA_SIG + struct.pack("<H", q.DD_WIN95)  # 0-byte area
    data_entry = q.DATA_ENTRY_SIG + e_raw + path + data_area + win_area
    entry = q.parse_directory(_with_data_entry_size(e_raw, len(data_entry)))[0]
    (lay,) = list(q.layout([entry], volume=data_entry))
    assert lay.entry_offset == 0 and lay.data_size == 4
    assert data_entry[lay.data_offset : lay.data_offset + 4] == b"DATA"
    # Without the volume bytes the copy's size is re-derived; same answer.
    assert next(q.layout([entry])).data_offset == lay.data_offset


def test_sparse_volume_reads_report_holes():
    v = SparseVolume(size=100)
    v.add(50, b"B" * 20)
    v.add(0, b"A" * 30)
    data, missing = v.read(20, 40)  # 20..60: A[20:30], hole 30..50, B[50:60]
    assert data == b"A" * 10 + b"\x00" * 20 + b"B" * 10
    assert missing == 20
    assert v.coverage() == 50
