"""Firmware flashing — owned by ``tw``, not delegated to the ``gw`` tool.

Tapewyrm ships a single self-sufficient executable (``tw``). Flashing is part of
that: we do NOT shell out to Greaseweazle's ``gw update`` (DESIGN.md §1, §12.3).
Two paths, mirroring the design's flashing tiers:

- **App bootloader (routine, over USB) — ``tw update``:** the GW-compatible
  application-bootloader update protocol, spoken by ``tw`` itself. The board
  stays a dual citizen (stock ``gw update`` drives the same bytes, §12.5), but
  ``tw`` carries its own client so it needs no ``gw`` install.
- **DFU (recovery / first-flash) — ``tw dfu``:** drive the AT32F403 built-in ROM
  bootloader via the standard, vendor-neutral ``dfu-util`` (or Artery's
  ISP/AT-Link if the AT32 ROM-DFU descriptor doesn't enumerate cleanly under
  stock dfu-util — §12.3 open item). ``dfu-util`` is a generic third-party
  flasher, not ``gw`` — using it does not make ``tw`` depend on GW tooling.

The update protocol (source of truth: the firmware in this tree)
-----------------------------------------------------------------

Flash layout on the AT32F4 (firmware/platformio.ini): the 16 KB application
bootloader at ``0x08000000`` (firmware/src/fw_update.c) and the 48 KB main
firmware at ``0x08004000``. The bootloader jumps straight into the main
firmware on reset unless one of two things is true (fw_update.c
``enter_bootloader``): the main firmware poked ``0xdeadbeef`` into
``_reset_flag`` before resetting (that is what ``CMD_SWITCH_FW_MODE(0)`` does,
firmware/src/floppy.c), or the update strap is fitted.

Both firmwares speak GW's command framing (link/transport.py) and share three
commands (firmware/inc/cdc_acm_protocol.h):

==========================  ========================================================
``CMD_GET_INFO`` (0)        ``{0, 3, 0}`` -> ack + 32-byte ``struct gw_info``.
                            ``is_main_firmware`` (byte 2) says which one answered;
                            in the bootloader ``sample_freq`` bit 0 = "strapped".
``CMD_SWITCH_FW_MODE`` (11) ``{11, 3, mode}``. **No response**: the board resets.
                            mode 0 = into the bootloader (from the main firmware),
                            mode 1 = into the main firmware (from the bootloader).
``CMD_UPDATE`` (1)          bootloader: ``{1, 6, len:u32}`` -> rewrite the main
                            firmware. main firmware: ``{1, 10, len:u32,
                            0xdeafbee3:u32}`` -> rewrite the *bootloader*.
                            Either way the host waits for the 2-byte ack, then
                            writes exactly ``len`` raw bytes, then reads ONE
                            status byte (0 = CRC good).
==========================  ========================================================

Two things here are easy to get wrong and both brick-adjacent:

1. **Wait for the ack before streaming the image.** In the bootloader's
   command state any bytes that arrived with the command packet are dropped
   when the command completes (fw_update.c ``end_command`` resets ``u_prod``),
   so an image sent hot on the heels of ``CMD_UPDATE`` loses its first packet
   and fails the CRC — after the old firmware has already been erased.
2. **The bytes on the wire are the payload *plus its 8-byte footer*.** The
   device checks ``crc16_ccitt(region, len, 0xffff) == 0``; that only holds if
   the trailing big-endian CRC16 is included in the region (the classic
   "append the CRC, CRC of the whole is zero" trick). The footer lands in flash
   too, which is harmless.

The ``.upd`` file (firmware/scripts/mk_update.py, tools/package.py)
------------------------------------------------------------------

::

    'GWUP'
    repeat:   len:u16le  hw_model:u16le            # catalogue header
              payload[len - 8]                      # raw binary, longword padded
              sig:2s ('GW' main | 'BL' boot)  major:u8  minor:u8  hw_model:u16le
              crc16:u16be                           # CRC16-CCITT-FALSE over payload+footer
    crc32:u32be                                     # CRC-32/MPEG-2 over the whole file

``len`` (the catalogue header) counts the payload *and* the footer, i.e.
exactly the bytes that go on the wire. ``tools/package.py`` emits one combined
``.upd`` holding a ``BL`` and a ``GW`` entry per MCU; we pick by ``hw_model``
(what the board reports in ``GET_INFO``) and signature.

A raw application ``.bin`` (what PlatformIO builds, and what ``just update``
points at) is accepted too: we wrap it in a footer ourselves, version 0.0. The
bootloader never reads the footer's version at boot (fw_update.c only checks
that the first vector-table word is not erased), so 0.0 is cosmetic.
"""

from __future__ import annotations

import logging
import shutil
import struct
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import crcmod.predefined
from tapewyrm_archive.progress import NULL_PROGRESS, Progress

from tapewyrm.link.transport import (
    ACK_NAMES,
    ACK_OKAY,
    SerialTransport,
    Transport,
    TransportError,
)

log = logging.getLogger(__name__)

# AT32F403 built-in ROM DFU identity (Artery). Left as the documented default;
# dfu-util can usually auto-detect, so we don't force --device unless asked.
AT32_ROM_DFU_VID_PID = "2e3c:df11"  # TODO(bench): confirm on the v4.1 (§12.3)


class FlashError(Exception):
    """A firmware flash could not be performed or failed."""


# ---------------------------------------------------------------------------
# .upd update files
# ---------------------------------------------------------------------------

UPD_MAGIC = b"GWUP"
SIG_MAIN = b"GW"  # main-firmware entry
SIG_BOOT = b"BL"  # bootloader entry
_SIG_NAMES = {SIG_MAIN: "main firmware", SIG_BOOT: "bootloader"}

# Greaseweazle hw_model ids, as GET_INFO reports them and as mk_update.py
# writes them (name_to_hw_model / hw_model_to_name there).
HW_MODEL_NAMES = {1: "STM32F1", 4: "AT32F4", 7: "STM32F7"}

_CAT_HEADER = struct.Struct("<HH")  # len (payload + footer), hw_model
_FOOTER_HEAD = struct.Struct("<2sBBH")  # sig, major, minor, hw_model
_FOOTER_LEN = _FOOTER_HEAD.size + 2  # + crc16:u16be = 8
_FILE_CRC = struct.Struct(">I")


@dataclass(frozen=True)
class FlashRegion:
    """Where one kind of image lives in the MCU's flash."""

    start: int
    max_bytes: int

    def contains(self, addr: int) -> bool:
        return self.start <= addr < self.start + self.max_bytes


# Flash map per hw_model: bootloader then main firmware. Starts are
# fw_update.c FIRMWARE_START (main) / floppy.c BL_START (boot); maxima are
# mk_update.py ``max_kb`` (the same numbers the linker scripts enforce).
_FLASH_MAP: dict[int, dict[bytes, FlashRegion]] = {
    1: {SIG_BOOT: FlashRegion(0x0800_0000, 8 << 10), SIG_MAIN: FlashRegion(0x0800_2000, 56 << 10)},
    4: {SIG_BOOT: FlashRegion(0x0800_0000, 16 << 10), SIG_MAIN: FlashRegion(0x0800_4000, 48 << 10)},
    7: {SIG_BOOT: FlashRegion(0x0800_0000, 16 << 10), SIG_MAIN: FlashRegion(0x0800_4000, 48 << 10)},
}  # fmt: skip

# Cortex-M SRAM lives at 0x2000_0000; no GW MCU has more than 1 MB of it, so a
# believable initial stack pointer (vector-table word 0) falls in this window.
_SRAM_START, _SRAM_END = 0x2000_0000, 0x2010_0000


def _crc16(data: bytes) -> int:
    """CRC16-CCITT-FALSE (poly 0x1021, seed 0xFFFF): the firmware's crc16_ccitt."""
    crc = crcmod.predefined.Crc("crc-ccitt-false")
    crc.update(data)
    return int(crc.crcValue)


def _crc32_mpeg2(data: bytes) -> int:
    """CRC-32/MPEG-2 (non-reflected; *not* zlib.crc32): mk_update.py's file CRC."""
    crc = crcmod.predefined.Crc("crc-32-mpeg")
    crc.update(data)
    return int(crc.crcValue)


@dataclass(frozen=True)
class UpdateEntry:
    """One flashable image from a ``.upd`` file (or a wrapped raw ``.bin``)."""

    sig: bytes  # SIG_MAIN or SIG_BOOT
    hw_model: int
    major: int
    minor: int
    wire: bytes  # payload + 8-byte footer: exactly the bytes CMD_UPDATE streams

    @property
    def kind(self) -> str:
        return _SIG_NAMES.get(self.sig, repr(self.sig))

    @property
    def version(self) -> str:
        return f"{self.major}.{self.minor}"

    @property
    def payload(self) -> bytes:
        return self.wire[:-_FOOTER_LEN]

    def describe(self) -> str:
        model = HW_MODEL_NAMES.get(self.hw_model, f"hw_model {self.hw_model}")
        return f"{model} {self.kind} v{self.version} ({len(self.payload)} B)"


def make_entry(
    payload: bytes, hw_model: int, sig: bytes, major: int = 0, minor: int = 0
) -> UpdateEntry:
    """Wrap a raw image in its footer, exactly as mk_update.py ``mk_cat_entry`` does."""
    if len(payload) % 4:
        # The firmware refuses lengths that are not a longword multiple
        # (update_prep: ``if (len & 3) return ACK_BAD_COMMAND``). mk_update.py
        # asserts instead; we pad with erased-flash 0xFF, which changes nothing.
        pad = 4 - len(payload) % 4
        log.debug("image is %d B, not longword aligned; padding %d B of 0xFF", len(payload), pad)
        payload += b"\xff" * pad
    footer = _FOOTER_HEAD.pack(sig, major, minor, hw_model)
    crc = _crc16(payload + footer)
    return UpdateEntry(sig, hw_model, major, minor, payload + footer + struct.pack(">H", crc))


def parse_upd(blob: bytes) -> list[UpdateEntry]:
    """Parse and fully verify a ``.upd`` file; raises ``FlashError`` if it is bad."""
    if blob[:4] != UPD_MAGIC:
        log.debug("upd: magic %r != %r; refusing", blob[:4], UPD_MAGIC)
        raise FlashError("not a Greaseweazle update file (no 'GWUP' magic)")
    if len(blob) < 4 + _FILE_CRC.size:
        log.debug("upd: file is only %d B; refusing", len(blob))
        raise FlashError("update file is truncated")
    # The file CRC is appended big-endian, so the CRC over the whole file
    # (CRC included) is 0 — the same check mk_update.py's verify makes.
    if _crc32_mpeg2(blob) != 0:
        log.debug("upd: whole-file CRC-32/MPEG-2 is not 0; refusing")
        raise FlashError("update file CRC32 mismatch: the file is corrupt or truncated")

    entries: list[UpdateEntry] = []
    body = blob[4 : -_FILE_CRC.size]
    off = 0
    while off < len(body):
        if len(body) - off < _CAT_HEADER.size:
            log.debug("upd: %d stray bytes after entry %d; refusing", len(body) - off, len(entries))
            raise FlashError("update file has a truncated catalogue entry")
        wire_len, hw_model = _CAT_HEADER.unpack_from(body, off)
        start = off + _CAT_HEADER.size
        wire = body[start : start + wire_len]
        if wire_len < _FOOTER_LEN or len(wire) != wire_len:
            log.debug(
                "upd: entry %d claims %d B, has %d B; refusing", len(entries), wire_len, len(wire)
            )
            raise FlashError(f"update file entry {len(entries)} is truncated")
        if _crc16(wire) != 0:
            log.debug("upd: entry %d CRC16 over payload+footer is not 0; refusing", len(entries))
            raise FlashError(f"update file entry {len(entries)} fails its CRC16")
        sig, major, minor, footer_model = _FOOTER_HEAD.unpack_from(wire, wire_len - _FOOTER_LEN)
        if footer_model != hw_model or sig not in _SIG_NAMES:
            log.debug(
                "upd: entry %d header model %d, footer model %d, sig %r; refusing",
                len(entries), hw_model, footer_model, sig,
            )  # fmt: skip
            raise FlashError(f"update file entry {len(entries)} has an inconsistent footer")
        entries.append(UpdateEntry(sig, hw_model, major, minor, wire))
        off = start + wire_len
    log.debug("upd: %d entries: %s", len(entries), [e.describe() for e in entries])
    return entries


def check_vectors(entry: UpdateEntry) -> None:
    """Refuse an image whose vector table says it was linked for the other slot.

    Word 0 of a Cortex-M image is the initial stack pointer (somewhere in SRAM)
    and word 1 the reset handler (a Thumb address, so odd, inside the image).
    The reset handler is the cheap tell that catches the expensive mistake:
    a bootloader ``.bin`` flashed into the main-firmware slot links at
    0x0800_0000, so its reset vector points outside 0x0800_4000.. and the board
    would jump into the weeds on every boot.
    """
    region = _FLASH_MAP.get(entry.hw_model, {}).get(entry.sig)
    if region is None:
        log.debug("vectors: no flash map for model %d %r; refusing", entry.hw_model, entry.sig)
        raise FlashError(f"don't know the flash layout of hw_model {entry.hw_model}")
    payload = entry.payload
    if len(payload) > region.max_bytes:
        log.debug("vectors: %d B > slot %d B; refusing", len(payload), region.max_bytes)
        raise FlashError(
            f"{entry.kind} image is {len(payload)} B; the slot holds {region.max_bytes} B"
        )
    if len(payload) < 8:
        log.debug("vectors: image only %d B; refusing", len(payload))
        raise FlashError(f"{entry.kind} image is too short to hold a vector table")
    sp, pc = struct.unpack_from("<II", payload)
    if not (_SRAM_START <= sp <= _SRAM_END):
        log.debug("vectors: initial SP 0x%08x not in SRAM; refusing", sp)
        raise FlashError(f"{entry.kind} image does not start with a vector table (SP 0x{sp:08x})")
    if not (pc & 1) or not (region.start <= pc & ~1 < region.start + len(payload)):
        log.debug(
            "vectors: reset 0x%08x outside 0x%08x+%d; refusing", pc, region.start, len(payload)
        )
        raise FlashError(
            f"image's reset vector 0x{pc:08x} is not inside the {entry.kind} slot at "
            f"0x{region.start:08x}: wrong image for this slot (a bootloader .bin?)"
        )


def load_image(path: str | Path, hw_model: int, sig: bytes) -> UpdateEntry:
    """Pick the ``sig`` image for ``hw_model`` out of ``path`` (``.upd`` or raw ``.bin``)."""
    path = Path(path)
    if not path.exists():
        log.debug("load_image: %s does not exist; refusing", path)
        raise FlashError(f"firmware image not found: {path}")
    blob = path.read_bytes()
    if blob[:4] == UPD_MAGIC:
        entries = parse_upd(blob)
        matches = [e for e in entries if e.hw_model == hw_model and e.sig == sig]
        if not matches:
            log.debug("load_image: no %r entry for model %d; refusing", sig, hw_model)
            have = ", ".join(e.describe() for e in entries) or "nothing"
            model = HW_MODEL_NAMES.get(hw_model, f"hw_model {hw_model}")
            raise FlashError(f"{path.name} has no {_SIG_NAMES[sig]} for {model} (it has {have})")
        entry = matches[0]
    elif path.suffix.lower() == ".hex" or blob[:1] == b":":
        log.debug("load_image: %s looks like Intel HEX; refusing", path)
        raise FlashError(
            "Intel HEX is for Artery ISP; tw update takes a .upd or the raw application .bin"
        )
    else:
        if sig == SIG_BOOT:
            # A raw .bin carries no "this is a bootloader" marker; the vector
            # check below would catch an app image, but a bootloader rewrite is
            # the one flash that can brick the board, so demand the real file.
            log.debug("load_image: raw .bin offered for a bootloader update; refusing")
            raise FlashError(
                "a bootloader update needs a .upd file (tools/package.py), not a raw .bin"
            )
        log.debug("load_image: %s is a raw binary; wrapping it as v0.0", path)
        entry = make_entry(blob, hw_model, sig)
    check_vectors(entry)
    return entry


# ---------------------------------------------------------------------------
# The on-wire update client
# ---------------------------------------------------------------------------

_CMD_GET_INFO = 0
_CMD_UPDATE = 1
_CMD_SWITCH_FW_MODE = 11
_FW_MODE_BOOTLOADER = 0
_FW_MODE_NORMAL = 1
_BL_UPDATE_SIGNATURE = 0xDEAFBEE3  # floppy.c CMD_UPDATE: guards the bootloader rewrite

# struct gw_info (cdc_acm_protocol.h): fw_major, fw_minor, is_main_firmware,
# max_cmd, sample_freq:u32, hw_model, hw_submodel, usb_speed, ... -- we only
# need the first nine bytes; the device always sends 32.
_GW_INFO_HEAD = struct.Struct("<4BI2B")
_GW_INFO_LEN = 32

# CMD_UPDATE's ack comes back only after update_prep has erased the old main
# firmware (48 KB of pages), so give it far longer than the link's 2 s.
_ERASE_TIMEOUT_S = 15.0
_CHUNK = 4096  # write granularity: only sets how often the progress bar moves
# Re-enumeration: the board waits 500 ms after usb_deinit before resetting
# (floppy.c / fw_update.c CMD_SWITCH_FW_MODE), then the host OS needs a moment
# to re-create the CDC-ACM node.
_REENUM_SETTLE_S = 1.0
_REENUM_TIMEOUT_S = 15.0
_REENUM_POLL_S = 0.25


@dataclass(frozen=True)
class BoardState:
    """What GET_INFO says about whichever firmware is answering."""

    fw_major: int
    fw_minor: int
    is_main_firmware: bool
    hw_model: int
    hw_submodel: int
    strapped: bool  # bootloader only: entered because the update strap is fitted

    @property
    def version(self) -> str:
        return f"{self.fw_major}.{self.fw_minor}"

    @property
    def mode(self) -> str:
        return "main firmware" if self.is_main_firmware else "bootloader"


@dataclass(frozen=True)
class UpdateResult:
    """What ``app_update`` did, for the CLI to report."""

    entry: UpdateEntry
    before: BoardState
    after: BoardState | None  # None when the board was left in the bootloader (strap)


# connect(serial_number) -> an open Transport. ``serial_number`` is the USB
# serial of the board we started with ("" = unknown); the real implementation
# uses it to find the same board again after it re-enumerates.
Connector = Callable[[str], Transport]


def get_state(t: Transport) -> BoardState:
    """GET_INFO, which both the main firmware and the bootloader answer."""
    log.debug("update: GET_INFO")
    t.send_frame(_CMD_GET_INFO, bytes([0]))
    ack, raw = t.recv_frame(_CMD_GET_INFO, _GW_INFO_LEN)
    if ack != ACK_OKAY:
        log.debug("update: GET_INFO -> ACK_%s; refusing", ACK_NAMES.get(ack, ack))
        raise FlashError(f"board refused GET_INFO (ACK_{ACK_NAMES.get(ack, ack)})")
    major, minor, is_main, _max_cmd, sample_freq, hw_model, hw_sub = _GW_INFO_HEAD.unpack_from(raw)
    state = BoardState(
        fw_major=major,
        fw_minor=minor,
        is_main_firmware=bool(is_main),
        hw_model=hw_model,
        hw_submodel=hw_sub,
        # In the bootloader sample_freq is a flags word (fw_update.c GET_INFO);
        # in the main firmware it is the real sample clock, so ignore it there.
        strapped=not is_main and bool(sample_freq & 1),
    )
    log.debug("update: board is in its %s v%s, hw_model %d.%d%s", state.mode, state.version,
              hw_model, hw_sub, " (strapped)" if state.strapped else "")  # fmt: skip
    return state


def _switch_mode(t: Transport, mode: int) -> None:
    """Send SWITCH_FW_MODE and drop the link: the board resets without answering."""
    log.info("rebooting the board into its %s", "bootloader" if mode == 0 else "main firmware")
    try:
        t.send_frame(_CMD_SWITCH_FW_MODE, bytes([mode]))
    finally:
        t.close()


def _stream_image(t: Transport, cmd_payload: bytes, entry: UpdateEntry, progress: Progress) -> None:
    """CMD_UPDATE, wait for the ack, stream the image, check the status byte."""
    log.debug("update: CMD_UPDATE %s for %d B", cmd_payload.hex(), len(entry.wire))
    t.send_frame(_CMD_UPDATE, cmd_payload)
    ack, _ = t.recv_frame(_CMD_UPDATE, 0, timeout_s=_ERASE_TIMEOUT_S)
    if ack != ACK_OKAY:
        log.debug("update: CMD_UPDATE -> ACK_%s; refusing", ACK_NAMES.get(ack, ack))
        raise FlashError(f"board refused the update (ACK_{ACK_NAMES.get(ack, ack)})")
    # Only now may the image go out (module docstring, point 1).
    log.info("writing %s", entry.describe())
    with progress.task(f"writing {entry.kind}", total=len(entry.wire), unit="bytes") as bar:
        for off in range(0, len(entry.wire), _CHUNK):
            chunk = entry.wire[off : off + _CHUNK]
            t.write(chunk)
            bar.advance(len(chunk))
    log.debug("update: image sent; waiting for the CRC status byte")
    (status,) = t.read_exact(1)
    if status != 0:
        log.debug("update: status byte %d (CRC FAIL)", status)
        raise FlashError(f"the board's CRC check of the new {entry.kind} failed")
    log.debug("update: status 0 (CRC OK)")


def serial_connector(port: str | None) -> Connector:
    """The real Connector: find the board (by port, then by USB serial) and open it."""
    from tapewyrm.link.device import GW_USB_PID, GW_USB_VID  # noqa: PLC0415

    def connect(serial_number: str) -> Transport:
        from serial.tools import list_ports  # noqa: PLC0415

        deadline = time.monotonic() + _REENUM_TIMEOUT_S
        last_err = "no board seen"
        while True:
            gws = [p for p in list_ports.comports() if (p.vid, p.pid) == (GW_USB_VID, GW_USB_PID)]
            if serial_number:
                # After a reset the board's node may be renamed; its USB serial
                # (from the MCU unique ID, firmware/src/usb/config.c) is not.
                devs = [p.device for p in gws if p.serial_number == serial_number]
            elif port is not None:
                devs = [p.device for p in gws if p.device == port] or [port]
            else:
                devs = [p.device for p in gws]
            if len(devs) > 1:
                log.debug("connect: several boards %s and no serial to pick by; refusing", devs)
                raise FlashError(f"several Greaseweazles found {devs}; pass --port to pick one")
            if devs:
                t = SerialTransport(devs[0])
                try:
                    t.open()
                    log.debug("connect: opened %s", devs[0])
                    return t
                except TransportError as exc:
                    # The node can exist a beat before the OS lets us open it.
                    log.debug("connect: %s not openable yet: %s", devs[0], exc)
                    last_err = str(exc)
            if time.monotonic() >= deadline:
                log.debug("connect: gave up after %.0f s: %s", _REENUM_TIMEOUT_S, last_err)
                raise FlashError(
                    f"board did not come back on USB within {_REENUM_TIMEOUT_S:.0f} s ({last_err})"
                )
            time.sleep(_REENUM_POLL_S)

    return connect


def _serial_of(t: Transport) -> str:
    if isinstance(t, SerialTransport):
        from tapewyrm.link.device import usb_serial_for  # noqa: PLC0415

        return usb_serial_for(t.port)
    return ""


def _reconnect(connect: Connector, serial_number: str) -> tuple[Transport, BoardState]:
    log.debug("update: waiting %.1f s for the board to re-enumerate", _REENUM_SETTLE_S)
    time.sleep(_REENUM_SETTLE_S)
    t = connect(serial_number)
    return t, get_state(t)


def app_update(
    image: str | Path,
    *,
    port: str | None = None,
    bootloader: bool = False,
    connect: Connector | None = None,
    progress: Progress = NULL_PROGRESS,
) -> UpdateResult:
    """Routine update via the GW-compatible application bootloader over USB.

    Main firmware (default): reboot into the bootloader if needed, rewrite the
    main firmware, reboot back into it and read its version. If the CRC fails
    the bootloader erases the half-written image and stays put, so rerunning
    ``tw update`` recovers.

    ``bootloader=True``: from the *main firmware*, rewrite the bootloader. The
    main firmware buffers the whole image in RAM and checks its CRC before it
    erases anything, but a power cut mid-write still leaves no bootloader:
    ``tw dfu`` is the way back then (DESIGN.md §12.3).
    """
    connect = connect if connect is not None else serial_connector(port)
    sig = SIG_BOOT if bootloader else SIG_MAIN
    log.info("connecting to the board")
    t = connect("")
    serial_number = _serial_of(t)
    try:
        before = get_state(t)
        entry = load_image(image, before.hw_model, sig)
        log.info("board: %s v%s; image: %s", before.mode, before.version, entry.describe())

        if bootloader:
            if not before.is_main_firmware:
                log.debug("update: bootloader rewrite asked for while in the bootloader; refusing")
                raise FlashError(
                    "the bootloader can only be rewritten from the main firmware; "
                    "the board is in its bootloader (remove the update strap, or run "
                    "`tw update` first)"
                )
            payload = struct.pack("<II", len(entry.wire), _BL_UPDATE_SIGNATURE)
            _stream_image(t, payload, entry, progress)
            log.info("bootloader updated")
            return UpdateResult(entry, before, get_state(t))

        state = before
        if state.is_main_firmware:
            _switch_mode(t, _FW_MODE_BOOTLOADER)
            t, state = _reconnect(connect, serial_number)
            if state.is_main_firmware:
                log.debug("update: still in main firmware after SWITCH_FW_MODE(0); refusing")
                raise FlashError(
                    "the board came back in its main firmware, not the bootloader: is the "
                    "Greaseweazle-compatible bootloader installed? (flash the merged .hex once)"
                )
        _stream_image(t, struct.pack("<I", len(entry.wire)), entry, progress)
        log.info("main firmware written and verified")

        if state.strapped:
            # fw_update.c re-checks the strap on every reset, so SWITCH_FW_MODE(1)
            # would only land us back here. The human has to pull it.
            log.warning(
                "update strap is fitted: remove it and power-cycle the board to run the new firmware"
            )
            return UpdateResult(entry, before, None)
        _switch_mode(t, _FW_MODE_NORMAL)
        t, after = _reconnect(connect, serial_number)
        if not after.is_main_firmware:
            log.debug("update: board stayed in the bootloader after SWITCH_FW_MODE(1); refusing")
            raise FlashError("the new firmware was written but the board stayed in its bootloader")
        if (entry.major, entry.minor) != (0, 0) and after.version != entry.version:
            log.warning("board reports v%s, the image said v%s", after.version, entry.version)
        return UpdateResult(entry, before, after)
    except TransportError as exc:
        log.debug("update: link failed: %s", exc)
        raise FlashError(f"USB link to the board failed mid-update: {exc}") from exc
    finally:
        t.close()


# ---------------------------------------------------------------------------
# DFU recovery path
# ---------------------------------------------------------------------------


def dfu_argv(
    binfile: str | Path,
    *,
    dfu_util: str = "dfu-util",
    alt: int = 0,
    vid_pid: str | None = None,
    serial: str | None = None,
    reset: bool = True,
) -> list[str]:
    """Build the ``dfu-util`` command line for a recovery/first flash.

    Pure (no side effects) so it is unit-testable without the tool installed.
    """
    argv = [dfu_util, "-a", str(alt)]
    if vid_pid:
        argv += ["-d", vid_pid]
    if serial:
        argv += ["-S", serial]
    if reset:
        # Leave DFU mode and run the freshly-flashed app when supported.
        argv += ["-s", ":leave"]
    argv += ["-D", str(binfile)]
    return argv


def run_dfu(
    binfile: str | Path,
    *,
    dfu_util: str = "dfu-util",
    alt: int = 0,
    vid_pid: str | None = None,
    serial: str | None = None,
    reset: bool = True,
) -> int:
    """Flash ``binfile`` via the AT32 ROM bootloader using ``dfu-util``.

    Raises ``FlashError`` if the binary is missing, ``dfu-util`` is not on PATH,
    or the flash fails.
    """
    path = Path(binfile)
    if not path.exists():
        log.debug("dfu: image %s does not exist; refusing", path)
        raise FlashError(f"firmware image not found: {path}")
    if shutil.which(dfu_util) is None:
        log.debug("dfu: %r not on PATH; refusing", dfu_util)
        raise FlashError(
            f"{dfu_util!r} not found on PATH. Install dfu-util (or use Artery ISP/"
            f"AT-Link if the AT32 ROM-DFU doesn't enumerate under stock dfu-util — "
            f"DESIGN.md §12.3)."
        )
    argv = dfu_argv(path, dfu_util=dfu_util, alt=alt, vid_pid=vid_pid, serial=serial, reset=reset)
    log.info("flashing %s with %s", path, dfu_util)
    log.debug("running: %s", " ".join(argv))
    try:
        proc = subprocess.run(argv, check=False)
    except OSError as exc:  # pragma: no cover - environment dependent
        log.debug("dfu: could not start %s: %s", dfu_util, exc)
        raise FlashError(f"failed to run {dfu_util}: {exc}") from exc
    if proc.returncode != 0:
        log.debug("dfu: %s exited %d; flash failed", dfu_util, proc.returncode)
        raise FlashError(f"{dfu_util} exited with status {proc.returncode}")
    return proc.returncode
