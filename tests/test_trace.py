# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The M17 receive trace: its lines, scoring, and the simulated radio."""

from oat import trace
from oat.m17 import codec
from oat.m17.codec import Lsf


def events(*lines):
    """Trace lines as events, a second apart."""
    out = []
    for i, line in enumerate(lines):
        e = trace.parse_line(line, float(i))
        if e is not None:
            out.append(e)
    return out


PAYLOADS = [bytes([i]) * 16 for i in range(4)]


def str_line(ms, fn, lsf_valid, payload):
    return f"STR {ms} {fn} {lsf_valid} {payload.hex()}"


def test_parse_line_keeps_keyword_fields_and_time():
    e = trace.parse_line("HB 1200 1024 380 0\n", 5.0)
    assert (e.kind, e.fields, e.time, e.ms) == ("HB", ["1200", "1024", "380",
                                                       "0"], 5.0, 1200)
    assert trace.parse_line("  \n", 0.0) is None
    assert trace.parse_line("ID pr524-643ad793", 0.0).ms is None


def test_score_stream_sorts_decoded_garbled_and_repeated_frames():
    lsf = Lsf().to_bytes().hex()
    ev = events(f"LSF 0 1 F {lsf}",
                str_line(40, 0, 1, PAYLOADS[0]), "PLAY 40 0",
                str_line(80, 1, 1, PAYLOADS[1]), "PLAY 80 1",
                # frame 2 rejected: the decoder still holds frame 1
                str_line(120, 1, 1, PAYLOADS[1]), "PLAY 120 1",
                # frame 3 decoded wrong
                str_line(160, 3, 1, bytes(16)), "PLAY 160 3")
    r = trace.score_stream(ev, PAYLOADS)
    assert (r["sent"], r["decoded"], r["missed"]) == (4, 2, 2)
    assert (r["garbled"], r["repeated"]) == (1, 1)
    assert (r["played"], r["played_garbled"]) == (4, 1)
    assert (r["lsf_after_frames"], r["lsf_from"], r["first_play_frame"]) == (
        0, "F", 0)


def test_score_stream_reports_a_late_lsf_from_the_lich():
    lsf = Lsf().to_bytes().hex()
    ev = events(*[str_line(40 * i, i, 0, PAYLOADS[i]) for i in range(3)],
                f"LSF 120 1 I {lsf}", str_line(160, 3, 1, PAYLOADS[3]),
                "PLAY 160 3")
    r = trace.score_stream(ev, PAYLOADS)
    assert (r["decoded"], r["lsf_after_frames"], r["lsf_from"]) == (4, 3, "I")
    assert (r["played"], r["first_play_frame"]) == (1, 3)


def pkt_lines(packet, corrupt_frame=None):
    lines = []
    for i, (data, counter, eof) in enumerate(codec.packet_chunks(packet)):
        if i == corrupt_frame:
            data = bytes(len(data))
        lines.append(f"PKT {40 * i} {counter} {int(eof)} {data.hex()}")
    return lines


def test_packets_reassemble_and_check_their_crc():
    a = codec.sms_packet("first message " * 3)
    b = codec.sms_packet("second")
    ev = events(*pkt_lines(a), *pkt_lines(b, corrupt_frame=0))
    packets = trace.reassemble_packets(ev)
    assert packets[0] == (a, True)
    assert len(packets) == 2 and not packets[1][1]


def test_a_missing_packet_frame_fails_the_crc():
    a = codec.sms_packet("x" * 70)  # three frames
    lines = pkt_lines(a)
    ev = events(lines[0], lines[2])
    assert [ok for _, ok in trace.reassemble_packets(ev)] == [False]


def test_a_frame_out_of_order_drops_the_packet_being_built():
    a = codec.sms_packet("x" * 70)
    lines = pkt_lines(a)
    ev = events(lines[1], *lines)
    assert trace.reassemble_packets(ev) == [(a, True)]


def test_stack_report_finds_the_least_free_stack_and_resets():
    ev = events("HB 1000 1024 400 0", "HB 2000 1024 368 0",
                "HB 500 1024 600 0")
    assert trace.stack_report(ev) == {"beats": 3, "stack": 1024,
                                      "least_free": 368, "dropped": 0,
                                      "resets": 1}
    assert trace.stack_report([]) == {"beats": 0}


def test_sim_trace_decodes_a_clean_stream():
    sim = trace.SimTrace()
    payloads = [bytes([i]) * 16 for i in range(12)]
    sim.hear(codec.stream_symbols(Lsf(source="AB1CDE"), payloads), -80.0)
    r = trace.score_stream(sim.events_since(0.0), payloads)
    assert (r["decoded"], r["garbled"], r["lsf_from"]) == (12, 0, "F")
    assert r["played"] == 12


def test_sim_trace_rebuilds_the_lsf_from_the_lich_without_one():
    sim = trace.SimTrace()
    payloads = [bytes([i]) * 16 for i in range(12)]
    sim.hear(codec.stream_symbols(Lsf(), payloads, with_lsf=False), -80.0)
    r = trace.score_stream(sim.events_since(0.0), payloads)
    assert r["lsf_from"] == "I" and r["lsf_after_frames"] == 5
    assert r["first_play_frame"] == 5


def test_sim_trace_loses_frames_in_heavy_noise():
    sim = trace.SimTrace()
    payloads = [bytes([i]) * 16 for i in range(40)]
    sim.hear(codec.stream_symbols(Lsf(), payloads), -130.0)
    r = trace.score_stream(sim.events_since(0.0), payloads)
    assert r["decoded"] < 40


def test_sim_trace_reports_an_sms():
    sim = trace.SimTrace()
    packet = codec.sms_packet("hello from the bench")
    sim.hear(codec.packet_symbols(Lsf(type=codec.TYPE_PACKET_DATA), packet),
             -80.0)
    assert trace.reassemble_packets(sim.events_since(0.0)) == [(packet, True)]


def test_score_stream_stops_at_the_eot():
    ev = events(*[str_line(40 * i, i, 1, PAYLOADS[i]) for i in range(4)],
                "EOT 200", "LOCK 900 1", str_line(940, 0, 0, bytes(16)))
    r = trace.score_stream(ev, PAYLOADS)
    assert (r["decoded"], r["garbled"]) == (4, 0)


def test_score_stream_takes_the_lsf_from_the_frames_without_an_lsf_line():
    ev = events(*[str_line(40 * i, i, 1, PAYLOADS[i]) for i in range(4)])
    r = trace.score_stream(ev, PAYLOADS)
    assert (r["lsf_after_frames"], r["lsf_from"]) == (0, "F")
    ev = events(*[str_line(40 * i, i, int(i >= 2), PAYLOADS[i])
                  for i in range(4)])
    r = trace.score_stream(ev, PAYLOADS)
    assert (r["lsf_after_frames"], r["lsf_from"]) == (2, "I")


def test_score_stream_counts_the_decoder_empty_frame_as_rejected():
    ev = events("LOCK 0 1", str_line(40, 0, 0, bytes(16)), "LOCK 80 0",
                "LOCK 200 1", *[str_line(240 + 40 * i, i, 1, PAYLOADS[i])
                                for i in range(4)])
    r = trace.score_stream(ev, PAYLOADS)
    assert (r["decoded"], r["garbled"], r["repeated"]) == (4, 0, 1)


def test_score_stream_reads_past_a_false_lock_before_the_transmission():
    payloads = [bytes([i + 1]) * 16 for i in range(4)]
    ev = events("LOCK 0 1", str_line(40, 0, 0, bytes(16)), "LOCK 80 0",
                "EOT 80", "LOCK 200 1",
                *[str_line(240 + 40 * i, i, 1, payloads[i]) for i in range(4)],
                "EOT 400")
    r = trace.score_stream(ev, payloads)
    assert (r["decoded"], r["garbled"], r["repeated"]) == (4, 0, 1)
    assert (r["lsf_after_frames"], r["lsf_from"]) == (0, "F")
