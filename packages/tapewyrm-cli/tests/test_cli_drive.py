"""`tw drive` CLI: argument handling over a stubbed drive session."""

from contextlib import contextmanager

import pytest
from click.testing import CliRunner

import tapewyrm.cli as cli_mod
from tapewyrm.cli import app as app_mod
from tapewyrm.cli import drive as drive_mod
from tapewyrm.cli import session as session_mod


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

    monkeypatch.setattr(drive_mod, "_drive_session", fake_session)
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

    monkeypatch.setattr(drive_mod, "_drive_session", fake_session)
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
    assert session_mod._decode_error(word) == text


def test_track_argument_is_six_bit(stub):
    res = CliRunner().invoke(cli_mod.cli, ["drive", "track", "64"])
    assert res.exit_code != 0


# ---------------------------------------------------------------------------
# Profile resolution: --profile > --config > per-user config > auto
# ---------------------------------------------------------------------------


def _user_config(text: str):
    path = app_mod.default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_no_profile_anywhere_means_auto():
    app = app_mod.AppContext.load(None, None, None)
    assert app.profile_name == "auto"
    assert app.profile is None  # picked per session by probing


def test_per_user_config_supplies_the_profile():
    _user_config('profile = "colorado.1400"\n')
    app = app_mod.AppContext.load(None, None, None)
    assert app.profile is not None and app.profile.name == "colorado.1400"


def test_explicit_profile_beats_every_config(tmp_path):
    _user_config('profile = "conner"\n')
    explicit = tmp_path / "explicit.toml"
    explicit.write_text('profile = "iomega"\n')
    app = app_mod.AppContext.load(None, "colorado", str(explicit))
    assert app.profile is not None and app.profile.name == "colorado"


def test_explicit_config_replaces_the_per_user_one(tmp_path):
    _user_config('profile = "conner"\n')
    explicit = tmp_path / "explicit.toml"
    explicit.write_text('port = "/dev/ttyX"\n')  # names no profile
    app = app_mod.AppContext.load(None, None, str(explicit))
    assert app.port == "/dev/ttyX"
    assert app.profile_name == "auto"  # the per-user file was not read


def test_broken_user_config_names_the_file():
    import click

    path = _user_config("profile = \n")
    with pytest.raises(click.ClickException, match=str(path)):
        app_mod.AppContext.load(None, None, None)


def test_relative_xdg_config_home_is_ignored(monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(app_mod.os, "name", "posix")
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/dir")
    monkeypatch.setenv("HOME", "/home/someone")
    assert app_mod.default_config_path() == Path("/home/someone/.config/tapewyrm/config.toml")


# ---------------------------------------------------------------------------
# A whole `tw drive` session over a fake board: auto vs explicit --profile
# ---------------------------------------------------------------------------


class _FakeBoard:
    """DeviceLink stand-in with one Colorado-style phantom drive (unit 0) on it.

    The drive answers reports only after Phantom Select 46 + unit-0 argument
    (2 pulses), like the bench 350/1400; until then a report gets no ACK.
    """

    instances: list["_FakeBoard"] = []

    def __init__(self):
        self.codes: list[int] = []
        self.lines: list[tuple] = []  # select / motor / deselect calls
        self.selected = False
        self._after_46 = False
        _FakeBoard.instances.append(self)

    def open(self, port):
        pass

    def close(self):
        pass

    def deselect(self):
        self.lines.append(("deselect",))

    def select(self, hint):
        self.lines.append(("select", hint.bus, hint.unit, hint.motor))

    def motor(self, unit, on):
        self.lines.append(("motor", unit, on))

    def set_timing(self, timing):
        pass

    def command_txn(self, n, report_bits=0):
        from tapewyrm.link.device import LinkError

        self.codes.append(n)
        if self._after_46:
            self._after_46, self.selected = False, n == 2
            return b""
        if n == 46:
            self._after_46 = True
        elif n in (1, 47):
            self.selected = False
        if not report_bits:
            return b""
        if not self.selected:
            raise LinkError(f"command {n}: no ACK bit -- drive not selected/listening")
        return bytes([{6: 0x25, 9: 0x58}[n]])


@pytest.fixture
def board(monkeypatch):
    import tapewyrm.link.device as device_mod

    _FakeBoard.instances.clear()
    monkeypatch.setattr(device_mod, "DeviceLink", _FakeBoard)
    return _FakeBoard.instances


def test_default_auto_selects_a_colorado_and_logs_the_attempt(board):
    res = CliRunner(env={"COLUMNS": "200"}).invoke(cli_mod.cli, ["drive", "report", "rom version"])
    assert res.exit_code == 0, res.output
    assert "0x58" in res.stdout
    # ftape's order: None first (no answer, 4 status tries), then Colorado.
    assert "auto: trying default: no wake steps" in res.stderr
    assert "auto: trying colorado: phantom select 0, enter primary mode" in res.stderr
    assert "using profile 'colorado'" in res.stderr
    assert "auto: trying mountain" not in res.stderr  # stopped at the answer
    assert board[0].codes == [6, 6, 6, 6, 46, 2, 30, 6, 6, 9]


def test_explicit_colorado_profile_wakes_without_probing(board):
    res = CliRunner(env={"COLUMNS": "200"}).invoke(
        cli_mod.cli, ["--profile", "colorado", "drive", "report", "rom version"]
    )
    assert res.exit_code == 0, res.output
    assert "auto:" not in res.stderr
    assert board[0].codes == [46, 2, 30, 6, 9]


def test_config_file_profile_also_skips_the_probe(board, tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('profile = "colorado"\n')
    res = CliRunner(env={"COLUMNS": "200"}).invoke(
        cli_mod.cli, ["--config", str(config), "drive", "report", "rom version"]
    )
    assert res.exit_code == 0, res.output
    assert "auto:" not in res.stderr
    assert board[0].codes == [46, 2, 30, 6, 9]


def test_explicit_profile_skips_the_probe(board):
    # `default` has no wake steps: no Phantom Select is sent, so this drive
    # stays silent -- proof the explicit profile, not auto, ran the session.
    res = CliRunner(env={"COLUMNS": "200"}).invoke(
        cli_mod.cli, ["--profile", "default", "drive", "report", "rom version"]
    )
    assert res.exit_code != 0
    assert "auto:" not in res.stderr
    assert board[0].codes == [6]  # wake's status read only, unanswered


def test_auto_with_no_drive_fails_with_a_hint(board, monkeypatch):
    monkeypatch.setattr(_FakeBoard, "command_txn", _no_drive_txn)
    res = CliRunner(env={"COLUMNS": "200"}).invoke(cli_mod.cli, ["drive", "report", "9"])
    assert res.exit_code != 0
    assert "tried: default, colorado, mountain, insight" in res.output
    no_answer = [6, 6, 6, 6]
    # ftape's methods in ftape's order; no 47/24 undo between them (ftape sends none).
    assert board[0].codes == [*no_answer, 46, 2, 30, *no_answer, 23, 20, *no_answer, *no_answer]
    # Motor-on: select + motor, then (no answer) motor off + deselect.
    assert board[0].lines[1:4] == [
        ("select", "ibmpc", 0, True),
        ("motor", 0, False),
        ("deselect",),
    ]


def _no_drive_txn(self, n, report_bits=0):
    from tapewyrm.link.device import LinkError

    self.codes.append(n)
    if report_bits:
        raise LinkError(f"command {n}: no ACK bit")
    return b""
