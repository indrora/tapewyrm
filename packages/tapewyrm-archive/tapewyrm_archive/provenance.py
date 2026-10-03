"""How a header records the path of the file it was made from.

TWTI headers list their captures (``sources[].file``, TWS-2 section 4.5) and
TWVL headers name their image (``source_image``, TWS-3 section 3.1). Those
members are provenance: they tell a person which capture or image a file
came from. Recording the path exactly as given leaked the machine's layout
-- ``/Users/<name>/...`` puts a user name into every image and volume, and
those files get shared (TWS-2 section 11.1, TWS-3 section 8.2).

The rule, chosen by the project owner: record the *name of the directory the
file was in* plus the file name, and nothing above it. Capture sets and
tapes live in a directory named after the tape (``captures/jc-1998/``), so
that one directory is what identifies them; everything above it is the
user's own filesystem. It is a pure string operation on the path as given:
nothing is resolved against the working directory, so ``jc.twtz`` stays
``jc.twtz`` rather than gaining the (private) name of the current directory.

Both tapewyrm-cli (``tw convert``) and qiclib (``qicsilver extract``) depend
on this package, so the rule lives here once.
"""

from __future__ import annotations

from os import PathLike
from pathlib import Path, PurePath

# Path components that name no directory: an empty parent (a bare file name
# or the filesystem root) and the relative markers.
_NOT_A_NAME = {"", ".", ".."}


def provenance_path(path: str | PathLike[str]) -> str:
    """``dir/name`` of ``path``'s file: its directory's name and its own.

    ``/Users/x/tapes/jc.twtz`` -> ``tapes/jc.twtz``; ``jc.twtz`` -> ``jc.twtz``;
    ``../jc.twtz`` -> ``jc.twtz``. Always ``/``-separated, so a header written
    on Windows reads the same everywhere. Never absolute.
    """
    # A PurePath (e.g. PureWindowsPath in tests) keeps its own flavour; a
    # string is parsed with the host's rules, the way it was given to us.
    pure = path if isinstance(path, PurePath) else Path(path)
    parent = pure.parent.name
    if parent in _NOT_A_NAME:
        return pure.name
    return f"{parent}/{pure.name}"
