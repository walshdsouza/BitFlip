"""Stop-and-Wait ARQ (Automatic Repeat reQuest): reliable delivery over a noisy channel.

Sender rules
    * Send ONE frame, then stop and wait for the answer before sending the next one.
    * ACK with the matching SEQ  -> success, flip SEQ (0 -> 1 -> 0 ...), next frame.
    * NACK                        -> the receiver saw a damaged frame: retransmit at once.
    * nothing before the timer    -> frame or ACK was lost: retransmit.
    * ACK with the wrong SEQ      -> a stale/duplicate ACK: ignore it.
    * give up after MAX_RETRIES retransmissions.

Receiver rules
    * damaged frame (CRC or structure)  -> drop it, send NACK(expected SEQ).
    * good frame, SEQ == expected       -> deliver payload, send ACK(SEQ), flip expected.
    * good frame, SEQ != expected       -> DUPLICATE (our ACK was lost and the sender
                                           retransmitted): discard, but ACK it again.

Why one bit of sequence number is enough: with only one frame "in flight" the receiver only
ever has to tell "the next new frame" from "a copy of the previous one" - two cases, so the
alternating bit 0/1 distinguishes them.

Each node runs one sender (its own messages, on the outgoing TCP link) and one receiver
(the peer's messages, on the incoming TCP link). They share nothing, so both nodes can send
at the same time (full duplex).
"""

from __future__ import annotations

import itertools
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable

from . import config
from .events import EventBus
from .framer import BuiltFrame, ParsedFrame, build_frame, parse_frame, split_payload, text_to_bits
from .noise import NoiseInjector
from .stats import Stats
from .transport import PeerTransport

#: Pauses the receiver makes before replying (used to stretch the timer in slow-motion).
RECEIVER_PAUSES = 3


@dataclass
class LinkContext:
    """Everything the ARQ layer needs from the node (keeps the classes easy to test)."""

    bus: EventBus
    stats: Stats
    noise: NoiseInjector
    transport: PeerTransport | None
    stop: threading.Event
    delay: float = 0.0
    code: str = config.CODE_CRC

    def pause(self, factor: float = 1.0) -> None:
        """Wait ``--delay`` seconds so a human can follow the steps (interrupted by stop)."""
        if self.delay > 0:
            self.stop.wait(self.delay * factor)


def frame_view(frame: BuiltFrame, wire: str | None = None, flipped: list[int] | None = None) -> dict:
    """Fields every 'frame on the wire' event carries for the dashboard's visualizer."""
    return {"wire": wire if wire is not None else frame.wire,
            "segments": frame.segments,
            "stuffed": frame.wire_stuffed_positions,
            "flipped": flipped or [],
            "fields": {"type": frame.kind, "seq": frame.seq, "len": len(frame.payload),
                       "payload": frame.payload.decode("utf-8", "replace"),
                       "crc": f"0x{int(frame.crc_bits, 2):04X}"}}


def parsed_view(frame: ParsedFrame) -> dict:
    """Same as :func:`frame_view` for a received frame (fields only if it parsed cleanly)."""
    fields = None
    if frame.ok:
        crc_bits = frame.body[-config.CRC_BITS:]
        fields = {"type": frame.kind, "seq": frame.seq, "len": len(frame.payload),
                  "payload": frame.payload.decode("utf-8", "replace"),
                  "crc": f"0x{int(crc_bits, 2):04X}"}
    return {"wire": frame.wire, "segments": frame.segments,
            "stuffed": [p + len(config.FLAG) for p in frame.removed_positions],
            "corrected": [p + len(config.FLAG) for p in frame.corrected_raw_positions],
            "fields": fields}


# ======================================================================================
# Sender
# ======================================================================================


@dataclass
class _Outgoing:
    msg_id: int
    text: str
    data: bytes


class StopAndWaitSender:
    """Sends our messages one frame at a time, retransmitting until each is acknowledged."""

    def __init__(self, ctx: LinkContext, timeout: float = config.DEFAULT_TIMEOUT,
                 max_retries: int = config.MAX_RETRIES,
                 max_payload: int = config.MAX_PAYLOAD) -> None:
        self.ctx = ctx
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_payload = max_payload
        self.seq = 0
        self._queue: queue.Queue[_Outgoing | None] = queue.Queue()
        self._replies: queue.Queue[tuple] = queue.Queue()
        self._ids = itertools.count(1)
        self._generation = -1          # transport.out_generation of the last message
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"{ctx.bus.node_name}-sender")

    @property
    def effective_timeout(self) -> float:
        """Timer length. In slow-motion demos the receiver pauses before replying, so the
        timer is stretched by those pauses - otherwise every frame would time out."""
        return self.timeout + RECEIVER_PAUSES * self.ctx.delay

    # ------------------------------------------------------------------ public API
    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._queue.put(None)          # wake the worker so it can exit
        self._replies.put(("stop",))

    def submit(self, text: str, data: bytes) -> int:
        """Queue a message for sending; returns its message id."""
        msg_id = next(self._ids)
        nfrags = len(split_payload(data, self.max_payload))
        self.ctx.bus.emit("msg_queued", msg_id=msg_id, text=text, nbytes=len(data),
                          fragments=nfrags)
        self._queue.put(_Outgoing(msg_id, text, data))
        return msg_id

    def on_control_body(self, body: str) -> None:
        """ACK/NACK arrived on the outgoing link (reader thread). Hand it to the worker."""
        parsed = parse_frame(body, self.ctx.code)
        if not parsed.ok:
            # A damaged ACK/NACK is simply ignored - the sender's timer will recover.
            self.ctx.stats.incr("crc_errors_detected")
            kind = "crc_fail" if parsed.error.kind == "crc" else "structural_error"
            self.ctx.bus.emit(kind, conn="outgoing", reason=parsed.error.reason,
                              remainder=parsed.remainder, **parsed_view(parsed))
            return
        if parsed.ftype in (config.TYPE_ACK, config.TYPE_NACK):
            self._replies.put((parsed.kind.lower(), parsed.seq, parsed_view(parsed)))

    def on_link_change(self, up: bool) -> None:
        """Wake a waiting worker immediately when the outgoing link breaks."""
        if not up:
            self._replies.put(("link_down",))

    # ------------------------------------------------------------------ worker
    def _run(self) -> None:
        while not self.ctx.stop.is_set():
            item = self._queue.get()
            if item is None or self.ctx.stop.is_set():
                return
            self._send_message(item)

    def _wait_for_link(self) -> bool:
        """Block until the outgoing connection is up (messages typed early are kept)."""
        transport = self.ctx.transport
        if transport.out_up.is_set():
            return True
        self.ctx.bus.emit("info", text="peer not connected yet - message waits in queue")
        while not self.ctx.stop.is_set():
            if transport.out_up.wait(0.5):
                return True
        return False

    def _send_message(self, msg: _Outgoing) -> None:
        bus, stats = self.ctx.bus, self.ctx.stats
        if not self._wait_for_link():
            return
        generation = self.ctx.transport.out_generation
        if generation != self._generation:
            # New TCP connection = new link: both ends restart from SEQ 0.
            self.seq = 0
            self._generation = generation
        fragments = split_payload(msg.data, self.max_payload)
        for index, chunk in enumerate(fragments, start=1):
            last = index == len(fragments)
            ftype = config.TYPE_DATA if last else config.TYPE_DATA_MORE
            info = {"msg_id": msg.msg_id, "frag": index, "nfrags": len(fragments)}
            bus.emit("text_to_bits", chunk=chunk.decode("utf-8", "replace"),
                     bits=text_to_bits(chunk.decode("utf-8", "replace")), **info)
            self.ctx.pause()
            frame = build_frame(ftype, self.seq, chunk, self.ctx.code)
            bus.emit("frame_built", kind=frame.kind, seq=frame.seq, length=len(chunk),
                     crc=frame.crc_bits, bits=frame.body_bits, header=frame.header_bits,
                     payload_bits=frame.payload_bits, **info)
            if frame.coded_bits != frame.body_bits:
                bus.emit("hamming_encoded", data_len=len(frame.body_bits),
                         coded_len=len(frame.coded_bits), bits=frame.coded_bits, **info)
            self.ctx.pause()
            bus.emit("bit_stuffed", bits=frame.stuffed_bits, stuffed_body=frame.stuffed_positions,
                     count=len(frame.stuffed_positions), **info)
            self.ctx.pause()
            failure = self._send_until_acked(frame, info, generation)
            if failure:
                stats.incr("send_failures")
                bus.emit("send_failed", reason=failure, seq=frame.seq, **info)
                return
            stats.incr("frames_acked")
            self.seq ^= 1
        stats.incr("messages_sent")
        bus.emit("message_acked", msg_id=msg.msg_id, text=msg.text, frames=len(fragments))

    def _send_until_acked(self, frame: BuiltFrame, info: dict, generation: int) -> str | None:
        """Transmit ``frame`` until its ACK arrives. Returns None or a failure reason."""
        bus, stats = self.ctx.bus, self.ctx.stats
        self._drain_replies()
        reason = ""
        for attempt in range(1, self.max_retries + 2):
            if self.ctx.stop.is_set():
                return "node shutting down"
            if attempt > 1:
                stats.incr("retransmissions")
                bus.emit("retransmit", seq=frame.seq, reason=reason, attempt=attempt,
                         kind=frame.kind, **info)
                self.ctx.pause()
            transport = self.ctx.transport
            if not transport.out_up.is_set() or transport.out_generation != generation:
                return "link to peer lost"
            self._transmit(frame, attempt, info)
            outcome = self._wait_reply(frame.seq)
            if outcome == "ack":
                return None
            if outcome in ("stop", "link_down"):
                return "link to peer lost" if outcome == "link_down" else "node shutting down"
            if outcome == "timeout":
                stats.incr("timeouts")
                bus.emit("timeout", seq=frame.seq, after=round(self.effective_timeout, 2),
                         attempt=attempt, **info)
            reason = outcome          # 'nack' or 'timeout'
        return f"gave up after {self.max_retries} retransmissions"

    def _transmit(self, frame: BuiltFrame, attempt: int, info: dict) -> None:
        """Put one DATA frame into the (noisy) channel."""
        bus, noise = self.ctx.bus, self.ctx.noise
        self.ctx.stats.incr("frames_sent")
        if noise.take_drop_frame():
            bus.emit("frame_dropped_by_noise", seq=frame.seq, kind=frame.kind, attempt=attempt,
                     **frame_view(frame), **info)
            return
        wire, flipped = noise.maybe_corrupt(frame.wire)
        if flipped:
            bus.emit("noise_injected", positions=flipped, before=frame.wire, after=wire,
                     seq=frame.seq, **info)
        self.ctx.transport.send_out(wire)
        bus.emit("frame_sent", kind=frame.kind, seq=frame.seq, attempt=attempt,
                 **frame_view(frame, wire, flipped), **info)

    def _wait_reply(self, seq: int) -> str:
        """Wait (blocking, no busy loop) for ACK/NACK until the timer expires."""
        deadline = time.monotonic() + self.effective_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timeout"
            try:
                reply = self._replies.get(timeout=remaining)
            except queue.Empty:
                return "timeout"
            kind = reply[0]
            if kind in ("stop", "link_down"):
                return kind
            _, reply_seq, view = reply
            if kind == "ack":
                self.ctx.bus.emit("ack_received", seq=reply_seq, **view)
                if reply_seq == seq:
                    return "ack"
                self.ctx.bus.emit("stale_ack_ignored", kind="ACK", seq=reply_seq, expected=seq)
                continue
            self.ctx.bus.emit("nack_received", seq=reply_seq, **view)
            return "nack"

    def _drain_replies(self) -> None:
        """Forget replies that belong to earlier frames before sending a new one."""
        while True:
            try:
                reply = self._replies.get_nowait()
            except queue.Empty:
                return
            if reply[0] == "stop":
                self._replies.put(reply)
                return


# ======================================================================================
# Receiver
# ======================================================================================


class StopAndWaitReceiver:
    """Checks the peer's DATA frames, answers ACK/NACK, drops duplicates, reassembles."""

    def __init__(self, ctx: LinkContext,
                 on_message: Callable[[str], None] | None = None) -> None:
        self.ctx = ctx
        self.expected = 0
        self._buffer = bytearray()
        self._frames_in_message = 0
        self._lock = threading.Lock()
        self._on_message = on_message or (lambda text: None)

    def reset(self) -> None:
        """New incoming connection = new link: expect SEQ 0, forget partial messages."""
        with self._lock:
            self.expected = 0
            self._buffer.clear()
            self._frames_in_message = 0

    def on_data_body(self, body: str) -> None:
        """A frame arrived on the incoming link (reader thread)."""
        with self._lock:
            self._handle(body)

    def _handle(self, body: str) -> None:
        bus, stats = self.ctx.bus, self.ctx.stats
        stats.incr("frames_received")
        bus.emit("frame_received_raw", bits=body, conn="incoming")
        self.ctx.pause()
        frame = parse_frame(body, self.ctx.code)
        if frame.destuffed:
            bus.emit("destuffed", bits=frame.destuffed, removed=frame.removed_positions)
        if frame.corrected_blocks:
            stats.incr("hamming_corrections", len(frame.corrected_blocks))
            bus.emit("hamming_corrected", positions=frame.corrected_positions,
                     blocks=frame.corrected_blocks, bits=frame.coded_bits,
                     wire_positions=[p + len(config.FLAG) for p in frame.corrected_raw_positions])
        if not frame.ok:
            stats.incr("crc_errors_detected")
            kind = "crc_fail" if frame.error.kind == "crc" else "structural_error"
            bus.emit(kind, conn="incoming", reason=frame.error.reason,
                     remainder=frame.remainder, **parsed_view(frame))
            self.ctx.pause()
            self._send_control(config.TYPE_NACK, self.expected)
            return
        if frame.ftype not in config.DATA_TYPES:
            bus.emit("structural_error", conn="incoming", reason=f"unexpected {frame.kind} "
                     f"frame on the data link", **parsed_view(frame))
            return
        bus.emit("crc_ok", kind=frame.kind, seq=frame.seq, remainder=frame.remainder,
                 conn="incoming", **parsed_view(frame))
        self.ctx.pause()
        if frame.seq != self.expected:
            stats.incr("duplicates_discarded")
            bus.emit("duplicate_discarded", seq=frame.seq, expected=self.expected)
            self._send_control(config.TYPE_ACK, frame.seq, duplicate=True)
            return
        # In-order, error-free, new frame: accept it.
        stats.incr("frames_delivered")
        self._buffer += frame.payload
        self._frames_in_message += 1
        self.expected ^= 1
        if frame.ftype == config.TYPE_DATA:      # last fragment -> whole message ready
            data = bytes(self._buffer)
            frames = self._frames_in_message
            self._buffer.clear()
            self._frames_in_message = 0
            text = data.decode("utf-8", "replace")
            stats.incr("messages_delivered")
            bus.emit("message_delivered", text=text, nbytes=len(data), frames=frames)
            self._on_message(text)
        self._send_control(config.TYPE_ACK, frame.seq)

    def _send_control(self, ftype: int, seq: int, duplicate: bool = False) -> None:
        """Build and send an ACK or NACK (no payload, still CRC protected)."""
        bus, stats = self.ctx.bus, self.ctx.stats
        frame = build_frame(ftype, seq, b"", self.ctx.code)
        name = "ack" if ftype == config.TYPE_ACK else "nack"
        stats.incr(f"{name}s_sent")
        if ftype == config.TYPE_ACK and self.ctx.noise.take_drop_ack():
            bus.emit("ack_dropped_by_noise", seq=seq, duplicate=duplicate, **frame_view(frame))
            return
        self.ctx.transport.send_in(frame.wire)
        bus.emit(f"{name}_sent", seq=seq, duplicate=duplicate, **frame_view(frame))
