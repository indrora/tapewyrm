"""Firmware updating: the .upd format, the `tw update` client, `tw dfu`.

`tw` owns flashing; it must never require the `gw` tool (DESIGN.md §1, §12.3).
The update-client tests script each USB connection with a FakeTransport: the
board re-enumerates on every SWITCH_FW_MODE, so a whole update is three
connections (main firmware -> bootloader -> new main firmware).
"""

from __future__ import annotations

import struct
import types

import crcmod.predefined
import pytest
from click.testing import CliRunner

from tapewyrm.cli import cli
from tapewyrm.link import update
from tapewyrm.link.transport import FakeTransport
from tapewyrm.link.update import (
    SIG_BOOT,
    SIG_MAIN,
    FlashError,
    app_update,
    dfu_argv,
    load_image,
    make_entry,
    parse_upd,
    run_dfu,
)

AT32F4 = 4
APP_START, BOOT_START = 0x0800_4000, 0x0800_0000
ACK_BAD_COMMAND = 1


def fw_image(start: int, size: int = 1024) -> bytes:
    """A plausible Cortex-M image: SP in SRAM, Thumb reset vector inside it."""
    body = struct.pack("<II", 0x2001_8000, start + 0x101)
    return (
        body
        + bytes(range(256)) * ((size - len(body)) // 256)
        + b"\x00" * ((size - len(body)) % 256)
    )


def mk_update_entry(dat: bytes, hw_model: int, major: int, minor: int, sig: bytes) -> bytes:
    """firmware/scripts/mk_update.py ``mk_cat_entry``, verbatim: the oracle."""
    header = struct.pack("<2H", len(dat) + 8, hw_model)
    footer = struct.pack("<2s2BH", sig, major, minor, hw_model)
    crc16 = crcmod.predefined.Crc("crc-ccitt-false")
    crc16.update(dat)
    crc16.update(footer)
    footer += struct.pack(">H", crc16.crcValue)
    return header + dat + footer


def mk_update_file(*entries: bytes) -> bytes:
    """mk_update.py ``new``/``cat`` + its trailing whole-file CRC32."""
    dat = b"GWUP" + b"".join(entries)
    crc32 = crcmod.predefined.Crc("crc-32-mpeg")
    crc32.update(dat)
    return dat + struct.pack(">I", crc32.crcValue)


def gw_info(major: int, minor: int, *, main: bool, strapped: bool = False) -> bytes:
    """A 32-byte GET_INFO payload (struct gw_info, cdc_acm_protocol.h)."""
    sample = (1 if strapped else 0) if not main else 72_000_000
    raw = struct.pack("<4BI4B3H", major, minor, int(main), 22, sample, AT32F4, 2, 1, 0, 240, 96, 1)
    return raw.ljust(32, b"\x00")


@pytest.fixture
def upd_file(tmp_path):
    path = tmp_path / "tapewyrm.upd"
    path.write_bytes(
        mk_update_file(
            mk_update_entry(fw_image(BOOT_START), AT32F4, 1, 2, b"BL"),
            mk_update_entry(fw_image(APP_START), AT32F4, 1, 2, b"GW"),
        )
    )
    return path


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Re-enumeration waits are real seconds on hardware; skip them here."""
    monkeypatch.setattr(update.time, "sleep", lambda _s: None)


def connector(*transports: FakeTransport):
    """A Connector that hands out the scripted connections in order."""
    queue = list(transports)

    def connect(_serial: str) -> FakeTransport:
        t = queue.pop(0)
        t.open()
        return t

    return connect


# --- .upd format -----------------------------------------------------------


def test_parse_upd_reads_a_file_built_like_mk_update(upd_file):
    entries = parse_upd(upd_file.read_bytes())
    assert [(e.sig, e.hw_model, e.version) for e in entries] == [
        (SIG_BOOT, AT32F4, "1.2"),
        (SIG_MAIN, AT32F4, "1.2"),
    ]
    assert entries[1].payload == fw_image(APP_START)
    assert len(entries[1].wire) == len(fw_image(APP_START)) + 8  # payload + footer


def test_make_entry_matches_mk_update_byte_for_byte():
    dat = fw_image(APP_START)
    assert (
        make_entry(dat, AT32F4, SIG_MAIN, 1, 2).wire
        == mk_update_entry(dat, AT32F4, 1, 2, b"GW")[4:]
    )


def test_make_entry_pads_to_a_longword_with_erased_flash():
    entry = make_entry(fw_image(APP_START, 1022), AT32F4, SIG_MAIN)
    assert entry.payload.endswith(b"\xff\xff") and len(entry.payload) == 1024


def test_parse_upd_rejects_a_corrupt_file(upd_file):
    blob = bytearray(upd_file.read_bytes())
    blob[100] ^= 0xFF
    with pytest.raises(FlashError, match="CRC32"):
        parse_upd(bytes(blob))
    with pytest.raises(FlashError, match="GWUP"):
        parse_upd(b"NOPE" + bytes(blob[4:]))


def test_load_image_picks_the_entry_for_the_board(upd_file):
    assert load_image(upd_file, AT32F4, SIG_BOOT).payload == fw_image(BOOT_START)
    with pytest.raises(FlashError, match="no main firmware for STM32F7"):
        load_image(upd_file, 7, SIG_MAIN)


def test_load_image_wraps_a_raw_app_bin_as_v0_0(tmp_path):
    path = tmp_path / "firmware.bin"
    path.write_bytes(fw_image(APP_START))
    entry = load_image(path, AT32F4, SIG_MAIN)
    assert (entry.sig, entry.version, entry.payload) == (SIG_MAIN, "0.0", fw_image(APP_START))


def test_load_image_refuses_a_bootloader_bin_in_the_app_slot(tmp_path):
    path = tmp_path / "bootloader.bin"
    path.write_bytes(fw_image(BOOT_START))
    with pytest.raises(FlashError, match="reset vector"):
        load_image(path, AT32F4, SIG_MAIN)


def test_load_image_refuses_hex_and_raw_bootloaders(tmp_path):
    hexfile = tmp_path / "tapewyrm.hex"
    hexfile.write_bytes(b":020000040800F2\n")
    with pytest.raises(FlashError, match="Intel HEX"):
        load_image(hexfile, AT32F4, SIG_MAIN)
    binfile = tmp_path / "bootloader.bin"
    binfile.write_bytes(fw_image(BOOT_START))
    with pytest.raises(FlashError, match="needs a .upd"):
        load_image(binfile, AT32F4, SIG_BOOT)


# --- the update client ----------------------------------------------------


def test_update_from_main_firmware_round_trips_through_the_bootloader(upd_file):
    main, boot, back = FakeTransport(), FakeTransport(), FakeTransport()
    main.queue_response(0, gw_info(1, 0, main=True))
    boot.queue_response(0, gw_info(1, 0, main=False))
    boot.queue_response(1, b"\x00")  # CMD_UPDATE ack, then status byte 0 = CRC OK
    back.queue_response(0, gw_info(1, 2, main=True))

    result = app_update(upd_file, connect=connector(main, boot, back))

    entry = load_image(upd_file, AT32F4, SIG_MAIN)
    assert main.sent_frames == [(0, b"\x00"), (11, b"\x00")]  # GET_INFO, into bootloader
    assert boot.sent_frames == [
        (0, b"\x00"),
        (1, struct.pack("<I", len(entry.wire))),
        (11, b"\x01"),
    ]
    assert bytes(boot.sent_raw) == entry.wire  # payload AND footer, nothing more
    assert result.before.version == "1.0" and result.after is not None
    assert result.after.version == "1.2" and result.after.is_main_firmware
    assert not (main.is_open or boot.is_open or back.is_open)


def test_update_when_strapped_stays_in_the_bootloader(upd_file):
    boot = FakeTransport()
    boot.queue_response(0, gw_info(1, 0, main=False, strapped=True))
    boot.queue_response(1, b"\x00")

    result = app_update(upd_file, connect=connector(boot))

    assert [op for op, _ in boot.sent_frames] == [0, 1]  # no SWITCH_FW_MODE either way
    assert result.after is None


def test_update_reports_a_crc_failure(upd_file):
    boot = FakeTransport()
    boot.queue_response(0, gw_info(1, 0, main=False))
    boot.queue_response(1, b"\x01")
    with pytest.raises(FlashError, match="CRC check"):
        app_update(upd_file, connect=connector(boot))


def test_update_does_not_stream_when_the_bootloader_refuses(upd_file):
    boot = FakeTransport()
    boot.queue_response(0, gw_info(1, 0, main=False))
    boot.queue_response(1, ack=ACK_BAD_COMMAND)
    with pytest.raises(FlashError, match="BAD_COMMAND"):
        app_update(upd_file, connect=connector(boot))
    assert boot.sent_raw == b""


def test_update_refuses_a_board_with_no_bootloader(upd_file):
    main, again = FakeTransport(), FakeTransport()
    main.queue_response(0, gw_info(1, 0, main=True))
    again.queue_response(0, gw_info(1, 0, main=True))
    with pytest.raises(FlashError, match="bootloader installed"):
        app_update(upd_file, connect=connector(main, again))
    assert again.sent_raw == b""


def test_bootloader_update_runs_from_main_firmware_with_its_signature(upd_file):
    main = FakeTransport()
    main.queue_response(0, gw_info(1, 2, main=True))
    main.queue_response(1, b"\x00")
    main.queue_response(0, gw_info(1, 2, main=True))

    result = app_update(upd_file, bootloader=True, connect=connector(main))

    entry = load_image(upd_file, AT32F4, SIG_BOOT)
    assert main.sent_frames[1] == (1, struct.pack("<II", len(entry.wire), 0xDEAFBEE3))
    assert bytes(main.sent_raw) == entry.wire
    assert result.entry.sig == SIG_BOOT


def test_bootloader_update_refused_from_the_bootloader(upd_file):
    boot = FakeTransport()
    boot.queue_response(0, gw_info(1, 0, main=False))
    with pytest.raises(FlashError, match="only be rewritten from the main firmware"):
        app_update(upd_file, bootloader=True, connect=connector(boot))


# --- DFU ----------------------------------------------------------------


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


# --- CLI -----------------------------------------------------------------


def test_cli_version():
    res = CliRunner().invoke(cli, ["--version"])
    assert res.exit_code == 0
    assert "tw, version" in res.output


def test_cli_update_and_dfu_help():
    runner = CliRunner()
    for cmd in ("update", "dfu"):
        res = runner.invoke(cli, [cmd, "--help"])
        assert res.exit_code == 0, res.output


def test_cli_dfu_reports_missing_tool(tmp_path, monkeypatch):
    img = tmp_path / "fw.bin"
    img.write_bytes(b"\x00")
    monkeypatch.setattr(update.shutil, "which", lambda _: None)
    res = CliRunner().invoke(cli, ["dfu", str(img)])
    assert res.exit_code != 0
    assert "dfu-util" in res.output


def test_cli_update_reports_the_new_version(upd_file, monkeypatch):
    main, boot, back = FakeTransport(), FakeTransport(), FakeTransport()
    main.queue_response(0, gw_info(1, 0, main=True))
    boot.queue_response(0, gw_info(1, 0, main=False))
    boot.queue_response(1, b"\x00")
    back.queue_response(0, gw_info(1, 2, main=True))
    monkeypatch.setattr(update, "serial_connector", lambda _port: connector(main, boot, back))

    res = CliRunner().invoke(cli, ["update", str(upd_file)])

    assert res.exit_code == 0, res.output
    assert "running main firmware v1.2" in res.output


def test_cli_update_turns_flash_errors_into_messages(tmp_path):
    img = tmp_path / "tapewyrm.hex"
    img.write_bytes(b":00000001FF\n")
    res = CliRunner().invoke(cli, ["update", "--dfu", "--bootloader", str(img)])
    assert res.exit_code != 0
    assert "drop --bootloader" in res.output


def test_board_id_ignores_gw_tw_prefix():
    # Regression (2026-11-09): the bootloader enumerates as GW..., the new app as
    # TW...; matching the whole serial lost the board after the reboot.
    uid = "053C112228470000077CA413"
    assert update.board_id("GW" + uid) == update.board_id("TW" + uid) == uid
    assert update.board_id("XY" + uid) == "XY" + uid  # not ours: unchanged
    assert update.board_id("GW1234") == "GW1234"  # wrong length: unchanged
    assert update.board_id("") == ""


def test_serial_connector_finds_board_after_prefix_change(monkeypatch):
    from serial.tools import list_ports

    from tapewyrm.link.device import GW_USB_PID, GW_USB_VID

    uid = "053C112228470000077CA413"
    port = types.SimpleNamespace(
        vid=GW_USB_VID, pid=GW_USB_PID, device="/dev/cu.fake1", serial_number="TW" + uid
    )
    monkeypatch.setattr(list_ports, "comports", lambda: [port])
    opened = []

    class _Transport:
        def __init__(self, dev):
            self.dev = dev

        def open(self):
            opened.append(self.dev)

    monkeypatch.setattr(update, "SerialTransport", _Transport)
    t = update.serial_connector(None)("GW" + uid)  # started from a GW-prefixed app
    assert t.dev == "/dev/cu.fake1" and opened == ["/dev/cu.fake1"]
