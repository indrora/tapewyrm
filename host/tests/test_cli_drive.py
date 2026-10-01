"""`tw drive` CLI: argument handling over a stubbed drive session."""

from contextlib import contextmanager

import pytest
from click.testing import CliRunner

import tapewyrm.cli as cli_mod


class _StubDrive:
    def __init__(self):
        self.reports: list[tuple[int, int]] = []

    def report(self, cmd, nbits):
        self.reports.append((cmd.code, nbits))
        return 0x58


@pytest.fixture
def stub(monkeypatch):
    drive = _StubDrive()

    @contextmanager
    def fake_session(app):
        yield drive

    monkeypatch.setattr(cli_mod, "_drive_session", fake_session)
    return drive


@pytest.mark.parametrize("name", ["rom version", "report rom version", "ROM-VERSION", "9"])
def test_report_accepts_names_and_codes(stub, name):
    res = CliRunner().invoke(cli_mod.cli, ["drive", "report", name])
    assert res.exit_code == 0, res.output
    assert stub.reports == [(9, 8)]
    assert "0x58" in res.output


def test_report_rejects_non_report_commands(stub):
    res = CliRunner().invoke(cli_mod.cli, ["drive", "report", "38"])
    assert res.exit_code != 0
    assert "not a report command" in res.output
    assert stub.reports == []
