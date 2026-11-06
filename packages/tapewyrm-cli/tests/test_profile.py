"""DriveProfile TOML loading (DESIGN.md §6A.3)."""

import pytest

from tapewyrm.qic117.profile import ProfileError, load_profile
from tapewyrm.types import DriveProfile


@pytest.mark.parametrize("name", ["colorado", "iomega", "conner"])
def test_load_each_packaged_profile(name):
    prof = load_profile(name)
    assert isinstance(prof, DriveProfile)
    assert prof.name == name
    assert prof.bit_order in ("lsb", "msb")
    assert prof.report_strategy in ("index_edge", "fixed_settle")
    # Every packaged profile defines a non-empty wake sequence of valid steps.
    assert len(prof.wake_sequence) >= 1
    for cmd, arg, delay_ms in prof.wake_sequence:
        assert isinstance(cmd, str) and cmd
        assert arg is None or isinstance(arg, int)
        assert isinstance(delay_ms, int)
    # Timing nominals are present and sane.
    assert prof.timing.inter_pulse_us > 0
    assert prof.timing.terminate_gap_us > prof.timing.inter_pulse_us


def test_default_fallback():
    prof = load_profile("default")
    assert prof.name == "default"
    assert prof.wake_sequence == ()


def test_missing_profile_raises():
    with pytest.raises(ProfileError):
        load_profile("does_not_exist_xyz")


def test_load_from_explicit_path(tmp_path):
    p = tmp_path / "custom.toml"
    p.write_text(
        """
name = "custom"
bit_order = "msb"

[timing]
inter_pulse_us = 1500
terminate_gap_us = 3100

[[wake]]
cmd = "soft reset"
delay_ms = 500
""",
        encoding="utf-8",
    )
    prof = load_profile(str(p))
    assert prof.name == "custom"
    assert prof.bit_order == "msb"
    assert prof.timing.inter_pulse_us == 1500
    assert prof.wake_sequence == (("soft reset", None, 500),)


def test_profiles_load_into_drive_lookup():
    # Wake command names in every packaged profile must resolve in the table.
    from tapewyrm.qic117.commands import TABLE

    for name in ("colorado", "iomega", "conner"):
        prof = load_profile(name)
        for cmd, _arg, _delay in prof.wake_sequence:
            key = cmd.upper().replace(" ", "_").replace("-", "_")
            assert key in TABLE, f"profile {name}: unknown wake command {cmd!r}"


def test_generic_is_the_rev_j_nominal_base():
    """generic.toml: no wake, 2.0 ms leading-edge period, 10 ms TACK."""
    prof = load_profile("generic")
    assert prof.wakeup == "none" and prof.wake_sequence == ()
    assert prof.timing.pulse_us + prof.timing.inter_pulse_us == 2000
    assert prof.timing.tack_us == 10000


def test_teac_inherits_generic_and_overrides_only_the_cadence():
    """teac.toml keeps generic's TACK/settle and only tightens the pulse train."""
    from tapewyrm.qic117.profile import WAKEUPS

    teac, generic = load_profile("teac"), load_profile("generic")
    assert teac.name == "teac"
    assert teac.timing.inter_pulse_us == 1300 and teac.timing.terminate_gap_us == 3300
    assert teac.timing.tack_us == generic.timing.tack_us
    assert teac.timing.report_settle_us == generic.timing.report_settle_us
    assert teac.wakeup == "colorado" and teac.wake_sequence == WAKEUPS["colorado"]
    assert teac.vendor_ids == (0x01CC,)


def test_wakeup_expands_to_ftapes_method_and_explicit_steps_win(tmp_path):
    """`wakeup` names an ftape method; [[wake]] steps replace it when present."""
    from tapewyrm.qic117.profile import WAKEUPS

    (tmp_path / "m.toml").write_text('name = "m"\nwakeup = "mountain"\n', encoding="utf-8")
    assert load_profile(str(tmp_path / "m.toml")).wake_sequence == WAKEUPS["mountain"]
    (tmp_path / "x.toml").write_text(
        'name = "x"\nwakeup = "mountain"\n[[wake]]\ncmd = "soft reset"\n', encoding="utf-8"
    )
    assert load_profile(str(tmp_path / "x.toml")).wake_sequence == (("soft reset", None, 0),)
    (tmp_path / "bad.toml").write_text('name = "bad"\nwakeup = "warp"\n', encoding="utf-8")
    with pytest.raises(ProfileError):
        load_profile(str(tmp_path / "bad.toml"))


def test_inherits_merges_timing_per_key_and_replaces_lists(tmp_path):
    """Child timing keys override one at a time; wake/vendor_ids are replaced whole."""
    (tmp_path / "base.toml").write_text(
        'name = "base"\nvendor_ids = [1, 2]\n[timing]\ninter_pulse_us = 1700\ntack_us = 4000\n'
        '[[wake]]\ncmd = "soft reset"\n',
        encoding="utf-8",
    )
    (tmp_path / "kid.toml").write_text(
        f'inherits = "{(tmp_path / "base.toml").as_posix()}"\nvendor_ids = [3]\n'
        "[timing]\ninter_pulse_us = 1300\n",
        encoding="utf-8",
    )
    kid = load_profile(str(tmp_path / "kid.toml"))
    assert kid.name == "kid"  # never inherited: falls back to the file stem
    assert kid.timing.inter_pulse_us == 1300 and kid.timing.tack_us == 4000
    assert kid.vendor_ids == (3,)
    assert kid.wake_sequence == (("soft reset", None, 0),)  # inherited list, untouched


def test_inherits_cycle_raises(tmp_path):
    a, b = tmp_path / "a.toml", tmp_path / "b.toml"
    a.write_text(f'inherits = "{b.as_posix()}"\n', encoding="utf-8")
    b.write_text(f'inherits = "{a.as_posix()}"\n', encoding="utf-8")
    with pytest.raises(ProfileError, match="cycle"):
        load_profile(str(a))


def test_profiles_for_vendor_names_teac():
    from tapewyrm.qic117.profile import profiles_for_vendor

    assert profiles_for_vendor(0x01CC) == ["teac"]
    assert profiles_for_vendor(0x7FFF) == []
