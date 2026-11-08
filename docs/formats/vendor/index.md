# Vendor extensions

The QIC standards fix the recording, the header and the first 57 bytes of a
volume table entry. Everything after that, and often the volume contents
too, was decided by the backup program that wrote the tape. This section
documents the vendor layouts Tapewyrm has read from real tapes.

Each layout here is encoded as a volume profile in
`packages/qiclib/qiclib/volume/profiles/`. A profile answers three questions:
which volume table fields sit where (`[vtbl]`), how wide the compression
extent fields are (`[extent]`), and which parser reads the volume
(`[fileset]`).

## Layouts

| Page | Software | Recognised by | Volume contents | Status |
|---|---|---|---|---|
| [Colorado (CMS) backup](cms.md) | Colorado Memory Systems / HP Colorado Backup | `113` at VTBL byte 58 and `CMS` at 62 | QIC-113 extended, QIC-122 compressed | Bench: QIC-80 and QIC-3010 tapes recovered |
| [MTN backup](mtn.md) | Unknown; the volume table says `MTN` | `MTN` at VTBL byte 58 | QIC-113 Basic DOS, QIC-122 compressed with 4-byte extent offsets | Bench: two QIC-80 tapes recovered |

Two more profiles, `qicstream-v2` and `novastor`, were written from another
project's reader source code and have never seen a real tape. They are
listed on [Volume table](../volume-table.md#vendor-variants).

## Adding a layout

When a tape reads as `vendor-unknown` or `qic80-rev-n` with warnings, its
software has a layout of its own. To work it out:

1. Run `qicsilver identify --json` and look at the volume table record's
   `raw_hex`.
2. Look for a mark the software wrote in bytes 57–127: a signature, a name,
   a version word. That becomes the profile's `[match]`.
3. Find the sizes and the label by their values: a directory size that fits
   the segments, a data size that fits the compression ratio, printable text.
4. Try the reading by hand with `--vtbl-field`, `--extent-offset-bytes` and
   `--fileset-parser` ([Manual read overrides](../../qicsilver/overrides.md)).
5. When it reads, write the profile ([Writing a volume profile](../../dev/volume-profiles.md)).

!!! note
    Never put file names, directory listings or file contents from a real
    tape in a profile, a test fixture or these pages. Header and volume table
    bytes are fine. See [Keep captures private](../../guide/privacy.md).

## See also

- [Volume table](../volume-table.md)
- [QIC-113 file sets](../qic-113.md)
- [Cartridge and volume profiles](../../qicsilver/profiles.md)
