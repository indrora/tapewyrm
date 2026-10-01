"""Tapewyrm CLI (DESIGN.md §6A.7).

A single ``@click.group()`` named ``cli`` with shared ``--port`` / ``--profile``
/ ``--config`` options carried on ``ctx.obj`` as an ``AppContext``. Five verbs,
each mapping to a layer:

    info     host + firmware build identity, board, port and USB serial
    probe    open device, wake, identify; print config / tape status / geometry
    drive    poke the drive by hand: status, reports, motion, scope (read-only)
    capture  sweep tracks -> write RawFluxCapture files
    decode   RawFluxCapture(s) -> files + recovery report (no hardware)
    recover  capture + decode + multi-pass retries on weak segments
    replay   re-decode saved flux with different options
    flash    update Tapewyrm firmware (app bootloader over USB)
    dfu      recovery/first flash via the AT32 ROM bootloader (dfu-util)

The shipped command is ``tw`` (DESIGN.md §1): it owns all functionality —
capture, decode, AND firmware flashing — so the ``gw`` tool is never required.

The codec is being written concurrently, so ``decode`` / ``recover`` / ``replay``
import it **lazily inside the function body** — this module imports cleanly even
while ``tapewyrm.codec`` is incomplete (DESIGN.md §6A.7).

Config precedence: CLI flags -> config file -> profile defaults, resolved once in
``AppContext.load`` and carried on ``ctx.obj``.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import click

from tapewyrm.qic117.profile import load_profile
from tapewyrm.types import DriveProfile


@dataclass
class AppContext:
    """Resolved run context shared across subcommands (DESIGN.md §6A.7)."""

    port: str | None
    profile_name: str
    profile: DriveProfile
    passes: int = 1
    out_dir: Path | None = None
    settings: dict[str, Any] = field(default_factory=dict)

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
        if config is not None:
            cfg_path = Path(config)
            if cfg_path.exists():
                with cfg_path.open("rb") as f:
                    file_settings = tomllib.load(f)

        # Precedence for each resolvable setting.
        resolved_port = port if port is not None else file_settings.get("port")
        resolved_profile_name = (
            profile if profile is not None else file_settings.get("profile", "default")
        )
        try:
            prof = load_profile(resolved_profile_name)
        except Exception as exc:  # ProfileError or IO — surface as a CLI error
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
@click.pass_context
def cli(ctx: click.Context, port: str | None, profile: str | None, config: str | None) -> None:
    """tw — Tapewyrm: QIC-80 floppy-tape recovery over Greaseweazle v4.1.

    The single Tapewyrm tool: capture, decode, recover, and flash firmware.
    Does not require the ``gw`` executable.
    """
    ctx.obj = AppContext.load(port, profile, config)


# ---------------------------------------------------------------------------
# Device-backed verbs (link + qic117 + tape)
# ---------------------------------------------------------------------------


def _open_stack(app: AppContext):
    """Open the link and build a TapeTransport. Returns (link, transport)."""
    from tapewyrm.link.device import DeviceLink
    from tapewyrm.qic117.drive import Qic117Drive
    from tapewyrm.tape.transport import TapeTransport

    link = DeviceLink()
    link.open(app.port)
    drive = Qic117Drive(link, app.profile)
    return link, TapeTransport(drive)


# ---------------------------------------------------------------------------
# `tw drive ...` -- hand-driven drive control (bench / bring-up)
# ---------------------------------------------------------------------------

# Report commands and their payload widths in bits (QIC-117 Rev J Table 2c).
_REPORT_BITS = {6: 8, 7: 16, 8: 8, 9: 8, 32: 16, 33: 8, 37: 16}


@contextmanager
def _drive_session(app: AppContext) -> Iterator[Any]:
    """Open the link, wake the drive with the profile, yield a Qic117Drive.

    Every ``tw drive`` command is one short session. On the way out we always
    release the drive select and the port; commands that move tape stop it
    themselves before returning (``Qic117Drive.jog``) or wait for Ready.
    """
    from tapewyrm.link.device import DeviceLink, LinkError
    from tapewyrm.qic117.drive import DriveError, Qic117Drive

    link = DeviceLink()
    try:
        link.open(app.port)
        link.deselect()  # phantom drives want every DS line idle
        drive = Qic117Drive(link, app.profile)
        drive.wake()
        yield drive
    except (LinkError, DriveError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        try:
            link.deselect()
        except Exception:
            pass
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
    from tapewyrm.types import DriveConfig

    cfg = DriveConfig.decode(b)
    rate = f"{cfg.rate_kbps} kbps" + (" (or 4 Mbps)" if cfg.rate_ambiguous else "")
    parts = [rate, "QIC-80 mode" if cfg.qic80_mode else "QIC-40 mode"]
    if cfg.extra_length:
        parts.append("extra-length tape")
    return ", ".join(parts)


def _decode_rom(b: int) -> str:
    return f"version {b & 0x7F}" + (" (BETA)" if b & 0x80 else "")


def _decode_vendor(w: int) -> str:
    from tapewyrm.qic117.status import LEGACY_VENDOR_IDS, decode_vendor_id

    make, model, name = decode_vendor_id(w)
    if w in LEGACY_VENDOR_IDS:
        return name
    return f"make {make} {name}, model {model}"


def _decode_tape(b: int) -> str:
    from tapewyrm.qic117.status import TAPE_TYPES
    from tapewyrm.types import TapeStatus

    ts = TapeStatus.decode(b)
    kind = TAPE_TYPES.get(ts.tape_type, f"reserved type {ts.tape_type}")
    return f"format {ts.format.name}, {kind} tape" + (", wide (8mm)" if ts.wide else "")


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
            except LinkError:
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
        dev = link.open(app.port, gate=False)
        fw = link.build_info() if dev.proto_ver else None
    except LinkError as exc:
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


@cli.command()
@click.pass_obj
def probe(app: AppContext) -> None:
    """Open device, wake, identify; print config / tape status / geometry."""
    link, transport = _open_stack(app)
    try:
        info = link.info
        if info is not None:
            click.echo(
                f"device: {info.model} ({info.mcu}) fw={info.firmware} sram={info.sram_bytes}"
            )
            click.echo(f"caps: {sorted(info.qic_caps)} proto_ver={info.proto_ver}")
        cfg, tape, geom = transport.identify()
        click.echo(
            f"config: rate={cfg.rate_kbps} kbps"
            + (" (ambiguous 4M/250k)" if cfg.rate_ambiguous else "")
        )
        click.echo(f"tape: format={tape.format.name} type={tape.tape_type} wide={tape.wide}")
        click.echo(
            f"geometry: {geom.tracks} tracks x {geom.segments_per_track} segs/track "
            f"= {geom.total_segments()} segments"
        )
    finally:
        link.close()


@cli.command()
@click.option("--passes", default=None, type=int, help="passes per track")
@click.option("-o", "--out", "out", type=click.Path(), required=True, help="output directory")
@click.pass_obj
def capture(app: AppContext, passes: int | None, out: str) -> None:
    """Sweep tracks -> write RawFluxCapture files."""
    n_passes = passes if passes is not None else app.passes
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)

    link, transport = _open_stack(app)
    try:
        transport.identify()
        count = 0
        for cap in transport.walk_all(passes=n_passes):
            hdr = cap.header
            name = f"track{hdr.track:02d}_pass{hdr.pass_id}.twrf"
            path = out_dir / name
            cap.save(path)
            count += 1
            click.echo(f"wrote {path} ({len(cap.flux)} flux bytes)")
        click.echo(f"captured {count} pass(es) to {out_dir}")
    finally:
        link.close()


# ---------------------------------------------------------------------------
# Codec-backed verbs (lazy codec import — it is being written concurrently)
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("inputs", nargs=-1, type=click.Path(exists=True), required=True)
@click.option("-o", "--out", "out", type=click.Path(), required=True, help="output directory")
@click.pass_obj
def decode(app: AppContext, inputs: tuple[str, ...], out: str) -> None:
    """Decode flux file(s) -> recovered files + recovery report (no hardware)."""
    # Lazy import: codec may be incomplete while this module must still load.
    from tapewyrm.codec import pipeline  # noqa: PLC0415
    from tapewyrm.rawflux import RawFluxCapture
    from tapewyrm.report import print_report

    caps = [RawFluxCapture.load(p) for p in inputs]
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)

    filesets, report = pipeline.decode(caps)
    _write_filesets(filesets, out_dir)
    print_report(report)


@cli.command()
@click.option("--passes", default=None, type=int, help="passes per track")
@click.option("-o", "--out", "out", type=click.Path(), required=True, help="output directory")
@click.pass_obj
def recover(app: AppContext, passes: int | None, out: str) -> None:
    """Capture + decode + multi-pass retries on weak segments (all layers)."""
    from tapewyrm.codec import pipeline  # noqa: PLC0415
    from tapewyrm.report import print_report

    n_passes = passes if passes is not None else app.passes
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)

    link, transport = _open_stack(app)
    try:
        transport.identify()
        caps = list(transport.walk_all(passes=n_passes))
        for cap in caps:
            cap.save(out_dir / f"track{cap.header.track:02d}_pass{cap.header.pass_id}.twrf")
    finally:
        link.close()

    filesets, report = pipeline.decode(caps)
    _write_filesets(filesets, out_dir)
    print_report(report)


@cli.command()
@click.argument("inputs", nargs=-1, type=click.Path(exists=True), required=True)
@click.option("-o", "--out", "out", type=click.Path(), required=True, help="output directory")
@click.pass_obj
def replay(app: AppContext, inputs: tuple[str, ...], out: str) -> None:
    """Re-decode saved flux with (potentially) different PLL/RS options."""
    from tapewyrm.codec import pipeline  # noqa: PLC0415
    from tapewyrm.rawflux import RawFluxCapture
    from tapewyrm.report import print_report

    caps = [RawFluxCapture.load(p) for p in inputs]
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)

    filesets, report = pipeline.decode(caps)
    _write_filesets(filesets, out_dir)
    print_report(report)


# ---------------------------------------------------------------------------
# Firmware flashing (tw owns this — no dependency on the `gw` tool, DESIGN §12.3)
# ---------------------------------------------------------------------------


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
            run_dfu(image)
        else:
            app_update(image, port=app.port)
        click.echo(f"flashed {image}")
    except FlashError as exc:
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

    try:
        run_dfu(image, dfu_util=dfu_util, vid_pid=vid_pid, alt=alt)
        click.echo(f"flashed {image} via DFU")
    except FlashError as exc:
        raise click.ClickException(str(exc)) from exc


def _write_filesets(filesets: Any, out_dir: Path) -> None:
    """Write recovered file sets to disk (best-effort; codec dataclasses)."""
    for fs in filesets:
        base = out_dir / getattr(fs, "name", "fileset").replace(":", "").replace("\\", "_")
        base.mkdir(parents=True, exist_ok=True)
        for entry in getattr(fs, "files", []):
            if getattr(entry, "is_dir", False):
                continue
            rel = str(getattr(entry, "path", "")).lstrip("/\\")
            dest = base / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(getattr(entry, "data", b""))


if __name__ == "__main__":  # pragma: no cover
    cli()
