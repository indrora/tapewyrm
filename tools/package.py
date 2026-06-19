#!/usr/bin/env python3
"""Build the whole Tapewyrm project as one package.

Produces `dist/tapewyrm-<version>/` (and a .zip of it) containing:
  - firmware/   flashable images per MCU: tapewyrm-<mcu>.hex (bootloader+app,
                merged in pure Python) and tapewyrm-<mcu>.bin (app, for DFU)
  - host/       the Tapewyrm host wheel + sdist (built with `uv build`)
  - UNLICENSE, README.md, DESIGN.md

Needs only: Python, the ARM GNU toolchain (arm-none-eabi-gcc/objcopy), GNU make
(mingw32-make on Windows/MSYS), and `uv`. No srecord / crcmod / zip required —
the bootloader+app HEX merge is done by tools/ihex.py and the archive by
zipfile. (CI/Linux can still run `make -C firmware dist` for the full GW-style
multi-MCU release with .upd files.)

Usage:
    python tools/package.py                 # host wheel + at32f4 firmware -> dist/
    python tools/package.py --mcus at32f4 stm32f7
    python tools/package.py --skip-firmware # host wheel only
    python tools/package.py --skip-host     # firmware only
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
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


def tool(*names: str) -> str:
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    raise SystemExit(f"required tool not found on PATH: {' / '.join(names)}")


def host_version() -> str:
    data = tomllib.loads((HOST / "pyproject.toml").read_text())
    return data["project"]["version"]


def fw_version() -> str:
    text = (FW / "Makefile").read_text()
    major = re.search(r"FW_MAJOR\s*:=\s*(\d+)", text).group(1)
    minor = re.search(r"FW_MINOR\s*:=\s*(\d+)", text).group(1)
    return f"{major}.{minor}"


def _make_env() -> dict[str, str]:
    env = dict(os.environ)
    env["ROOT"] = str(FW)
    maj, min_ = fw_version().split(".")
    env["FW_MAJOR"], env["FW_MINOR"] = maj, min_
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

        # Bootloader: .hex needs no srecord; skip .upd (avoids crcmod).
        run_make(boot, "target.hex", "target.bin", bootloader="y")
        # Application: build .bin (+ .elf dependency); skip the .hex rule (srecord).
        run_make(app, "target.bin", tapewyrm="y")

        # objcopy the app ELF -> app-only HEX ourselves, then merge with bootloader.
        app_hex = app / "app-only.hex"
        subprocess.run([objcopy, "-O", "ihex", str(app / "target.elf"), str(app_hex)],
                       check=True)
        merged = app / "tapewyrm.hex"
        ihex.merge([boot / "target.hex", app_hex], merged)

        out[mcu] = {"hex": merged, "bin": app / "target.bin", "elf": app / "target.elf"}
        print(f"   {mcu}: {merged.name} ({merged.stat().st_size} B), "
              f"target.bin ({(app / 'target.bin').stat().st_size} B)")
    return out


def build_host() -> list[Path]:
    print("== host wheel + sdist ==")
    uv = tool("uv")
    subprocess.run([uv, "build", "--out-dir", str(HOST / "dist")], cwd=str(HOST), check=True)
    return sorted((HOST / "dist").glob("tapewyrm-*"))


def assemble(version: str, fw: dict[str, dict[str, Path]], host_artifacts: list[Path]) -> Path:
    stage = DIST / f"tapewyrm-{version}"
    if stage.exists():
        shutil.rmtree(stage)
    (stage / "firmware").mkdir(parents=True)
    (stage / "host").mkdir(parents=True)

    fwver = fw_version()
    for mcu, arts in fw.items():
        shutil.copy2(arts["hex"], stage / "firmware" / f"tapewyrm-{mcu}-{fwver}.hex")
        shutil.copy2(arts["bin"], stage / "firmware" / f"tapewyrm-{mcu}-{fwver}.bin")
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
    ap.add_argument("--mcus", nargs="+", default=["at32f4"],
                    help="firmware MCU targets (default: at32f4)")
    ap.add_argument("--skip-firmware", action="store_true")
    ap.add_argument("--skip-host", action="store_true")
    args = ap.parse_args()

    version = host_version()
    fw = build_firmware(args.mcus) if not args.skip_firmware else {}
    host_artifacts = build_host() if not args.skip_host else []
    archive = assemble(version, fw, host_artifacts)
    print(f"\npackaged -> {archive.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
