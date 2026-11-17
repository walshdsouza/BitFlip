"""Tests for CRC-16-CCITT long division."""

import binascii
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dccn import config, crc  # noqa: E402
from dccn.framer import build_frame, bytes_to_bits, parse_frame  # noqa: E402


def flip(bits: str, i: int) -> str:
    return bits[:i] + ("1" if bits[i] == "0" else "0") + bits[i + 1:]


class TestCrc16(unittest.TestCase):
    def test_matches_binascii_for_random_bytes(self):
        rng = random.Random(42)
        for _ in range(500):
            data = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 64)))
            expected = binascii.crc_hqx(data, 0)
            self.assertEqual(int(crc.crc16_bits(bytes_to_bits(data)), 2), expected, data)

    def test_known_check_value(self):
        # CRC-16/XMODEM (poly 0x1021, init 0) check value for "123456789" is 0x31C3.
        self.assertEqual(crc.crc16_bits(bytes_to_bits(b"123456789")), f"{0x31C3:016b}")

    def test_verify_clean_codeword(self):
        bits = bytes_to_bits(b"Hello DCCN")
        self.assertTrue(crc.verify(bits + crc.crc16_bits(bits)))

    def test_every_single_bit_flip_detected(self):
        bits = bytes_to_bits(b"Hi there")
        codeword = bits + crc.crc16_bits(bits)
        for i in range(len(codeword)):
            self.assertFalse(crc.verify(flip(codeword, i)), f"flip at {i} not detected")

    def test_burst_errors_up_to_16_bits_detected(self):
        rng = random.Random(5)
        bits = bytes_to_bits(b"burst error test!")
        codeword = bits + crc.crc16_bits(bits)
        for length in range(1, 17):
            for start in range(0, len(codeword) - length + 1):
                # A burst of length L starts and ends with a flipped bit; inside: random.
                pattern = ["1"] + [rng.choice("01") for _ in range(length - 2)] + ["1"]
                pattern = pattern[:length]
                damaged = list(codeword)
                for k, p in enumerate(pattern):
                    if p == "1":
                        damaged[start + k] = "1" if damaged[start + k] == "0" else "0"
                self.assertFalse(crc.verify("".join(damaged)), f"burst {length}@{start}")

    def test_steps_reproduce_remainder(self):
        bits = bytes_to_bits(b"Hi")
        steps = crc.crc16_steps(bits)
        self.assertTrue(steps)
        self.assertEqual(steps[-1]["remaining"][-16:], crc.crc16_bits(bits))
        for s in steps:
            self.assertEqual(s["result"][0], "0")           # leading bit cleared by XOR
            self.assertEqual(s["window"][0], "1")

    def test_format_division(self):
        text = crc.format_division(bytes_to_bits(b"Hi"))
        self.assertIn(crc.crc16_bits(bytes_to_bits(b"Hi")), text)
        self.assertIn("OK", text)

    def test_rejects_non_bits(self):
        with self.assertRaises(ValueError):
            crc.crc16_bits("0102")


class TestCrcInFrames(unittest.TestCase):
    def test_frame_crc_field(self):
        frame = build_frame(config.TYPE_DATA, 1, b"abc")
        self.assertEqual(frame.crc_bits, crc.crc16_bits(frame.header_bits + frame.payload_bits))

    def test_corrupted_frame_rejected(self):
        frame = build_frame(config.TYPE_DATA, 0, b"Hello")
        detected = 0
        for i in range(len(frame.stuffed_bits)):
            parsed = parse_frame(flip(frame.stuffed_bits, i))
            self.assertFalse(parsed.ok, f"flip at {i} accepted")
            detected += 1
        self.assertEqual(detected, len(frame.stuffed_bits))


if __name__ == "__main__":
    unittest.main()
