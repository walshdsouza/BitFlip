#!/usr/bin/env python3
"""Entry point: run one peer of the DCCN P2P network.

Example (two terminals):
    python3 node.py --name A --listen 5000 --peer 127.0.0.1:5001 --web 8000
    python3 node.py --name B --listen 5001 --peer 127.0.0.1:5000 --web 8001

Both nodes run exactly this file (true peer-to-peer, no dedicated server).
"""

from __future__ import annotations

import argparse
import os
import sys
import threading

from dccn import config
from dccn.events import EventBus
from dccn.framer import (FrameError, bits_to_text, build_frame, parse_frame, split_payload,
                         text_to_bits)
from dccn.transport import PeerTransport, PortInUseError

HELP = """Commands:
  <text>            send a message to the peer
  /help             show this help
  /quit             exit"""


class Node:
    """One peer: wires the event bus, transport and protocol layers together."""

    def __init__(self, name: str, listen_port: int, peer_host: str, peer_port: int,
                 delay: float = config.DEFAULT_DELAY, color: bool = True,
                 bind_host: str = "0.0.0.0", echo: bool = True) -> None:
        self.name = name
        self.delay = delay
        self.bus = EventBus(name, color=color, echo=echo)
        self._stop = threading.Event()
        self._send_seq = 0
        self._send_lock = threading.Lock()
        self._rx_buffer = bytearray()
        self.transport = PeerTransport(self.bus, listen_port, peer_host, peer_port,
                                       on_data_body=self._on_data_body,
                                       on_control_body=lambda body: None,
                                       bind_host=bind_host)

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self.transport.start()

    def stop(self) -> None:
        self._stop.set()
        self.transport.stop()

    def pause(self) -> None:
        """Wait ``--delay`` seconds between protocol steps so humans can follow them."""
        if self.delay > 0:
            self._stop.wait(self.delay)

    # ------------------------------------------------------------------ sending
    def send_text(self, text: str) -> bool:
        """Split a message into frames and send them (Phase 1: no ARQ yet)."""
        data = text.encode("utf-8")
        if not data:
            self.bus.emit("error", text="empty message not sent")
            return False
        fragments = split_payload(data)
        self.bus.emit("msg_queued", msg_id=0, text=text, nbytes=len(data),
                      fragments=len(fragments))
        with self._send_lock:
            for i, chunk in enumerate(fragments, start=1):
                last = i == len(fragments)
                ftype = config.TYPE_DATA if last else config.TYPE_DATA_MORE
                self.bus.emit("text_to_bits", chunk=chunk.decode("utf-8", "replace"),
                              bits=text_to_bits(chunk.decode("utf-8", "replace")))
                self.pause()
                frame = build_frame(ftype, self._send_seq, chunk)
                self.bus.emit("frame_built", kind=frame.kind, seq=frame.seq,
                              length=len(chunk), crc=frame.crc_bits, bits=frame.body_bits,
                              frag=i, nfrags=len(fragments))
                self.pause()
                self.bus.emit("bit_stuffed", bits=frame.stuffed_bits,
                              stuffed_body=frame.stuffed_positions,
                              count=len(frame.stuffed_positions))
                self.pause()
                if not self.transport.send_out(frame.wire):
                    self.bus.emit("send_failed", msg_id=0, reason="peer not connected")
                    return False
                self.bus.emit("frame_sent", kind=frame.kind, seq=frame.seq, wire=frame.wire,
                              stuffed=frame.wire_stuffed_positions, flipped=[],
                              frag=i, nfrags=len(fragments))
                self._send_seq ^= 1
        return True

    # ------------------------------------------------------------------ receiving
    def _on_data_body(self, body: str) -> None:
        """A frame arrived on the incoming link: de-stuff, parse, reassemble, deliver."""
        self.bus.emit("frame_received_raw", bits=body, conn="incoming")
        self.pause()
        frame = parse_frame(body)
        self.bus.emit("destuffed", bits=frame.destuffed, removed=frame.removed_positions)
        if not frame.ok:
            self.bus.emit("structural_error", reason=frame.error.reason)
            return
        self._rx_buffer += frame.payload
        if frame.ftype == config.TYPE_DATA:
            data = bytes(self._rx_buffer)
            self._rx_buffer.clear()
            self.bus.emit("message_delivered", text=data.decode("utf-8", "replace"),
                          nbytes=len(data), frames=0)

    # ------------------------------------------------------------------ terminal
    def handle_line(self, line: str) -> bool:
        """Process one line typed in the terminal. Returns False when the node should quit."""
        line = line.strip()
        if not line:
            return True
        if not line.startswith("/"):
            threading.Thread(target=self.send_text, args=(line,), daemon=True).start()
            return True
        cmd = line.split()[0].lower()
        if cmd == "/quit":
            return False
        if cmd == "/help":
            self.bus.print_line(HELP)
        else:
            self.bus.print_line(f"unknown command {cmd!r} - type /help")
        return True


def parse_peer(value: str) -> tuple[str, int]:
    """Parse ``host:port`` for the --peer option."""
    host, sep, port = value.rpartition(":")
    if not sep or not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise argparse.ArgumentTypeError("peer must look like 127.0.0.1:5001")
    return host, int(port)


def port_number(value: str) -> int:
    if not value.isdigit() or not 0 < int(value) < 65536:
        raise argparse.ArgumentTypeError("port must be 1-65535")
    return int(value)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="DCCN peer-to-peer node (custom Data Link "
                                            "protocol over raw TCP)")
    p.add_argument("--name", required=True, help="node name shown everywhere, e.g. A")
    p.add_argument("--listen", required=True, type=port_number, help="TCP port to listen on")
    p.add_argument("--peer", required=True, type=parse_peer, help="peer address host:port")
    p.add_argument("--web", type=port_number, default=None, help="dashboard HTTP port")
    p.add_argument("--delay", type=float, default=config.DEFAULT_DELAY,
                   help="pause between protocol steps, seconds (demo slow-motion)")
    p.add_argument("--no-color", action="store_true", help="disable terminal colours")
    p.add_argument("--bind", default="0.0.0.0", help="address to listen on (default all)")
    return p


def enable_windows_ansi() -> None:
    """Windows consoles need VT processing switched on before ANSI colours work."""
    if os.name == "nt":
        os.system("")


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.delay < 0:
        print("--delay must be >= 0", file=sys.stderr)
        return 2
    enable_windows_ansi()
    peer_host, peer_port = args.peer
    node = Node(args.name, args.listen, peer_host, peer_port, delay=args.delay,
                color=not args.no_color, bind_host=args.bind)
    try:
        node.start()
    except PortInUseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    node.bus.print_line(f"Node {args.name} listening on {args.bind}:{args.listen}, "
                        f"peer {peer_host}:{peer_port}. Type /help for commands.")
    try:
        for line in sys.stdin:
            if not node.handle_line(line):
                break
    except KeyboardInterrupt:
        pass
    finally:
        node.bus.print_line("shutting down ...")
        node.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
