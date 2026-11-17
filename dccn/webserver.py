"""Local web dashboard: static files, a live Server-Sent-Events stream and JSON controls.

Two completely separate links exist in this project:

* node <-> node      raw TCP sockets carrying OUR frames (the network being studied)
* node <-> browser   plain HTTP on localhost, only to *display* what the node is doing
                     and to let the examiner press buttons

Server-Sent Events (SSE): the browser opens ``GET /events`` once and the server simply
keeps the response open, writing ``data: <json>\\n\\n`` whenever something happens. It is
one-way (server -> browser), built into every browser (``EventSource``) and needs nothing
but the standard library - ideal for a live log. Browser -> node commands use ordinary
``POST`` requests.

The server binds to 127.0.0.1 only, so the dashboard is never reachable from the network.
"""

from __future__ import annotations

import json
import os
import queue
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from . import config, crc
from .framer import text_to_bits
from .transport import PortInUseError

if TYPE_CHECKING:  # pragma: no cover - imported only for type hints
    from node import Node

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")

#: Only these files are served - no path from the URL ever touches the file system.
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
}
MAX_BODY = 64 * 1024
CRC_DEMO_MAX_CHARS = 8


class DashboardServer:
    """Owns the ThreadingHTTPServer (one thread per request, so many SSE tabs work)."""

    def __init__(self, node: "Node", port: int, host: str = "127.0.0.1") -> None:
        self.node = node
        self.port = port
        handler = type("BoundHandler", (DashboardHandler,), {"node": node})
        try:
            self.httpd = ThreadingHTTPServer((host, port), handler)
        except OSError as exc:
            raise PortInUseError(f"cannot start dashboard on port {port}: "
                                 f"{exc.strerror or exc}") from exc
        self.httpd.daemon_threads = True
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True,
                                        name=f"{node.name}-web")

    @property
    def url(self) -> str:
        return f"http://localhost:{self.port}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class DashboardHandler(BaseHTTPRequestHandler):
    """Routes: GET / /style.css /app.js /events /state /crcdemo, POST /send /noise /reset."""

    node: "Node"            # injected by DashboardServer
    server_version = "DCCN-Dashboard/1.0"

    def log_message(self, fmt: str, *args) -> None:
        """Silence the default per-request logging (it would flood the protocol log)."""

    # ------------------------------------------------------------------ GET
    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path in STATIC_FILES:
            self._serve_static(*STATIC_FILES[url.path])
        elif url.path == "/events":
            self._serve_events()
        elif url.path == "/state":
            self._send_json(self.node.state())
        elif url.path == "/crcdemo":
            text = parse_qs(url.query).get("text", [""])[0]
            self._crc_demo(text)
        elif url.path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
        else:
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _serve_static(self, filename: str, content_type: str) -> None:
        try:
            with open(os.path.join(WEB_DIR, filename), "rb") as fh:
                body = fh.read()
        except OSError:
            self._send_json({"error": f"{filename} missing"}, HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_events(self) -> None:
        """The SSE stream: snapshot first, then every event as it happens, plus pings."""
        bus = self.node.bus
        q = bus.subscribe()       # subscribe BEFORE the snapshot so nothing is missed
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            snapshot = {"state": self.node.state(), "backlog": bus.backlog()}
            self._write_sse(snapshot, event="snapshot", retry=2000)
            while True:
                try:
                    event = q.get(timeout=config.SSE_PING_SECONDS)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")     # comment line keeps proxies happy
                    self.wfile.flush()
                    continue
                if event is None:                      # node shutting down
                    break
                self._write_sse(event)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass                                       # browser tab closed
        finally:
            bus.unsubscribe(q)

    def _write_sse(self, payload: dict, event: str | None = None, retry: int | None = None) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=str)
        chunk = ""
        if retry is not None:
            chunk += f"retry: {retry}\n"
        if event:
            chunk += f"event: {event}\n"
        chunk += f"data: {data}\n\n"
        self.wfile.write(chunk.encode("utf-8"))
        self.wfile.flush()

    def _crc_demo(self, text: str) -> None:
        if not text or len(text) > CRC_DEMO_MAX_CHARS:
            self._send_json({"error": f"enter 1-{CRC_DEMO_MAX_CHARS} characters"},
                            HTTPStatus.BAD_REQUEST)
            return
        bits = text_to_bits(text)
        self._send_json({"text": text, "bits": bits, "poly": crc.POLY,
                         "crc": crc.crc16_bits(bits), "steps": crc.crc16_steps(bits)[:64],
                         "division": crc.format_division(bits, max_steps=64)})

    # ------------------------------------------------------------------ POST
    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            body = self._read_json()
            if path == "/send":
                text = body.get("text", "")
                if not isinstance(text, str):
                    raise ValueError("text must be a string")
                msg_id = self.node.send_text(text)
                self._send_json({"ok": True, "msg_id": msg_id})
            elif path == "/noise":
                allowed = {"enabled", "frame_error_prob", "bits_to_flip", "drop_next_frame",
                           "drop_next_ack"}
                settings = {k: v for k, v in body.items() if k in allowed}
                self._send_json({"ok": True, "noise": self.node.set_noise(**settings)})
            elif path == "/reset":
                self.node.reset_stats()
                self._send_json({"ok": True})
            else:
                self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except (ValueError, TypeError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("request body too large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def _send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
