"""``tw update`` and ``tw dfu``: Tapewyrm firmware updates (DESIGN.md §12.3).

tw owns flashing -- no dependency on the ``gw`` tool. ``tw update`` speaks the
Greaseweazle-compatible application-bootloader protocol itself (the same bytes
``gw update`` sends; see link/update.py); ``tw dfu`` is the recovery path.
"""

from __future__ import annotations

import logging

import rich_click as click

from tapewyrm.cli.app import AppContext, cli

log = logging.getLogger(__name__)


@cli.command()
@click.argument("image", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--bootloader",
    is_flag=True,
    help="rewrite the bootloader instead of the main firmware (needs a .upd; "
    "if it goes wrong, recover with `tw dfu`)",
)
@click.option("--dfu", "use_dfu", is_flag=True, help="flash via DFU instead (same as `tw dfu`)")
@click.pass_obj
def update(app: AppContext, image: str, bootloader: bool, use_dfu: bool) -> None:
    """Update the board's firmware over USB through its Greaseweazle-compatible bootloader.

    IMAGE is a .upd update file (from `just fw-dist`) or the raw application
    .bin that PlatformIO builds. The board is rebooted into its bootloader,
    rewritten, verified by CRC and rebooted back into the new firmware.
    """
    from tapewyrm.link.update import FlashError, app_update, run_dfu  # noqa: PLC0415

    try:
        if use_dfu:
            if bootloader:
                log.debug("--dfu with --bootloader; refusing")
                raise click.UsageError("--dfu flashes the application .bin; drop --bootloader")
            log.debug("flashing %s via DFU", image)
            run_dfu(image)
            click.echo(f"flashed {image} via DFU")
            return
        log.debug("updating from %s on port %r (bootloader=%s)", image, app.port, bootloader)
        with app.progress() as prog:
            result = app_update(image, port=app.port, bootloader=bootloader, progress=prog)
    except FlashError as exc:
        log.debug("update failed: %r", exc)
        raise click.ClickException(str(exc)) from exc

    click.echo(f"wrote {result.entry.describe()}")
    if result.after is None:
        click.echo("left in the bootloader: remove the update strap and power-cycle the board")
    elif bootloader:
        click.echo(f"bootloader updated; main firmware v{result.after.version} still running")
    else:
        click.echo(f"board now running main firmware v{result.after.version}")


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
