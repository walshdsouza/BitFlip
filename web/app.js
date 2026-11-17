/* =====================================================================================
   BitFlip dashboard logic — plain JavaScript, no libraries.

   The node pushes every protocol event over Server-Sent Events (GET /events).
   This file turns each event into: a frame-scope picture, a timeline arrow, a chat
   bubble, a stats update and a log line. Buttons talk back with small JSON POSTs.
   ===================================================================================== */
"use strict";

(() => {
  const $ = (sel) => document.querySelector(sel);

  const el = {
    nodeName: $("#nodeName"), peerAddr: $("#peerAddr"), ledOut: $("#ledOut"), ledIn: $("#ledIn"),
    linkText: $("#linkText"), codeBadge: $("#codeBadge"), timing: $("#timing"), btnReset: $("#btnReset"),
    sendForm: $("#sendForm"), msgInput: $("#msgInput"), byteCount: $("#byteCount"),
    frameCount: $("#frameCount"), btnSend: $("#btnSend"),
    noiseToggle: $("#noiseToggle"), noiseText: $("#noiseText"), noiseLed: $("#noiseLed"),
    probSlider: $("#probSlider"), probValue: $("#probValue"), bitsValue: $("#bitsValue"),
    bitsDown: $("#bitsDown"), bitsUp: $("#bitsUp"), btnDropFrame: $("#btnDropFrame"),
    btnDropAck: $("#btnDropAck"),
    txView: $("#txView"), rxView: $("#rxView"),
    timeline: $("#timeline"), laneMe: $("#laneMe"), lanePeer: $("#lanePeer"),
    messages: $("#messages"), stats: $("#stats"), effValue: $("#effValue"),
    needle: $("#gaugeNeedle"), ticks: $("#gaugeTicks"),
    log: $("#log"), logFilter: $("#logFilter"), logKind: $("#logKind"), autoScroll: $("#autoScroll"),
    crcForm: $("#crcForm"), crcInput: $("#crcInput"), crcOut: $("#crcOut"), toast: $("#toast"),
  };

  /** Everything the page knows about its node. */
  const S = {
    lastId: 0,
    online: false,
    replaying: false,
    links: { outgoing: false, incoming: false },
    noise: { enabled: false, frame_error_prob: 0.3, bits_to_flip: 1 },
    maxPayload: 32,
    stats: {},
    msgs: new Map(),         // msg_id -> { el, retx }
    logItems: [],            // events attached to log lines (click -> show frame)
  };

  const MAX_TIMELINE_ROWS = 120;
  const MAX_LOG_LINES = 600;
  const TYPE_HEX = { DATA: "0x01", ACK: "0x02", NACK: "0x03", DATA_MORE: "0x04" };

  const STAT_CARDS = [
    ["frames_sent", "Frames sent", "var(--cyan)"],
    ["frames_acked", "Frames ACKed", "var(--green)"],
    ["retransmissions", "Retransmissions", "var(--amber)"],
    ["timeouts", "Timeouts", "var(--amber)"],
    ["frames_received", "Frames received", "var(--cyan)"],
    ["crc_errors_detected", "Errors detected", "var(--red)"],
    ["acks_sent", "ACKs sent", "var(--green)"],
    ["nacks_sent", "NACKs sent", "var(--red)"],
    ["duplicates_discarded", "Duplicates dropped", "var(--amber)"],
    ["messages_delivered", "Msgs delivered", "var(--green)"],
    ["messages_sent", "Msgs sent ✓", "var(--green)"],
    ["hamming_corrections", "Hamming fixes", "var(--magenta)"],
  ];

  // ----------------------------------------------------------------------- helpers
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const clock = (ts) => new Date(ts * 1000).toTimeString().slice(0, 8);
  const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
  const shorten = (s, n) => (s.length > n ? s.slice(0, n - 1) + "…" : s);

  let toastTimer = 0;
  function toast(text, kind = "error") {
    el.toast.textContent = text;
    el.toast.className = `toast show ${kind === "info" ? "info" : ""}`;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (el.toast.className = "toast"), 3200);
  }

  async function post(path, body) {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.ok === false) throw new Error(data.error || `${res.status} ${res.statusText}`);
    return data;
  }

  function pressFx(btn) {
    btn.classList.add("pressed");
    setTimeout(() => btn.classList.remove("pressed"), 140);
  }

  function restartAnim(node, cls) {
    node.classList.remove("flash", "flash-bad");
    void node.offsetWidth;        // reflow so the animation plays again
    node.classList.add(cls);
  }

  // ----------------------------------------------------------------------- header / links
  function renderLinks() {
    const { outgoing, incoming } = S.links;
    el.ledOut.className = "led " + (S.online && outgoing ? "on" : S.online ? "bad" : "");
    el.ledIn.className = "led " + (S.online && incoming ? "on" : S.online ? "bad" : "");
    let text = "WAITING FOR PEER", cls = "link-state bad";
    if (!S.online) { text = "DASHBOARD OFFLINE"; cls = "link-state bad"; }
    else if (outgoing && incoming) { text = "CONNECTED"; cls = "link-state ok"; }
    else if (outgoing || incoming) { text = "HALF-OPEN"; cls = "link-state"; }
    el.linkText.textContent = text;
    el.linkText.className = cls;
  }

  function applyState(st) {
    el.nodeName.textContent = st.node;
    document.title = `Node ${st.node} · BitFlip`;
    el.peerAddr.textContent = st.peer;
    el.laneMe.textContent = `NODE ${st.node}`;
    el.lanePeer.textContent = `PEER :${String(st.peer).split(":").pop()}`;
    const hamming = st.code === "hamming";
    el.codeBadge.textContent = hamming ? "HAMMING(7,4) + CRC-16" : "CRC-16";
    el.codeBadge.className = "badge" + (hamming ? " hamming" : "");
    el.timing.textContent = `t/o ${Number(st.effective_timeout).toFixed(1)}s · delay ${st.delay}s`;
    const anim = Math.min(1.4, Math.max(0.35, (st.delay || 0) * 0.9));
    document.documentElement.style.setProperty("--anim", `${anim}s`);
    S.maxPayload = st.max_payload || 32;
    S.links = { ...st.links };
    applyNoise(st.noise);
    applyStats(st.stats, false);
    renderLinks();
    updateCounter();
  }

  // ----------------------------------------------------------------------- noise panel
  function applyNoise(cfg) {
    if (!cfg) return;
    S.noise = cfg;
    el.noiseToggle.checked = !!cfg.enabled;
    el.noiseText.textContent = cfg.enabled ? "ON" : "OFF";
    el.noiseLed.className = "led small" + (cfg.enabled ? " red" : "");
    if (document.activeElement !== el.probSlider) el.probSlider.value = cfg.frame_error_prob;
    el.probValue.textContent = Number(cfg.frame_error_prob).toFixed(2);
    el.bitsValue.textContent = cfg.bits_to_flip;
    el.btnDropFrame.classList.toggle("armed", !!cfg.drop_next_frame);
    el.btnDropAck.classList.toggle("armed", !!cfg.drop_next_ack);
  }

  async function setNoise(patch) {
    try {
      const res = await post("/noise", patch);
      applyNoise(res.noise);
    } catch (err) {
      toast(`Noise: ${err.message}`);
      applyNoise(S.noise);
    }
  }

  el.noiseToggle.addEventListener("change", () => setNoise({ enabled: el.noiseToggle.checked }));
  el.probSlider.addEventListener("input", () => {
    el.probValue.textContent = Number(el.probSlider.value).toFixed(2);
  });
  el.probSlider.addEventListener("change", () => setNoise({ frame_error_prob: Number(el.probSlider.value) }));
  el.bitsDown.addEventListener("click", () => {
    pressFx(el.bitsDown);
    if (S.noise.bits_to_flip > 1) setNoise({ bits_to_flip: S.noise.bits_to_flip - 1 });
  });
  el.bitsUp.addEventListener("click", () => {
    pressFx(el.bitsUp);
    if (S.noise.bits_to_flip < 8) setNoise({ bits_to_flip: S.noise.bits_to_flip + 1 });
  });
  el.btnDropFrame.addEventListener("click", () => setNoise({ drop_next_frame: !S.noise.drop_next_frame }));
  el.btnDropAck.addEventListener("click", () => setNoise({ drop_next_ack: !S.noise.drop_next_ack }));

  // ----------------------------------------------------------------------- send box
  const encoder = new TextEncoder();
  function updateCounter() {
    const bytes = encoder.encode(el.msgInput.value).length;
    el.byteCount.textContent = plural(bytes, "byte");
    el.frameCount.textContent = bytes ? plural(Math.ceil(bytes / S.maxPayload), "frame") : "0 frames";
  }
  el.msgInput.addEventListener("input", updateCounter);
  el.msgInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      el.sendForm.requestSubmit();
    }
  });
  el.sendForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = el.msgInput.value;
    if (!text.trim()) { toast("Type a message first"); return; }
    el.btnSend.disabled = true;
    try {
      await post("/send", { text });
      el.msgInput.value = "";
      updateCounter();
    } catch (err) {
      toast(`Send failed: ${err.message}`);
    } finally {
      el.btnSend.disabled = false;
      el.msgInput.focus();
    }
  });
  document.querySelectorAll(".chip[data-sample]").forEach((chip) => {
    chip.addEventListener("click", () => {
      el.msgInput.value = chip.dataset.sample;
      updateCounter();
      el.msgInput.focus();
    });
  });
  el.btnReset.addEventListener("click", async () => {
    pressFx(el.btnReset);
    try { await post("/reset"); } catch (err) { toast(`Reset failed: ${err.message}`); }
  });

  // ----------------------------------------------------------------------- frame scope
  function fieldValue(name, f) {
    if (name === "FLAG") return "0x7E";
    if (!f) return name === "BODY" ? "unreadable" : "";
    switch (name) {
      case "TYPE": return `${TYPE_HEX[f.type] || "?"} ${f.type}`;
      case "SEQ": return String(f.seq);
      case "LEN": return `${f.len} B`;
      case "PAYLOAD": return `"${shorten(f.payload, 22)}"`;
      case "CRC": return f.crc;
      default: return "";
    }
  }

  function bitsHtml(wire, start, end, sets) {
    let out = "";
    for (let i = start; i < end; i++) {
      const cls = sets.flipped.has(i) ? "b f" : sets.corrected.has(i) ? "b c" : sets.stuffed.has(i) ? "b s" : "";
      out += cls ? `<i class="${cls}">${wire[i]}</i>` : wire[i];
    }
    return out;
  }

  /** Draw one frame as coloured field blocks with every bit visible. */
  function renderFrame(target, ev, verdict, note) {
    if (!ev.wire) return;
    const wire = ev.wire;
    const sets = {
      stuffed: new Set(ev.stuffed || []),
      flipped: new Set(ev.flipped || []),
      corrected: new Set(ev.corrected || []),
    };
    const f = ev.fields;
    const kind = (f && f.type) || ev.kind || "FRAME";
    const kcls = kind.startsWith("DATA") ? "data" : kind === "ACK" ? "ack" : kind === "NACK" ? "nack" : "";
    const tags = [`<span class="tag ${kcls}">${esc(kind)}</span>`];
    if (f) tags.push(`<span class="tag">SEQ ${f.seq}</span>`, `<span class="tag">LEN ${f.len}</span>`);
    if (ev.nfrags > 1) tags.push(`<span class="tag">FRAG ${ev.frag}/${ev.nfrags}</span>`);
    if (ev.attempt > 1) tags.push(`<span class="tag warn">ATTEMPT ${ev.attempt}</span>`);
    if (sets.corrected.size) tags.push(`<span class="tag fix">✚ ${sets.corrected.size} CORRECTED</span>`);
    if (verdict) tags.push(`<span class="tag ${verdict.cls}">${esc(verdict.text)}</span>`);
    const info = [`${wire.length} bits`, `${sets.stuffed.size} stuffed`];
    if (sets.flipped.size) info.push(`${sets.flipped.size} flipped`);
    tags.push(`<span class="info">${info.join(" · ")}</span>`);

    const blocks = (ev.segments || []).map((seg) => {
      const grow = seg.field === "PAYLOAD" || seg.field === "BODY" ? " grow" : "";
      const value = fieldValue(seg.field, f);
      return `<div class="fblock f-${esc(seg.field)}${grow}">
          <div class="fl"><span>${esc(seg.field)}</span><span class="fv">${esc(value)}</span></div>
          <div class="fbits">${bitsHtml(wire, seg.start, seg.end, sets)}</div>
        </div>`;
    }).join("");

    target.innerHTML = `<div class="fmeta">${tags.join("")}</div>
      <div class="fblocks">${blocks}</div>${note ? `<div class="fnote">${note}</div>` : ""}`;
    restartAnim(target, verdict && verdict.cls === "bad" ? "flash-bad" : "flash");
  }

  function scopeFor(ev) {
    const t = ev.type;
    const stuffedNote = (ev.stuffed || []).length
      ? " Amber underlined 0s were <b>stuffed</b> after five 1s so the data can never look like a flag." : "";
    switch (t) {
      case "frame_sent":
        return ["tx", ev.flipped && ev.flipped.length
          ? { cls: "bad", text: `⚡ ${plural(ev.flipped.length, "BIT")} FLIPPED` }
          : { cls: "ok", text: "ON THE WIRE" },
          (ev.flipped && ev.flipped.length
            ? "Red bits were flipped by the noisy channel <b>after</b> the CRC was computed — the receiver will notice."
            : "Frame left this node intact.") + stuffedNote];
      case "frame_dropped_by_noise":
        return ["tx", { cls: "bad", text: "✕ LOST IN CHANNEL" }, "This frame never reaches the peer → no reply → the sender's timer will expire."];
      case "ack_sent":
        return ["tx", { cls: "ok", text: ev.duplicate ? "RE-ACK (DUPLICATE)" : "ACK SENT" }, "Acknowledgement: LEN = 0, no payload, still protected by CRC-16." + stuffedNote];
      case "nack_sent":
        return ["tx", { cls: "bad", text: "NACK SENT" }, `Negative acknowledgement: "frame damaged, please resend SEQ ${ev.seq}".`];
      case "ack_dropped_by_noise":
        return ["tx", { cls: "bad", text: "✕ ACK LOST" }, "This ACK vanishes → the sender times out and retransmits → we will see a duplicate."];
      case "crc_ok":
        return ["rx", { cls: "ok", text: "CRC OK · REMAINDER 0" }, `Codeword ÷ generator leaves remainder <b>${esc(ev.remainder)}</b> → no error detected.` + (ev.stuffed && ev.stuffed.length ? " Amber 0s were removed by de-stuffing." : "")];
      case "crc_fail":
        return ["rx", { cls: "bad", text: "CRC FAIL" }, `Remainder <b>${esc(ev.remainder)}</b> ≠ 0 → frame damaged → ` + (ev.conn === "outgoing" ? "damaged reply ignored (timer will recover)." : "discarded, NACK sent.")];
      case "structural_error":
        return ["rx", { cls: "bad", text: "STRUCTURE ERROR" }, `${esc(ev.reason)} → frame discarded.`];
      case "ack_received":
        return ["rx", { cls: "ok", text: "ACK RECEIVED" }, `Peer confirms SEQ ${ev.seq}.`];
      case "nack_received":
        return ["rx", { cls: "bad", text: "NACK RECEIVED" }, "Peer saw a damaged frame → retransmit immediately."];
      default:
        return null;
    }
  }

  function showInScope(ev) {
    const spec = scopeFor(ev);
    if (!spec || !ev.wire) return false;
    const [side, verdict, note] = spec;
    renderFrame(side === "tx" ? el.txView : el.rxView, ev, verdict, note);
    return true;
  }

  // ----------------------------------------------------------------------- timeline
  function tlRow(ev, inner) {
    const row = document.createElement("div");
    row.className = "tl-row";
    row.innerHTML = `<span class="tl-time">${clock(ev.ts)}</span>${inner}`;
    el.timeline.appendChild(row);
    while (el.timeline.children.length > MAX_TIMELINE_ROWS) el.timeline.firstChild.remove();
    el.timeline.scrollTop = el.timeline.scrollHeight;
  }
  function arrow(ev, dir, cls, label, opts = {}) {
    const extra = (opts.lost ? " lost" : "") + (opts.damaged ? " damaged" : "");
    tlRow(ev, `<div class="tl-arrow ${dir} ${cls}${extra}"><span class="tl-label">${label}</span></div>`);
  }
  function mark(ev, cls, label, side = "me") {
    tlRow(ev, `<span class="tl-mark ${side} ${cls}">${label}</span>`);
  }
  const fragTxt = (ev) => (ev.nfrags > 1 ? ` [${ev.frag}/${ev.nfrags}]` : "");

  function timelineFor(ev) {
    const s = ev.seq;
    switch (ev.type) {
      case "frame_sent": {
        const flipped = ev.flipped && ev.flipped.length;
        const label = `${ev.attempt > 1 ? "↻ " : ""}DATA ${s}${fragTxt(ev)}${flipped ? ' <span class="x">⚡ noise</span>' : ""}`;
        return arrow(ev, "out", ev.attempt > 1 ? "retx" : "data", label, { damaged: flipped });
      }
      case "frame_dropped_by_noise":
        return arrow(ev, "out", ev.attempt > 1 ? "retx" : "data", `DATA ${s}${fragTxt(ev)} <span class="x">lost</span>`, { lost: true });
      case "ack_received": return arrow(ev, "in", "ack", `ACK ${s}`);
      case "nack_received": return arrow(ev, "in", "nack", `NACK ${s}`);
      case "stale_ack_ignored": return mark(ev, "info", `stale ACK ${s} ignored`);
      case "timeout": return mark(ev, "warn", `⏱ TIMEOUT · seq ${s}`);
      case "crc_ok": return ev.conn === "incoming" ? arrow(ev, "in", "data", `DATA ${s}`) : undefined;
      case "crc_fail":
      case "structural_error":
        return ev.conn === "outgoing"
          ? arrow(ev, "in", "ack", 'reply <span class="x">✕ damaged</span>', { damaged: true })
          : arrow(ev, "in", "data", `DATA <span class="x">✕ ${ev.type === "crc_fail" ? "CRC FAIL" : "BAD FRAME"}</span>`, { damaged: true });
      case "ack_sent": return arrow(ev, "out", "ack", `ACK ${s}${ev.duplicate ? " (dup)" : ""}`);
      case "nack_sent": return arrow(ev, "out", "nack", `NACK · expect ${s}`);
      case "ack_dropped_by_noise": return arrow(ev, "out", "ack", `ACK ${s} <span class="x">lost</span>`, { lost: true });
      case "duplicate_discarded": return mark(ev, "warn", `DUPLICATE seq ${s} discarded`);
      case "hamming_corrected": return mark(ev, "ok", `✚ HAMMING fixed ${plural((ev.positions || []).length, "bit")}`);
      case "message_delivered": return mark(ev, "ok", `✉ “${esc(shorten(ev.text, 18))}” delivered`);
      case "message_acked": return mark(ev, "ok", `✓ message #${ev.msg_id} acknowledged`);
      case "send_failed": return mark(ev, "bad", `✕ #${ev.msg_id} failed`);
      case "connected": return mark(ev, "info", `${ev.direction} link up`);
      case "peer_disconnected": return mark(ev, "bad", `${ev.direction} link down`);
      default: return undefined;
    }
  }

  // ----------------------------------------------------------------------- messages
  function clearEmpty(box) {
    const empty = box.querySelector(".empty");
    if (empty) empty.remove();
  }
  function bubble(dir, text, meta) {
    clearEmpty(el.messages);
    const b = document.createElement("div");
    b.className = `bubble ${dir}`;
    b.innerHTML = `<div class="txt"></div><div class="meta">${meta}</div>`;
    b.querySelector(".txt").textContent = text;
    el.messages.appendChild(b);
    el.messages.scrollTop = el.messages.scrollHeight;
    return b;
  }
  function setStatus(msgId, cls, text) {
    const m = S.msgs.get(msgId);
    if (!m) return;
    const retx = m.retx ? ` · ↻ ${m.retx}` : "";
    m.el.querySelector(".meta").innerHTML =
      `<span>#${msgId} · ${plural(m.frames, "frame")}${retx}</span><span class="st ${cls}">${esc(text)}</span>`;
    if (cls === "fail") m.el.classList.add("failed");
  }
  function messagesFor(ev) {
    switch (ev.type) {
      case "msg_queued": {
        const b = bubble("out", ev.text, "");
        S.msgs.set(ev.msg_id, { el: b, retx: 0, frames: ev.fragments });
        return setStatus(ev.msg_id, "queued", "QUEUED");
      }
      case "text_to_bits":
        if (ev.frag === 1) setStatus(ev.msg_id, "sending", "SENDING…");
        return;
      case "retransmit": {
        const m = S.msgs.get(ev.msg_id);
        if (m) { m.retx += 1; setStatus(ev.msg_id, "sending", "RETRANSMITTING…"); }
        return;
      }
      case "message_acked": return setStatus(ev.msg_id, "done", "✓✓ DELIVERED");
      case "send_failed": return setStatus(ev.msg_id, "fail", `✕ ${ev.reason}`);
      case "message_delivered":
        bubble("in", ev.text, `<span>from peer · ${plural(ev.frames, "frame")} · ${plural(ev.nbytes, "byte")}</span><span>${clock(ev.ts)}</span>`);
        return;
      default: return;
    }
  }

  // ----------------------------------------------------------------------- stats + gauge
  function buildStats() {
    el.stats.innerHTML = STAT_CARDS.map(([key, label, color]) =>
      `<div class="stat" data-key="${key}" style="--sc:${color}"><div class="sv">0</div><div class="sl">${label}</div></div>`).join("");
    // gauge ticks
    let ticks = "";
    for (let i = 0; i <= 10; i++) {
      const a = Math.PI - (i / 10) * Math.PI;
      const major = i % 5 === 0;
      const r1 = major ? 62 : 66, r2 = 73;
      const x1 = 100 + r1 * Math.cos(a), y1 = 104 - r1 * Math.sin(a);
      const x2 = 100 + r2 * Math.cos(a), y2 = 104 - r2 * Math.sin(a);
      ticks += `<line class="tick${major ? " major" : ""}" x1="${x1.toFixed(1)}" y1="${y1.toFixed(1)}" x2="${x2.toFixed(1)}" y2="${y2.toFixed(1)}"/>`;
      if (major) {
        const lx = 100 + 52 * Math.cos(a), ly = 104 - 52 * Math.sin(a) + 3;
        ticks += `<text class="tick-label" x="${lx.toFixed(1)}" y="${ly.toFixed(1)}">${i * 10}</text>`;
      }
    }
    el.ticks.innerHTML = ticks;
  }

  function applyStats(stats, animate = true) {
    if (!stats) return;
    for (const [key] of STAT_CARDS) {
      const card = el.stats.querySelector(`[data-key="${key}"]`);
      if (!card) continue;
      const v = stats[key] ?? 0;
      const sv = card.querySelector(".sv");
      if (sv.textContent !== String(v)) {
        sv.textContent = v;
        if (animate) { card.classList.remove("bump"); void card.offsetWidth; card.classList.add("bump"); }
      }
    }
    const eff = stats.efficiency;
    const p = eff == null ? 0 : Math.max(0, Math.min(100, eff)) / 100;
    el.needle.style.transform = `rotate(${-90 + 180 * p}deg)`;
    el.effValue.textContent = eff == null ? "—" : `${eff.toFixed(1)}%`;
    const color = eff == null ? "var(--muted)" : eff >= 75 ? "var(--green)" : eff >= 40 ? "var(--amber)" : "var(--red)";
    el.effValue.style.color = color;
    S.stats = stats;
  }

  // ----------------------------------------------------------------------- event log
  const CAT = {
    frames: new Set(["text_to_bits", "frame_built", "hamming_encoded", "bit_stuffed", "frame_sent",
      "frame_received_raw", "destuffed", "crc_ok"]),
    arq: new Set(["msg_queued", "ack_sent", "ack_received", "nack_sent", "nack_received", "retransmit",
      "timeout", "duplicate_discarded", "stale_ack_ignored", "message_delivered", "message_acked",
      "send_failed", "crc_ok", "crc_fail"]),
    errors: new Set(["noise_injected", "frame_dropped_by_noise", "crc_fail", "structural_error",
      "ack_dropped_by_noise", "send_failed", "error", "timeout", "nack_sent", "nack_received",
      "duplicate_discarded", "peer_disconnected", "retransmit"]),
    link: new Set(["connected", "peer_disconnected", "info", "noise_config", "reset", "error"]),
  };
  const COLOR = {
    ok: ["crc_ok", "ack_sent", "ack_received", "message_delivered", "message_acked", "connected"],
    bad: ["crc_fail", "structural_error", "nack_sent", "nack_received", "noise_injected",
      "frame_dropped_by_noise", "ack_dropped_by_noise", "send_failed", "error", "peer_disconnected"],
    warn: ["retransmit", "timeout", "duplicate_discarded", "stale_ack_ignored"],
    frame: ["text_to_bits", "frame_built", "bit_stuffed", "frame_sent", "frame_received_raw", "destuffed"],
    fix: ["hamming_encoded", "hamming_corrected"],
  };
  const colorOf = (t) => Object.keys(COLOR).find((k) => COLOR[k].includes(t)) || "info";

  function lineMatches(line) {
    const kind = el.logKind.value;
    const q = el.logFilter.value.trim().toLowerCase();
    if (kind !== "all" && !CAT[kind].has(line.dataset.type)) return false;
    return !q || line.textContent.toLowerCase().includes(q);
  }
  function applyLogFilter() {
    for (const line of el.log.children) line.classList.toggle("hidden", !lineMatches(line));
  }
  el.logFilter.addEventListener("input", applyLogFilter);
  el.logKind.addEventListener("change", applyLogFilter);

  function logLine(ev) {
    const line = document.createElement("div");
    const clickable = ev.wire && scopeFor(ev);
    line.className = `ll c-${colorOf(ev.type)}${clickable ? " clickable" : ""}`;
    line.dataset.type = ev.type;
    if (clickable) {
      line.title = "Click to show this frame in the Frame Scope";
      line.addEventListener("click", () => showInScope(ev));
    }
    line.innerHTML = `<span class="t">${clock(ev.ts)}</span><span class="body"><span class="ty">${esc(ev.type.toUpperCase())}</span><span class="s"></span></span>`;
    line.querySelector(".s").textContent = ev.summary || "";
    if (!lineMatches(line)) line.classList.add("hidden");
    el.log.appendChild(line);
    while (el.log.children.length > MAX_LOG_LINES) el.log.firstChild.remove();
    if (el.autoScroll.checked) el.log.scrollTop = el.log.scrollHeight;
  }

  // ----------------------------------------------------------------------- dispatcher
  function clearViews() {
    el.timeline.innerHTML = "";
    el.log.innerHTML = "";
    el.messages.innerHTML = '<div class="empty">delivered messages appear here</div>';
    el.txView.innerHTML = '<div class="empty">no frame transmitted yet</div>';
    el.rxView.innerHTML = '<div class="empty">no frame received yet</div>';
    S.msgs.clear();
  }

  function handle(ev) {
    if (ev.id && ev.id <= S.lastId) return;     // already shown (snapshot overlap)
    if (ev.id) S.lastId = ev.id;
    switch (ev.type) {
      case "stats": applyStats(ev.stats, !S.replaying); return;
      case "noise_config": applyNoise(ev.config); break;
      case "connected": S.links[ev.direction] = true; renderLinks(); break;
      case "peer_disconnected": S.links[ev.direction] = false; renderLinks(); break;
      case "reset": clearViews(); break;
      default: break;
    }
    showInScope(ev);
    timelineFor(ev);
    messagesFor(ev);
    logLine(ev);
  }

  // ----------------------------------------------------------------------- SSE connection
  function connect() {
    const es = new EventSource("/events");
    es.addEventListener("snapshot", (msg) => {
      const snap = JSON.parse(msg.data);
      S.online = true;
      S.lastId = 0;
      S.replaying = true;
      clearViews();
      for (const ev of snap.backlog) handle(ev);
      S.replaying = false;
      applyState(snap.state);      // after the replay: links/stats reflect *now*
    });
    es.onmessage = (msg) => {
      try { handle(JSON.parse(msg.data)); } catch (err) { console.error(err); }
    };
    es.onerror = () => {
      if (S.online) toast("Lost contact with the node — retrying…");
      S.online = false;
      renderLinks();
    };
  }

  // ----------------------------------------------------------------------- CRC lab
  function renderDivision(text) {
    return text.split("\n").map((line) => {
      const e = esc(line);
      if (line.includes("<- XOR")) return `<span class="xor">${e}</span>`;
      if (/^\s*-+\s*$/.test(line)) return `<span class="rule">${e}</span>`;
      if (line.startsWith("Remainder") || line.startsWith("Receiver")) return `<span class="rem">${e}</span>`;
      if (/^(Data bits|Generator|Dividend|Transmitted)/.test(line)) return `<span class="hd">${e}</span>`;
      return e;
    }).join("\n");
  }
  async function crcDemo() {
    const text = el.crcInput.value;
    if (!text) { toast("Enter 1–8 characters"); return; }
    try {
      const res = await fetch(`/crcdemo?text=${encodeURIComponent(text)}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.statusText);
      el.crcOut.innerHTML = renderDivision(data.division);
    } catch (err) {
      toast(`CRC demo: ${err.message}`);
    }
  }
  el.crcForm.addEventListener("submit", (e) => { e.preventDefault(); crcDemo(); });

  // ----------------------------------------------------------------------- boot
  buildStats();
  renderLinks();
  connect();
  crcDemo();
})();
