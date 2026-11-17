"""Process-to-process delivery over raw TCP sockets (the simulated "wire").

True peer-to-peer: every node is at the same time a *server* (it listens for the peer) and a
*client* (it connects to the peer). That gives two independent TCP connections per pair:

    outgoing  (this node -> peer):  carries OUR DATA frames, peer replies with ACK/NACK on it
    incoming  (peer -> this node):  carries PEER'S DATA frames, we reply with ACK/NACK on it

Keeping the two directions on separate connections makes full duplex trivial: our sender
and our receiver never have to share a socket or untangle each other's ACKs.

Frames travel as the ASCII characters '0' and '1'. TCP is only the cable - all framing,
error detection and retransmission is done by our own Data Link protocol on top.
"""

from __future__ import annotations

import os
import socket
import threading
from typing import Callable

from . import config
from .events import EventBus
from .framer import StreamDeframer

FrameHandler = Callable[[str], None]
LinkHandler = Callable[[str, bool], None]


class PortInUseError(OSError):
    """Raised when the listen port is already taken (friendly message for the user)."""


class PeerTransport:
    """Owns the listening socket, the incoming and outgoing connections and their threads."""

    def __init__(self, bus: EventBus, listen_port: int, peer_host: str, peer_port: int,
                 on_data_body: FrameHandler, on_control_body: FrameHandler,
                 on_link_change: LinkHandler | None = None,
                 bind_host: str = "0.0.0.0") -> None:
        self.bus = bus
        self.listen_port = listen_port
        self.peer_host = peer_host
        self.peer_port = peer_port
        self.bind_host = bind_host
        self._on_data_body = on_data_body          # frames arriving on the incoming link
        self._on_control_body = on_control_body    # ACK/NACK arriving on the outgoing link
        self._on_link_change = on_link_change or (lambda direction, up: None)

        self._stop = threading.Event()
        self.out_up = threading.Event()   # set while the outgoing connection is alive
        self.in_up = threading.Event()    # set while an incoming connection is alive
        self._out_sock: socket.socket | None = None
        self._in_sock: socket.socket | None = None
        self._out_lock = threading.Lock()   # one sendall() at a time per socket
        self._in_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._listener: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        #: increases every time the outgoing link (re)connects; lets the sender notice
        #: that a connection was replaced in the middle of a message.
        self.out_generation = 0

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Bind the listening port and start the accept and connect threads."""
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name != "nt":
            # Lets us restart quickly after Ctrl+C (port stuck in TIME_WAIT). On Windows
            # this option would allow two programs on one port, so it is skipped there.
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((self.bind_host, self.listen_port))
        except OSError as exc:
            listener.close()
            raise PortInUseError(f"cannot listen on port {self.listen_port}: {exc.strerror or exc}. "
                                 f"Is another node already using it?") from exc
        listener.listen(1)
        listener.settimeout(0.5)  # lets the accept loop notice shutdown
        self._listener = listener
        self._spawn(self._accept_loop, "accept")
        self._spawn(self._connect_loop, "connect")

    def stop(self) -> None:
        """Graceful shutdown: stop loops, close every socket, wait briefly for threads."""
        self._stop.set()
        for sock in (self._listener, self._out_sock, self._in_sock):
            _close(sock)
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(timeout=2.0)

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    @property
    def peer_label(self) -> str:
        return f"{self.peer_host}:{self.peer_port}"

    # ------------------------------------------------------------------ sending
    def send_out(self, bits: str) -> bool:
        """Send a frame on the outgoing link (our DATA). Returns False if the link is down."""
        return self._send(self._out_sock, self._out_lock, bits)

    def send_in(self, bits: str) -> bool:
        """Send a frame on the incoming link (our ACK/NACK for the peer's DATA)."""
        return self._send(self._in_sock, self._in_lock, bits)

    def _send(self, sock: socket.socket | None, lock: threading.Lock, bits: str) -> bool:
        if sock is None:
            return False
        try:
            with lock:
                sock.sendall(bits.encode("ascii"))
            return True
        except OSError:
            return False

    # ------------------------------------------------------------------ threads
    def _spawn(self, target, name: str, *args) -> None:
        t = threading.Thread(target=target, args=args, name=f"{self.bus.node_name}-{name}",
                             daemon=True)
        self._threads.append(t)
        t.start()

    def _accept_loop(self) -> None:
        """Server role: accept the peer's connection (its DATA arrives here)."""
        while not self._stop.is_set():
            try:
                conn, addr = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break  # listener closed during shutdown
            conn.settimeout(None)
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._state_lock:
                old, self._in_sock = self._in_sock, conn
            if old is not None:
                _close(old)  # peer reconnected: the newest connection wins
            self.in_up.set()
            self.bus.emit("connected", direction="incoming", peer=f"{addr[0]}:{addr[1]}")
            self._on_link_change("incoming", True)
            self._spawn(self._read_loop, "rx-in", conn, "incoming")

    def _connect_loop(self) -> None:
        """Client role: keep (re)connecting to the peer; our DATA goes out on this link.

        Retrying means it does not matter which node is started first.
        """
        announced = False
        while not self._stop.is_set():
            try:
                sock = socket.create_connection((self.peer_host, self.peer_port), timeout=2.0)
            except OSError:
                if not announced:
                    self.bus.emit("info", text=f"waiting for peer {self.peer_label} ...")
                    announced = True
                self._stop.wait(config.CONNECT_RETRY_SECONDS)
                continue
            announced = False
            sock.settimeout(None)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._state_lock:
                self._out_sock = sock
                self.out_generation += 1
            self.out_up.set()
            self.bus.emit("connected", direction="outgoing", peer=self.peer_label)
            self._on_link_change("outgoing", True)
            self._read_loop(sock, "outgoing")  # returns when the link breaks
            if not self._stop.is_set():
                self._stop.wait(config.CONNECT_RETRY_SECONDS)

    def _read_loop(self, sock: socket.socket, direction: str) -> None:
        """Read the byte stream, cut it into frames with the deframer, hand them upward."""
        deframer = StreamDeframer()
        handler = self._on_data_body if direction == "incoming" else self._on_control_body
        reason = "connection closed by peer"
        while not self._stop.is_set():
            try:
                chunk = sock.recv(config.RECV_CHUNK)
            except OSError as exc:
                reason = f"connection error: {exc.__class__.__name__}"
                break
            if not chunk:
                break
            for body in deframer.feed(chunk.decode("ascii", errors="ignore")):
                try:
                    handler(body)
                except Exception as exc:  # never let one bad frame kill the link
                    self.bus.emit("error", text=f"frame handler crashed: {exc!r}")
        _close(sock)
        with self._state_lock:
            current = self._out_sock if direction == "outgoing" else self._in_sock
            if current is not sock:
                return  # already replaced by a newer connection
            if direction == "outgoing":
                self._out_sock = None
                self.out_up.clear()
            else:
                self._in_sock = None
                self.in_up.clear()
        if not self._stop.is_set():
            self.bus.emit("peer_disconnected", direction=direction, reason=reason)
            self._on_link_change(direction, False)


def _close(sock: socket.socket | None) -> None:
    """Shut down and close a socket, ignoring errors (it may already be closed)."""
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass
