"""Stop-and-Wait ARQ end-to-end tests: two real nodes talking over localhost TCP."""

import os
import random
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dccn.noise import NoiseInjector  # noqa: E402
from node import Node  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Recorder:
    """Collects a node's events and lets a test wait for a condition."""

    def __init__(self, node: Node) -> None:
        self.events: list[dict] = []
        self._cond = threading.Condition()
        node.bus.add_listener(self._on_event)

    def _on_event(self, event: dict) -> None:
        with self._cond:
            self.events.append(event)
            self._cond.notify_all()

    def of(self, type_: str) -> list[dict]:
        with self._cond:
            return [e for e in self.events if e["type"] == type_]

    def wait_for(self, type_: str, count: int = 1, timeout: float = 10.0) -> list[dict]:
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                found = [e for e in self.events if e["type"] == type_]
                if len(found) >= count:
                    return found
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"timed out waiting for {count} x {type_}; "
                                         f"got {len(found)}")
                self._cond.wait(remaining)


class ArqTestCase(unittest.TestCase):
    timeout = 0.6
    code = "crc"

    def setUp(self):
        pa, pb = free_port(), free_port()
        kwargs = dict(timeout=self.timeout, echo=False, bind_host="127.0.0.1")
        if self.code != "crc":
            kwargs["code"] = self.code
        self.a = Node("A", pa, "127.0.0.1", pb, noise=NoiseInjector(random.Random(1)), **kwargs)
        self.b = Node("B", pb, "127.0.0.1", pa, noise=NoiseInjector(random.Random(2)), **kwargs)
        self.ra, self.rb = Recorder(self.a), Recorder(self.b)
        self.a.start()
        self.b.start()
        deadline = time.monotonic() + 10
        while not (self.a.connected and self.b.connected):
            if time.monotonic() > deadline:
                self.fail("nodes did not connect")
            time.sleep(0.05)

    def tearDown(self):
        self.a.stop()
        self.b.stop()

    def delivered(self, rec: Recorder) -> list[str]:
        return [e["text"] for e in rec.of("message_delivered")]


class TestStopAndWait(ArqTestCase):
    def test_clean_delivery(self):
        self.a.send_text("Hello")
        self.rb.wait_for("message_delivered")
        self.ra.wait_for("message_acked")
        self.assertEqual(self.delivered(self.rb), ["Hello"])
        self.assertEqual(self.ra.of("retransmit"), [])
        self.assertEqual(self.a.stats.snapshot()["efficiency"], 100.0)

    def test_corrupted_first_attempt_then_recovery(self):
        # Noise hits the first transmission only, then the channel becomes clean.
        self.a.set_noise(enabled=True, frame_error_prob=1.0, bits_to_flip=2)
        self.a.bus.add_listener(lambda e: e["type"] == "noise_injected"
                                and self.a.noise.update(enabled=False))
        self.a.send_text("noisy hello")
        self.ra.wait_for("message_acked")
        self.assertGreaterEqual(len(self.rb.of("crc_fail") + self.rb.of("structural_error")), 1)
        self.assertGreaterEqual(len(self.rb.of("nack_sent")), 1)
        self.assertGreaterEqual(len(self.ra.of("nack_received")), 1)
        self.assertEqual(self.ra.of("retransmit")[0]["reason"], "nack")
        self.assertEqual(self.delivered(self.rb), ["noisy hello"])
        self.assertLess(self.a.stats.snapshot()["efficiency"], 100.0)

    def test_lost_frame_times_out_and_is_retransmitted(self):
        self.a.set_noise(drop_next_frame=True)
        self.a.send_text("lost then found")
        self.ra.wait_for("message_acked")
        self.assertEqual(len(self.ra.of("frame_dropped_by_noise")), 1)
        self.assertEqual(len(self.ra.of("timeout")), 1)
        self.assertEqual(self.ra.of("retransmit")[0]["reason"], "timeout")
        self.assertEqual(self.delivered(self.rb), ["lost then found"])

    def test_lost_ack_gives_duplicate_delivered_once(self):
        self.b.set_noise(drop_next_ack=True)
        self.a.send_text("exactly once")
        self.ra.wait_for("message_acked")
        self.rb.wait_for("duplicate_discarded")
        time.sleep(0.3)
        self.assertEqual(len(self.rb.of("ack_dropped_by_noise")), 1)
        self.assertEqual(self.ra.of("retransmit")[0]["reason"], "timeout")
        self.assertEqual(self.delivered(self.rb), ["exactly once"])
        self.assertEqual(self.b.stats.get("duplicates_discarded"), 1)

    def test_long_message_multi_frame_in_order(self):
        text = "".join(f"[{i:03d}]" for i in range(60)) + " नमस्ते 🌐 end"
        self.a.send_text(text)
        self.rb.wait_for("message_delivered")
        delivered = self.rb.of("message_delivered")[0]
        self.assertEqual(delivered["text"], text)
        self.assertGreater(delivered["frames"], 5)
        seqs = [e["seq"] for e in self.ra.of("frame_sent")]
        self.assertEqual(seqs, [i % 2 for i in range(len(seqs))])  # alternating bit

    def test_both_sides_send_simultaneously(self):
        a_msgs = [f"A says {i} " * 5 for i in range(5)]
        b_msgs = [f"B says {i} " * 5 for i in range(5)]
        ta = threading.Thread(target=lambda: [self.a.send_text(m) for m in a_msgs])
        tb = threading.Thread(target=lambda: [self.b.send_text(m) for m in b_msgs])
        ta.start(), tb.start()
        ta.join(), tb.join()
        self.rb.wait_for("message_delivered", 5)
        self.ra.wait_for("message_delivered", 5)
        self.assertEqual(self.delivered(self.rb), a_msgs)
        self.assertEqual(self.delivered(self.ra), b_msgs)

    def test_random_noise_still_exactly_once_in_order(self):
        for n in (self.a, self.b):
            n.set_noise(enabled=True, frame_error_prob=0.4, bits_to_flip=1)
        msgs = [f"message {i} ~~~ ???" for i in range(6)]
        for m in msgs:
            self.a.send_text(m)
        self.b.set_noise(drop_next_ack=True)
        self.rb.wait_for("message_delivered", len(msgs), timeout=60)
        self.ra.wait_for("message_acked", len(msgs), timeout=30)
        self.assertEqual(self.delivered(self.rb), msgs)
        stats = self.a.stats.snapshot()
        self.assertEqual(stats["frames_acked"], len(msgs))
        self.assertEqual(stats["frames_sent"], len(msgs) + stats["retransmissions"])

    def test_stale_ack_is_ignored_and_validation(self):
        with self.assertRaises(ValueError):
            self.a.send_text("   ")
        with self.assertRaises(ValueError):
            self.a.send_text("x" * 5000)

    def test_gives_up_after_max_retries(self):
        self.a.sender.max_retries = 2
        self.a.set_noise(enabled=True, frame_error_prob=1.0)
        self.a.send_text("doomed")
        failed = self.ra.wait_for("send_failed", timeout=15)
        self.assertIn("gave up", failed[0]["reason"])
        self.assertEqual(len(self.ra.of("retransmit")), 2)
        self.assertEqual(self.delivered(self.rb), [])


if __name__ == "__main__":
    unittest.main()
