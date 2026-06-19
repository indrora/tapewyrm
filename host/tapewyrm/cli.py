"""Tapewyrm CLI (DESIGN.md §6A.7).

A single ``@click.group()`` named ``cli`` with shared ``--port`` / ``--profile``
/ ``--config`` options carried on ``ctx.obj`` as an ``AppContext``. Five verbs,
each mapping to a layer:

    probe    open device, wake, identify; print config / tape status / geometry
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
