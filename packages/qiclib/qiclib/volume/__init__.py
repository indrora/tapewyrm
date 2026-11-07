"""A volume: what one backup program wrote on the tape, read in three layers (DESIGN.md §7.8).

The header segment (:mod:`qiclib.header`) says what the *tape* is. This
package says what the tape *contains*: the volume table's 128-byte ``VTBL``
records (:mod:`~qiclib.volume.table`), each describing a volume the backup
program wrote, and that volume's bytes, laid out however the program liked.
A read goes through three layers, each configured by a volume profile
(:mod:`~qiclib.volume.profile`, one TOML file per writer under
``qiclib/volume/profiles/``):

1. **the table.** ``[vtbl]`` places the writer-specific fields of bytes
   57-127 (section sizes, source label, compression, OS type); a ``[match]``
   fingerprint of bytes the writer wrote gates the profile and a plausibility
   score ranks the survivors (:mod:`~qiclib.volume.profile`).
2. **the extents.** ``[extent]`` gives the widths of the QIC-122 extent
   headers a compressed volume's segments carry
   (:mod:`~qiclib.volume.qic122`).
3. **the file set.** ``[fileset]`` names the parser that turns the volume's
   bytes into entries and says whether the parse can be right
   (:mod:`~qiclib.volume.fileset`); the QIC-113 directory decoders behind two
   of those parsers are :mod:`~qiclib.volume.qic113` and
   :mod:`~qiclib.volume.qic113ext`.

Every stage reports in one vocabulary (:mod:`~qiclib.volume.verdict`):
``Check``s with points, a parser's ``Probe``, and the closed ``VolumeWarning``
enum that travels with the data into ``identify --json``, the TWVL header,
``inspect`` and the tar's pax headers.

Nothing is re-exported from here: import the submodule you mean
(``from qiclib.volume import profile as vp``,
``from qiclib.volume.table import VtblEntry``). :mod:`qiclib.identify` and
:mod:`qiclib.extract` are the consumers that wire the three layers together.
"""
