"""Firmware flashing — owned by ``tw``, not delegated to the ``gw`` tool.

Tapewyrm ships a single self-sufficient executable (``tw``). Flashing is part of
that: we do NOT shell out to Greaseweazle's ``gw update`` (DESIGN.md §1, §12.3).
Two paths, mirroring the design's flashing tiers:

- **DFU (recovery / first-flash):** drive the AT32F403 built-in ROM bootloader via
  the standard, vendor-neutral ``dfu-util`` (or Artery's ISP/AT-Link if the AT32
  ROM-DFU descriptor doesn't enumerate cleanly under stock dfu-util — §12.3 open
  item). ``dfu-util`` is a generic third-party flasher, not ``gw`` — using it does
  not make ``tw`` depend on Greaseweazle tooling.
- **App bootloader (routine, over USB):** the GW-compatible application bootloader
  update protocol, spoken by ``tw`` itself. We keep that protocol wire-compatible
  (§12.5) so the board stays a dual citizen, but ``tw`` carries its own client so
  it needs no ``gw`` install. The protocol bytes are a TODO(bench) seam (§13.6).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

# AT32F403 built-in ROM DFU identity (Artery). Left as the documented default;
# dfu-util can usually auto-detect, so we don't force --device unless asked.
AT32_ROM_DFU_VID_PID = "2e3c:df11"  # TODO(bench): confirm on the v4.1 (§12.3)


class FlashError(Exception):
    """A firmware flash could not be performed or failed."""


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
        raise FlashError(f"firmware image not found: {path}")
    if shutil.which(dfu_util) is None:
        raise FlashError(
            f"{dfu_util!r} not found on PATH. Install dfu-util (or use Artery ISP/"
            f"AT-Link if the AT32 ROM-DFU doesn't enumerate under stock dfu-util — "
            f"DESIGN.md §12.3)."
        )
    argv = dfu_argv(path, dfu_util=dfu_util, alt=alt, vid_pid=vid_pid, serial=serial, reset=reset)
    try:
        proc = subprocess.run(argv, check=False)
    except OSError as exc:  # pragma: no cover - environment dependent
        raise FlashError(f"failed to run {dfu_util}: {exc}") from exc
    if proc.returncode != 0:
        raise FlashError(f"{dfu_util} exited with status {proc.returncode}")
    return proc.returncode


def app_update(hexfile: str | Path, *, port: str | None = None) -> int:
    """Routine update via the GW-compatible application bootloader over USB.

    TODO(bench), DESIGN.md §13.6 item 2: implement the application-bootloader
    update protocol on top of our own USB link (it is wire-compatible with
    ``gw update``, but ``tw`` speaks it itself so no ``gw`` install is required).
    The protocol framing must be read from greaseweazle-firmware's bootloader and
    confirmed against the v4.1 before this can run. Until then, direct the user to
    the always-works DFU path.
    """
    path = Path(hexfile)
    if not path.exists():
        raise FlashError(f"firmware image not found: {path}")
    raise FlashError(
        "over-USB application-bootloader update is not yet wired (TODO(bench), "
        "DESIGN.md §13.6 item 2). Use the DFU path for now:  tw dfu <image.bin>  "
        "(strap the hardware DFU header first — §12.3)."
    )
