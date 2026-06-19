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
