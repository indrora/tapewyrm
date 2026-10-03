"""``tw flash`` and ``tw dfu``: Tapewyrm firmware updates (DESIGN.md §12.3).

tw owns flashing -- no dependency on the ``gw`` tool.
"""

from __future__ import annotations

import logging

import rich_click as click

from tapewyrm.cli.app import AppContext, cli

log = logging.getLogger(__name__)


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
