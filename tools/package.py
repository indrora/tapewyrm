#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["crcmod>=1.7"]
# ///
"""Build the whole Tapewyrm project as one package. Run with `uv run`.

Default (`uv run tools/package.py`): host wheel/sdist + the at32f4 firmware image
-> dist/tapewyrm-<version>.zip.

Release (`uv run tools/package.py --dist`): the full Greaseweazle-style firmware
release — every MCU with a PlatformIO env (today: at32f4), each as a flashable .hex
(bootloader+app) and .bin, PLUS a combined .upd update file — alongside the host
wheel, zipped.

Needs only: Python, PlatformIO Core (`pio`, which fetches its own pinned ARM GCC),
`uv`, and (declared above, fetched by uv) crcmod for the .upd CRCs. The bootloader+app HEX merge is pure-Python (tools/ihex.py),
the .upd is a faithful port of firmware/scripts/mk_update.py, and the archive is
zipfile — so no srecord / system crcmod / zip are required.
"""

from __future__ import annotations

import argparse
import shutil
import struct
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FW = ROOT / "firmware"
HOST = ROOT / "packages" / "tapewyrm-cli"
ARCHIVE = ROOT / "packages" / "tapewyrm-archive"
QICLIB = ROOT / "packages" / "qiclib"
QICSILVER = ROOT / "packages" / "qicsilver"
DIST = ROOT / "dist"

sys.path.insert(0, str(ROOT / "tools"))
import ihex  # noqa: E402

# Greaseweazle hardware-model ids (firmware/scripts/mk_update.py). Canonical .upd
# ordering matches GW's `make dist` (f1, f7, at32f4; each bootloader then app).
HW_MODEL = {"stm32f1": 1, "stm32f7": 7, "at32f4": 4}
DIST_MCUS = ["at32f4"]  # only MCU with PlatformIO envs (firmware/platformio.ini)


def tool(*names: str) -> str:
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    raise SystemExit(f"required tool not found on PATH: {' / '.join(names)}")


def host_version() -> str:
    data = tomllib.loads((HOST / "pyproject.toml").read_text())
    return data["project"]["version"]


def fw_version() -> tuple[int, int]:
    """Firmware version, single-sourced from firmware/platformio.ini.

    PlatformIO's .ini is configparser syntax (with `;` comments), so we read the
    ``custom_fw_major`` / ``custom_fw_minor`` options from the shared ``[env]``
    section rather than regex-scraping like the old Makefile reader did.
    """
    import configparser

    ini = configparser.ConfigParser(inline_comment_prefixes=(";",))
    ini.read(FW / "platformio.ini")
    return int(ini["env"]["custom_fw_major"]), int(ini["env"]["custom_fw_minor"])


def build_firmware(mcus: list[str]) -> dict[str, dict[str, Path]]:
    """Build bootloader + app with PlatformIO; merge them into one HEX.

    Only at32f4 (Greaseweazle V4.x) has PlatformIO environments; the STM32F1/F7
    sources are still in-tree but unwired (see firmware/platformio.ini). PIO
    emits firmware.{elf,bin,hex} per env under firmware/.pio/build/<env>/; the
    app HEX is app-only, so we merge it with the bootloader HEX (tools/ihex.py)
    to get a single image for DFU/first flash.
    """
    unsupported = [m for m in mcus if m != "at32f4"]
    if unsupported:
        raise SystemExit(f"no PlatformIO env for MCU(s): {', '.join(unsupported)}")
    pio = tool("pio", "platformio")
    print("== firmware: at32f4 (pio run -e bootloader -e tapewyrm) ==")
    subprocess.run(
        [pio, "run", "-d", str(FW), "-e", "bootloader", "-e", "tapewyrm"], check=True
    )

    build = FW / ".pio" / "build"
    boot, app = build / "bootloader", build / "tapewyrm"
    merged = app / "tapewyrm.hex"
    ihex.merge([boot / "firmware.hex", app / "firmware.hex"], merged)
    print(
        f"   at32f4: tapewyrm.hex ({merged.stat().st_size} B), "
        f"app.bin ({(app / 'firmware.bin').stat().st_size} B)"
    )
    return {
        "at32f4": {
            "hex": merged,
            "bin": app / "firmware.bin",
            "elf": app / "firmware.elf",
            "boot_bin": boot / "firmware.bin",
        }
    }


# --- .upd update-file generation (faithful port of firmware/scripts/mk_update.py) ---


def _cat_entry(dat: bytes, hw_model: int, major: int, minor: int, sig: bytes) -> bytes:
    import crcmod.predefined  # declared in the PEP 723 block above

    if len(dat) % 4:  # longword-pad (GW relies on linker alignment; pad defensively)
        dat += b"\x00" * (4 - (len(dat) % 4))
    header = struct.pack("<2H", len(dat) + 8, hw_model)
    footer = struct.pack("<2s2BH", sig, major, minor, hw_model)
    crc16 = crcmod.predefined.Crc("crc-ccitt-false")
    crc16.update(dat)
    crc16.update(footer)
    footer += struct.pack(">H", crc16.crcValue)
    return header + dat + footer


def make_upd(fw: dict[str, dict[str, Path]], major: int, minor: int) -> bytes:
    import crcmod.predefined

    dat = b"GWUP"
    for mcu in DIST_MCUS:
        if mcu not in fw:
            continue
        hw = HW_MODEL[mcu]
        dat += _cat_entry(fw[mcu]["boot_bin"].read_bytes(), hw, major, minor, b"BL")
        dat += _cat_entry(fw[mcu]["bin"].read_bytes(), hw, major, minor, b"GW")
    crc32 = crcmod.predefined.Crc("crc-32-mpeg")
    crc32.update(dat)
    dat += struct.pack(">I", crc32.crcValue)
    return dat


def build_host() -> list[Path]:
    print("== host wheels + sdists (tapewyrm-archive, qiclib, tapewyrm, qicsilver) ==")
    uv = tool("uv")
    # tapewyrm and qicsilver depend on tapewyrm-archive and qiclib, which are not
    # on PyPI, so the bundle ships all four; `pip install host/*.whl` then resolves locally.
    for pkg in (ARCHIVE, QICLIB, HOST, QICSILVER):
        subprocess.run([uv, "build", "--out-dir", str(HOST / "dist")], cwd=str(pkg), check=True)
    dist = HOST / "dist"
    return sorted([*dist.glob("tapewyrm-*"), *dist.glob("tapewyrm_archive-*"), *dist.glob("qiclib-*"), *dist.glob("qicsilver-*")])


def assemble(
    version: str,
    fw: dict[str, dict[str, Path]],
    host_artifacts: list[Path],
    upd: bytes | None = None,
) -> Path:
    stage = DIST / f"tapewyrm-{version}"
    if stage.exists():
        shutil.rmtree(stage)
    (stage / "firmware").mkdir(parents=True)
    (stage / "host").mkdir(parents=True)

    maj, minr = fw_version()
    fwver = f"{maj}.{minr}"
    for mcu, arts in fw.items():
        shutil.copy2(arts["hex"], stage / "firmware" / f"tapewyrm-{mcu}-{fwver}.hex")
        shutil.copy2(arts["bin"], stage / "firmware" / f"tapewyrm-{mcu}-{fwver}.bin")
    if upd is not None:
        (stage / "firmware" / f"tapewyrm-{fwver}.upd").write_bytes(upd)
    for a in host_artifacts:
        shutil.copy2(a, stage / "host" / a.name)
    for doc in ("UNLICENSE", "README.md", "DESIGN.md"):
        if (ROOT / doc).exists():
            shutil.copy2(ROOT / doc, stage / doc)

    archive = DIST / f"tapewyrm-{version}.zip"
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                z.write(path, path.relative_to(DIST))
    return archive


def main() -> int:
    ap = argparse.ArgumentParser(description="Build Tapewyrm as one package.")
    ap.add_argument("--mcus", nargs="+", default=None,
                    help="firmware MCU targets (default: at32f4; or all 3 with --dist)")
    ap.add_argument("--dist", action="store_true",
                    help="full release: all MCUs + a combined .upd update file")
    ap.add_argument("--skip-firmware", action="store_true")
    ap.add_argument("--skip-host", action="store_true")
    args = ap.parse_args()

    if args.mcus:
        mcus = args.mcus
    elif args.dist:
        mcus = DIST_MCUS
    else:
        mcus = ["at32f4"]

    version = host_version()
    fw = build_firmware(mcus) if not args.skip_firmware else {}
    upd = None
    if args.dist and fw:
        maj, minr = fw_version()
        upd = make_upd(fw, maj, minr)
        print(f"== combined .upd: {len(upd)} bytes "
              f"({sum(1 for m in DIST_MCUS if m in fw)} MCUs) ==")
    host_artifacts = build_host() if not args.skip_host else []
    archive = assemble(version, fw, host_artifacts, upd=upd)
    print(f"\npackaged -> {archive.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
