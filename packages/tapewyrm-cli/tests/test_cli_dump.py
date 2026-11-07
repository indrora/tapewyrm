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

    def fake_dump_tracks(drive, outdir, tracks, *, progress, check, retries, no_clobber):
        seen.append(
            {"tracks": tracks, "check": check, "retries": retries, "no_clobber": no_clobber}
        )
        return []

    monkeypatch.setattr(dump_cli, "_drive_session", fake_session)
    monkeypatch.setattr(dump_mod, "dump_tracks", fake_dump_tracks)
    return seen


def test_retries_reach_dump_tracks(calls, tmp_path):
    res = CliRunner().invoke(cli_mod.cli, ["dump", "--retries", "2", str(tmp_path / "o"), "0-1"])
    assert res.exit_code == 0, res.output
    assert calls == [{"tracks": [0, 1], "check": False, "retries": 2, "no_clobber": False}]


def test_retries_default_to_none(calls, tmp_path):
    res = CliRunner().invoke(cli_mod.cli, ["dump", str(tmp_path / "o")])
    assert res.exit_code == 0, res.output
    assert calls == [{"tracks": None, "check": False, "retries": 0, "no_clobber": False}]


def test_no_clobber_reaches_dump_tracks(calls, tmp_path):
    res = CliRunner().invoke(cli_mod.cli, ["dump", "--no-clobber", str(tmp_path / "o")])
    assert res.exit_code == 0, res.output
    assert calls == [{"tracks": None, "check": False, "retries": 0, "no_clobber": True}]


def test_negative_retries_are_refused_before_the_drive_is_touched(calls, tmp_path):
    res = CliRunner().invoke(cli_mod.cli, ["dump", "--retries", "-1", str(tmp_path / "o")])
    assert res.exit_code == 2
    assert calls == []
