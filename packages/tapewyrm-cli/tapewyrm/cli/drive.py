"""``tw drive ...``: hand-driven drive control for bench work and bring-up.

Each subcommand is one short ``_drive_session`` (tapewyrm/cli/session.py):
open the link, wake the drive with the profile, do one thing, release it.
Read-only: commands that write to tape are refused by the drive layer.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import rich_click as click

from tapewyrm.cli.app import AppContext, cli
from tapewyrm.cli.session import (
    _decode_config,
    _decode_error,
    _decode_rom,
    _decode_tape,
    _decode_vendor,
    _drive_session,
    _fmt_status,
)
from tapewyrm.types import DriveProfile

log = logging.getLogger(__name__)

# Report commands and their payload widths in bits (QIC-117 Rev J Table 2c).
_REPORT_BITS = {6: 8, 7: 16, 8: 8, 9: 8, 32: 16, 33: 8, 37: 16}


@cli.group()
def drive() -> None:
    """Poke the tape drive by hand: status, reports, motion, scope.

    Read-only: commands that write to tape are refused by the drive layer.
    Wakes the drive with --profile first. The default, auto, tries the wake-ups
    the Linux ftape driver tries, in its order -- none, Colorado (Phantom
    Select), Mountain (Soft Select), Motor-on -- and stops at the first the
    drive answers; pass --profile NAME (default, colorado, colorado.1400,
    mountain, insight, conner, iomega or a .toml path) to skip the probe.
    """


def _cue_period_ms(trace: Any) -> float | None:
    """Mean spacing of INDEX assertions in a SCOPE trace, or None if < 2."""
    prev_set = trace.initial & 0b10
    rises = []
    for t, st in trace.edges:
        now = st & 0b10
        if now and not prev_set:
            rises.append(t)
        prev_set = now
    if len(rises) < 2:
        return None
    return (rises[-1] - rises[0]) / (len(rises) - 1) / 1000


def _has_phantom_select(profile: DriveProfile) -> bool:
    return any(name.strip().lower() == "phantom select" for name, _a, _d in profile.wake_sequence)


def _with_phantom_unit(profile: DriveProfile, unit: int) -> DriveProfile:
    """The profile's wake sequence, with Phantom Select addressing ``unit``."""
    from dataclasses import replace

    steps = list(profile.wake_sequence)
    for i, (name, _arg, delay) in enumerate(steps):
        if name.strip().lower() == "phantom select":
            steps[i] = (name, unit, delay)
            break
    else:
        steps.insert(0, ("phantom select", unit, 50))
    return replace(profile, wake_sequence=tuple(steps))


@drive.command("select")
@click.option(
    "--unit",
    type=click.IntRange(0, 63),
    default=None,
    help="phantom unit address (default: the profile's; the Colorado 350 is 0)",
)
@click.pass_obj
def drive_select(app: AppContext, unit: int | None) -> None:
    """Wake and select the drive, and prove it is listening.

    Runs the profile's wake sequence (for phantom drives such as the Colorado:
    Phantom Select 46 with its N+2 unit argument, then Enter Primary Mode),
    reads status, and checks for the cue INDEX pulses a selected, ready drive
    emits every few ms (QIC-117 Rev J Fig. 6). The drive stays selected after
    this command exits.
    """
    from tapewyrm.qic117.profile import auto_candidates

    profile: DriveProfile | None = app.profile
    candidates: tuple[DriveProfile, ...] | None = None
    if unit is not None and profile is not None:
        profile = _with_phantom_unit(profile, unit)
    elif unit is not None:
        # --profile auto with --unit: probe the usual candidates; the ones that
        # Phantom Select address the requested unit instead of their own. The
        # others are left as they are, so each still sends only its own ftape
        # method (inserting a Phantom Select would blur them together).
        log.debug("profile auto with --unit %d: re-addressing phantom candidates", unit)
        candidates = tuple(
            _with_phantom_unit(p, unit) if _has_phantom_select(p) else p for p in auto_candidates()
        )
    with _drive_session(app, profile=profile, candidates=candidates) as d:
        st = d.status()
        click.echo(f"status : {_fmt_status(st)}")
        period = _cue_period_ms(d.link.scope(0, 30))
        if period is not None:
            click.echo(f"select : yes -- cue INDEX every {period:.1f} ms")
        elif st.ready:
            click.echo("select : answering reports, but no cue INDEX seen")
        else:
            click.echo("select : answering reports; drive busy (no cue INDEX until Ready)")


@drive.command("deselect")
@click.pass_obj
def drive_deselect(app: AppContext) -> None:
    """Release a phantom-selected drive (Phantom Deselect, 47).

    Phantom drives ignore the drive-select lines, so they stay selected until
    told otherwise; this lets another drive on the same cable be used.
    """
    from tapewyrm.qic117 import commands

    with _drive_session(app, wake=False) as d:
        log.debug("sending Phantom Deselect")
        d.link.command_txn(commands.TABLE["PHANTOM_DESELECT"].code)
        period = _cue_period_ms(d.link.scope(0, 30))
        click.echo(
            "deselect: done" if period is None else f"deselect: still cueing ({period:.1f} ms)!"
        )


@drive.command("status")
@click.pass_obj
def drive_status(app: AppContext) -> None:
    """Every report the drive answers: status, error, config, ROM, vendor, tape.

    Older drives don't implement every report (the Colorado Jumbo 350 predates
    some of them); those print "not supported" and the latched error is
    cleared so the remaining reports still work.
    """
    from tapewyrm.link.device import LinkError
    from tapewyrm.qic117 import commands

    # (label, command, bits, decoder) -- QIC-117 Rev J Table 2c.
    reports = (
        # Rev J: the code is only meaningful while Error Detected is set; the
        # drive otherwise repeats the last one it reported, hence "last error".
        ("last error", "REPORT_ERROR_CODE", 16, _decode_error),
        ("drive config", "REPORT_DRIVE_CONFIGURATION", 8, _decode_config),
        ("rom version", "REPORT_ROM_VERSION", 8, _decode_rom),
        ("vendor id", "REPORT_VENDOR_ID", 16, _decode_vendor),
        ("tape status", "REPORT_TAPE_STATUS", 8, _decode_tape),
    )
    with _drive_session(app) as d:
        if d.last_error is not None:
            click.echo(f"{'cleared on wake':<13}: {_decode_error(d.last_error.raw)}")
        click.echo(f"{'drive status':<13}: {_fmt_status(d.status())}")
        for label, name, bits, decode in reports:
            try:
                val = d.report(commands.TABLE[name], bits)
            except LinkError as exc:
                log.debug("%s (%s) not acknowledged: %r; clearing status", label, name, exc)
                click.echo(f"{label:<13}: not supported by this drive (no ACK)")
                d.status()  # clear whatever the unsupported command latched
                continue
            click.echo(f"{label:<13}: 0x{val:0{bits // 4}x} -> {decode(val)}")


@drive.command("report")
@click.argument("name")
@click.pass_obj
def drive_report(app: AppContext, name: str) -> None:
    """Run one report command by name or code, e.g. `report rom version` or `report 9`."""
    from tapewyrm.qic117 import commands

    key = name.strip().upper().replace(" ", "_").replace("-", "_")
    if key.isdigit():
        cmd = commands.BY_CODE.get(int(key))
    else:  # accept "rom version" as well as "report rom version"
        cmd = commands.TABLE.get(key) or commands.TABLE.get(f"REPORT_{key}")
    if cmd is None or cmd.code not in _REPORT_BITS:
        log.debug("report name %r -> key %r -> %r; not a report command", name, key, cmd)
        names = ", ".join(commands.BY_CODE[c].name for c in sorted(_REPORT_BITS))
        raise click.BadParameter(f"not a report command; one of: {names}", param_hint="NAME")
    with _drive_session(app) as d:
        bits = _REPORT_BITS[cmd.code]
        val = d.report(cmd, bits)
        click.echo(f"{cmd.name} (cmd {cmd.code}): 0x{val:0{bits // 4}x} 0b{val:0{bits}b}")


@drive.command("load-point")
@click.pass_obj
def drive_load_point(app: AppContext) -> None:
    """Seek Load Point (may take ~30 s+: the drive re-references the tape)."""
    from tapewyrm.qic117 import commands

    with _drive_session(app) as d:
        click.echo(f"status : {_fmt_status(d.command(commands.SEEK_LOAD_POINT))}")


@drive.command("fwd")
@click.option("--seconds", default=2.0, show_default=True, help="how long to run")
@click.pass_obj
def drive_fwd(app: AppContext, seconds: float) -> None:
    """Physical Forward for --seconds, then Stop (stops early at EOT)."""
    from tapewyrm.qic117 import commands

    with _drive_session(app) as d:
        click.echo(f"status : {_fmt_status(d.jog(commands.PHYSICAL_FORWARD, seconds))}")


@drive.command("rev")
@click.option("--seconds", default=2.0, show_default=True, help="how long to run")
@click.pass_obj
def drive_rev(app: AppContext, seconds: float) -> None:
    """Physical Reverse for --seconds, then Stop (stops early at BOT)."""
    from tapewyrm.qic117 import commands

    with _drive_session(app) as d:
        click.echo(f"status : {_fmt_status(d.jog(commands.PHYSICAL_REVERSE, seconds))}")


@drive.command("stop")
@click.pass_obj
def drive_stop(app: AppContext) -> None:
    """Stop Tape."""
    from tapewyrm.qic117 import commands

    with _drive_session(app) as d:
        click.echo(f"status : {_fmt_status(d.command(commands.STOP_TAPE))}")


@drive.command("track")
@click.argument("track", type=click.IntRange(0, 63))  # 6-bit argument (Rev J §1.4.3)
@click.pass_obj
def drive_track(app: AppContext, track: int) -> None:
    """Seek Head to Track N."""
    from tapewyrm.qic117 import commands

    with _drive_session(app) as d:
        click.echo(f"status : {_fmt_status(d.command(commands.SEEK_HEAD_TO_TRACK, arg=track))}")


@drive.command("flux")
@click.option(
    "--motion",
    type=click.Choice(["fwd", "rev", "logical"]),
    default="fwd",
    show_default=True,
    help="Physical Forward/Reverse (works unreferenced) or Logical Forward",
)
@click.option("--seconds", default=5.0, show_default=True, help="how long to run the tape")
@click.option(
    "-o", "--out", type=click.Path(dir_okay=False, path_type=Path), default="flux-probe.twrf",
    show_default=True, help="TWRF file to record into",
)  # fmt: skip
@click.pass_obj
def drive_flux(app: AppContext, motion: str, seconds: float, out: Path) -> None:
    """Run the tape for --seconds and record whatever comes off the head.

    A diagnostic for drives that won't reference a tape: is there any signal at
    all? Prints the amount of flux, a histogram of transition spacing, and how
    many sectors decode. See tapewyrm/tape/fluxprobe.py for how to read it.
    """
    from tapewyrm.tape.fluxprobe import format_report, probe

    with _drive_session(app) as d, app.progress() as prog:
        report = probe(d, motion, seconds, out, progress=prog)
        click.echo(f"status    : {_fmt_status(d.status())}")
    for line in format_report(report):
        click.echo(line)


# Select Rate or Format (27) argument N for each data rate (QIC-117 Rev J
# Table 2b). N = 0 means 250 kbps only on drives that can't do QIC-3020; on a
# QIC-3020 drive it means 4 Mbps instead, so 250 is refused rather than guessed.
_RATE_ARG = {500: 2, 1000: 3, 2000: 1}


@drive.command("rate")
@click.argument("kbps", type=click.Choice([str(k) for k in _RATE_ARG]))
@click.pass_obj
def drive_rate(app: AppContext, kbps: str) -> None:
    """Select the data rate (command 27), then confirm it from the config report.

    The drive reports the rate it will use for Logical Forward in Report Drive
    Configuration; Rev J says to check that after a Select Rate, so this does.
    A drive that can't change rate latches error 31 (Rate or Format Selection
    Error), shown in the status line.
    """
    from tapewyrm.qic117 import commands

    with _drive_session(app) as d:
        d.command(commands.TABLE["SELECT_RATE_OR_FORMAT"], arg=_RATE_ARG[int(kbps)])
        st = d.status()
        click.echo(f"status : {_fmt_status(st)}")
        if st.error and d.last_error is not None:
            click.echo(f"error  : {_decode_error(d.last_error.raw)}")
        config = d.report(commands.REPORT_DRIVE_CONFIGURATION, 8)
        click.echo(f"config : 0x{config:02x} -> {_decode_config(config)}")


# Select Format shares command 27 with Select Rate (Rev E and later drives):
# N = tape format x 4 + increment, increment 1 = "standard" (Rev J Table 2b).
_FORMAT_ARG = {"qic40": 1 * 4 + 1, "qic80": 2 * 4 + 1, "qic3020": 3 * 4 + 1, "qic3010": 4 * 4 + 1}


@drive.command("format")
@click.argument("fmt", metavar="FORMAT", type=click.Choice(list(_FORMAT_ARG)))
@click.pass_obj
def drive_format(app: AppContext, fmt: str) -> None:
    """Select the tape format the drive works in (command 27). Writes nothing.

    Rev J ties the selected format to formatting (load-zone layout, segment
    counts). It is also our probe for whether a multi-format drive such as the
    Colorado 1400 (QIC-3010) needs telling before it will reference an older
    QIC-80 tape. Older drives latch error 31 (Rate or Format Selection Error).
    """
    from tapewyrm.qic117 import commands

    with _drive_session(app) as d:
        d.command(commands.TABLE["SELECT_RATE_OR_FORMAT"], arg=_FORMAT_ARG[fmt])
        st = d.status()
        click.echo(f"status : {_fmt_status(st)}")
        if st.error and d.last_error is not None:
            click.echo(f"error  : {_decode_error(d.last_error.raw)}")
        config = d.report(commands.REPORT_DRIVE_CONFIGURATION, 8)
        click.echo(f"config : 0x{config:02x} -> {_decode_config(config)}")


@drive.command("micro")
@click.argument("direction", type=click.Choice(["up", "down"]))
@click.pass_obj
def drive_micro(app: AppContext, direction: str) -> None:
    """Micro-step the head up or down (re-seek the track to recentre)."""
    from tapewyrm.qic117 import commands

    name = "MICRO_STEP_HEAD_UP" if direction == "up" else "MICRO_STEP_HEAD_DOWN"
    with _drive_session(app) as d:
        click.echo(f"status : {_fmt_status(d.command(commands.TABLE[name]))}")


@drive.command("scope")
@click.option("--pulses", default=0, type=click.IntRange(0, 255), help="STEP pulses first")
@click.option("--ms", default=300, type=click.IntRange(1, 10_000), show_default=True)
@click.pass_obj
def drive_scope(app: AppContext, pulses: int, ms: int) -> None:
    """Edge-log TRK0/INDEX/WRPROT/pin34 (after optional pulses). Bench probe.

    A selected, ready drive shows cue INDEX pulses every few ms.
    """
    from tapewyrm.link.device import ScopeTrace

    with _drive_session(app) as d:
        tr = d.link.scope(pulses, ms)
        active = {k: v for k, v in tr.counts.items() if v}
        click.echo(
            f"initial={ScopeTrace.describe(tr.initial)} edges={sum(tr.counts.values())} "
            f"by line={active or '{}'}"
        )
        for t_us, state in tr.edges:
            click.echo(f"  {t_us / 1000:9.3f} ms  {ScopeTrace.describe(state)}")
        if tr.overflow:
            click.echo(f"  ... (edge log holds {len(tr.edges)}; totals above are complete)")
