"""``tw inspect``: show the header of any Tapewyrm file. No hardware needed.

Works on all four formats -- TWRF captures, TWTI/TWTZ tape images and TWVL
volumes -- told apart by their magic, never their names. The reading and the
wording live in the library (``tapewyrm_archive.inspect``: :func:`inspect`
reads the preamble, header and cheap extras; :func:`describe` turns them into
titled sections of formatted rows); this module only renders.

Output channels (STYLE.md section 2.5): the description, or the ``--json``
header, is the command's *result*, so it goes to stdout and pipes cleanly;
logs stay on stderr through the shared console. The tables are drawn with a
stdout rich console made here, and use no colours of their own (bold labels
and a plain frame only), so they read the same on light and dark terminals.
"""

from __future__ import annotations

import logging
from pathlib import Path

import rich_click as click

from tapewyrm.cli.app import AppContext, cli

log = logging.getLogger(__name__)


@cli.command()
@click.argument(
    "file", metavar="FILE",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)  # fmt: skip
@click.option(
    "--json", "as_json", is_flag=True,
    help="Print the header exactly as stored in the file, instead of the summary.",
)  # fmt: skip
@click.pass_obj
def inspect(app: AppContext, file: Path, as_json: bool) -> None:
    """Show FILE's header: a TWRF capture, a TWTI/TWTZ image or a TWVL volume.

    The format is recognised by the file's magic bytes, not its name. Only the
    header is read (plus a tape image's segment table, for the segment
    counts), so this is instant even on a 1.7 GB image; a .twtz is
    decompressed only as far as its segment table.

    By default prints a readable summary: the drive's report bytes decoded,
    geometry, segment counts, volume holes. With --json, prints the stored
    JSON header byte for byte -- no re-formatting, keys in stored order --
    followed by a newline only if the stored header does not already end in
    one, so the output always ends in exactly one line break.
    """
    from tapewyrm_archive.inspect import describe
    from tapewyrm_archive.inspect import inspect as inspect_file

    log.debug("inspecting %s (json=%s)", file, as_json)
    try:
        found = inspect_file(file)
    except ValueError as exc:
        # MalformedFileError / TruncatedFileError are ValueErrors too: their
        # messages already name the file and what to do about it.
        log.debug("inspect failed: %r", exc)
        raise click.ClickException(str(exc)) from exc

    if as_json:
        text = found.header_text
        click.echo(text, nl=not text.endswith("\n"))
        return
    _render(describe(found))


def _render(sections: list) -> None:
    """Each section as a titled two-column table on stdout.

    Cells are ``Text``, not strings: header values come from the file (a
    tape name, a description) and must never be read as rich markup.
    """
    from rich import box
    from rich.console import Console
    from rich.table import Table
    from rich.text import Text

    console = Console()  # stdout: this is the command's result
    for section in sections:
        table = Table(
            title=section.title,
            title_justify="left",
            title_style="bold",
            show_header=False,
            box=box.ROUNDED,
            expand=False,
        )
        table.add_column("label", style="bold", no_wrap=True)
        table.add_column("value", overflow="fold")
        for label, value in section.rows:
            table.add_row(Text(label), Text(value))
        console.print(table)
