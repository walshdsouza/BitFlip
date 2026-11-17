"""Protocol statistics: how much work the link did to deliver the data.

Efficiency shows the cost of a noisy channel: every corrupted or lost frame has to be sent
again, so more frames go on the wire for the same amount of useful data.

    efficiency = unique frames acknowledged by the peer / total DATA frames sent  (x 100 %)

(Measured at the sender, because only the sender knows how many attempts it made.)
"""

from __future__ import annotations

import threading
from typing import Callable

COUNTERS = (
    "frames_sent",            # DATA transmissions incl. retransmissions and lost frames
    "frames_acked",           # unique DATA frames confirmed by the peer
    "frames_received",        # DATA frames that arrived (good or bad)
    "frames_delivered",       # unique, error-free DATA frames accepted in order
    "retransmissions",
    "timeouts",
    "crc_errors_detected",    # CRC failures + structural errors
    "hamming_corrections",    # bits repaired by Hamming(7,4) (bonus mode)
    "acks_sent",
    "nacks_sent",
    "duplicates_discarded",
    "messages_delivered",     # complete messages handed to the user
    "messages_sent",          # complete messages fully acknowledged
    "send_failures",
)


class Stats:
    """Thread-safe counters; calls ``on_change(snapshot)`` after every update."""

    def __init__(self, on_change: Callable[[dict], None] | None = None) -> None:
        self._lock = threading.Lock()
        self._values = dict.fromkeys(COUNTERS, 0)
        self._on_change = on_change or (lambda snap: None)

    def incr(self, name: str, amount: int = 1) -> None:
        if name not in self._values:
            raise KeyError(name)
        with self._lock:
            self._values[name] += amount
        self._on_change(self.snapshot())

    def get(self, name: str) -> int:
        with self._lock:
            return self._values[name]

    def reset(self) -> None:
        with self._lock:
            self._values = dict.fromkeys(COUNTERS, 0)
        self._on_change(self.snapshot())

    def snapshot(self) -> dict:
        """All counters plus efficiency in percent (None until something was sent)."""
        with self._lock:
            snap = dict(self._values)
        sent = snap["frames_sent"]
        snap["efficiency"] = round(100.0 * snap["frames_acked"] / sent, 1) if sent else None
        return snap

    def format_table(self) -> str:
        """Text table for the /stats terminal command."""
        snap = self.snapshot()
        eff = snap.pop("efficiency")
        width = max(len(k) for k in snap)
        lines = [f"  {k:<{width}} : {v}" for k, v in snap.items()]
        lines.append(f"  {'efficiency':<{width}} : {'-' if eff is None else f'{eff:.1f} %'}")
        return "\n".join(lines)
