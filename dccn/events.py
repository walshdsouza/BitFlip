"""Event bus: one place where every protocol step is announced.

Each step of the protocol (text -> bits, framing, stuffing, noise, CRC check, ACK...) calls
``bus.emit(type, **data)``. The bus then:

1. stamps the event with a timestamp and the node name,
2. prints a coloured line in the terminal (so the examiner can follow along), and
3. pushes the event as JSON-ready dict to every subscriber queue (the web dashboard's
   Server-Sent-Events streams) and to in-process listeners (used by tests).

The terminal and the dashboard therefore always show exactly the same story.
"""

from __future__ import annotations

import collections
import queue
import sys
import threading
import time
from typing import Any, Callable

from . import config

# ANSI escape codes for terminal colours.
RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
MAGENTA = "\033[95m"
CYAN = "\033[96m"
REVERSE_RED = "\033[1;97;41m"

#: Colour used for each event type in the terminal (CLAUDE.md section 9).
EVENT_COLOURS = {
    "connected": GREEN,
    "peer_disconnected": YELLOW,
    "msg_queued": BOLD,
    "text_to_bits": CYAN,
    "frame_built": CYAN,
    "hamming_encoded": CYAN,
    "bit_stuffed": CYAN,
    "noise_injected": RED,
    "frame_dropped_by_noise": RED,
    "frame_sent": CYAN,
    "retransmit": YELLOW,
    "timeout": YELLOW,
    "frame_received_raw": CYAN,
    "destuffed": CYAN,
    "hamming_corrected": MAGENTA,
    "crc_ok": GREEN,
    "crc_fail": RED,
    "structural_error": RED,
    "ack_sent": GREEN,
    "nack_sent": RED,
    "ack_dropped_by_noise": RED,
    "ack_received": GREEN,
    "nack_received": RED,
    "stale_ack_ignored": DIM,
    "duplicate_discarded": YELLOW,
    "message_delivered": GREEN + BOLD,
    "message_acked": GREEN,
    "send_failed": RED + BOLD,
    "noise_config": MAGENTA,
    "info": DIM,
    "error": RED + BOLD,
}

#: Events that are streamed to the dashboard but would only clutter the terminal.
SILENT_IN_TERMINAL = {"stats"}

Listener = Callable[[dict], None]


def highlight_bits(bits: str, red: set[int] | None = None, under: set[int] | None = None,
                   green: set[int] | None = None, color: bool = True) -> str:
    """Return ``bits`` with chosen positions coloured for the terminal.

    ``red`` marks flipped (noisy) bits, ``under`` marks stuffed bits and ``green`` marks
    bits repaired by Hamming correction. Without colour support the string is unchanged.
    """
    if not color or not (red or under or green):
        return bits
    red, under, green = red or set(), under or set(), green or set()
    out = []
    for i, b in enumerate(bits):
        if i in red:
            out.append(f"{REVERSE_RED}{b}{RESET}")
        elif i in green:
            out.append(f"\033[1;30;42m{b}{RESET}")
        elif i in under:
            out.append(f"\033[4;93m{b}{RESET}")
        else:
            out.append(b)
    return "".join(out)


class EventBus:
    """Thread-safe publisher of protocol events to the terminal and to subscribers."""

    def __init__(self, node_name: str, color: bool = True, echo: bool = True,
                 stream=None) -> None:
        self.node_name = node_name
        self.color = color
        self.echo = echo                    # print to the terminal at all?
        self._stream = stream or sys.stdout
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue] = []
        self._listeners: list[Listener] = []
        self._backlog: collections.deque[dict] = collections.deque(maxlen=config.EVENT_BACKLOG)

    # ---------------------------------------------------------------- publishing
    def emit(self, type_: str, **data: Any) -> dict:
        """Publish one event: stamp it, print it, and fan it out to every subscriber."""
        event = {"type": type_, "ts": time.time(), "node": self.node_name, **data}
        with self._lock:
            if type_ != "stats":
                self._backlog.append(event)
            subscribers = list(self._subscribers)
            listeners = list(self._listeners)
        if self.echo and type_ not in SILENT_IN_TERMINAL:
            self._print(event)
        for q in subscribers:
            q.put(event)
        for listener in listeners:
            try:
                listener(event)
            except Exception as exc:  # a broken listener must never break the protocol
                self.print_line(f"listener error: {exc!r}", RED)
        return event

    # ---------------------------------------------------------------- subscribers
    def subscribe(self) -> queue.Queue:
        """Create a queue that receives every future event (one per dashboard tab)."""
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        """Stop delivering events to ``q`` (dashboard tab closed)."""
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def close_subscribers(self) -> None:
        """Wake every subscriber with a ``None`` sentinel so its stream can end (shutdown)."""
        with self._lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            q.put(None)

    def add_listener(self, fn: Listener) -> None:
        """Call ``fn(event)`` synchronously for every event (used by tests and hooks)."""
        with self._lock:
            self._listeners.append(fn)

    def remove_listener(self, fn: Listener) -> None:
        with self._lock:
            if fn in self._listeners:
                self._listeners.remove(fn)

    def backlog(self) -> list[dict]:
        """Recent events, replayed to a dashboard when it connects."""
        with self._lock:
            return list(self._backlog)

    def clear_backlog(self) -> None:
        with self._lock:
            self._backlog.clear()

    # ---------------------------------------------------------------- terminal
    def print_line(self, text: str, colour: str = "") -> None:
        """Print one line atomically (threads must not interleave half-lines)."""
        if self.color and colour:
            text = f"{colour}{text}{RESET}"
        with self._lock:
            try:
                self._stream.write(text + "\n")
                self._stream.flush()
            except (ValueError, OSError):
                pass  # stdout closed during shutdown

    def _print(self, event: dict) -> None:
        colour = EVENT_COLOURS.get(event["type"], "")
        stamp = time.strftime("%H:%M:%S", time.localtime(event["ts"]))
        ms = int((event["ts"] % 1) * 1000)
        head = f"[{stamp}.{ms:03d}] {self.node_name} {event['type'].upper():<22}"
        summary = describe(event)
        bits_line = self._bits_line(event)
        text = f"{head} {summary}"
        if self.color and colour:
            text = f"{colour}{text}{RESET}"
        if bits_line:
            text += "\n" + bits_line
        with self._lock:
            try:
                self._stream.write(text + "\n")
                self._stream.flush()
            except (ValueError, OSError):
                pass

    def _bits_line(self, event: dict) -> str:
        """Second terminal line showing the actual bits for frame-level events."""
        t = event["type"]
        if t == "frame_sent" and event.get("wire"):
            bits = highlight_bits(event["wire"], set(event.get("flipped", [])),
                                  set(event.get("stuffed", [])), color=self.color)
        elif t == "bit_stuffed" and event.get("bits"):
            bits = highlight_bits(event["bits"], under=set(event.get("stuffed_body", [])),
                                  color=self.color)
        elif t == "noise_injected" and event.get("after"):
            bits = highlight_bits(event["after"], set(event.get("positions", [])),
                                  color=self.color)
        elif t in ("text_to_bits", "frame_built", "destuffed", "frame_received_raw") \
                and event.get("bits"):
            bits = event["bits"]
        elif t == "hamming_corrected" and event.get("bits"):
            bits = highlight_bits(event["bits"], green=set(event.get("positions", [])),
                                  color=self.color)
        else:
            return ""
        prefix = "    " + (DIM if self.color else "") + "bits: " + (RESET if self.color else "")
        return prefix + bits


def describe(event: dict) -> str:
    """One-line human readable summary of an event (used by the terminal printer)."""
    t = event["type"]
    g = event.get
    seq = g("seq")
    frag = f" frag {g('frag')}/{g('nfrags')}" if g("nfrags", 1) and g("nfrags", 1) > 1 else ""
    summaries = {
        "connected": lambda: f"{g('direction')} link up ({g('peer', '')})",
        "peer_disconnected": lambda: f"{g('direction')} link down ({g('reason', '')})",
        "msg_queued": lambda: f"#{g('msg_id')} \"{g('text')}\" -> {g('nbytes')} bytes in "
                              f"{g('fragments')} frame(s)",
        "text_to_bits": lambda: f"\"{g('chunk')}\" -> {len(g('bits', ''))} bits",
        "frame_built": lambda: f"{g('kind')} seq={seq} len={g('length')}{frag} "
                               f"CRC={g('crc')} ({len(g('bits', ''))} bits before stuffing)",
        "hamming_encoded": lambda: f"{g('data_len')} -> {g('coded_len')} bits "
                                   f"(Hamming 7,4)",
        "bit_stuffed": lambda: f"{g('count')} stuffed 0(s) inserted (underlined)",
        "noise_injected": lambda: f"flipped {len(g('positions', []))} bit(s) at "
                                  f"{g('positions')} (red)",
        "frame_dropped_by_noise": lambda: f"DATA seq={seq} lost in the channel",
        "frame_sent": lambda: f"{g('kind')} seq={seq}{frag} {len(g('wire', ''))} bits"
                              + (f" attempt {g('attempt')}" if g("attempt", 1) > 1 else ""),
        "retransmit": lambda: f"seq={seq} reason={g('reason')} attempt {g('attempt')}",
        "timeout": lambda: f"no reply for seq={seq} after {g('after')}s",
        "frame_received_raw": lambda: f"{len(g('bits', ''))} bits between flags "
                                      f"({g('conn')} link)",
        "destuffed": lambda: f"removed {len(g('removed', []))} stuffed 0(s)",
        "hamming_corrected": lambda: f"corrected {len(g('positions', []))} bit(s) in "
                                     f"blocks {g('blocks')}",
        "crc_ok": lambda: f"{g('kind')} seq={seq} remainder {g('remainder')} = 0  CRC OK",
        "crc_fail": lambda: f"remainder {g('remainder')} != 0  CRC FAIL -> frame dropped",
        "structural_error": lambda: f"{g('reason')} -> frame dropped",
        "ack_sent": lambda: f"ACK seq={seq}" + (" (re-ACK of duplicate)" if g("duplicate") else ""),
        "nack_sent": lambda: f"NACK (expected seq={seq})",
        "ack_dropped_by_noise": lambda: f"ACK seq={seq} lost in the channel",
        "ack_received": lambda: f"ACK seq={seq}",
        "nack_received": lambda: f"NACK seq={seq}",
        "stale_ack_ignored": lambda: f"{g('kind')} seq={seq} does not match outstanding "
                                     f"seq={g('expected')}",
        "duplicate_discarded": lambda: f"DATA seq={seq} already delivered (expected "
                                       f"{g('expected')}) -> discard, re-ACK",
        "message_delivered": lambda: f"\"{g('text')}\" ({g('nbytes')} bytes, "
                                     f"{g('frames')} frame(s))",
        "message_acked": lambda: f"#{g('msg_id')} fully acknowledged by peer",
        "send_failed": lambda: f"#{g('msg_id')} {g('reason')}",
        "noise_config": lambda: ", ".join(f"{k}={v}" for k, v in g("config", {}).items()),
        "reset": lambda: "statistics and views cleared",
        "info": lambda: str(g("text", "")),
        "error": lambda: str(g("text", "")),
    }
    fn = summaries.get(t)
    try:
        return fn() if fn else ""
    except Exception:
        return ""
