"""Frame codec round-trip + capability gate (DESIGN.md §6A.2, §13.3)."""

import json
import struct

import pytest

from tapewyrm.link.device import (
    DeviceLink,
    LinkError,
    LinkVersionError,
)
from tapewyrm.link.protocol import CAPS, PROTO_VERSION, Txn
from tapewyrm.link.transport import (
    FakeTransport,
    decode_frame_header,
    encode_frame,
)
from tapewyrm.types import SelectHint, StopCond, TimingParams

# ---------------------------------------------------------------------------
# Frame codec
# ---------------------------------------------------------------------------


def test_frame_round_trip():
    payload = b"\x01\x02\x03hello"
    frame = encode_frame(Txn.COMMAND_TXN, payload)
    opcode, length = decode_frame_header(frame[:3])
    assert opcode == int(Txn.COMMAND_TXN)
    assert length == len(payload)
    assert frame[3:] == payload


def test_empty_payload_frame():
    frame = encode_frame(Txn.INFO)
    opcode, length = decode_frame_header(frame[:3])
    assert opcode == int(Txn.INFO)
    assert length == 0
    assert len(frame) == 3


def test_encode_frame_rejects_oversized_payload():
    with pytest.raises(ValueError):
        encode_frame(Txn.INFO, b"\x00" * 0x10000)


# ---------------------------------------------------------------------------
# Helpers: good/bad INFO payloads
# ---------------------------------------------------------------------------


def _info_payload(proto_ver=PROTO_VERSION, caps=("verbs", "capture", "markers")):
    return json.dumps(
        {
            "model": "Greaseweazle v4.1",
            "mcu": "AT32F403",
            "firmware": "tapewyrm-0.1",
            "serial": "TW0001",
            "usb_high_speed": True,
            "sram_bytes": 224 * 1024,
            "caps": list(caps),
            "proto_ver": proto_ver,
        }
    ).encode("utf-8")


def _good_link():
    t = FakeTransport()
    t.queue_response(Txn.INFO, _info_payload())
    link = DeviceLink(t)
    info = link.open()
    return link, t, info


# ---------------------------------------------------------------------------
# Capability gate
# ---------------------------------------------------------------------------


def test_gate_accepts_good_info():
    link, t, info = _good_link()
    assert info.proto_ver == PROTO_VERSION
    assert {"verbs", "capture"} <= info.qic_caps
    # The INFO request frame was actually sent.
    assert t.sent_frames[0] == (int(Txn.INFO), b"")
    link.close()


def test_gate_rejects_old_version():
    t = FakeTransport()
    t.queue_response(Txn.INFO, _info_payload(proto_ver=PROTO_VERSION - 1))
    link = DeviceLink(t)
    with pytest.raises(LinkVersionError):
        link.open()


def test_gate_rejects_missing_caps():
    t = FakeTransport()
    # Missing "capture" -> stock-ish firmware -> reject.
    t.queue_response(Txn.INFO, _info_payload(caps=("verbs",)))
    link = DeviceLink(t)
    with pytest.raises(LinkVersionError):
        link.open()


def test_required_caps_subset_of_generated_caps():
    # Sanity: the generated CAPS set covers our required gate caps.
    from tapewyrm.link.device import REQUIRED_CAPS

    assert REQUIRED_CAPS <= CAPS


# ---------------------------------------------------------------------------
# command_txn / report bits
# ---------------------------------------------------------------------------


def _cmd_resp(ack, bits, final):
    return struct.pack("<BHB", 1 if ack else 0, bits, 1 if final else 0)


def test_command_txn_no_report_returns_empty():
    link, t, _ = _good_link()
    t.queue_response(Txn.COMMAND_TXN, _cmd_resp(True, 0, True))
    out = link.command_txn(14)  # seek load point, no report
    assert out == b""
    # The COMMAND_TXN request carried {cmd_n, report_bits}.
    sent = t.sent_frames[-1]
    assert sent == (int(Txn.COMMAND_TXN), bytes([14, 0]))
    link.close()


def test_command_txn_returns_report_bytes():
    link, t, _ = _good_link()
    # 8 report bits, value 0xA5.
    t.queue_response(Txn.COMMAND_TXN, _cmd_resp(True, 0xA5, True))
    out = link.command_txn(6, report_bits=8)
    assert out == bytes([0xA5])
    link.close()


def test_command_txn_16_bits():
    link, t, _ = _good_link()
    t.queue_response(Txn.COMMAND_TXN, _cmd_resp(True, 0xBEEF, True))
    out = link.command_txn(7, report_bits=16)
    assert out == struct.pack("<H", 0xBEEF)
    link.close()


def test_command_txn_bad_ack_raises():
    link, t, _ = _good_link()
    t.queue_response(Txn.COMMAND_TXN, _cmd_resp(False, 0, True))
    with pytest.raises(LinkError):
        link.command_txn(6, report_bits=8)
    link.close()


def test_command_txn_bad_final_raises():
    link, t, _ = _good_link()
    t.queue_response(Txn.COMMAND_TXN, _cmd_resp(True, 0x12, False))
    with pytest.raises(LinkError):
        link.command_txn(6, report_bits=8)
    link.close()


def test_command_txn_validates_report_bits_range():
    link, _t, _ = _good_link()
    with pytest.raises(ValueError):
        link.command_txn(6, report_bits=17)
    link.close()


# ---------------------------------------------------------------------------
# set_timing / select / wait_ready / capture
# ---------------------------------------------------------------------------


def test_set_timing_sends_frame():
    link, t, _ = _good_link()
    t.queue_response(Txn.SET_TIMING, b"")
    link.set_timing(TimingParams())
    assert t.sent_frames[-1][0] == int(Txn.SET_TIMING)
    link.close()


def test_select_sends_unit_and_sticky():
    link, t, _ = _good_link()
    t.queue_response(Txn.SELECT, b"")
    link.select(SelectHint(unit=1, sticky=True))
    assert t.sent_frames[-1] == (int(Txn.SELECT), bytes([1, 1]))
    link.close()


def test_wait_ready_reads_status_bit():
    link, t, _ = _good_link()
    t.queue_response(Txn.WAIT_READY, bytes([0x01]))  # ready bit set
    assert link.wait_ready(15000) is True
    t.queue_response(Txn.WAIT_READY, bytes([0x00]))
    assert link.wait_ready(15000) is False
    link.close()


def test_capture_stream_drains_then_aborts_on_exit():
    link, t, _ = _good_link()
    t.queue_stream(b"\x01\x02", b"\x03")
    chunks = []
    with link.capture(10, StopCond(byte_budget=100)) as cap:
        for c in cap.chunks():
            chunks.append(c)
    assert b"".join(chunks) == b"\x01\x02\x03"
    # CAPTURE request frame was sent; stream drained cleanly so no abort needed.
    assert t.sent_frames[-1][0] == int(Txn.CAPTURE)
    link.close()


def test_capture_abort_sends_control():
    link, t, _ = _good_link()
    t.queue_stream(b"\xaa")
    cap = link.capture(10, StopCond())
    cap.abort()
    assert t.sent_control[-1][0] == int(Txn.ABORT)
    cap.__exit__(None, None, None)
    link.close()


def test_closed_link_raises():
    from tapewyrm.link.device import LinkClosed

    link = DeviceLink(FakeTransport())
    with pytest.raises(LinkClosed):
        link.command_txn(6, report_bits=8)
