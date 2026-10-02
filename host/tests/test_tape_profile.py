"""Tape profiles: VTBL layouts as data, and guessing between them (codec/tape_profile.py)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tapewyrm.codec import tape_profile as tp
from tapewyrm.codec import volume as volume_mod
from tests.fixtures import bench_3m
from tests.fixtures.builders import build_format_parameter_record, build_vtbl_entry
from tests.test_volume_bench import HEADER as JC_HEADER
from tests.test_volume_bench import VTBL as JC_VTBL

NOW = datetime(2026, 10, 1, tzinfo=UTC)


def _jc():
    vol, _ = volume_mod.parse_header_data(JC_HEADER)
    return vol, [JC_VTBL]


def _3m():
    vol, _ = volume_mod.parse_header_data(bench_3m.header_data())
    return vol, [bench_3m.VTBL]


def test_builtin_profiles_load():
    names = tp.builtin_names()
    assert {"qic80-rev-n", "cms-qic113", "vendor-unknown", "mtn"} <= set(names)
    for profile in tp.load_builtin():
        assert profile.name in names and profile.description


def test_guess_picks_cms_for_the_colorado_tape():
    vol, records = _jc()
    verdicts = tp.guess(records, vol, now=NOW)
    assert verdicts[0].profile.name == "cms-qic113"
    assert verdicts[0].failures == []
    assert verdicts[0].score > verdicts[1].score


def test_guess_picks_mtn_for_the_3m_tape():
    vol, records = _3m()
    verdicts = tp.guess(records, vol, now=NOW)
    best = verdicts[0]
    assert best.profile.name == "mtn" and best.failures == []
    entry = best.entries[0]
    assert entry.source_label == "DISK1_VOL1"
    assert (entry.dir_section_size, entry.data_section_size) == (57344, 172068357)
    assert (entry.compressed, entry.compression_code) == (True, 1)
    assert (entry.start_seg, entry.end_seg) == (4, 3461)


def test_rev_n_reads_garbage_from_the_3m_tape():
    """The reading `tw identify` printed before profiles: the checks must reject it."""
    vol, records = _3m()
    verdict = tp.evaluate(records, vol, tp.load("qic80-rev-n"), now=NOW)
    failed = {c.name for c in verdict.failures}
    assert {"label is text", "size fits"} <= failed


@pytest.mark.parametrize(
    "rec",
    [
        JC_VTBL,
        build_vtbl_entry(start_seg=3, end_seg=40, description="C:", dir_section_size=4096),
        build_vtbl_entry(start_seg=3, end_seg=40, compressed=True, os_type=1),
    ],
)
def test_profiles_agree_with_the_builtin_parser(rec):
    """`tw extract` still uses volume._parse_vtbl_entry; keep the two in step."""
    builtin = volume_mod._parse_vtbl_entry(rec)
    name = "cms-qic113" if builtin.vendor_specific else "qic80-rev-n"
    assert tp.decode_entry(rec, tp.load(name)) == builtin


def test_vendor_unknown_reads_nothing_past_byte_56():
    entry = tp.decode_entry(JC_VTBL, tp.load("vendor-unknown"))
    assert entry.source_label is None and entry.data_section_size is None
    assert entry.start_seg == 3  # bytes 0-56 are always decoded


def test_match_hints_score_double():
    vol, records = _3m()
    checks = tp.match_checks(records[0], vol, tp.load("mtn"))
    assert [c.points for c in checks] == [2, 2]


def test_date_before_first_format_fails():
    vol, _ = volume_mod.parse_header_data(bench_3m.header_data())  # formatted 1994
    old = bytearray(bench_3m.VTBL)
    old[52:56] = (0).to_bytes(4, "little")  # undefined date: no date check at all
    assert all(
        c.name != "date" for c in tp.entry_checks(volume_mod.parse_vtbl_base(bytes(old)), vol, NOW)
    )
    early = bytearray(bench_3m.VTBL)
    early[52:56] = ((1990 - 1970) << 25).to_bytes(4, "little")
    checks = tp.entry_checks(volume_mod.parse_vtbl_base(bytes(early)), vol, NOW)
    assert [c.ok for c in checks if c.name == "date"] == [False]


def test_load_from_path(tmp_path):
    path = tmp_path / "mine.toml"
    path.write_text('name = "mine"\n[vtbl]\nsource_label = [64, 16]\n')
    profile = tp.load(str(path))
    assert profile.name == "mine" and profile.fields["source_label"] == tp.FieldSpec(64, 16)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("[vtbl]\n", "missing 'name'"),
        ('name = "x"\n[vtbl]\nlabel = [102, 16]\n', "unknown [vtbl] field"),
        ('name = "x"\n[vtbl]\nsource_label = [40, 16]\n', "outside bytes 57-127"),
        ('name = "x"\n[vtbl]\nsource_label = [120, 16]\n', "outside bytes 57-127"),
        ("name = ", "mine.toml"),
    ],
)
def test_malformed_profiles(tmp_path, body, message):
    path = tmp_path / "mine.toml"
    path.write_text(body)
    with pytest.raises(tp.TapeProfileError, match=message.replace("[", r"\[")):
        tp.load(str(path))


def test_unknown_name_lists_builtins():
    with pytest.raises(tp.TapeProfileError, match="mtn"):
        tp.load("no-such-profile")


def test_synthetic_header_round_trip():
    """Builders' default header is a Rev N tape: rev-n wins on a plain entry."""
    vol, _ = volume_mod.parse_header_data(build_format_parameter_record())
    rec = build_vtbl_entry(start_seg=3, end_seg=40, description="C:", dir_section_size=4096)
    assert tp.guess([rec], vol, now=NOW)[0].profile.name == "qic80-rev-n"
