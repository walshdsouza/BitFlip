"""Tests for framing: bits conversion, bit stuffing, frame build/parse and stream deframing."""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dccn import config  # noqa: E402
from dccn.framer import (StreamDeframer, bits_to_bytes, bits_to_text, build_frame,  # noqa: E402
                         bytes_to_bits, destuff_bits, FrameError, parse_frame,
                         split_payload, stuff_bits, text_to_bits)


class TestBits(unittest.TestCase):
    def test_text_round_trip(self):
        for text in ["Hello", "", "~~~", "???", "नमस्ते 🌐", "a" * 300]:
            self.assertEqual(bits_to_text(text_to_bits(text)), text)

    def test_known_value(self):
        self.assertEqual(text_to_bits("A"), "01000001")
        self.assertEqual(bytes_to_bits(b"\x7e"), config.FLAG)

    def test_bytes_round_trip_random(self):
        rng = random.Random(1)
        for _ in range(100):
            data = bytes(rng.randrange(256) for _ in range(rng.randrange(40)))
            self.assertEqual(bits_to_bytes(bytes_to_bits(data)), data)

    def test_split_payload(self):
        data = bytes(range(70))
        parts = split_payload(data, 32)
        self.assertEqual([len(p) for p in parts], [32, 32, 6])
        self.assertEqual(b"".join(parts), data)
        self.assertEqual(split_payload(b"", 32), [b""])


class TestStuffing(unittest.TestCase):
    def test_zero_inserted_after_five_ones(self):
        stuffed, positions = stuff_bits("11111")
        self.assertEqual(stuffed, "111110")
        self.assertEqual(positions, [5])

    def test_flag_pattern_is_stuffed(self):
        stuffed, positions = stuff_bits(config.FLAG)
        self.assertEqual(stuffed, "011111010")
        self.assertNotIn("111111", stuffed)
        self.assertEqual(len(positions), 1)

    def test_long_run_of_ones(self):
        stuffed, positions = stuff_bits("1" * 12)
        self.assertEqual(stuffed, "11111011111011")
        self.assertEqual(positions, [5, 11])

    def test_round_trip_random(self):
        rng = random.Random(7)
        for _ in range(300):
            bits = "".join(rng.choice("0111") for _ in range(rng.randrange(1, 200)))
            stuffed, inserted = stuff_bits(bits)
            self.assertNotIn("111111", stuffed)
            original, removed = destuff_bits(stuffed)
            self.assertEqual(original, bits)
            self.assertEqual(inserted, removed)

    def test_six_ones_is_invalid(self):
        with self.assertRaises(FrameError) as ctx:
            destuff_bits("0111111")
        self.assertEqual(ctx.exception.kind, "structural")


class TestFrames(unittest.TestCase):
    def test_round_trip(self):
        for ftype in config.TYPE_NAMES:
            for seq in (0, 1):
                payload = b"" if ftype in (config.TYPE_ACK, config.TYPE_NACK) else b"Hi!"
                frame = build_frame(ftype, seq, payload)
                self.assertTrue(frame.wire.startswith(config.FLAG))
                self.assertTrue(frame.wire.endswith(config.FLAG))
                parsed = parse_frame(frame.stuffed_bits)
                self.assertTrue(parsed.ok, parsed.error)
                self.assertEqual((parsed.ftype, parsed.seq, parsed.payload),
                                 (ftype, seq, payload))

    def test_payload_with_flag_bytes_and_ones(self):
        payload = b"\x7e\xff\x7e~~~???\xff\xff"
        frame = build_frame(config.TYPE_DATA, 1, payload)
        self.assertGreater(len(frame.stuffed_positions), 0)
        body = frame.wire[8:-8]
        self.assertNotIn(config.FLAG, body)  # flag can never appear inside the frame
        parsed = parse_frame(frame.stuffed_bits)
        self.assertTrue(parsed.ok)
        self.assertEqual(parsed.payload, payload)

    def test_tilde_triggers_stuffing(self):
        frame = build_frame(config.TYPE_DATA, 0, "~~~".encode())
        self.assertGreaterEqual(len(frame.stuffed_positions), 3)

    def test_ack_has_no_payload(self):
        frame = build_frame(config.TYPE_ACK, 1)
        self.assertEqual(frame.header_bits[16:24], "00000000")
        self.assertEqual(len(frame.body_bits), config.HEADER_BITS + config.CRC_BITS)

    def test_bad_inputs(self):
        with self.assertRaises(ValueError):
            build_frame(config.TYPE_DATA, 2, b"x")
        with self.assertRaises(ValueError):
            build_frame(0x99, 0, b"x")
        with self.assertRaises(ValueError):
            build_frame(config.TYPE_DATA, 0, b"x" * 256)

    def test_structural_errors(self):
        self.assertEqual(parse_frame("0101").error.kind, "structural")       # too short
        frame = build_frame(config.TYPE_DATA, 0, b"abc")
        self.assertEqual(parse_frame(frame.stuffed_bits + "0").error.kind, "structural")

    def test_segments_cover_wire(self):
        frame = build_frame(config.TYPE_DATA, 0, b"~~?")
        segs = frame.segments
        self.assertEqual(segs[0]["start"], 0)
        self.assertEqual(segs[-1]["end"], len(frame.wire))
        names = [s["field"] for s in segs]
        self.assertEqual(names, ["FLAG", "TYPE", "SEQ", "LEN", "PAYLOAD", "CRC", "FLAG"])
        for a, b in zip(segs, segs[1:]):
            self.assertEqual(a["end"], b["start"])


class TestStreamDeframer(unittest.TestCase):
    def setUp(self):
        self.frames = [build_frame(config.TYPE_DATA_MORE, 0, b"first~"),
                       build_frame(config.TYPE_DATA, 1, b"\x7e\x7e second"),
                       build_frame(config.TYPE_ACK, 0)]
        self.stream = "".join(f.wire for f in self.frames)

    def test_multiple_frames_in_one_chunk(self):
        bodies = StreamDeframer().feed(self.stream)
        self.assertEqual(bodies, [f.stuffed_bits for f in self.frames])

    def test_frames_split_across_chunks(self):
        for size in (1, 3, 7, 8, 13, 50):
            d = StreamDeframer()
            out = []
            for i in range(0, len(self.stream), size):
                out += d.feed(self.stream[i:i + size])
            self.assertEqual(out, [f.stuffed_bits for f in self.frames], f"chunk={size}")

    def test_random_chunking(self):
        rng = random.Random(3)
        for _ in range(50):
            d = StreamDeframer()
            out, i = [], 0
            while i < len(self.stream):
                n = rng.randrange(1, 40)
                out += d.feed(self.stream[i:i + n])
                i += n
            self.assertEqual(len(out), 3)

    def test_garbage_before_first_flag_is_ignored(self):
        bodies = StreamDeframer().feed("1010" + self.frames[0].wire)
        self.assertEqual(bodies, [self.frames[0].stuffed_bits])

    def test_resync_on_back_to_back_flags(self):
        bodies = StreamDeframer().feed(config.FLAG + self.frames[0].wire)
        self.assertEqual(bodies, [self.frames[0].stuffed_bits])

    def test_partial_frame_waits(self):
        d = StreamDeframer()
        wire = self.frames[0].wire
        self.assertEqual(d.feed(wire[:-3]), [])
        self.assertEqual(d.feed(wire[-3:]), [self.frames[0].stuffed_bits])


if __name__ == "__main__":
    unittest.main()
