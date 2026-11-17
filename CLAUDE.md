# CLAUDE.md — DCCN Project: P2P Socket Network with Custom Framing, Channel Simulation, Error Detection & Web Visualization

This file is the single source of truth for this project. Read it fully before every phase.
If anything in a prompt conflicts with this file, ask before deviating.

## 1. What this project is

A pure-software Peer-to-Peer network simulator for a university Data Communication & Computer
Networks (DCCN) course. Two identical Python "nodes" (A and B) talk to each other over **raw TCP
sockets** using a **custom Data Link protocol** built from scratch:

text → binary → frame (flags, header, payload, CRC-16) → bit stuffing → optional noise
(bit flips / dropped frames) → raw TCP → receiver de-frames, de-stuffs, verifies CRC →
ACK / NACK → Stop-and-Wait ARQ retransmission.

Every step is (a) printed to the terminal with colours and (b) streamed live to a small
browser dashboard per node, so the examiner can *see* the protocol working.

The goal is educational transparency: make framing, error detection and ARQ visible.
Code must be clean, well-commented and easy for a student to explain in a viva.

## 2. Hard constraints (never violate)

- **Python 3 standard library only.** No pip installs, no requirements.txt dependencies.
- **Frontend = plain HTML + CSS + vanilla JavaScript.** No React/Next/Vue/Tailwind/CDNs/build step.
- **Node ↔ Node communication = raw TCP sockets only** carrying our own frames. Never HTTP/WebSocket between nodes.
- **HTTP is used ONLY between a node and its own browser dashboard** (localhost), for display and controls.
  Live updates use **Server-Sent Events (SSE)** via `http.server` — not WebSockets.
- Both nodes run **the same code** (true P2P; no dedicated server).
- Must run on **WSL Ubuntu**, Python 3.10+.
- Use `unittest` (stdlib) for tests.
- Keep each module small and readable; prefer clarity over cleverness. Add docstrings explaining the
  networking concept each function implements.

## 3. Project structure

```
dccn-p2p/
├── CLAUDE.md
├── README.md
├── DEMO.md                 # step-by-step demo script for the viva
├── node.py                 # entry point (argparse), wires everything together
├── dccn/
│   ├── __init__.py
│   ├── config.py           # constants: FLAG, frame types, timeouts, defaults
│   ├── events.py           # event bus: terminal printer (ANSI colours) + SSE subscribers
│   ├── framer.py           # text<->bits, build/parse frames, bit stuffing/de-stuffing, deframing from stream
│   ├── crc.py              # CRC-16-CCITT via manual mod-2 long division (+ verification helper)
│   ├── noise.py            # noise injector (bit flips, drop frame, drop ACK)
│   ├── transport.py        # sockets + threads (listen, connect with retry, send/recv bit streams)
│   ├── arq.py              # Stop-and-Wait ARQ sender & receiver logic
│   ├── stats.py            # counters (frames sent, errors detected, retransmissions, efficiency)
│   ├── hamming.py          # BONUS (Phase 5): Hamming(7,4) single-bit correction mode
│   └── webserver.py        # ThreadingHTTPServer: static files, SSE /events, JSON control endpoints
├── web/
│   ├── index.html
│   ├── style.css
│   └── app.js
└── tests/
    ├── test_framer.py
    ├── test_crc.py
    ├── test_noise.py
    ├── test_arq.py
    └── test_hamming.py     # Phase 5
```

## 4. How to run

```
# Terminal 1
python3 node.py --name A --listen 5000 --peer 127.0.0.1:5001 --web 8000
# Terminal 2
python3 node.py --name B --listen 5001 --peer 127.0.0.1:5000 --web 8001
# Browser: open http://localhost:8000 and http://localhost:8001 side by side
```

Optional flags: `--timeout 3.0` (ARQ timeout seconds), `--delay 0.6` (step delay for demo
visibility), `--no-color`, `--code crc|hamming` (Phase 5).

Tests: `python3 -m unittest discover -s tests -v`

## 5. Frame format (Data Link layer)

All frames are handled as **bit strings** (Python `str` of '0'/'1') so every bit is visible
and stuffing can work at bit level.

```
| START FLAG | TYPE | SEQ | LEN | PAYLOAD      | CRC-16 | END FLAG |
| 01111110   | 8b   | 8b  | 8b  | LEN*8 bits   | 16b    | 01111110 |
```

- **FLAG** = `01111110` (HDLC style).
- **TYPE**: `0x01` DATA, `0x02` ACK, `0x03` NACK.
- **SEQ**: 0 or 1 (Stop-and-Wait alternating bit).
- **LEN**: payload length in bytes (0–255). Messages longer than 255 bytes (or a configurable
  `MAX_PAYLOAD`, default 32 so multi-frame sends are easy to demo) are split into multiple frames.
- **PAYLOAD**: UTF-8 bytes of the message as bits (8 bits per byte).
- **CRC-16**: computed over TYPE+SEQ+LEN+PAYLOAD bits.
- ACK/NACK frames: LEN = 0, no payload, still carry CRC.

**Build order (sender):** header+payload bits → append CRC → **bit-stuff** the body
(insert `0` after every five consecutive `1`s) → wrap with flags.

**Parse order (receiver):** find frame between flags in the incoming bit stream → **de-stuff** →
split fields → verify CRC → validate LEN matches payload length.
Any structural failure (bad de-stuffing, wrong length, unknown type) = corrupted frame.

**On the wire:** the stuffed frame bit string is sent over TCP as ASCII characters `'0'`/`'1'`.
(Rationale: stuffed frames are not byte-aligned; sending visible bits keeps the simulation
transparent. TCP is just the "wire".) The receiver keeps a buffer and extracts frames by scanning
for FLAG pairs — real flag-based framing, no newline delimiters.

## 6. CRC-16

- Generator polynomial: **CRC-16-CCITT, x^16 + x^12 + x^5 + 1 = 0x1021** (17-bit divisor `10001000000100001`).
- Implement as **textbook mod-2 (XOR) long division** on bit strings: append 16 zeros to data,
  divide, remainder = CRC. Initial value 0, no reflection, no final XOR.
- Provide `crc16_bits(bits) -> str` (16-bit remainder) and `verify(bits_with_crc) -> bool`
  (remainder of whole codeword == 0).
- Provide `crc16_steps(bits)` returning the division steps (for optional display/explanation).
- **Cross-check** in tests against `binascii.crc_hqx(data_bytes, 0)` — must match for byte-aligned input.

## 7. Noise injector (simulated physical layer)

Configurable at runtime (CLI commands + dashboard):
- `enabled` (bool), default off.
- `frame_error_prob` (0.0–1.0): probability a DATA frame gets corrupted. Default 0.3 when enabled.
- `bits_to_flip` (1–8): how many random bits to flip in a corrupted frame. Default 1.
- `drop_next_frame` (one-shot): next outgoing DATA frame is not sent at all (tests timeout).
- `drop_next_ack` (one-shot, on receiver side): next outgoing ACK is not sent (tests duplicate detection).

Rules:
- Noise is applied **after** CRC and stuffing, **only to the bits between the flags** (never the flags themselves),
  so the frame still delimits but its contents are damaged.
- Noise applies to DATA frames only (ACK/NACK are kept clean, configurable later).
- Every injection emits an event listing the flipped bit positions so the UI can highlight them.

## 8. Stop-and-Wait ARQ

**Sender** (per node, one outstanding frame at a time):
1. Take next frame from the send queue, set SEQ (alternating 0/1).
2. Send, start timer (`--timeout`, default 3s).
3. ACK with matching SEQ → success, flip SEQ, next frame.
4. NACK → retransmit same frame immediately.
5. Timeout → retransmit same frame.
6. Ignore ACKs with wrong SEQ (stale). Give up after `MAX_RETRIES` (default 10) and emit an event.

**Receiver**:
- Frame corrupted (CRC fail / structural error) → drop it, send **NACK** (with expected SEQ).
- Valid and SEQ == expected → deliver payload, send **ACK(SEQ)**, flip expected.
- Valid but SEQ != expected → **duplicate**: discard, re-send **ACK(SEQ)** (handles lost ACKs).
- Reassemble multi-frame messages and deliver the full message when the last part arrives
  (use a simple "more fragments" convention, e.g. reserve TYPE `0x04` = DATA_MORE for non-final fragments,
  or another clearly documented approach).

**Full duplex**: both nodes can send at the same time. Use two TCP connections per pair:
- Outgoing connection (this node → peer): carries this node's DATA, returns ACK/NACK for it.
- Incoming connection (peer → this node): carries peer's DATA, this node sends ACK/NACK back.
Sender and receiver state are independent. Connect with retries so start-up order does not matter.

## 9. Events (shared by terminal and dashboard)

`events.emit(type, **data)` → adds `ts`, `node`; prints a coloured terminal line; pushes JSON to all SSE clients.
Event types (extend if needed, keep names stable):

`connected`, `peer_disconnected`, `msg_queued`, `text_to_bits`, `frame_built`, `bit_stuffed`,
`noise_injected` (positions), `frame_dropped_by_noise`, `frame_sent`, `retransmit` (reason: nack|timeout),
`timeout`, `frame_received_raw`, `destuffed`, `crc_ok`, `crc_fail`, `structural_error`,
`ack_sent`, `nack_sent`, `ack_dropped_by_noise`, `ack_received`, `nack_received`, `duplicate_discarded`,
`message_delivered`, `send_failed`, `stats`, `noise_config`.

Terminal colours: green = ACK/OK/delivered, red = CRC fail/NACK/noise, yellow = retransmit/timeout,
cyan = frames/bits, dim = info. Highlight flipped bits in red inside printed bit strings.

## 10. Stats

frames_sent, frames_received, retransmissions, timeouts, crc_errors_detected, nacks_sent, acks_sent,
duplicates_discarded, messages_delivered, efficiency = unique frames delivered / total frames sent (%).
Emit a `stats` event after every change.

## 11. Terminal commands (available in every phase)

Type text + Enter = send message. Commands:
`/noise on|off`, `/prob 0.5`, `/bits 2`, `/drop` (drop next frame), `/dropack` (drop next ACK),
`/stats`, `/reset`, `/help`, `/quit`.

## 12. Web dashboard (Phase 4)

Served by each node at `http://localhost:<web>`; binds 127.0.0.1 only.
Endpoints: `GET /` `GET /style.css` `GET /app.js` `GET /events` (SSE, `text/event-stream`, keep-alive pings)
`GET /state` (JSON snapshot: node name, peer, noise config, stats) `POST /send` `{text}`
`POST /noise` `{enabled, frame_error_prob, bits_to_flip, drop_next_frame, drop_next_ack}` `POST /reset`.

UI sections:
1. Header: node name, peer address, connection status dot.
2. Send box (+ quick-fill sample messages, including one whose bits trigger stuffing, e.g. "~~~" / "???").
3. Noise panel: toggle, probability slider, bits-to-flip, "Drop next frame", "Drop next ACK".
4. **Frame visualizer**: latest frame as coloured field blocks (FLAG | TYPE | SEQ | LEN | PAYLOAD | CRC | FLAG),
   every bit shown, stuffed bits marked, flipped bits flashing red.
5. **Timeline**: sender ↔ receiver lane diagram; DATA arrows, green ACK, red NACK, yellow TIMEOUT/retransmit.
6. Received messages panel.
7. Event log (filterable, auto-scroll).
8. Stats cards incl. efficiency %.

Clean, modern, readable on a projector (large fonts, dark theme, high contrast). Respect `--delay` so animations are followable.

## 13. Coding conventions

- Type hints, docstrings, small functions. Explain DCCN concepts in comments (e.g. "Bit stuffing ensures data transparency...").
- Thread safety: use `threading.Lock` / `queue.Queue` / `threading.Event`; no busy-waiting.
- Graceful shutdown on Ctrl+C and `/quit`; handle peer disconnect and reconnect.
- No global mutable state outside a `Node` object.
- After finishing each phase: run all tests, then give a short summary of what was built, how to test it manually,
  and a plain-English explanation of the key functions (for viva prep). Then STOP and wait for the next phase.
