"""Noise injector: a simulated noisy physical channel.

Our real "cable" (TCP on localhost) never makes mistakes - TCP has its own checksum and
retransmissions. To study error detection and ARQ we therefore *add* errors on purpose,
right before a frame goes onto the wire:

* bit flips   - a 0 becomes 1 or vice versa (electrical noise, interference)
* lost frame  - the frame never arrives (forces a sender timeout)
* lost ACK    - the receiver's acknowledgement never arrives (forces a duplicate)

Noise is applied AFTER the CRC was computed and AFTER bit stuffing, exactly where a real
channel would damage the signal, and only to the bits *between* the flags so the receiver
can still find the frame boundaries - it just finds damaged contents.
"""

from __future__ import annotations

import random
import threading

from . import config

FLAG_LEN = len(config.FLAG)


def frame_still_delimits(wire: str) -> bool:
    """True if the only flags in ``wire`` are the first and the last 8 bits.

    A flip can, rarely, turn body bits into 01111110. The receiver would then see the
    frame split in two. We avoid such flips so one damaged frame stays one damaged frame.
    """
    return wire.find(config.FLAG, FLAG_LEN) == len(wire) - FLAG_LEN


def flip_bits(wire: str, count: int, rng: random.Random) -> tuple[str, list[int]]:
    """Flip ``count`` distinct random bits of ``wire``, never touching the two flags.

    Returns the damaged frame and the sorted flipped positions (indices in ``wire``).
    """
    first, last = FLAG_LEN, len(wire) - FLAG_LEN      # body = wire[first:last]
    body_len = last - first
    count = max(0, min(count, body_len))
    if count == 0:
        return wire, []
    for _ in range(50):                               # retry if a fake flag would appear
        positions = sorted(rng.sample(range(first, last), count))
        bits = list(wire)
        for p in positions:
            bits[p] = "1" if bits[p] == "0" else "0"
        damaged = "".join(bits)
        if frame_still_delimits(damaged):
            return damaged, positions
    return wire, []


class NoiseInjector:
    """Runtime-configurable channel noise. Thread safe; ``rng`` is injectable for tests."""

    def __init__(self, rng: random.Random | None = None) -> None:
        self._rng = rng or random.Random()
        self._lock = threading.Lock()
        self.enabled = False
        self.frame_error_prob = config.DEFAULT_FRAME_ERROR_PROB
        self.bits_to_flip = config.DEFAULT_BITS_TO_FLIP
        self.drop_next_frame = False   # one-shot: next DATA frame vanishes
        self.drop_next_ack = False     # one-shot: next ACK we send vanishes

    def config(self) -> dict:
        """Snapshot of the current settings (sent to the dashboard)."""
        with self._lock:
            return {"enabled": self.enabled, "frame_error_prob": self.frame_error_prob,
                    "bits_to_flip": self.bits_to_flip, "drop_next_frame": self.drop_next_frame,
                    "drop_next_ack": self.drop_next_ack}

    def update(self, **settings) -> dict:
        """Change any subset of the settings; values are validated. Returns new config."""
        with self._lock:
            if "enabled" in settings:
                self.enabled = _as_bool(settings["enabled"])
            if "frame_error_prob" in settings:
                prob = float(settings["frame_error_prob"])
                if not 0.0 <= prob <= 1.0:
                    raise ValueError("frame_error_prob must be between 0 and 1")
                self.frame_error_prob = prob
            if "bits_to_flip" in settings:
                n = int(settings["bits_to_flip"])
                if not 1 <= n <= config.MAX_BITS_TO_FLIP:
                    raise ValueError(f"bits_to_flip must be 1-{config.MAX_BITS_TO_FLIP}")
                self.bits_to_flip = n
            if "drop_next_frame" in settings:
                self.drop_next_frame = _as_bool(settings["drop_next_frame"])
            if "drop_next_ack" in settings:
                self.drop_next_ack = _as_bool(settings["drop_next_ack"])
        return self.config()

    def take_drop_frame(self) -> bool:
        """Consume the one-shot 'drop next frame' order (True = do not send this frame)."""
        with self._lock:
            drop, self.drop_next_frame = self.drop_next_frame, False
            return drop

    def take_drop_ack(self) -> bool:
        """Consume the one-shot 'drop next ACK' order (True = do not send this ACK)."""
        with self._lock:
            drop, self.drop_next_ack = self.drop_next_ack, False
            return drop

    def maybe_corrupt(self, wire: str) -> tuple[str, list[int]]:
        """With probability ``frame_error_prob`` flip ``bits_to_flip`` bits of a DATA frame.

        Returns (possibly damaged frame, flipped positions). Disabled noise = no change.
        """
        with self._lock:
            if not self.enabled or self._rng.random() >= self.frame_error_prob:
                return wire, []
            return flip_bits(wire, self.bits_to_flip, self._rng)


def _as_bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "on", "yes")
    return bool(value)
