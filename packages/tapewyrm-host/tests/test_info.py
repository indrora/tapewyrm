"""`tw info`: host/firmware build identity, port + USB serial, stock firmware."""

import struct
import sys
import types

import pytest
from click.testing import CliRunner

import tapewyrm.cli as cli_mod
from tapewyrm import buildinfo
from tapewyrm.link.device import DeviceLink, FirmwareBuild
from tapewyrm.link.protocol import Txn
from tapewyrm.link.transport import FakeTransport
from tapewyrm.types import DeviceInfo

SHA = "97a98939dfb4ee51cb4a198d5725887cec7c82c2"

# ---------------------------------------------------------------------------
# host build identity
# ---------------------------------------------------------------------------


def test_host_build_from_this_checkout():
    hb = buildinfo.host_build()
    assert hb.source == "git checkout"
    assert hb.commit is not None and len(hb.commit) == 40


def test_host_build_falls_back_to_wheel_stamp(monkeypatch):
    monkeypatch.setattr(buildinfo, "_git", lambda *a: None)  # not a checkout
    stamp = types.ModuleType("tapewyrm._build_stamp")
    stamp.COMMIT, stamp.DIRTY = SHA, False
    monkeypatch.setitem(sys.modules, "tapewyrm._build_stamp", stamp)
    hb = buildinfo.host_build()
    assert (hb.commit, hb.dirty, hb.source) == (SHA, False, "build stamp")


def test_host_build_unknown_without_git_or_stamp(monkeypatch):
    monkeypatch.setattr(buildinfo, "_git", lambda *a: None)
    monkeypatch.setitem(sys.modules, "tapewyrm._build_stamp", None)  # -> ImportError
    hb = buildinfo.host_build()
    assert (hb.commit, hb.source) == (None, "unknown")


# ---------------------------------------------------------------------------
# link: BUILD_INFO verb + ungated open
# ---------------------------------------------------------------------------


def _gw_info() -> bytes:
    return struct.pack("<4BI4B3H", 1, 6, 1, 22, 72_000_000, 4, 2, 0, 2, 216, 224, 128).ljust(
        32, b"\0"
    )


def _open(info_ack: int = 0) -> tuple[DeviceLink, FakeTransport]:
    t = FakeTransport()
    t.queue_response(0, _gw_info())
    if info_ack:
        t.queue_response(Txn.INFO, b"", ack=info_ack)
    else:
        t.queue_response(Txn.INFO, struct.pack("<BIII", 1, 0b111, 131072, 72_000_000))
    link = DeviceLink(t)
    link.open(gate=False)
    return link, t


def test_build_info_parses_commit_and_dirty():
    link, t = _open()
    t.queue_response(Txn.BUILD_INFO, SHA.encode() + b"\x01")
    assert link.build_info() == FirmwareBuild(commit=SHA, dirty=True)


def test_build_info_unknown_commit_is_none():
    link, t = _open()
    t.queue_response(Txn.BUILD_INFO, b"\x00" * 40 + b"\x00")
    assert link.build_info() == FirmwareBuild(commit=None, dirty=False)


def test_build_info_on_older_image_is_none():
    link, t = _open()
    t.queue_response(Txn.BUILD_INFO, b"", ack=1)  # BAD_COMMAND
    assert link.build_info() is None


def test_ungated_open_describes_stock_firmware():
    link, _t = _open(info_ack=1)  # stock GW: BAD_COMMAND to our INFO
    assert link.info is not None
    assert link.info.proto_ver == 0 and link.info.qic_caps == frozenset()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _dev(proto_ver: int = 1) -> DeviceInfo:
    return DeviceInfo(
        model="tapewyrm-GW V4.1",
        mcu="216 MHz, 224 kB SRAM",
        firmware="1.6",
        serial="GW053C112228470000077CA413",
        usb_high_speed=False,
        sram_bytes=131072,
        qic_caps=frozenset({"verbs", "capture", "markers"}) if proto_ver else frozenset(),
        proto_ver=proto_ver,
        port="/dev/cu.usbmodem121401",
    )


@pytest.fixture
def fake_hw(monkeypatch):
    state = {"dev": _dev(), "fw": FirmwareBuild(commit=SHA, dirty=False)}

    class _Link:
        def open(self, port=None, *, gate=True):
            assert gate is False  # tw info must not refuse stock firmware
            return state["dev"]

        def build_info(self):
            return state["fw"]

        def close(self):
            pass

    import tapewyrm.link.device as device_mod

    monkeypatch.setattr(device_mod, "DeviceLink", _Link)
    monkeypatch.setattr(
        buildinfo, "host_build", lambda: buildinfo.HostBuild("0.1.0", SHA, False, "git checkout")
    )
    return state


def test_info_shows_everything_asked_for(fake_hw):
    res = CliRunner().invoke(cli_mod.cli, ["info"])
    assert res.exit_code == 0, res.output
    out = res.output
    assert "tw        : 0.1.0, commit 97a98939dfb4 [git checkout]" in out
    assert "port      : /dev/cu.usbmodem121401 (USB serial GW053C112228470000077CA413)" in out
    assert "firmware  : 1.6, commit 97a98939dfb4, protocol v1" in out
    assert "different commits" not in out


def test_info_flags_mismatched_commits(fake_hw):
    fake_hw["fw"] = FirmwareBuild(commit="1aeb368" + "0" * 33, dirty=True)
    res = CliRunner().invoke(cli_mod.cli, ["info"])
    assert "(+uncommitted changes)" in res.output
    assert "built from different commits" in res.output


def test_info_describes_stock_firmware(fake_hw):
    fake_hw["dev"] = _dev(proto_ver=0)
    res = CliRunner().invoke(cli_mod.cli, ["info"])
    assert res.exit_code == 0, res.output
    assert "stock Greaseweazle (no Tapewyrm verbs)" in res.output
