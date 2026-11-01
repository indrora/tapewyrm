"""The QIC-100-MC records lane: GCR records with their pieces (Rev F section 6).

One chevron per record, labelled by type and frame, with the sketch's
pieces as children: olive preamble and postamble, green start-of-data and
record-type markers, the blue data field (split into its 64-byte system
and 4,096-byte user sub-fields, 6.6), the green ID field and CRCC. Times
come from :class:`cinnabar.decode.jobs.PlacedRecord`.
"""

from __future__ import annotations

from collections.abc import Sequence

from qiclib import qic100

from cinnabar.decode.jobs import PlacedRecord
from cinnabar.model import Shape, Span

_STYLE = {
    qic100.RecordType.DATA: "record",
    qic100.RecordType.ID: "record-id",
    qic100.RecordType.MANUFACTURER: "record-man",
    qic100.RecordType.PARITY: "record-parity",
    qic100.RecordType.FILE_SYSTEM: "record-fs",
    qic100.RecordType.USELOG: "record-uselog",
}


def record_spans(records: Sequence[PlacedRecord]) -> list[Span]:
    out: list[Span] = []
    for rec in records:
        name = qic100.record_type_name(rec.record_type)
        style = _STYLE.get(rec.record_type, "unknown") if rec.crcc_ok else "record-bad"
        fields: list[Span] = [
            Span(*rec.preamble, "Preamble", "", "preamble", Shape.CHEVRON),
            Span(*rec.sod, "SOD", "11111 11110", "mark", Shape.HEX),
            Span(*rec.type_marker, f"F{rec.record_type:X}", name, "mark", Shape.HEX),
        ]
        if rec.data is not None:
            d0, d1 = rec.data
            split = d0 + (d1 - d0) * qic100.SYSTEM_SUBFIELD_BYTES / qic100.DATA_FIELD_BYTES
            fields += [
                Span(d0, split, "system", "64 B", "data-sys", Shape.BLOCK),
                Span(split, d1, "user data", "4,096 B", "data", Shape.BLOCK),
            ]
        fields += [
            Span(*rec.id_field, "ID", f"frame {rec.frame}", "idfield", Shape.BLOCK, rec.id_detail),
            Span(
                *rec.crcc,
                "CRCC",
                "ok" if rec.crcc_ok else "BAD",
                "crc" if rec.crcc_ok else "crc-bad",
                Shape.HEX,
            ),
            Span(*rec.postamble, "Postamble", "", "postamble", Shape.CHEVRON),
        ]
        label = f"{name.capitalize()} Record"
        sub = f"frame {rec.frame}" if rec.frame is not None else ""
        if not rec.crcc_ok:
            sub = (sub + " CRCC BAD").strip()
        detail = (("type", name), ("crcc", "ok" if rec.crcc_ok else "bad"), *rec.id_detail)
        if rec.notes:
            detail += (("notes", "; ".join(rec.notes)),)
        out.append(
            Span(
                rec.preamble[0],
                rec.postamble[1],
                label,
                sub,
                style,
                Shape.CHEVRON,
                detail,
                tuple(fields),
            )
        )
    return out
