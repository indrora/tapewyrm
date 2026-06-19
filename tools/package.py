#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["crcmod>=1.7"]
# ///
"""Build the whole Tapewyrm project as one package. Run with `uv run`.

Default (`uv run tools/package.py`): host wheel/sdist + the at32f4 firmware image
-> dist/tapewyrm-<version>.zip.

Release (`uv run tools/package.py --dist`): the full Greaseweazle-style firmware
release — all MCUs (stm32f1, stm32f7, at32f4), each as a flashable .hex
(bootloader+app) and .bin, PLUS a combined .upd update file — alongside the host
wheel, zipped.

Needs only: Python, the ARM GNU toolchain (arm-none-eabi-gcc/objcopy), GNU make
(mingw32-make on Windows/MSYS), `uv`, and (declared above, fetched by uv) crcmod
for the .upd CRCs. The bootloader+app HEX merge is pure-Python (tools/ihex.py),
the .upd is a faithful port of firmware/scripts/mk_update.py, and the archive is
zipfile — so no srecord / system crcmod / zip are required.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import struct
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FW = ROOT / "firmware"
HOST = ROOT / "host"
DIST = ROOT / "dist"

sys.path.insert(0, str(ROOT / "tools"))
import ihex  # noqa: E402

# Greaseweazle hardware-model ids (firmware/scripts/mk_update.py). Canonical .upd
# ordering matches GW's `make dist` (f1, f7, at32f4; each bootloader then app).
HW_MODEL = {"stm32f1": 1, "stm32f7": 7, "at32f4": 4}
DIST_MCUS = ["stm32f1", "stm32f7", "at32f4"]


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
    text = (FW / "Makefile").read_text()
    return (
        int(re.search(r"FW_MAJOR\s*:=\s*(\d+)", text).group(1)),
        int(re.search(r"FW_MINOR\s*:=\s*(\d+)", text).group(1)),
    )


def _make_env() -> dict[str, str]:
    env = dict(os.environ)
    env["ROOT"] = str(FW)
    maj, minr = fw_version()
    env["FW_MAJOR"], env["FW_MINOR"] = str(maj), str(minr)
    return env


def build_firmware(mcus: list[str]) -> dict[str, dict[str, Path]]:
    make = tool("mingw32-make", "make")
    objcopy = tool("arm-none-eabi-objcopy")
    py = sys.executable
    env = _make_env()
    out: dict[str, dict[str, Path]] = {}

    for mcu in mcus:
        print(f"== firmware: {mcu} ==")
        boot = FW / "out" / mcu / "prod" / "bootloader"
        app = FW / "out" / mcu / "prod" / "tapewyrm"
        boot.mkdir(parents=True, exist_ok=True)
        app.mkdir(parents=True, exist_ok=True)

        def run_make(cwd: Path, *targets: str, **flags: str) -> None:
            flag_args = [f"{k}={v}" for k, v in flags.items()]
            subprocess.run(
                [make, "-C", str(cwd), "-f", str(FW / "Rules.mk"), *targets,
                 f"PYTHON={py}", f"mcu={mcu}", "prod=y", *flag_args],
                check=True, env=env,
            )

        # Bootloader: .hex/.bin need no srecord; skip .upd (we generate it here).
        run_make(boot, "target.hex", "target.bin", bootloader="y")
        # Application: build .bin (+ .elf dependency); skip the .hex rule (srecord).
        run_make(app, "target.bin", tapewyrm="y")

        # objcopy the app ELF -> app-only HEX ourselves, then merge with bootloader.
        app_hex = app / "app-only.hex"
        subprocess.run([objcopy, "-O", "ihex", str(app / "target.elf"), str(app_hex)],
                       check=True)
        merged = app / "tapewyrm.hex"
        ihex.merge([boot / "target.hex", app_hex], merged)

        out[mcu] = {
            "hex": merged, "bin": app / "target.bin", "elf": app / "target.elf",
            "boot_bin": boot / "target.bin",
        }
        print(f"   {mcu}: tapewyrm.hex ({merged.stat().st_size} B), "
              f"app.bin ({(app / 'target.bin').stat().st_size} B)")
    return out


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
    print("== host wheel + sdist ==")
    uv = tool("uv")
    subprocess.run([uv, "build", "--out-dir", str(HOST / "dist")], cwd=str(HOST), check=True)
    return sorted((HOST / "dist").glob("tapewyrm-*"))


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
