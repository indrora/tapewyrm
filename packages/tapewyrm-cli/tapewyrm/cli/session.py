"""Shared drive plumbing for the ``tw`` commands (DESIGN.md §6A.7).

``_drive_session`` opens the link, wakes the drive and always releases it
again; ``tw drive ...`` and ``tw dump`` both run inside one. The formatters
and report decoders below turn raw QIC-117 status bytes and report words into
the text the drive commands print. Every heavy import is done lazily inside
the function that needs it, so importing this module stays cheap.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import rich_click as click

from tapewyrm.cli.app import AppContext
from tapewyrm.types import DriveProfile

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Drive session
# ---------------------------------------------------------------------------


@contextmanager
def _drive_session(
    app: AppContext,
    *,
    wake: bool = True,
    profile: DriveProfile | None = None,
    candidates: tuple[DriveProfile, ...] | None = None,
) -> Iterator[Any]:
    """Open the link, wake the drive with the profile, yield a Qic117Drive.

    The profile is ``profile`` if given, else ``app.profile``. If both are None
    (``--profile auto``) the session probes ``candidates`` (default: the
    ``AUTO_ORDER`` profiles) with ``auto_wake`` and keeps the one that answers;
    with ``wake=False`` there is nothing to probe and the built-in ``default``
    profile (no wake steps) carries the link.

    Every ``tw drive`` command is one short session. On the way out we always
    release the GW drive-select lines and the port; commands that move tape
    stop it themselves before returning (``Qic117Drive.jog``) or wait for Ready.
    A phantom-selected drive stays selected across sessions (it ignores the DS
    lines) until ``tw drive deselect``, a reset or a power cycle.
    ``wake=False`` skips the profile's wake sequence (for select/deselect).
    """
    from tapewyrm.link.device import DeviceLink, LinkError
    from tapewyrm.qic117.drive import DriveError, Qic117Drive, auto_wake
    from tapewyrm.qic117.profile import ProfileError, auto_candidates

    chosen = profile or app.profile
    link = DeviceLink()
    drive: Qic117Drive | None = None
    try:
        log.debug("opening link on port %r", app.port)
        link.open(app.port)
        log.debug("releasing drive-select lines before wake")
        link.deselect()  # phantom drives want every DS line idle
        if not wake:
            log.debug("skipping wake sequence (wake=False)")
            drive = Qic117Drive(link, chosen or DriveProfile.default())
        elif chosen is None:
            log.debug("profile auto: probing for a drive that answers")
            drive = auto_wake(link, candidates or auto_candidates())
        else:
            log.debug("waking drive with profile %r", chosen.name)
            drive = Qic117Drive(link, chosen)
            drive.wake()
        yield drive
    except ProfileError as exc:
        log.debug("auto-detect profile failed to load: %r", exc)
        raise click.ClickException(str(exc)) from exc
    except (LinkError, DriveError) as exc:
        log.debug("drive session failed: %r", exc)
        raise click.ClickException(str(exc)) from exc
    finally:
        log.debug("closing drive session: motor off, deselect and close link")
        try:
            if drive is not None:
                drive.release_lines()  # a Motor-on wake's motor (ftape's sleep)
            link.deselect()
        except Exception as exc:
            log.debug("deselect on close failed (ignored): %r", exc)
        link.close()


def _fmt_status(st: Any) -> str:
    flags = [
        name
        for name in (
            "ready", "error", "cartridge_present", "write_protect", "new_cartridge",
            "referenced", "at_bot", "at_eot",
        )
        if getattr(st, name)
    ]  # fmt: skip
    return f"0x{st.raw:02x} [{' '.join(flags) or '-'}]"


# ---------------------------------------------------------------------------
# Report decoders (QIC-117 Rev J Table 2c)
# ---------------------------------------------------------------------------


def _cmd_label(code: int) -> str:
    from tapewyrm.qic117 import commands

    cmd = commands.BY_CODE.get(code)
    return f"cmd {code} {cmd.name}" if cmd else f"cmd {code}"


def _decode_error(w: int) -> str:
    from tapewyrm.qic117.status import error_name

    code, assoc = w & 0xFF, (w >> 8) & 0xFF
    if not code:
        return f"{code} {error_name(code)}"
    # Rev J p.13: "A process error returns a command code of zero and an
    # initialization error returns the command code of one."
    where = {0: "process error", 1: "initialization error"}.get(assoc) or (
        f"from {_cmd_label(assoc)}"
    )
    return f"{code} {error_name(code)} ({where})"


def _decode_config(b: int) -> str:
    from tapewyrm_archive.qic117 import DriveConfig

    cfg = DriveConfig.decode(b)
    rate = f"{cfg.rate_kbps} kbps" + (" (or 4 Mbps)" if cfg.rate_ambiguous else "")
    parts = [rate, "QIC-80 mode" if cfg.qic80_mode else "QIC-40 mode"]
    if cfg.extra_length:
        parts.append("extra-length tape")
    return ", ".join(parts)


def _decode_rom(b: int) -> str:
    return f"version {b & 0x7F}" + (" (BETA)" if b & 0x80 else "")


def _decode_vendor(w: int) -> str:
    from tapewyrm_archive.qic117 import LEGACY_VENDOR_IDS, decode_vendor_id

    make, model, name = decode_vendor_id(w)
    if w in LEGACY_VENDOR_IDS:
        return name
    return f"make {make} {name}, model {model}"


def _decode_tape(b: int) -> str:
    from tapewyrm_archive.qic117 import TAPE_TYPES, TapeStatus

    ts = TapeStatus.decode(b)
    kind = TAPE_TYPES.get(ts.tape_type, f"reserved type {ts.tape_type}")
    return f"format {ts.format.name}, {kind} tape" + (", wide (8mm)" if ts.wide else "")
