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


class _StatusDrive:
    """Answers every report except Vendor ID, like a drive that predates it."""

    last_error = None

    def __init__(self):
        self.status_reads = 0

    def status(self):
        from tapewyrm_archive.qic117 import DriveStatus

        self.status_reads += 1
        return DriveStatus.decode(0x65)

    def report(self, cmd, nbits):
        from tapewyrm.link.device import LinkError

        values = {7: 0x2F29, 8: 0x90, 9: 0x58, 33: 0x12}
        if cmd.code not in values:
            raise LinkError(f"command {cmd.code}: no ACK bit")
        return values[cmd.code]


def test_status_shows_every_report_and_survives_unsupported(monkeypatch):
    drive = _StatusDrive()

    @contextmanager
    def fake_session(app):
        yield drive

    monkeypatch.setattr(cli_mod, "_drive_session", fake_session)
    res = CliRunner().invoke(cli_mod.cli, ["drive", "status"])
    assert res.exit_code == 0, res.output
    out = res.output
    assert "last error   : 0x2f29 -> 41 Drive Wakeup Reset Occurred (from cmd 47" in out
    assert "500 kbps, QIC-80 mode" in out
    assert "version 88" in out
    assert "vendor id    : not supported by this drive (no ACK)" in out
    assert "format QIC80, 205 ft or 425+ ft, 550 Oe tape" in out  # still read after the miss
    assert drive.status_reads == 2  # the status line + the post-miss error clear


@pytest.mark.parametrize(
    "word,text",
    [
        (26 | (1 << 8), "26 Power On Reset Occurred (initialization error)"),
        (23 | (0 << 8), "23 Motion Time-out Error (process error)"),
        (0, "0 no error"),
    ],
)
def test_error_associated_command_0_and_1_are_classes(word, text):
    assert cli_mod._decode_error(word) == text


def test_track_argument_is_six_bit(stub):
    res = CliRunner().invoke(cli_mod.cli, ["drive", "track", "64"])
    assert res.exit_code != 0
