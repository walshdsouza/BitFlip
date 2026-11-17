"""Tests for the noise injector (simulated noisy physical channel)."""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dccn import config  # noqa: E402
from dccn.framer import build_frame, parse_frame  # noqa: E402
from dccn.noise import NoiseInjector, flip_bits, frame_still_delimits  # noqa: E402


def diff_positions(a: str, b: str) -> list[int]:
    return [i for i, (x, y) in enumerate(zip(a, b)) if x != y]


class TestNoise(unittest.TestCase):
    def setUp(self):
        self.frame = build_frame(config.TYPE_DATA, 0, b"Hello noisy world ~~~")

    def test_flips_exactly_n_bits(self):
        rng = random.Random(1)
        for n in range(1, config.MAX_BITS_TO_FLIP + 1):
            for _ in range(30):
                damaged, positions = flip_bits(self.frame.wire, n, rng)
                self.assertEqual(len(positions), n)
                self.assertEqual(diff_positions(self.frame.wire, damaged), positions)

    def test_never_touches_flags(self):
        rng = random.Random(2)
        wire = self.frame.wire
        for _ in range(500):
            damaged, positions = flip_bits(wire, 8, rng)
            self.assertEqual(damaged[:8], config.FLAG)
            self.assertEqual(damaged[-8:], config.FLAG)
            self.assertTrue(all(8 <= p < len(wire) - 8 for p in positions))
            self.assertTrue(frame_still_delimits(damaged))

    def test_prob_zero_never_corrupts(self):
        noise = NoiseInjector(random.Random(3))
        noise.update(enabled=True, frame_error_prob=0.0)
        for _ in range(500):
            self.assertEqual(noise.maybe_corrupt(self.frame.wire), (self.frame.wire, []))

    def test_prob_one_always_corrupts(self):
        noise = NoiseInjector(random.Random(4))
        noise.update(enabled=True, frame_error_prob=1.0, bits_to_flip=1)
        for _ in range(300):
            damaged, positions = noise.maybe_corrupt(self.frame.wire)
            self.assertEqual(len(positions), 1)
            self.assertFalse(parse_frame(damaged[8:-8]).ok)  # receiver always notices

    def test_disabled_never_corrupts(self):
        noise = NoiseInjector(random.Random(5))
        noise.update(enabled=False, frame_error_prob=1.0)
        self.assertEqual(noise.maybe_corrupt(self.frame.wire)[1], [])

    def test_one_shot_drops(self):
        noise = NoiseInjector()
        self.assertFalse(noise.take_drop_frame())
        noise.update(drop_next_frame=True, drop_next_ack=True)
        self.assertTrue(noise.take_drop_frame())
        self.assertFalse(noise.take_drop_frame())
        self.assertTrue(noise.take_drop_ack())
        self.assertFalse(noise.take_drop_ack())

    def test_validation(self):
        noise = NoiseInjector()
        with self.assertRaises(ValueError):
            noise.update(frame_error_prob=1.5)
        with self.assertRaises(ValueError):
            noise.update(bits_to_flip=0)
        with self.assertRaises(ValueError):
            noise.update(bits_to_flip=9)
        cfg = noise.update(enabled="on", frame_error_prob="0.5", bits_to_flip="3")
        self.assertEqual((cfg["enabled"], cfg["frame_error_prob"], cfg["bits_to_flip"]),
                         (True, 0.5, 3))

    def test_seeded_is_reproducible(self):
        a = NoiseInjector(random.Random(9))
        b = NoiseInjector(random.Random(9))
        for n in (a, b):
            n.update(enabled=True, frame_error_prob=0.5, bits_to_flip=2)
        for _ in range(50):
            self.assertEqual(a.maybe_corrupt(self.frame.wire), b.maybe_corrupt(self.frame.wire))


if __name__ == "__main__":
    unittest.main()
