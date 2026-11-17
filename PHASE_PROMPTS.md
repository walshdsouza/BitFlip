# Claude Code Phase Prompts — DCCN P2P Project

How to use:
1. Do the **Setup** once.
2. Paste **one phase prompt at a time** into Claude Code.
3. After each phase, do the **"Your checklist"** before moving on. Don't skip it — this is your viva prep.
4. Commit to Git after every phase, so you can always roll back.

---

## SETUP (do this yourself, once)

In WSL Ubuntu:
```
python3 --version          # must be 3.10+
mkdir dccn-p2p && cd dccn-p2p
git init
# copy CLAUDE.md into this folder
claude                      # start Claude Code here
```

---

## ~~PHASE 1 — Transport + Framing (terminal only)~~ ✅ DONE

```
Read CLAUDE.md fully. We are building this project in 5 phases. This is PHASE 1 only.

Build:
1. Project skeleton exactly as in CLAUDE.md section 3 (create empty placeholder modules for later phases with a one-line docstring).
2. dccn/config.py with all constants (FLAG, frame types, defaults).
3. dccn/events.py: event bus with coloured terminal printing (SSE subscriber support can be a stub for now, but design the API for it).
4. dccn/framer.py: text<->bits, build_frame / parse_frame, bit stuffing / de-stuffing, and a StreamDeframer class that extracts frames from an incoming '0'/'1' character stream by scanning for FLAG pairs. For now use a placeholder CRC of 16 zeros (real CRC comes in Phase 2) but keep the CRC field in the frame.
5. dccn/transport.py: P2P transport using two TCP connections (outgoing + incoming) per CLAUDE.md section 8, with connect-retry so start order doesn't matter, receiver thread, graceful shutdown, disconnect handling.
6. node.py: argparse (--name --listen --peer --web --delay --no-color), a Node class, terminal input loop. Typing text sends it as DATA frames (split by MAX_PAYLOAD). Receiver prints the decoded message. /help and /quit work. No ARQ yet — just send and receive.
7. Every step emits events (text_to_bits, frame_built, bit_stuffed, frame_sent, frame_received_raw, destuffed, message_delivered) and prints them nicely.
8. tests/test_framer.py: round-trip tests, stuffing tests (e.g. payload containing 0x7E and runs of 1s), stream deframing with frames split across multiple recv() chunks and multiple frames in one chunk.

Constraints: standard library only. Follow CLAUDE.md conventions.

When done: run the tests, show me exact commands to run two nodes, and explain in simple words how framing, bit stuffing and the two-socket P2P setup work. Then STOP.
```

**Your checklist:**
- [x] ~~Two terminals: A sends "Hello", B prints it (and vice versa)~~
- [x] ~~Start B before A — still connects~~
- [x] ~~Send "~~~" or "???" and see a stuffed 0 being inserted~~
- [x] ~~Tests pass~~
- [ ] You can explain: why flags, why bit stuffing, why 2 sockets
- [x] ~~`git add . && git commit -m "Phase 1: transport + framing"`~~

---

## ~~PHASE 2 — CRC-16 + Noise Injector~~ ✅ DONE

```
Read CLAUDE.md again. PHASE 1 is done. This is PHASE 2 only.

Build:
1. dccn/crc.py: CRC-16-CCITT (0x1021) using textbook mod-2 long division on bit strings, exactly as CLAUDE.md section 6. Functions: crc16_bits, verify, crc16_steps (returns division steps for explanation). Replace the placeholder CRC in framer.py with this.
2. Receiver now verifies CRC and structure; emit crc_ok / crc_fail / structural_error events. Corrupted frames are dropped (no ACK/NACK yet — that's Phase 3), just log clearly.
3. dccn/noise.py: NoiseInjector per CLAUDE.md section 7 (enabled, frame_error_prob, bits_to_flip, drop_next_frame, drop_next_ack placeholder). Flip bits only between the flags. Return flipped positions. Emit noise_injected / frame_dropped_by_noise.
4. Terminal shows the transmitted frame bits with flipped bits highlighted in red.
5. Terminal commands: /noise on|off, /prob X, /bits N, /drop.
6. dccn/stats.py with the counters in CLAUDE.md section 10 (those that apply so far), and /stats command.
7. Add a /crcdemo <text> command that prints the CRC long division steps for that text (useful for viva).
8. tests/test_crc.py: compare against binascii.crc_hqx(data, 0) for many random byte strings; verify() true for clean, false for every single-bit flip of a sample codeword; burst errors up to 16 bits detected.
   tests/test_noise.py: flips exactly N bits, never touches flags, prob 0 never corrupts, prob 1 always corrupts (use seeded random).

Constraints: standard library only.

When done: run all tests, give me manual test steps, and explain CRC long division with a tiny worked example in simple words. Then STOP.
```

**Your checklist:**
- [x] ~~Noise off → all messages OK, "CRC OK"~~
- [x] ~~`/noise on`, `/prob 1` → every frame shows red flipped bit + "CRC FAIL"~~
- [x] ~~`/crcdemo Hi` shows the XOR division steps~~ — try to follow it by hand once
- [x] ~~Tests pass (including binascii match)~~
- [ ] You can explain: generator polynomial, why noise is added after the CRC
- [x] ~~Commit: "Phase 2: CRC + noise"~~

---

## PHASE 3 — Stop-and-Wait ARQ

```
Read CLAUDE.md again. PHASES 1–2 are done. This is PHASE 3 only.

Build dccn/arq.py implementing Stop-and-Wait ARQ exactly as CLAUDE.md section 8:
1. Sender: send queue, one outstanding frame, alternating SEQ 0/1, timer (--timeout), retransmit on NACK or timeout, ignore stale ACKs, MAX_RETRIES with send_failed event. Use threading.Event / Condition, no busy waiting.
2. Receiver: NACK on corrupted frame, ACK + deliver on expected SEQ, duplicate detection (discard + re-ACK), multi-frame message reassembly.
3. ACK/NACK frames use the same frame format (TYPE 0x02/0x03, LEN 0, CRC).
4. drop_next_ack works on the receiver side (/dropack command).
5. Full duplex: both nodes can send at the same time without interfering.
6. Complete all stats from CLAUDE.md section 10, including efficiency %.
7. Respect --delay between steps so the flow is followable live.
8. tests/test_arq.py: run two in-process nodes on localhost (random free ports) and test: clean delivery; delivery with prob=1.0 for first attempt then recovery (seeded / injectable noise); lost frame -> timeout -> retransmit; lost ACK -> duplicate discarded, message delivered exactly once; long message split into multiple frames delivered in order; both sides sending simultaneously.

Constraints: standard library only.

When done: run all tests, give me a manual demo walkthrough for these scenarios: (1) noiseless, (2) noisy → NACK → retransmit, (3) /drop → timeout → retransmit, (4) /dropack → duplicate discarded. Explain Stop-and-Wait and why sequence numbers are needed in simple words. Then STOP.
```

**Your checklist:**
- [ ] All 4 scenarios work in the terminal and you can see each step
- [ ] Message is never delivered twice, even with /dropack
- [ ] Long message arrives complete and in order
- [ ] Tests pass
- [ ] You can explain: ACK vs NACK vs timeout, why SEQ 0/1 is enough for Stop-and-Wait
- [ ] Commit: "Phase 3: Stop-and-Wait ARQ"

**At this point you have a complete, submittable project.** Phases 4–5 are the visual layer and polish.

---

## PHASE 4 — Web Dashboard (SSE)

```
Read CLAUDE.md again. PHASES 1–3 are done and working. This is PHASE 4 only. Do not change protocol behaviour — only add the visual layer.

Build:
1. dccn/webserver.py: ThreadingHTTPServer bound to 127.0.0.1:<--web>, endpoints exactly as CLAUDE.md section 12. SSE /events: proper headers, each event as `data: <json>\n\n`, keep-alive comment pings every ~15s, handle client disconnects, multiple tabs allowed. On connect send a snapshot (state + recent events backlog).
2. Hook events.py SSE subscribers to the real event bus (thread-safe queues per client).
3. web/index.html, style.css, app.js — plain HTML/CSS/vanilla JS only, no libraries/CDNs. Implement all UI sections from CLAUDE.md section 12:
   - Frame visualizer: coloured field blocks with every bit visible; stuffed bits marked (e.g. underlined/different colour); flipped bits flash red; show sender's transmitted frame and receiver's received frame.
   - Timeline lane diagram (this node ↔ peer) with animated arrows: DATA (blue), ACK (green), NACK (red), TIMEOUT/retransmit (yellow), dropped frame shown as an arrow that stops midway with an X.
   - Noise controls wired to POST /noise, send box to POST /send, reset button.
   - Stats cards (incl. efficiency %) updating live; event log with filter; received messages panel; connection status.
4. Dark theme, large readable fonts for a projector, responsive enough to fit two browser windows side by side on one laptop screen.
5. Terminal output and terminal commands must keep working alongside the dashboard.

When done: tell me exactly how to launch both nodes and open both dashboards, list what each panel shows, and explain in simple words how SSE pushes events from Python to the browser and why the browser-to-node HTTP link is separate from the node-to-node TCP link. Then STOP.
```

**Your checklist:**
- [ ] Both dashboards open side by side, status shows connected
- [ ] Sending from the dashboard works; frame blocks + timeline animate
- [ ] Noise toggle on the dashboard → red bits + NACK + retransmit visible
- [ ] Drop frame / Drop ACK buttons show timeout and duplicate handling
- [ ] Stats update live
- [ ] You can explain: what SSE is, why it's not used between nodes
- [ ] Commit: "Phase 4: web dashboard"

---

## PHASE 5 — Polish, Bonus Hamming, Docs, Demo Script

```
Read CLAUDE.md again. PHASES 1–4 are done. This is PHASE 5 — final polish.

1. BONUS: dccn/hamming.py — Hamming(7,4) encoding per 4-bit nibble with single-bit error correction. Add --code crc|hamming. In hamming mode, single-bit errors per block are corrected at the receiver (emit hamming_corrected with positions) and delivered without retransmission; still keep CRC on top to detect multi-bit errors. Show corrections on the dashboard. tests/test_hamming.py.
2. Robustness pass: peer disconnect/reconnect, Ctrl+C cleanup, port-in-use error messages, invalid input, empty messages, very long messages, unicode text. Fix any race conditions you find.
3. Code quality pass: consistent docstrings explaining the DCCN concept in each module, remove dead code, keep functions short.
4. README.md: project overview, architecture diagram (ASCII), frame format table, how to run, how to test, module descriptions, syllabus mapping (Unit 2: P2P + noisy/noiseless channels; Unit 3: framing, bit stuffing, CRC/Hamming, Stop-and-Wait ARQ; Unit 4: process-to-process delivery via TCP sockets).
5. DEMO.md: a 7–10 minute demo script for the viva, in order:
   (1) noiseless send, (2) bit stuffing example, (3) noisy channel → CRC fail → NACK → retransmit, (4) dropped frame → timeout, (5) dropped ACK → duplicate discarded, (6) raise noise and watch efficiency drop, (7) /crcdemo division, (8) Hamming correction bonus.
   For each: what to click/type, what appears, and one line to say to the examiner.
6. Add a VIVA_QA.md with 20 likely viva questions and short answers based on THIS implementation (e.g. "TCP already checks errors — how do errors happen?", "Why two sockets?", "Why SEQ 0/1?", "CRC vs checksum?", "Why SSE and not WebSockets?").
7. Run the full test suite and do a final end-to-end run of every DEMO.md step.

When done: give me a final summary of the project, file by file, and anything I should practise before the demo. Then STOP.
```

**Your checklist:**
- [ ] Run through DEMO.md fully, start to finish, at least twice
- [ ] Read VIVA_QA.md and answer each question out loud without looking
- [ ] Read through every module once, with Claude Code explaining anything unclear
- [ ] Final commit: "Phase 5: polish, Hamming bonus, docs"

---

## Tips while working with Claude Code

- If something breaks, don't continue to the next phase. Say: *"X isn't working: <paste error/output>. Fix it without changing anything else."*
- If it tries to pip install something or use a framework, say: *"Stop — CLAUDE.md says standard library only."*
- After each phase, ask: *"Explain <function name> line by line like I'm in a viva."*
- Keep phases in **separate sessions** (or `/clear` between them) if the conversation gets long — CLAUDE.md carries the context.
