"""Firmware-flashing module + `tw flash` / `tw dfu` CLI commands.

`tw` owns flashing; it must never require the `gw` tool (DESIGN.md §1, §12.3).
"""

from __future__ import annotations

import types

import pytest
from click.testing import CliRunner

from tapewyrm.cli import cli
from tapewyrm.link import update
from tapewyrm.link.update import FlashError, app_update, dfu_argv, run_dfu


def test_dfu_argv_basic(tmp_path):
    img = tmp_path / "fw.bin"
    img.write_bytes(b"\x00")
    argv = dfu_argv(img)
    assert argv[0] == "dfu-util"
    assert "-a" in argv and "0" in argv
    assert argv[-2:] == ["-D", str(img)]
    assert ":leave" in argv  # reset-and-run by default


def test_dfu_argv_with_device_and_alt(tmp_path):
    img = tmp_path / "fw.bin"
    img.write_bytes(b"\x00")
    argv = dfu_argv(img, vid_pid="2e3c:df11", alt=1, reset=False)
    assert "2e3c:df11" in argv
    assert argv[argv.index("-a") + 1] == "1"
    assert ":leave" not in argv


def test_run_dfu_missing_image(tmp_path):
    with pytest.raises(FlashError, match="not found"):
        run_dfu(tmp_path / "does-not-exist.bin")


def test_run_dfu_missing_tool(tmp_path, monkeypatch):
    img = tmp_path / "fw.bin"
    img.write_bytes(b"\x00")
    monkeypatch.setattr(update.shutil, "which", lambda _: None)
    with pytest.raises(FlashError, match="dfu-util"):
        run_dfu(img)


def test_run_dfu_success(tmp_path, monkeypatch):
    img = tmp_path / "fw.bin"
    img.write_bytes(b"\x00")
    monkeypatch.setattr(update.shutil, "which", lambda _: "/usr/bin/dfu-util")
    seen = {}

    def fake_run(argv, check=False):
        seen["argv"] = argv
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(update.subprocess, "run", fake_run)
    assert run_dfu(img) == 0
    assert str(img) in seen["argv"]


def test_app_update_is_bench_gated(tmp_path):
    img = tmp_path / "fw.hex"
    img.write_bytes(b"\x00")
    with pytest.raises(FlashError, match="tw dfu"):
        app_update(img)


def test_cli_version():
    res = CliRunner().invoke(cli, ["--version"])
    assert res.exit_code == 0
    assert "tw, version" in res.output


def test_cli_flash_and_dfu_help():
    runner = CliRunner()
    for cmd in ("flash", "dfu"):
        res = runner.invoke(cli, [cmd, "--help"])
        assert res.exit_code == 0, res.output


def test_cli_dfu_reports_missing_tool(tmp_path, monkeypatch):
    img = tmp_path / "fw.bin"
    img.write_bytes(b"\x00")
    monkeypatch.setattr(update.shutil, "which", lambda _: None)
    res = CliRunner().invoke(cli, ["dfu", str(img)])
    assert res.exit_code != 0
    assert "dfu-util" in res.output


def test_cli_flash_app_path_directs_to_dfu(tmp_path):
    img = tmp_path / "fw.hex"
    img.write_bytes(b"\x00")
    res = CliRunner().invoke(cli, ["flash", str(img)])
    assert res.exit_code != 0
    assert "tw dfu" in res.output  # app-bootloader path is TODO(bench)
