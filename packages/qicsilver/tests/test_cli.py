"""qicsilver CLI: identify on TWTI/TWTZ images; clean errors on truncated inputs."""

from __future__ import annotations

import json
import re
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

from qicsilver.cli import cli

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


def _write_image(
    path: Path, segs: dict[int, Segment | None], count: int = 4, qic80: dict | None = None
) -> Path:
    """Save a small TWTI whose segment n holds ``segs[n]``'s data (None = missing).

    ``qic80`` replaces the header's qic80_header object.
    """
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
        "qic80_header": qic80 if qic80 is not None else {"header_seg": 0, "dup_header_seg": 1},
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


def test_cli_identify_twtz(tmp_path):
    path = _write_image(tmp_path / "t.twtz", {0: _header(0), 2: _vtbl()})
    result = CliRunner().invoke(cli, ["identify", str(path)])
    assert result.exit_code == 0, result.output
    assert "tape name     BACKUP" in result.output


def test_cli_volume_profile_and_json(tmp_path):
    data = bench_3m.header_data()
    header = make_segment_from_sectors(
        0, 0, 0, [data[k * 1024 : (k + 1) * 1024] for k in range(29)]
    )
    vtbl = make_segment_from_sectors(0, 2, 2, [bench_3m.VTBL])
    path = _write_image(tmp_path / "t.twti", {0: header, 2: vtbl})
    result = CliRunner().invoke(cli, ["identify", "--json", str(path)])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert doc["volume_profile"] == "mtn"
    assert doc["header"]["lot_code"] == "0001"
    assert doc["cartridge"]["catalogue"]["name"] == "DC2120"
    assert doc["volumes"][0]["raw_hex"].startswith("5654424c")

    forced = CliRunner().invoke(cli, ["identify", "--volume-profile", "cms-qic113", str(path)])
    assert forced.exit_code == 0 and "volume profile  cms-qic113" in forced.output

    bad = CliRunner().invoke(cli, ["identify", "--volume-profile", "nope", str(path)])
    assert bad.exit_code != 0 and "no volume profile 'nope'" in bad.output


# ---------------------------------------------------------------------------
# Truncated inputs: a one-line error and exit 1, never a traceback
# ---------------------------------------------------------------------------


def _truncated_image(tmp_path: Path, name: str = "t.twti") -> Path:
    path = _write_image(tmp_path / name, {0: _header(0), 1: _header(1), 2: _vtbl()})
    with path.open("r+b") as f:
        f.truncate(path.stat().st_size - SEGMENT_STRIDE)  # the last slot is gone
    return path


def _assert_clean_error(result, *needles: str) -> None:
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit)  # ClickException, not a crash
    assert "Traceback" not in result.output
    # rich-click wraps the message in a box at the terminal width, which can
    # break a long tmp path anywhere: compare with the box and spaces gone.
    flat = re.sub(r"[\s\u2500-\u257f]+", "", result.output)
    for needle in needles:
        assert re.sub(r"\s+", "", needle) in flat


def test_identify_truncated_image_is_a_clean_error(tmp_path):
    path = _truncated_image(tmp_path)
    result = CliRunner().invoke(cli, ["identify", str(path)])
    _assert_clean_error(result, "t.twti", "truncated")


def test_extract_truncated_twtz_is_a_clean_error(tmp_path):
    from tapewyrm_archive._zstd import zstd

    twti = _write_image(tmp_path / "t.twti", {0: _header(0), 1: _header(1), 2: _vtbl()})
    twtz = tmp_path / "t.twtz"
    packed = zstd.compress(twti.read_bytes())
    twtz.write_bytes(packed[: len(packed) // 2])
    result = CliRunner().invoke(cli, ["extract", str(twtz), str(tmp_path / "out")])
    _assert_clean_error(result, "t.twtz", "truncated")


def test_identify_mistyped_header_seg_is_a_clean_error(tmp_path):
    path = _write_image(
        tmp_path / "m.twti", {0: _header(0), 1: _header(1), 2: _vtbl()}, qic80={"header_seg": "0"}
    )
    result = CliRunner().invoke(cli, ["identify", str(path)])
    _assert_clean_error(result, "m.twti", "qic80_header.header_seg")


def test_extract_header_without_first_data_seg_is_a_clean_error(tmp_path):
    path = _write_image(
        tmp_path / "m.twti",
        {0: _header(0), 1: _header(1), 2: _vtbl()},
        qic80={"header_seg": 0, "tape_name": "BACKUP"},
    )
    result = CliRunner().invoke(cli, ["extract", str(path), str(tmp_path / "out")])
    _assert_clean_error(result, "m.twti", "qic80_header.first_data_seg")


def test_tar_truncated_volume_is_a_clean_error(tmp_path):
    from tapewyrm_archive.twvl import Volume

    path = tmp_path / "vol-00.twvl"
    Volume(header={"format": "TWVL", "holes": [], "volume_size": 100}, data=bytes(100)).save(path)
    with path.open("r+b") as f:
        f.truncate(path.stat().st_size - 10)
    result = CliRunner().invoke(cli, ["tar", str(path), str(tmp_path / "x.tar")])
    _assert_clean_error(result, "vol-00.twvl", "truncated")
    short = tmp_path / "short.twvl"
    short.write_bytes(b"TWVL\x01")  # cut inside the preamble
    result = CliRunner().invoke(cli, ["tar", str(short), str(tmp_path / "y.tar")])
    _assert_clean_error(result, "short.twvl", "truncated")


# ---------------------------------------------------------------------------
# extract: OUTDIR default, --volumes, --prefix, -o
# ---------------------------------------------------------------------------


def _multi_volume_image(path: Path, count: int = 3) -> Path:
    """``count`` one-segment uncompressed volumes (segments 3..), volume k all ``k``."""
    import struct

    records = []
    for k in range(count):
        rec = bytearray(build_vtbl_entry(start_seg=3 + k, end_seg=3 + k, description=f"V{k}"))
        struct.pack_into("<Q", rec, 96, 29 * 1024)  # Rev N quadword data_section_size
        records.append(bytes(rec))
    data = {
        0: seg_mod.segment_data(_header(0)),
        1: seg_mod.segment_data(_header(1)),
        2: seg_mod.segment_data(build_volume_table_segment(2, records)),
        **{3 + k: bytes([k]) * (29 * 1024) for k in range(count)},
    }
    entries = [SegmentEntry(SegmentState.CLEAN, 0, len(data[n])) for n in range(3 + count)]
    header = {
        "format": "TWTI",
        "segment_count": 3 + count,
        "segment_stride": SEGMENT_STRIDE,
        "qic80_header": {
            "header_seg": 0,
            "dup_header_seg": 1,
            "first_data_seg": 2,
            "tape_name": "BACKUP",
        },
    }
    TapeImage(header=header, entries=entries).save(path, lambda n: data[n])
    return path


def test_extract_outdir_defaults_to_the_current_directory(tmp_path, monkeypatch):
    image = _multi_volume_image(tmp_path / "m.twti")
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["extract", str(image)])
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in tmp_path.glob("vol-*.twvl")) == [
        "vol-00.twvl",
        "vol-01.twvl",
        "vol-02.twvl",
    ]


def test_extract_volumes_and_prefix(tmp_path):
    image = _multi_volume_image(tmp_path / "m.twti")
    out = tmp_path / "out"
    result = CliRunner().invoke(
        cli, ["extract", str(image), str(out), "--volumes", "0,2", "--prefix", "jc-"]
    )
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in out.iterdir()) == ["jc-00.twvl", "jc-02.twvl"]
    assert result.stdout.split() == [str(out / "jc-00.twvl"), str(out / "jc-02.twvl")]


def test_extract_outfile_is_relative_to_the_cwd(tmp_path, monkeypatch):
    from tapewyrm_archive.twvl import Volume

    image = _multi_volume_image(tmp_path / "m.twti")
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["extract", str(image), "--volumes", "1", "-o", "one.twvl"])
    assert result.exit_code == 0, result.output
    assert Volume.load(tmp_path / "one.twvl").data == bytes([1]) * (29 * 1024)
    assert not list(tmp_path.glob("vol-*"))


def test_extract_outfile_errors(tmp_path):
    image = _multi_volume_image(tmp_path / "m.twti")
    out = str(tmp_path / "x.twvl")
    several = CliRunner().invoke(cli, ["extract", str(image), "-o", out])
    _assert_clean_error(several, "3 volumes", "--volumes")
    listed = CliRunner().invoke(cli, ["extract", str(image), "--volumes", "0-1", "-o", out])
    assert listed.exit_code == 2 and "2 volumes" in listed.output
    with_dir = CliRunner().invoke(
        cli, ["extract", str(image), str(tmp_path / "d"), "--volumes", "0", "-o", out]
    )
    assert with_dir.exit_code == 2 and "OUTDIR" in with_dir.output
    with_prefix = CliRunner().invoke(
        cli, ["extract", str(image), "--volumes", "0", "-o", out, "--prefix", "p-"]
    )
    assert with_prefix.exit_code == 2 and "--prefix" in with_prefix.output
    assert not (tmp_path / "x.twvl").exists() and not (tmp_path / "d").exists()


def test_extract_bad_volume_numbers(tmp_path):
    image = _multi_volume_image(tmp_path / "m.twti")
    absent = CliRunner().invoke(cli, ["extract", str(image), str(tmp_path / "o"), "--volumes", "7"])
    _assert_clean_error(absent, "no volume 7", "0-2")
    assert not (tmp_path / "o").exists()
    typo = CliRunner().invoke(cli, ["extract", str(image), "--volumes", "2-a"])
    assert typo.exit_code == 2 and "bad volume range" in typo.output
