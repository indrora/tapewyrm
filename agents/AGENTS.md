# Notes for coding assistants

This directory holds material for people and assistants changing the code. It
is not published on the documentation site.

## Read first

1. `STYLE.md` at the repository root: coding, commit and documentation
   conventions. Section 4 says where documentation goes and how to write it.
2. `agents/DESIGN.md`: the design record, with the reasons behind the
   architecture. Parts of it describe earlier plans (module names, commands)
   and are out of date. Where it disagrees with the code, the code is right.
   Code comments cite it as `DESIGN.md §N`.

## Where things live

| You need | Look in |
|---|---|
| How a command behaves for users | `docs/tw/`, `docs/qicsilver/`, `docs/cinnabar/` |
| A drive's quirks | `docs/hardware/drives/` |
| An on-tape layout (header, volume table, QIC-113) | `docs/formats/` and the PDFs in `docs/qic-standards/` |
| Package structure and extension points | `docs/dev/` |
| Tapewyrm's own file formats (TWRF, TWTI, TWVL) | `docs/spec/` (normative) |

## Keeping the docs current

- Changed a command's options or output? Update its page under `docs/tw/` or
  `docs/qicsilver/` in the same change.
- Found a drive quirk on the bench? Add it to the drive's `## Quirks` section.
- Changed an on-disk format? Update the TWS spec in `docs/spec/` and bump its
  revision.
- Added a page? Add it to `nav` in `mkdocs.yml`; `just docs-build` fails on a
  page missing from the nav or a broken link.
