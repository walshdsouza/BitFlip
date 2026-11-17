#!/usr/bin/env python3
"""Entry point: run one peer of the DCCN P2P network.

Example (two terminals):
    python3 node.py --name A --listen 5000 --peer 127.0.0.1:5001 --web 8000
    python3 node.py --name B --listen 5001 --peer 127.0.0.1:5000 --web 8001

Both nodes run exactly this file (true peer-to-peer, no dedicated server).

Layers inside one node:
    terminal / dashboard  ->  Node  ->  Stop-and-Wait ARQ  ->  framer (+CRC, stuffing)
                                     ->  noise injector     ->  transport (raw TCP)
"""

from __future__ import annotations

import argparse
import collections
import os
import sys
import threading
import time

from dccn import config, crc
from dccn.arq import LinkContext, StopAndWaitReceiver, StopAndWaitSender
from dccn.events import EventBus
from dccn.framer import text_to_bits
from dccn.noise import NoiseInjector
from dccn.stats import Stats
from dccn.transport import PeerTransport, PortInUseError

HELP = """Commands:
  <text>            send a message to the peer
  /noise on|off     switch channel noise on or off
  /prob 0.5         probability that a DATA frame is corrupted (0..1)
  /bits 2           number of bits flipped in a corrupted frame (1..8)
  /drop             lose the next DATA frame in the channel   (-> timeout)
  /dropack          lose the next ACK this node sends          (-> duplicate)
  /stats            show statistics
  /reset            reset statistics
  /crcdemo <text>   show the CRC-16 long division for <text>
  /help             show this help
  /quit             exit"""


class Node:
    """One peer: wires the event bus, transport, noise and ARQ layers together.

    All mutable state lives inside this object (no globals), so tests can run two nodes
    in one process.
    """

    def __init__(self, name: str, listen_port: int, peer_host: str, peer_port: int,
                 delay: float = config.DEFAULT_DELAY, timeout: float = config.DEFAULT_TIMEOUT,
                 max_payload: int = config.MAX_PAYLOAD, max_retries: int = config.MAX_RETRIES,
                 color: bool = True, bind_host: str = "0.0.0.0", echo: bool = True,
                 noise: NoiseInjector | None = None) -> None:
        self.name = name
        self.peer = f"{peer_host}:{peer_port}"
        self.listen_port = listen_port
        self.started_at = time.time()
        self.bus = EventBus(name, color=color, echo=echo)
        self.stats = Stats(on_change=lambda snap: self.bus.emit("stats", stats=snap))
        self.noise = noise or NoiseInjector()
        self._stop = threading.Event()
        self.received: collections.deque[dict] = collections.deque(maxlen=100)
        self.ctx = LinkContext(self.bus, self.stats, self.noise, None, self._stop, delay)
        self.sender = StopAndWaitSender(self.ctx, timeout, max_retries, max_payload)
        self.receiver = StopAndWaitReceiver(self.ctx, on_message=self._remember_message)
        self.transport = PeerTransport(self.bus, listen_port, peer_host, peer_port,
                                       on_data_body=self.receiver.on_data_body,
                                       on_control_body=self.sender.on_control_body,
                                       on_link_change=self._on_link_change,
                                       bind_host=bind_host)
        self.ctx.transport = self.transport

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Open the listening socket (may raise PortInUseError) and start all threads."""
        self.transport.start()
        self.sender.start()

    def stop(self) -> None:
        """Graceful shutdown: stop the ARQ worker, close sockets."""
        self._stop.set()
        self.sender.stop()
        self.transport.stop()
        self.bus.close_subscribers()

    def _on_link_change(self, direction: str, up: bool) -> None:
        if direction == "incoming" and up:
            self.receiver.reset()       # peer (re)connected: its sender restarts at SEQ 0
        if direction == "outgoing":
            self.sender.on_link_change(up)

    def _remember_message(self, text: str) -> None:
        self.received.append({"text": text, "ts": time.time()})

    @property
    def connected(self) -> bool:
        return self.transport.out_up.is_set() and self.transport.in_up.is_set()

    # ------------------------------------------------------------------ actions
    def send_text(self, text: str) -> int:
        """Validate and queue a message. Returns its id; raises ValueError if invalid."""
        if not text or not text.strip():
            raise ValueError("message is empty")
        data = text.encode("utf-8")
        if len(data) > config.MAX_MESSAGE_BYTES:
            raise ValueError(f"message is {len(data)} bytes; the limit is "
                             f"{config.MAX_MESSAGE_BYTES}")
        return self.sender.submit(text, data)

    def set_noise(self, **settings) -> dict:
        """Change noise settings (terminal or dashboard) and announce the new config."""
        cfg = self.noise.update(**settings)
        self.bus.emit("noise_config", config=cfg)
        return cfg

    def reset_stats(self) -> None:
        self.stats.reset()
        self.bus.emit("reset")

    def state(self) -> dict:
        """JSON-friendly snapshot for the dashboard."""
        return {"node": self.name, "peer": self.peer, "listen": self.listen_port,
                "connected": self.connected,
                "links": {"outgoing": self.transport.out_up.is_set(),
                          "incoming": self.transport.in_up.is_set()},
                "noise": self.noise.config(), "stats": self.stats.snapshot(),
                "delay": self.ctx.delay, "timeout": self.sender.timeout,
                "effective_timeout": self.sender.effective_timeout,
                "max_payload": self.sender.max_payload, "code": self.ctx.code,
                "received": list(self.received)}

    # ------------------------------------------------------------------ terminal
    def handle_line(self, line: str) -> bool:
        """Process one line typed in the terminal. Returns False when the node should quit."""
        line = line.strip()
        if not line:
            return True
        cmd, _, arg = line.partition(" ")
        cmd, arg = cmd.lower(), arg.strip()
        try:
            if not line.startswith("/"):
                self.send_text(line)
            elif cmd == "/quit":
                return False
            elif cmd == "/help":
                self.bus.print_line(HELP)
            elif cmd == "/noise" and arg.lower() in ("on", "off"):
                self.set_noise(enabled=arg.lower() == "on")
            elif cmd == "/prob" and arg:
                self.set_noise(frame_error_prob=float(arg))
            elif cmd == "/bits" and arg:
                self.set_noise(bits_to_flip=int(arg))
            elif cmd == "/drop":
                self.set_noise(drop_next_frame=True)
            elif cmd == "/dropack":
                self.set_noise(drop_next_ack=True)
            elif cmd == "/stats":
                self.bus.print_line("Statistics:\n" + self.stats.format_table())
            elif cmd == "/reset":
                self.reset_stats()
                self.bus.print_line("statistics reset")
            elif cmd == "/crcdemo" and arg:
                self.bus.print_line(crc.format_division(text_to_bits(arg)))
            else:
                self.bus.print_line(f"unknown or incomplete command {line!r} - type /help")
        except ValueError as exc:
            self.bus.print_line(f"invalid input: {exc}")
        return True


# ======================================================================================
# Command line
# ======================================================================================


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
    p.add_argument("--timeout", type=float, default=config.DEFAULT_TIMEOUT,
                   help="ARQ retransmission timeout in seconds (default 3.0)")
    p.add_argument("--delay", type=float, default=config.DEFAULT_DELAY,
                   help="pause between protocol steps, seconds (demo slow-motion)")
    p.add_argument("--max-payload", type=int, default=config.MAX_PAYLOAD,
                   help="bytes per frame before a message is split (1-255, default 32)")
    p.add_argument("--no-color", action="store_true", help="disable terminal colours")
    p.add_argument("--bind", default="0.0.0.0", help="address to listen on (default all)")
    return p


def enable_windows_ansi() -> None:
    """Windows consoles need VT processing switched on before ANSI colours work."""
    if os.name == "nt":
        os.system("")


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.delay < 0 or args.timeout <= 0:
        parser.error("--delay must be >= 0 and --timeout > 0")
    if not 1 <= args.max_payload <= config.MAX_PAYLOAD_LIMIT:
        parser.error("--max-payload must be between 1 and 255")
    enable_windows_ansi()
    peer_host, peer_port = args.peer
    node = Node(args.name, args.listen, peer_host, peer_port, delay=args.delay,
                timeout=args.timeout, max_payload=args.max_payload,
                color=not args.no_color, bind_host=args.bind)
    try:
        node.start()
    except PortInUseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    node.bus.print_line(f"Node {args.name} listening on {args.bind}:{args.listen}, "
                        f"peer {peer_host}:{peer_port}, ARQ timeout "
                        f"{node.sender.effective_timeout:.1f}s. Type /help for commands.")
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
