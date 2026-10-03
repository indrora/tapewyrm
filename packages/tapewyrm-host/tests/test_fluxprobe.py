"""`tw drive flux` plumbing: timed capture, abort, truncated streams, analysis."""

from tapewyrm.codec import gwstream
from tapewyrm.link.device import CaptureStream
from tapewyrm.link.protocol import Txn
from tapewyrm.link.transport import FakeTransport
from tapewyrm.rawflux.container import write_preamble
from tapewyrm.tape.fluxprobe import analyse, format_report
from tapewyrm.types import CaptureHeader, Direction


def test_chunks_for_survives_silence_then_aborts():
    t = FakeTransport()
    t.open()
    t.queue_stream_chunk(b"\x48\x48")
    cap = CaptureStream(t)
    got = b"".join(cap.chunks_for(0.05))  # empty reads after the chunk: keep going
    assert got == b"\x48\x48"
    assert t.sent_control == [(int(Txn.ABORT), b"")]


def test_parse_tolerates_a_stream_cut_mid_opcode():
    ps = gwstream.parse(b"\x48\x48\xff\x01\x01")  # INDEX opcode cut by the abort
    assert ps.intervals == [0x48, 0x48] and ps.end is None


def _probe_file(tmp_path, flux: bytes):
    hdr = CaptureHeader(
        rate_kbps=1000, sample_clock_hz=72_000_000, track=-1,
        direction=Direction.FORWARD, pass_id=0, utc="",
    )  # fmt: skip
    path = tmp_path / "p.twrf"
    with path.open("wb") as f:
        write_preamble(f, hdr)
        f.write(flux)
    return path


def test_analyse_histograms_intervals(tmp_path):
    # 72 ticks = 1.0 us, 144 ticks = 2.0 us at the 72 MHz sample clock.
    r = analyse(_probe_file(tmp_path, bytes([72] * 30 + [144] * 10)))
    assert r.transitions == 40 and r.end_reason == "aborted"
    hist = dict(r.histogram)
    assert hist[1.0] == 30 and hist[2.0] == 10
    assert any("1.00-1.25" in line for line in format_report(r))


def test_analyse_reports_silence(tmp_path):
    r = analyse(_probe_file(tmp_path, b""))
    assert r.transitions == 0
    assert any("NOTHING on RDATA" in line for line in format_report(r))
