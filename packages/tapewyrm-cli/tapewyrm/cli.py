"""Tapewyrm CLI (DESIGN.md §6A.7).

A single ``@click.group()`` named ``cli`` with shared ``--port`` / ``--profile``
/ ``--config`` options carried on ``ctx.obj`` as an ``AppContext``.

The recovery workflow is three steps, each with its own file format:

    dump     tape tracks -> TWRF flux captures (self-describing: rate, drive identity)
    convert  TWRF captures -> TWTI logical tape image (sectors placed, RS-corrected;
             several dumps of the same tape are merged)
    extract  TWTI image -> TWVL volume files (QIC-113 volume table, QIC-122
             decompression, holes recorded); contrib/qic2tar.py makes a tar

plus the hardware side:

    info     host + firmware build identity, board, port and USB serial
    drive    poke the drive by hand: select, status, reports, motion, scope
    flash    update Tapewyrm firmware (app bootloader over USB)
    dfu      recovery/first flash via the AT32 ROM bootloader (dfu-util)

Codec imports happen inside the commands, so ``tw --help`` stays fast.

Config precedence: CLI flags -> config file -> profile defaults, resolved once in
``AppContext.load`` and carried on ``ctx.obj``.

Output channels (STYLE.md §2.5): command results -- status lines, summaries,
``--json`` -- go to **stdout** via ``click.echo``. The library's narrative goes
through :mod:`logging` and, like ``--progress`` bars, to **stderr** through one
shared rich console (:mod:`tapewyrm.console`), so stdout stays pipeable.
``click`` here is ``rich_click``: the same API, with rich-rendered help and
errors.
"""

from __future__ import annotations

import logging
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import rich_click as click
from rich.console import Console
from tapewyrm_archive.progress import Progress

from tapewyrm.console import make_console, progress_display, setup_logging
from tapewyrm.qic117.profile import load_profile
from tapewyrm.types import DriveProfile

log = logging.getLogger(__name__)


@dataclass
class AppContext:
    """Resolved run context shared across subcommands (DESIGN.md §6A.7)."""

    port: str | None
    profile_name: str
    profile: DriveProfile
    passes: int = 1
    out_dir: Path | None = None
    settings: dict[str, Any] = field(default_factory=dict)
    # stderr console shared by logging and the progress bars (tapewyrm.console)
    console: Console = field(default_factory=make_console)
    show_progress: bool = False

    @contextmanager
    def progress(self) -> Iterator[Progress]:
        """Live progress bars for one command if ``--progress``, else a no-op."""
        with progress_display(self.console, self.show_progress) as prog:
            yield prog

    @classmethod
    def load(
        cls,
        port: str | None,
        profile: str | None,
        config: str | None,
    ) -> AppContext:
        """Resolve config precedence CLI -> file -> profile defaults.

        A value set on the CLI wins; otherwise the config file supplies it;
        otherwise the profile / built-in defaults apply.
        """
        file_settings: dict[str, Any] = {}
        # Note: this runs before setup_logging(), so these debug lines only
        # show if logging was configured by something else first.
        if config is not None:
            cfg_path = Path(config)
            if cfg_path.exists():
                log.debug("loading config %s", cfg_path)
                with cfg_path.open("rb") as f:
                    file_settings = tomllib.load(f)
            else:
                log.debug("config %s does not exist; using CLI/profile defaults", cfg_path)

        # Precedence for each resolvable setting.
        resolved_port = port if port is not None else file_settings.get("port")
        resolved_profile_name = (
            profile if profile is not None else file_settings.get("profile", "default")
        )
        log.debug("loading drive profile %r", resolved_profile_name)
        try:
            prof = load_profile(resolved_profile_name)
        except Exception as exc:  # ProfileError or IO — surface as a CLI error
            log.debug("profile %r failed to load: %r", resolved_profile_name, exc)
            raise click.ClickException(
                f"could not load profile {resolved_profile_name!r}: {exc}"
            ) from exc

        passes = int(file_settings.get("passes", 1))
        out_dir = file_settings.get("out_dir")
        return cls(
            port=resolved_port,
            profile_name=resolved_profile_name,
            profile=prof,
            passes=passes,
            out_dir=Path(out_dir) if out_dir else None,
            settings=file_settings,
        )


# ---------------------------------------------------------------------------
# Group + shared options
# ---------------------------------------------------------------------------


@click.group()
@click.version_option(package_name="tapewyrm", prog_name="tw")
@click.option("--port", default=None, help="GW serial port (autodetect if unset)")
@click.option("--profile", default=None, help="drive profile name or path")
@click.option("--config", type=click.Path(), default=None, help="config TOML file")
@click.option("--progress", "show_progress", is_flag=True, help="show progress bars (stderr)")
@click.option("-v", "--verbose", count=True, help="more log output (-v for debug)")
@click.option("-q", "--quiet", count=True, help="less log output (-q warnings, -qq errors)")
@click.pass_context
def cli(
    ctx: click.Context,
    port: str | None,
    profile: str | None,
    config: str | None,
    show_progress: bool,
    verbose: int,
    quiet: int,
) -> None:
    """tw — Tapewyrm: QIC-80 floppy-tape recovery over Greaseweazle v4.1.

    The single Tapewyrm tool: capture, decode, recover, and flash firmware.
    Does not require the ``gw`` executable.
    """
    app = AppContext.load(port, profile, config)
    app.show_progress = show_progress
    setup_logging(app.console, verbose, quiet)
    log.debug(
        "context: port=%r profile=%r passes=%d out_dir=%s progress=%s",
        app.port,
        app.profile_name,
        app.passes,
        app.out_dir,
        show_progress,
    )
    ctx.obj = app


# ---------------------------------------------------------------------------
# `tw drive ...` -- hand-driven drive control (bench / bring-up)
# ---------------------------------------------------------------------------

# Report commands and their payload widths in bits (QIC-117 Rev J Table 2c).
_REPORT_BITS = {6: 8, 7: 16, 8: 8, 9: 8, 32: 16, 33: 8, 37: 16}


@contextmanager
def _drive_session(
    app: AppContext, *, wake: bool = True, profile: DriveProfile | None = None
) -> Iterator[Any]:
    """Open the link, wake the drive with the profile, yield a Qic117Drive.

    Every ``tw drive`` command is one short session. On the way out we always
    release the GW drive-select lines and the port; commands that move tape
    stop it themselves before returning (``Qic117Drive.jog``) or wait for Ready.
    A phantom-selected drive stays selected across sessions (it ignores the DS
    lines) until ``tw drive deselect``, a reset or a power cycle.
    ``wake=False`` skips the profile's wake sequence (for select/deselect).
    """
    from tapewyrm.link.device import DeviceLink, LinkError
    from tapewyrm.qic117.drive import DriveError, Qic117Drive

    link = DeviceLink()
    try:
        log.debug("opening link on port %r", app.port)
        link.open(app.port)
        log.debug("releasing drive-select lines before wake")
        link.deselect()  # phantom drives want every DS line idle
        drive = Qic117Drive(link, profile or app.profile)
        if wake:
            log.debug("waking drive with profile %r", (profile or app.profile).name)
            drive.wake()
        else:
            log.debug("skipping wake sequence (wake=False)")
        yield drive
    except (LinkError, DriveError) as exc:
        log.debug("drive session failed: %r", exc)
        raise click.ClickException(str(exc)) from exc
    finally:
        log.debug("closing drive session: deselect and close link")
        try:
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


@cli.group()
def drive() -> None:
    """Poke the tape drive by hand: status, reports, motion, scope.

    Read-only: commands that write to tape are refused by the drive layer.
    Wakes the drive with --profile first (e.g. --profile colorado).
    """


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
    profile = _with_phantom_unit(app.profile, unit) if unit is not None else app.profile
    with _drive_session(app, profile=profile) as d:
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


def _fmt_commit(commit: str | None, dirty: bool) -> str:
    if not commit:
        return "commit unknown"
    return f"commit {commit[:12]}" + (" (+uncommitted changes)" if dirty else "")


@cli.command()
@click.pass_obj
def info(app: AppContext) -> None:
    """Which tw and firmware builds are in play, and which hardware.

    Talks to the Greaseweazle only (never the drive), and describes stock GW
    firmware rather than refusing it.
    """
    from tapewyrm.buildinfo import host_build
    from tapewyrm.link.device import DeviceLink, LinkError

    hb = host_build()
    click.echo(f"tw        : {hb.version}, {_fmt_commit(hb.commit, hb.dirty)} [{hb.source}]")

    link = DeviceLink()
    try:
        log.debug("opening link on port %r (ungated) for info", app.port)
        dev = link.open(app.port, gate=False)
        if not dev.proto_ver:
            log.debug("no Tapewyrm protocol version; skipping BUILD_INFO")
        fw = link.build_info() if dev.proto_ver else None
    except LinkError as exc:
        log.debug("info: link failed: %r", exc)
        raise click.ClickException(str(exc)) from exc
    finally:
        link.close()

    click.echo(f"device    : {dev.model}")
    serial = f" (USB serial {dev.serial})" if dev.serial else ""
    click.echo(f"port      : {dev.port or app.port or '?'}{serial}")
    click.echo(f"mcu       : {dev.mcu}")
    if not dev.proto_ver:
        click.echo(f"firmware  : {dev.firmware}, stock Greaseweazle (no Tapewyrm verbs)")
        return
    built = _fmt_commit(fw.commit, fw.dirty) if fw else "commit unknown (pre-BUILD_INFO image)"
    caps = ", ".join(sorted(dev.qic_caps)) or "none"
    click.echo(f"firmware  : {dev.firmware}, {built}, protocol v{dev.proto_ver} [{caps}]")
    if fw and fw.commit and hb.commit and fw.commit != hb.commit:
        click.echo("note      : tw and firmware were built from different commits")


def _parse_tracks(spec: str) -> list[int]:
    """'0-12' or '0,3,5-7' -> sorted unique track numbers (each 0..63)."""
    out: set[int] = set()
    for part in spec.split(","):
        a, _, b = part.strip().partition("-")
        lo, hi = int(a), int(b or a)
        if not 0 <= lo <= hi <= 63:
            log.debug("track range %r -> %d..%d outside 0..63 or reversed; refusing", part, lo, hi)
            raise click.BadParameter(f"bad track range {part!r}", param_hint="--tracks")
        out.update(range(lo, hi + 1))
    return sorted(out)


@cli.command()
@click.option("--tracks", required=True, help="tracks to capture, e.g. 0-12 or 0,2,5-7")
@click.option(
    "--out", "out", type=click.Path(file_okay=False), required=True, help="output directory"
)
@click.option(
    "--check", is_flag=True, help="also decode each pass and stop if < 80% of sectors CRC-clean"
)
@click.pass_obj
def dump(app: AppContext, tracks: str, out: str, check: bool) -> None:
    """Capture whole tracks to TWRF files, one Logical Forward pass each.

    Each pass winds to its track's starting end, then ends when the tape stops
    at logical EOT. Dump only reads transitions; `tw convert` judges the data.
    After every pass it checks, without decoding, that the pass ended cleanly
    and the drive found as many segments as before, and stops early if the
    tape looks unhealthy. --check also decodes and checks sector CRCs.
    Serpentine order: run tracks in ascending order to avoid rewinds.
    """
    from tapewyrm.tape.dump import DumpStopped, dump_tracks

    track_list = _parse_tracks(tracks)
    log.debug("dumping tracks %s to %s (check=%s)", track_list, out, check)
    with _drive_session(app) as d, app.progress() as prog:
        try:
            results = dump_tracks(d, track_list, Path(out), progress=prog, check=check)
        except DumpStopped as exc:
            log.debug("dump stopped: %r", exc)
            raise click.ClickException(f"dump stopped: {exc}") from exc
    segments = sum(r.index_pulses for r in results)
    line = f"done: {len(results)} tracks, {segments} segments by INDEX"
    if check:
        good = sum(r.good or 0 for r in results)
        total = sum(r.sectors or 0 for r in results)
        line += f", {good}/{total} sectors CRC-clean"
    click.echo(f"{line} -> {out}")


# ---------------------------------------------------------------------------
# Firmware flashing (tw owns this — no dependency on the `gw` tool, DESIGN §12.3)
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("sources", nargs=-1, required=True, type=click.Path(exists=True, path_type=Path))
@click.option(
    "-o", "--out", "out", required=True, type=click.Path(dir_okay=False, path_type=Path),
    help="tape image to write (.twti)",
)  # fmt: skip
@click.pass_obj
def convert(app: AppContext, sources: tuple[Path, ...], out: Path) -> None:
    """TWRF dump(s) -> TWTI logical tape image.

    SOURCES are dump directories (or individual track-NN.twrf files). Every
    capture is decoded at its own recorded bit rate; when several dumps of the
    same tape are given, their sectors are merged so a re-read fills the gaps
    of an earlier pass. No hardware needed.
    """
    from tapewyrm.image.convert import convert as do_convert

    log.debug("converting %d sources to %s", len(sources), out)
    try:
        with app.progress() as prog:
            do_convert(list(sources), out, progress=prog)
    except ValueError as exc:
        log.debug("convert failed: %r", exc)
        raise click.ClickException(str(exc)) from exc


@cli.command()
@click.argument("image", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "-o", "--out", "out", required=True, type=click.Path(file_okay=False, path_type=Path),
    help="directory for the volume files (vol-NN.twvl)",
)  # fmt: skip
@click.option(
    "--tape-profile", default="guess", show_default=True,
    help="volume-table layout: a profile name or path, or 'guess' (as tw identify)",
)  # fmt: skip
@click.pass_obj
def extract(app: AppContext, image: Path, out: Path, tape_profile: str) -> None:
    """TWTI tape image -> one TWVL file per backup volume.

    Reads the volume table through the tape profile `tw identify` would pick,
    decompresses QIC-122 data and lays each volume out by its QIC-113 offsets,
    recording the byte ranges that were lost. Turn a volume into a tar with
    contrib/qic2tar.py.
    """
    from qiclib.extract import extract as do_extract
    from qiclib.tape_profile import TapeProfileError

    log.debug("extracting %s to %s (tape profile %r)", image, out, tape_profile)
    try:
        with app.progress() as prog:
            do_extract(image, out, tape_profile=tape_profile, progress=prog)
    except (ValueError, TapeProfileError) as exc:
        log.debug("extract failed: %r", exc)
        raise click.ClickException(str(exc)) from exc


@cli.command()
@click.argument("source", type=click.Path(exists=True, path_type=Path))
@click.option("--json", "as_json", is_flag=True, help="print machine-readable JSON")
@click.option(
    "--tape-profile", default="guess", show_default=True,
    help="volume-table layout: a profile name or path, or 'guess' to try them all",
)  # fmt: skip
# Not -v/--verbose: that is the global log-level flag on `tw` itself.
@click.option("--raw", is_flag=True, help="also dump raw records and profile scoring")
@click.pass_obj
def identify(app: AppContext, source: Path, as_json: bool, tape_profile: str, raw: bool) -> None:
    """What is on a tape: cartridge, factory stamp, dates, bad sectors, volumes.

    SOURCE is a TWTI image, a TWRF (or legacy .raw) capture, or a dump
    directory. Only the header segment and the volume table at the start of
    track 0 are corrected, so a short capture of BOT is enough.
    """
    import json

    from qiclib.identify import format_info, to_dict
    from qiclib.tape_profile import TapeProfileError

    from tapewyrm.image.identify import identify

    log.debug("identifying %s (tape profile %r)", source, tape_profile)
    try:
        with app.progress() as prog:
            info = identify(source, tape_profile=tape_profile, progress=prog)
    except (ValueError, TapeProfileError) as exc:
        log.debug("identify failed: %r", exc)
        raise click.ClickException(str(exc)) from exc
    if as_json:
        click.echo(json.dumps(to_dict(info), indent=1))
    else:
        for line in format_info(info, verbose=raw):
            click.echo(line)


@cli.command()
@click.argument("image", type=click.Path(exists=True))
@click.option("--dfu", "use_dfu", is_flag=True, help="flash via DFU instead of the app bootloader")
@click.pass_obj
def flash(app: AppContext, image: str, use_dfu: bool) -> None:
    """Update Tapewyrm firmware via the GW-compatible application bootloader.

    Pass --dfu to route to the recovery DFU path instead (same as `tw dfu`).
    """
    from tapewyrm.link.update import FlashError, app_update, run_dfu  # noqa: PLC0415

    try:
        if use_dfu:
            log.debug("flashing %s via DFU", image)
            run_dfu(image)
        else:
            log.debug("flashing %s via app bootloader on port %r", image, app.port)
            app_update(image, port=app.port)
        click.echo(f"flashed {image}")
    except FlashError as exc:
        log.debug("flash failed: %r", exc)
        raise click.ClickException(str(exc)) from exc


@cli.command()
@click.argument("image", type=click.Path(exists=True))
@click.option("--dfu-util", default="dfu-util", help="path to the dfu-util binary")
@click.option("--device", "vid_pid", default=None, help="USB VID:PID (e.g. 2e3c:df11)")
@click.option("--alt", default=0, type=int, help="DFU alternate interface")
@click.pass_obj
def dfu(app: AppContext, image: str, dfu_util: str, vid_pid: str | None, alt: int) -> None:
    """Recovery / first flash via the AT32 ROM bootloader (strap the DFU header)."""
    from tapewyrm.link.update import FlashError, run_dfu  # noqa: PLC0415

    log.debug("running %s for %s (device %r, alt %d)", dfu_util, image, vid_pid, alt)
    try:
        run_dfu(image, dfu_util=dfu_util, vid_pid=vid_pid, alt=alt)
        click.echo(f"flashed {image} via DFU")
    except FlashError as exc:
        log.debug("dfu failed: %r", exc)
        raise click.ClickException(str(exc)) from exc


if __name__ == "__main__":  # pragma: no cover
    cli()
