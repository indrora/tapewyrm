"""Tapewyrm CLI (DESIGN.md §6A.7).

A single ``@click.group()`` named ``cli`` with shared ``--port`` / ``--profile``
/ ``--config`` options carried on ``ctx.obj`` as an ``AppContext``.

The recovery workflow starts here with two steps, each with its own file format:

    dump     tape tracks -> TWRF flux captures (self-describing: rate, drive identity)
    convert  TWRF captures -> TWTI logical tape image (sectors placed, RS-corrected;
             several dumps of the same tape are merged)

and continues offline in **qicsilver** (packages/qicsilver): ``identify``,
``extract`` (TWTI -> TWVL volumes) and ``tar``. tw ends at the TWTI image.

plus the hardware side:

    inspect  show the header of any TWRF, TWTI/TWTZ or TWVL file (no hardware)
    info     host + firmware build identity, board, port and USB serial
    drive    poke the drive by hand: select, status, reports, motion, scope
    flash    update Tapewyrm firmware (app bootloader over USB)
    dfu      recovery/first flash via the AT32 ROM bootloader (dfu-util)

Package layout, one module per command group (STYLE.md §2.2):

    app.py       AppContext, config-file resolution and profile precedence, and
                 the root ``cli`` group with the global options (--port,
                 --profile, --config, --progress, -v/-q; logging set-up)
    session.py   _drive_session and the helpers more than one drive command
                 uses: _fmt_status and the QIC-117 report decoders
    drive.py     the ``tw drive`` group and every subcommand under it
    dump.py      ``tw dump``
    convert.py   ``tw convert``
    info.py      ``tw info``
    inspect.py   ``tw inspect`` (any TWRF/TWTI/TWTZ/TWVL header)
    firmware.py  ``tw flash`` and ``tw dfu``

Registration without import cycles: ``cli`` is defined in app.py, which
imports no command module. Each command module imports ``cli`` from app.py and
registers itself with ``@cli.command()`` / ``@cli.group()`` as a side effect
of being imported, and this file imports every command module once, *after*
app.py. ``tw = "tapewyrm.cli:cli"`` (pyproject) resolves to the re-export
below. Code that monkeypatches a name must patch the module that looks it up
(e.g. ``tapewyrm.cli.drive._drive_session`` for the drive commands).

Codec imports happen inside the commands, so ``tw --help`` stays fast.

Output channels (STYLE.md §2.5): command results -- status lines, summaries,
``--json`` -- go to **stdout** via ``click.echo``. The library's narrative goes
through :mod:`logging` and, like ``--progress`` bars, to **stderr** through one
shared rich console (:mod:`tapewyrm.console`), so stdout stays pipeable.
``click`` here is ``rich_click``: the same API, with rich-rendered help and
errors.
"""

from __future__ import annotations

from tapewyrm.cli.app import AppContext, cli, default_config_path

# Imported for their side effect: each registers its commands on ``cli``.
from tapewyrm.cli import convert, drive, dump, firmware, info, inspect  # noqa: F401  # isort: skip

__all__ = ["AppContext", "cli", "default_config_path"]
