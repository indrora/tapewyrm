# Tapewyrm Specifications (TWS)

The TWS series defines the files Tapewyrm writes. Each one stands alone: with
it, you can read or write the format without reading Tapewyrm's code. Where a
specification and the code disagree, that is a bug in one of them; report it.

| Number | Title | File | Status |
|---|---|---|---|
| TWS-1 | TWRF, the Tapewyrm Raw Flux Capture Format (version 2) | [twrf.md](twrf.md) | Draft |
| TWS-2 | TWTI, the Tapewyrm Tape Image format, and TWTZ, its Zstandard-compressed form | [twti.md](twti.md) | Draft |
| TWS-3 | TWVL, the Tapewyrm Volume format | [twvl.md](twvl.md) | Draft |

Where they sit in the pipeline:

```
tw dump --> TWRF (TWS-1) --> tw convert --> TWTI/TWTZ (TWS-2) --> qicsilver extract --> TWVL (TWS-3) --> qicsilver tar
```

The reference implementations are in
[`packages/tapewyrm-archive`](../../packages/tapewyrm-archive/README.md):
`tapewyrm_archive.twrf`, `tapewyrm_archive.twti` and `tapewyrm_archive.twvl`.

## Conventions

The specifications are written in Markdown and follow the structure of an IETF
RFC: a header block (number, category, status, date, license), an abstract,
numbered sections starting with an introduction and the terminology, the
format itself, and normative and informative references at the end. The key
words "MUST", "MUST NOT", "REQUIRED", "SHALL", "SHALL NOT", "SHOULD", "SHOULD NOT",
"RECOMMENDED", "NOT RECOMMENDED", "MAY" and "OPTIONAL" are used as described in
BCP 14 ([RFC 2119](https://www.rfc-editor.org/rfc/rfc2119),
[RFC 8174](https://www.rfc-editor.org/rfc/rfc8174)) when, and only when, they
appear in all capitals. Integers are little-endian unless a specification says
otherwise. QIC terms (segment, track, sector, volume table) mean what the QIC
standards in [`docs/qic-standards/`](../qic-standards/) say they mean, and each
specification cites the standard and section it relies on.
