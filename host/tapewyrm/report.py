"""Human-readable formatting over RecoveryReport (DESIGN.md §6A.9).

The ``RecoveryReport`` dataclass lives in ``tapewyrm.types``; this module only
formats it for the CLI: per-segment status counts, per-track coverage %, BSM
expected-vs-unexpected accounting, and the list of segments worth re-capturing.
"""

from __future__ import annotations

from tapewyrm.types import RecoveryReport, SegmentStatus

_STATUS_ORDER = (
    SegmentStatus.CLEAN,
    SegmentStatus.CORRECTED,
    SegmentStatus.UNCORRECTABLE,
    SegmentStatus.MISSING,
)


def status_counts(report: RecoveryReport) -> dict[SegmentStatus, int]:
    """Count segments by status (every status present, zero-filled)."""
    counts = dict.fromkeys(_STATUS_ORDER, 0)
    for st in report.segment_status.values():
        counts[st] = counts.get(st, 0) + 1
    return counts


def format_status_counts(report: RecoveryReport) -> str:
    counts = status_counts(report)
    total = sum(counts.values())
    parts = [f"{counts[s]} {s.value}" for s in _STATUS_ORDER]
    return f"{total} segments: " + ", ".join(parts)


def format_track_coverage(report: RecoveryReport) -> str:
    """Per-track coverage %, one line per track (ascending)."""
    coverage = report.track_coverage()
    if not coverage:
        return "no track coverage data"
    lines = [f"  track {t:>2}: {coverage[t] * 100:5.1f}%" for t in sorted(coverage)]
    return "track coverage:\n" + "\n".join(lines)


def format_bsm_accounting(report: RecoveryReport) -> str:
    """BSM expected-vs-unexpected bad accounting."""
    return (
        f"bad sectors: {report.expected_bad} expected (BSM-mapped), "
        f"{report.unexpected_bad} unexpected (beyond BSM)"
    )


def format_recapture(report: RecoveryReport, limit: int = 50) -> str:
    """List the segments worth re-capturing (keyed by (track, seg-in-track))."""
    if not report.recapture:
        return "recapture: none — all segments clean or corrected"
    shown = report.recapture[:limit]
    listing = ", ".join(f"(t{t},s{s})" for t, s in shown)
    suffix = "" if len(report.recapture) <= limit else f" (+{len(report.recapture) - limit} more)"
    return f"recapture {len(report.recapture)} segment(s): {listing}{suffix}"


def format_report(report: RecoveryReport) -> str:
    """Full human-readable summary block for the CLI."""
    sections = [
        "=== Recovery report ===",
        format_status_counts(report),
        format_bsm_accounting(report),
        format_track_coverage(report),
        format_recapture(report),
    ]
    if report.notes:
        sections.append("notes:\n" + "\n".join(f"  - {n}" for n in report.notes))
    return "\n".join(sections)


def print_report(report: RecoveryReport) -> None:
    """Print the formatted report to stdout (CLI convenience)."""
    print(format_report(report))
