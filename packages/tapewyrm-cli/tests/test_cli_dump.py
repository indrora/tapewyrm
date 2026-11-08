"""`tw dump` CLI: its options reach dump_tracks, over a stubbed drive session."""

from contextlib import contextmanager

import pytest
from click.testing import CliRunner

import tapewyrm.cli as cli_mod
from tapewyrm.cli import dump as dump_cli
from tapewyrm.tape import dump as dump_mod


@pytest.fixture
def calls(monkeypatch):
    seen: list[dict] = []

    @contextmanager
    def fake_session(app):
        yield object()

    def fake_dump_tracks(
        drive, outdir, tracks, *, progress, check, retries, passes, keep_all, no_clobber
    ):
        seen.append(
            {
                "tracks": tracks,
                "check": check,
                "retries": retries,
                "passes": passes,
                "keep_all": keep_all,
                "no_clobber": no_clobber,
            }
        )
        return []

    monkeypatch.setattr(dump_cli, "_drive_session", fake_session)
    monkeypatch.setattr(dump_mod, "dump_tracks", fake_dump_tracks)
    return seen


# What dump_tracks gets when no option says otherwise.
DEFAULTS = {
    "tracks": None, "check": False, "retries": 0, "passes": 1, "keep_all": False,
    "no_clobber": False,
}  # fmt: skip


def test_retries_reach_dump_tracks(calls, tmp_path):
    res = CliRunner().invoke(cli_mod.cli, ["dump", "--retries", "2", str(tmp_path / "o"), "0-1"])
    assert res.exit_code == 0, res.output
    assert calls == [{**DEFAULTS, "tracks": [0, 1], "retries": 2}]


def test_retries_default_to_none(calls, tmp_path):
    res = CliRunner().invoke(cli_mod.cli, ["dump", str(tmp_path / "o")])
    assert res.exit_code == 0, res.output
    assert calls == [DEFAULTS]


def test_no_clobber_reaches_dump_tracks(calls, tmp_path):
    res = CliRunner().invoke(cli_mod.cli, ["dump", "--no-clobber", str(tmp_path / "o")])
    assert res.exit_code == 0, res.output
    assert calls == [{**DEFAULTS, "no_clobber": True}]


def test_negative_retries_are_refused_before_the_drive_is_touched(calls, tmp_path):
    res = CliRunner().invoke(cli_mod.cli, ["dump", "--retries", "-1", str(tmp_path / "o")])
    assert res.exit_code == 2
    assert calls == []


def test_passes_and_keep_all_reach_dump_tracks(calls, tmp_path):
    res = CliRunner().invoke(
        cli_mod.cli, ["dump", "--passes", "3", "--keep-all", str(tmp_path / "o")]
    )
    assert res.exit_code == 0, res.output
    assert calls == [{**DEFAULTS, "passes": 3, "keep_all": True}]
    assert "every pass kept as track-NN.P.twrf" in res.output


def test_zero_passes_are_refused_before_the_drive_is_touched(calls, tmp_path):
    res = CliRunner().invoke(cli_mod.cli, ["dump", "--passes", "0", str(tmp_path / "o")])
    assert res.exit_code == 2
    assert calls == []
