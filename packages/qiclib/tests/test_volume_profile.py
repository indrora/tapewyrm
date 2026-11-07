"""Volume profiles: VTBL layouts as data, the fingerprint gate, and guessing between them."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from qiclib import header as header_mod
from qiclib.testing import bench_3m, bench_unk_co
from qiclib.testing.builders import build_format_parameter_record, build_vtbl_entry
from qiclib.volume import fileset as fs
from qiclib.volume import profile as vp
from qiclib.volume import table as table_mod
from qiclib.volume.verdict import VolumeWarning
from tests.test_volume_bench import HEADER as JC_HEADER
from tests.test_volume_bench import VTBL as JC_VTBL

NOW = datetime(2026, 11, 7, tzinfo=UTC)


def _jc():
    vol, _ = header_mod.parse_header_data(JC_HEADER)
    return vol, [JC_VTBL]


def _3m():
    vol, _ = header_mod.parse_header_data(bench_3m.header_data())
    return vol, [bench_3m.VTBL]


def _unk_co():
    vol, _ = header_mod.parse_header_data(bench_unk_co.header_data())
    return vol, [bench_unk_co.VTBL]


def test_builtin_profiles_load():
    names = vp.builtin_names()
    assert {"qic80-rev-n", "cms-qic113", "qic113-rev-g", "vendor-unknown", "mtn"} <= set(names)
    for profile in vp.load_builtin():
        assert profile.name in names and profile.description
        assert profile.status in vp.STATUSES and profile.fileset_parser in fs.names()


# ---------------------------------------------------------------------------
# Guessing on the bench tapes
# ---------------------------------------------------------------------------


def test_guess_picks_cms_for_the_colorado_tape():
    vol, records = _jc()
    verdicts = vp.guess(records, vol, now=NOW)
    best, runner = verdicts[0], verdicts[1]
    assert best.profile.name == "cms-qic113" and best.failures == []
    # The spec-level layout reads the table the same way, one hint less
    # specific: a sibling, not a competitor, so no low-confidence.
    assert runner.profile.name == "qic113-rev-g" and best.score > runner.score
    assert best.warnings == set()
    assert {v.profile.name for v in verdicts if v.eliminated} == {
        "qic80-rev-n",
        "mtn",
    } | _reader_profiles()


def test_guess_picks_cms_for_unk_co_without_the_vendor_bit():
    """The 2026-11-07 regression: flags 0x24, vendor bit clear, yet CMS wrote it."""
    vol, records = _unk_co()
    verdicts = vp.guess(records, vol, now=NOW)
    best = verdicts[0]
    assert best.profile.name == "cms-qic113" and best.failures == [] and best.warnings == set()
    entry = best.entries[0]
    assert (entry.dir_section_size, entry.data_section_size) == (25694, 386322749)
    assert (entry.compressed, entry.compression_code, entry.os_type) == (True, 1, 7)
    assert (entry.start_seg, entry.end_seg) == (3, 7081)
    # Rev N survives (its only mark is the clear vendor bit) but ranks below
    # both QIC-113 layouts; the vendor-specific and MTN profiles are out.
    names = [v.profile.name for v in verdicts]
    assert names.index("qic80-rev-n") > names.index("qic113-rev-g")
    assert {v.profile.name for v in verdicts if v.eliminated} == {
        "vendor-unknown",
        "mtn",
    } | _reader_profiles()


def test_guess_picks_mtn_for_the_3m_tape():
    vol, records = _3m()
    verdicts = vp.guess(records, vol, now=NOW)
    best = verdicts[0]
    assert best.profile.name == "mtn" and best.failures == [] and best.warnings == set()
    entry = best.entries[0]
    assert entry.source_label == "DISK1_VOL1"
    assert (entry.dir_section_size, entry.data_section_size) == (57344, 172068357)
    assert (entry.compressed, entry.compression_code) == (True, 1)
    assert (entry.start_seg, entry.end_seg) == (4, 3461)
    assert {v.profile.name for v in verdicts if v.eliminated} == {
        "cms-qic113",
        "qic113-rev-g",
        "vendor-unknown",
    } | _reader_profiles()


def test_rev_n_reads_garbage_from_the_3m_tape():
    """The reading identify printed before volume profiles: the checks must reject it."""
    vol, records = _3m()
    verdict = vp.evaluate(records, vol, vp.load("qic80-rev-n"), now=NOW)
    failed = {c.name for c in verdict.failures}
    assert {"label is text", "size fits"} <= failed
    assert not verdict.eliminated  # its only mark, the clear vendor bit, holds


# ---------------------------------------------------------------------------
# The fingerprint gate and the selection warnings
# ---------------------------------------------------------------------------


def test_fingerprint_is_a_gate_not_a_hint():
    """A failed [match] item eliminates the profile, whatever the rest scores."""
    vol, records = _jc()
    verdict = vp.evaluate(records, vol, vp.load("mtn"), now=NOW)
    assert verdict.eliminated and [c.ok for c in verdict.gate] == [False]
    assert vp.match_checks(records[0], vol, vp.load("mtn"))[0].points == -vp.HINT_WEIGHT
    kept = vp.evaluate(records, vol, vp.load("cms-qic113"), now=NOW)
    assert not kept.eliminated and [c.points for c in kept.gate] == [2, 2]
    assert vp.rank([verdict, kept])[0] is kept and vp.survivors([verdict, kept]) == [kept]


def test_match_hints_are_worth_double():
    vol, records = _3m()
    checks = vp.match_checks(records[0], vol, vp.load("mtn"))
    assert [c.points for c in checks] == [2]


def test_status_and_match_decide_the_static_warnings():
    assert vp.load("cms-qic113").static_warnings == frozenset()  # bench, two tapes, a real mark
    assert vp.load("mtn").static_warnings == frozenset()
    assert vp.load("qic113-rev-g").static_warnings == frozenset()
    assert vp.load("qic80-rev-n").static_warnings == {VolumeWarning.GENERIC}
    assert vp.load("vendor-unknown").static_warnings == {VolumeWarning.GENERIC}
    one_tape = replace(vp.load("mtn"), tapes=("3m-unknown-1",))
    assert one_tape.static_warnings == {VolumeWarning.SINGLE_TAPE}
    untested = replace(
        vp.load("mtn"), status="untested", warnings=frozenset({VolumeWarning.SPECIAL_FS})
    )
    assert untested.static_warnings == {VolumeWarning.UNTESTED, VolumeWarning.SPECIAL_FS}


def test_generic_winner_carries_its_warning():
    """Builders' default header is a Rev N tape: rev-n wins on a plain entry, and says it is a default."""
    vol, _ = header_mod.parse_header_data(build_format_parameter_record())
    rec = build_vtbl_entry(start_seg=3, end_seg=40, description="C:", dir_section_size=4096)
    best = vp.guess([rec], vol, now=NOW)[0]
    assert best.profile.name == "qic80-rev-n" and VolumeWarning.GENERIC in best.warnings


def test_identical_and_close_readings_warn():
    vol, records = _jc()
    rev_g = vp.load("qic113-rev-g")
    twin = replace(rev_g, name="twin")
    [best, runner] = vp.guess(records, vol, [rev_g, twin], now=NOW)
    assert best.score == runner.score and VolumeWarning.VTBL_AMBIGUOUS in best.warnings
    # A profile reading the cartridge sequence from byte 122 (0xbf on jc)
    # decodes differently and loses exactly one check: close, so low-confidence.
    rival = replace(
        rev_g, name="rival", fields={**rev_g.fields, "multi_cartridge_seq": vp.FieldSpec(122, 1)}
    )
    [best, runner] = vp.guess(records, vol, [rev_g, rival], now=NOW)
    assert best.profile.name == "qic113-rev-g" and runner.entries != best.entries
    assert best.score - runner.score == 2 and VolumeWarning.LOW_CONFIDENCE in best.warnings


def test_volume_gate_checks_bytes_at_the_start_of_the_volume():
    profile = replace(vp.load("qic113-rev-g"), volume_bytes=((4, b"FSET"),))
    assert [c.ok for c in vp.volume_gate(profile, b"\x01\x00\x00\x00FSET....")] == [True]
    assert [c.ok for c in vp.volume_gate(profile, b"VTBL")] == [False]
    assert vp.volume_gate(vp.load("qic113-rev-g"), b"") == []


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rec",
    [
        JC_VTBL,
        build_vtbl_entry(start_seg=3, end_seg=40, description="C:", dir_section_size=4096),
        build_vtbl_entry(start_seg=3, end_seg=40, compressed=True, os_type=1),
    ],
)
def test_profiles_agree_with_the_builtin_parser(rec):
    """volume._parse_vtbl_entry is the reference parser; keep the profiles in step with it."""
    builtin = table_mod._parse_vtbl_entry(rec)
    names = ("cms-qic113", "qic113-rev-g") if builtin.vendor_specific else ("qic80-rev-n",)
    for name in names:
        assert vp.decode_entry(rec, vp.load(name)) == builtin


def test_vendor_unknown_reads_nothing_past_byte_56():
    entry = vp.decode_entry(JC_VTBL, vp.load("vendor-unknown"))
    assert entry.source_label is None and entry.data_section_size is None
    assert entry.start_seg == 3  # bytes 0-56 are always decoded


def test_date_before_first_format_fails():
    vol, _ = header_mod.parse_header_data(bench_3m.header_data())  # formatted 1994
    old = bytearray(bench_3m.VTBL)
    old[52:56] = (0).to_bytes(4, "little")  # undefined date: no date check at all
    assert all(
        c.name != "date" for c in vp.entry_checks(table_mod.parse_vtbl_base(bytes(old)), vol, NOW)
    )
    early = bytearray(bench_3m.VTBL)
    early[52:56] = ((1990 - 1970) << 25).to_bytes(4, "little")
    checks = vp.entry_checks(table_mod.parse_vtbl_base(bytes(early)), vol, NOW)
    assert [c.ok for c in checks if c.name == "date"] == [False]


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def test_load_from_path(tmp_path):
    path = tmp_path / "mine.toml"
    path.write_text('name = "mine"\n[vtbl]\nsource_label = [64, 16]\n')
    profile = vp.load(str(path))
    assert profile.name == "mine" and profile.fields["source_label"] == vp.FieldSpec(64, 16)
    # Defaults: unexercised, no parser, no positive mark.
    assert profile.status == "untested" and profile.fileset_parser == fs.NONE
    assert profile.static_warnings == {VolumeWarning.UNTESTED, VolumeWarning.GENERIC}


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("[vtbl]\n", "missing 'name'"),
        ('name = "x"\n[vtbl]\nlabel = [102, 16]\n', "unknown [vtbl] field"),
        ('name = "x"\n[vtbl]\nsource_label = [40, 16]\n', "outside bytes 57-127"),
        ('name = "x"\n[vtbl]\nsource_label = [120, 16]\n', "outside bytes 57-127"),
        ("name = ", "mine.toml"),
        ('name = "x"\n[extent]\noffset_bytes = 6\n', "must be 4 or 8"),
        ('name = "x"\n[extent]\nframe_size_bytes = 3\n', "must be 2 or 4"),
        ('name = "x"\n[extent]\nwidth = 4\n', "unknown [extent] key"),
        ('name = "x"\nstatus = "guess"\n', "status = 'guess'"),
        ('name = "x"\nwarnings = ["spooky"]\n', "unknown warning 'spooky'"),
        ('name = "x"\n[match]\nmagic = 1\n', "unknown [match] key"),
        ('name = "x"\n[match]\nvtbl_bytes = [[126, "0000ff"]]\n', "out of range"),
        ('name = "x"\n[fileset]\nparser = "zip"\n', "no file-set parser named 'zip'"),
        ('name = "x"\n[fileset]\nbogus = 1\n', "needs a parser key"),
        (
            'name = "x"\n[fileset]\nparser = "qic113-basic"\nbogus = 1\n',
            "not parameters of the qic113-basic parser",
        ),
    ],
)
def test_malformed_profiles(tmp_path, body, message):
    path = tmp_path / "mine.toml"
    path.write_text(body)
    with pytest.raises(vp.VolumeProfileError, match=message.replace("[", r"\[")):
        vp.load(str(path))


def test_unknown_name_lists_builtins():
    with pytest.raises(vp.VolumeProfileError, match="mtn"):
        vp.load("no-such-profile")


def test_extent_widths_default_to_rev_g_and_mtn_overrides_the_offset():
    assert vp.load("qic80-rev-n").extent_offset_bytes == 8
    assert vp.load("qic80-rev-n").extent_frame_size_bytes == 2
    assert vp.load("mtn").extent_offset_bytes == 4


def test_fileset_section_names_the_parser():
    assert vp.load("cms-qic113").fileset_parser == "qic113-extended"
    assert vp.load("qic113-rev-g").fileset_parser == "qic113-extended"
    assert vp.load("mtn").fileset_parser == "qic113-basic"
    assert vp.load("vendor-unknown").fileset_parser == "qic113-basic"


def test_volume_only_fingerprint_is_deferred_at_the_table_stage():
    """qicstream-v2 has no table-stage gate: it must neither win nor warn against the real winner.

    Before rank() seated deferred profiles last (2026-11-07), its Rev N
    reading outscored vendor-unknown, which reads nothing past byte 56, on
    every tape with the vendor bit set.
    """
    vol, _ = header_mod.parse_header_data(build_format_parameter_record())
    assert vp.load("qicstream-v2").deferred
    deferred = {n for n in vp.builtin_names() if vp.load(n).deferred}
    assert "qicstream-v2" in deferred
    assert not deferred & {"qic80-rev-n", "vendor-unknown", "cms-qic113", "qic113-rev-g", "mtn"}
    for flags, winner in ((0x00, "qic80-rev-n"), (0x01, "vendor-unknown")):
        rec = build_vtbl_entry(
            start_seg=3, end_seg=40, description="C:", flags=flags, dir_section_size=4096
        )
        ranked = vp.guess([rec], vol, now=NOW)
        alive = vp.survivors(ranked)
        assert alive[0].profile.name == winner and alive[0].warnings == {VolumeWarning.GENERIC}
        assert {v.profile.name for v in alive[1:]} == deferred
        assert [v.profile.name for v in vp.contenders(ranked)] == [winner]


# ---------------------------------------------------------------------------
# Profiles written from QICStreamReader, not from a tape (status "untested")
# ---------------------------------------------------------------------------


def _reader_profiles() -> set[str]:
    """The untested built-ins that gate on VTBL bytes.

    Each was written from one of Dmitry Brant's readers, never from a tape of
    ours, so on every record we have -- the three bench tapes and the
    builders' synthetic ones -- its fingerprint must fail and the table stage
    must eliminate it. The bench selection tests above add this set to the
    profiles they expect eliminated, so a new reader profile cannot change a
    bench verdict without a test saying so.
    """
    return {p.name for p in vp.load_builtin() if p.status == "untested" and p.vtbl_bytes}


def test_untested_profiles_earn_their_place():
    """An untested profile carries a positive fingerprint or a listable parser (DESIGN.md §7.8).

    A fingerprint-less profile whose parser is ``none`` would survive the gate,
    fail the probe and be pure noise in ``identify --raw``; one with no
    positive mark would also win by default and carry ``generic`` on top of
    ``untested``, which is two ways of saying "we guessed".
    """
    for profile in vp.load_builtin():
        if profile.status != "untested":
            continue
        assert profile.positive_match or profile.fileset_parser != fs.NONE, profile.name
        assert VolumeWarning.UNTESTED in profile.static_warnings, profile.name
        assert VolumeWarning.GENERIC not in profile.static_warnings, profile.name


def test_reader_fingerprints_stay_quiet_on_every_known_record():
    """No bench or synthetic record carries a reader profile's mark, so none of them fires."""
    plain = header_mod.parse_header_data(build_format_parameter_record())[0]
    cases = [("jc", *_jc()), ("3m", *_3m()), ("unk-co", *_unk_co())]
    cases += [
        (
            "rev-n",
            plain,
            [build_vtbl_entry(start_seg=3, end_seg=40, description="C:", dir_section_size=4096)],
        ),
        (
            "vendor",
            plain,
            [build_vtbl_entry(start_seg=3, end_seg=40, description="C:", flags=0x01)],
        ),
    ]
    reader = _reader_profiles()
    for profile in (p for p in vp.load_builtin() if p.name in reader):
        for name, vol, records in cases:
            checks = vp.match_checks(records[0], vol, profile)
            assert any(not c.ok for c in checks), f"{profile.name} fires on {name}"


def test_novastor_is_a_volume_only_profile_with_wide_extents():
    """novastor (from QICStreamReader): the mark at 0x74 of record 0, 8/4 extent widths, no parser."""
    profile = vp.load("novastor")
    assert profile.deferred and profile.status == "untested"
    assert (profile.extent_offset_bytes, profile.extent_frame_size_bytes) == (8, 4)
    assert profile.fileset_parser == fs.NONE and not profile.fields
    head = bytearray(0x80)
    head[0x74 : 0x74 + 12] = b"<<NoVaStOr>>"
    assert [c.ok for c in vp.volume_gate(profile, bytes(head))] == [True]
    assert [c.ok for c in vp.volume_gate(profile, b"\x01\x00\x00\x00FSET" + bytes(0x80))] == [False]


# ---------------------------------------------------------------------------
# Hand-tuned overrides
# ---------------------------------------------------------------------------


def test_override_lays_on_the_profile_and_marks_the_read_manual():
    """Each layer moves on its own; the fingerprint and status stay the profile's; the name says so."""
    base = vp.load("cms-qic113")
    ov = vp.Overrides.from_strings(
        fileset_parser="qic113-basic",
        extent_offset_bytes=4,
        extent_frame_size_bytes=4,
        vtbl_fields=["source_label=102,16"],
    )
    tuned = vp.override(base, ov)
    assert tuned.name == "cms-qic113+overrides" and base.name == "cms-qic113"
    assert tuned.fileset_parser == "qic113-basic" and tuned.fileset_params == {}
    assert (tuned.extent_offset_bytes, tuned.extent_frame_size_bytes) == (4, 4)
    assert tuned.fields["source_label"] == vp.FieldSpec(102, 16)
    assert tuned.fields["data_section_size"] == base.fields["data_section_size"]
    assert (tuned.vtbl_bytes, tuned.status, tuned.tapes) == (
        base.vtbl_bytes,
        base.status,
        base.tapes,
    )
    assert VolumeWarning.MANUAL in tuned.static_warnings
    assert VolumeWarning.MANUAL not in base.static_warnings
    assert ov.describe() == (
        "parser qic113-basic; extent offset_bytes 4; extent frame_size_bytes 4; "
        "vtbl source_label = [102, 16]"
    )


def test_override_with_nothing_is_the_profile_itself():
    base = vp.load("mtn")
    assert vp.override(base, None) is base
    assert vp.override(base, vp.Overrides()) is base
    assert not vp.Overrides.from_strings() and vp.Overrides().describe() == "nothing"


def test_overridden_profile_reads_the_table_with_the_moved_field():
    """The jc tape under cms-qic113 with the label four bytes left: the gate holds, the field moves."""
    vol, records = _jc()
    tuned = vp.override(
        vp.load("cms-qic113"), vp.Overrides(vtbl_fields={"source_label": vp.FieldSpec(102, 16)})
    )
    verdict = vp.evaluate(records, vol, tuned, now=NOW)
    assert not verdict.eliminated and VolumeWarning.MANUAL in verdict.warnings
    moved = JC_VTBL[102:118].split(b"\x00", 1)[0].decode("latin-1").rstrip(" ")
    assert verdict.entries[0].source_label == moved


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"fileset_parser": "zip"}, "no file-set parser named 'zip'"),
        ({"fileset_params": ["bogus"]}, "not KEY=VALUE"),
        ({"vtbl_fields": ["label=102,16"]}, "unknown [vtbl] field 'label'"),
        ({"vtbl_fields": ["source_label=40,16"]}, "outside bytes 57-127"),
        ({"vtbl_fields": ["source_label=120,16"]}, "outside bytes 57-127"),
        ({"vtbl_fields": ["source_label=102"]}, "not NAME=OFFSET,LENGTH"),
        ({"vtbl_fields": ["source_label=a,b"]}, "not NAME=OFFSET,LENGTH"),
        ({"extent_offset_bytes": 6}, "must be 4 or 8"),
        ({"extent_frame_size_bytes": 3}, "must be 2 or 4"),
    ],
)
def test_malformed_overrides(kwargs, message):
    """The TOML loader's rules, in its words, before any tape is opened."""
    with pytest.raises(vp.VolumeProfileError, match=re.escape(message)):
        vp.Overrides.from_strings(**kwargs)


def test_override_parameters_belong_to_the_parser_that_will_read():
    """Checked against the override's parser when given, else the profile's; a direct record too."""
    cms = vp.load("cms-qic113")
    with pytest.raises(vp.VolumeProfileError, match="not parameters of the qic113-extended parser"):
        vp.override(cms, vp.Overrides.from_strings(fileset_params=["depth=3"]))
    with pytest.raises(vp.VolumeProfileError, match="not parameters of the none parser"):
        vp.override(
            cms, vp.Overrides.from_strings(fileset_parser="none", fileset_params=["depth=3"])
        )
    with pytest.raises(vp.VolumeProfileError, match="outside bytes 57-127"):
        vp.override(cms, vp.Overrides(vtbl_fields={"os_type": vp.FieldSpec(127, 2)}))
    with pytest.raises(vp.VolumeProfileError, match="must be 4 or 8"):
        vp.override(cms, vp.Overrides(extent_offset_bytes=5))


def test_override_parameters_take_the_parsers_types(monkeypatch):
    """A command-line string becomes what the parser's default is: int, bool or text."""
    from qiclib.volume.fileset import base

    class Knobs(fs.FileSetParser):
        name = "knobs"
        params = {"depth": 2, "strict": False, "label": "x"}

    monkeypatch.setitem(base._REGISTRY, "knobs", Knobs())
    ov = vp.Overrides.from_strings(fileset_parser="knobs", fileset_params=["depth=5", "strict=yes"])
    tuned = vp.override(vp.load("vendor-unknown"), ov)
    assert tuned.fileset_params == {"depth": 5, "strict": True, "label": "x"}
    # The profile's own parameters carry while the parser stays; the suffix does not stack.
    again = vp.override(tuned, vp.Overrides.from_strings(fileset_params=["label=hi"]))
    assert again.fileset_params == {"depth": 5, "strict": True, "label": "hi"}
    assert again.name == "vendor-unknown+overrides"
    with pytest.raises(vp.VolumeProfileError, match="not an integer"):
        vp.override(
            vp.load("vendor-unknown"),
            vp.Overrides.from_strings(fileset_parser="knobs", fileset_params=["depth=deep"]),
        )
    with pytest.raises(vp.VolumeProfileError, match="not a boolean"):
        vp.override(
            vp.load("vendor-unknown"),
            vp.Overrides.from_strings(fileset_parser="knobs", fileset_params=["strict=maybe"]),
        )
