"""`tw identify` front end (moves to qicsilver)."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner
from qiclib import segment as seg_mod
from qiclib.testing import bench_3m
from qiclib.testing.builders import (
    build_header_segment,
    build_volume_table_segment,
    build_vtbl_entry,
    make_segment_from_sectors,
    make_short_date,
)
from qiclib.types import Segment
from tapewyrm_archive.twti import SEGMENT_STRIDE, SegmentEntry, SegmentState, TapeImage

from tapewyrm.cli import cli

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


def test_cli_json(tmp_path):
    path = _write_image(tmp_path / "t.twti", {0: _header(0), 1: _header(1), 2: _vtbl()})
    result = CliRunner().invoke(cli, ["identify", "--json", str(path)])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert doc["header"]["tape_name"] == "BACKUP"
    assert doc["header"]["format_date_decoded"] == "1998-03-14 12:30:00"
    assert [v["description"] for v in doc["volumes"]] == ["C: full", "D: docs"]


def test_cli_text(tmp_path):
    path = _write_image(tmp_path / "t.twti", {0: _header(0), 2: _vtbl()})
    result = CliRunner().invoke(cli, ["identify", str(path)])
    assert result.exit_code == 0, result.output
    assert "tape name     BACKUP" in result.output


def test_cli_tape_profile_and_json(tmp_path):
    data = bench_3m.header_data()
    header = make_segment_from_sectors(
        0, 0, 0, [data[k * 1024 : (k + 1) * 1024] for k in range(29)]
    )
    vtbl = make_segment_from_sectors(0, 2, 2, [bench_3m.VTBL])
    path = _write_image(tmp_path / "t.twti", {0: header, 2: vtbl})
    result = CliRunner().invoke(cli, ["identify", "--json", str(path)])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert doc["tape_profile"] == "mtn"
    assert doc["header"]["lot_code"] == "0001"
    assert doc["cartridge"]["catalogue"]["name"] == "DC2120"
    assert doc["volumes"][0]["raw_hex"].startswith("5654424c")

    forced = CliRunner().invoke(cli, ["identify", "--tape-profile", "cms-qic113", str(path)])
    assert forced.exit_code == 0 and "tape profile  cms-qic113" in forced.output

    bad = CliRunner().invoke(cli, ["identify", "--tape-profile", "nope", str(path)])
    assert bad.exit_code != 0 and "no tape profile 'nope'" in bad.output
