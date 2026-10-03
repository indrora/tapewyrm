"""``tw info``: which tw and firmware builds are in play, and which hardware."""

from __future__ import annotations

import logging

import rich_click as click

from tapewyrm.cli.app import AppContext, cli

log = logging.getLogger(__name__)


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
