"""Data Link layer framing: text <-> bits, frame build/parse, bit stuffing, stream deframing.

Frame layout on the wire (CLAUDE.md section 5)::

    | FLAG 01111110 | TYPE 8b | SEQ 8b | LEN 8b | PAYLOAD LEN*8b | CRC-16 | FLAG 01111110 |

Everything is handled as Python strings of '0'/'1' so that every single bit is visible and
bit stuffing can work at bit level (stuffed frames are no longer byte aligned).

Sender:   header+payload -> append CRC -> bit-stuff -> wrap with flags
Receiver: find frame between flags -> de-stuff -> verify CRC -> split fields -> check LEN
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import config, crc

# ======================================================================================
# Bits <-> bytes <-> text
# ======================================================================================


def bytes_to_bits(data: bytes) -> str:
    """Turn bytes into a '0'/'1' string, 8 bits per byte, most significant bit first."""
    return "".join(f"{byte:08b}" for byte in data)


def bits_to_bytes(bits: str) -> bytes:
    """Inverse of :func:`bytes_to_bits`. ``len(bits)`` must be a multiple of 8."""
    if len(bits) % 8:
        raise ValueError("bit string length is not a multiple of 8")
    return bytes(int(bits[i:i + 8], 2) for i in range(0, len(bits), 8))


def text_to_bits(text: str) -> str:
    """Encode text as UTF-8 and return its bits (the 'binary' step of the pipeline)."""
    return bytes_to_bits(text.encode("utf-8"))


def bits_to_text(bits: str) -> str:
    """Decode bits back to text (invalid UTF-8 is replaced, never crashes)."""
    return bits_to_bytes(bits).decode("utf-8", errors="replace")


def int_to_bits(value: int, width: int) -> str:
    """Fixed-width binary representation, e.g. ``int_to_bits(2, 8) == '00000010'``."""
    if not 0 <= value < (1 << width):
        raise ValueError(f"{value} does not fit in {width} bits")
    return format(value, f"0{width}b")


def split_payload(data: bytes, max_payload: int = config.MAX_PAYLOAD) -> list[bytes]:
    """Split a message into frame-sized fragments (segmentation).

    LEN is an 8-bit field, so one frame carries at most 255 bytes; we use a smaller
    MAX_PAYLOAD so multi-frame messages are easy to demonstrate. UTF-8 characters may be
    cut between fragments - that is fine because the receiver reassembles the *bytes*
    before decoding the text.
    """
    if not 1 <= max_payload <= config.MAX_PAYLOAD_LIMIT:
        raise ValueError("max_payload must be between 1 and 255")
    if not data:
        return [b""]
    return [data[i:i + max_payload] for i in range(0, len(data), max_payload)]


# ======================================================================================
# Bit stuffing
# ======================================================================================


class FrameError(Exception):
    """A received frame is damaged. ``kind`` is 'crc' or 'structural'."""

    def __init__(self, kind: str, reason: str) -> None:
        super().__init__(reason)
        self.kind = kind
        self.reason = reason


def stuff_bits(bits: str) -> tuple[str, list[int]]:
    """Bit stuffing: insert a 0 after every run of five consecutive 1s.

    Why: the flag 01111110 contains six 1s in a row. If the data itself contained six 1s,
    the receiver would mistake it for the end of the frame. After stuffing, the body can
    never contain six 1s in a row, so a flag can only ever mean "frame boundary". This is
    called *data transparency*: any bit pattern can be sent.

    Returns the stuffed bits and the positions (in the stuffed string) of inserted zeros.
    """
    out: list[str] = []
    inserted: list[int] = []
    run = 0
    for bit in bits:
        out.append(bit)
        if bit == "1":
            run += 1
            if run == config.STUFF_RUN:
                inserted.append(len(out))
                out.append("0")
                run = 0
        else:
            run = 0
    return "".join(out), inserted


def destuff_bits(bits: str) -> tuple[str, list[int]]:
    """Reverse bit stuffing: drop the 0 that follows every five consecutive 1s.

    If five 1s are followed by another 1, the body is invalid (a legal sender never
    produces that), so a :class:`FrameError` is raised. Returns the original bits and the
    positions (in the *received* string) of the removed zeros.
    """
    out: list[str] = []
    removed: list[int] = []
    run = 0
    i = 0
    while i < len(bits):
        bit = bits[i]
        out.append(bit)
        if bit == "1":
            run += 1
            if run == config.STUFF_RUN:
                nxt = i + 1
                if nxt < len(bits):
                    if bits[nxt] == "1":
                        raise FrameError("structural",
                                         f"six consecutive 1s at bit {nxt} (invalid stuffing)")
                    removed.append(nxt)
                    i += 1  # skip the stuffed zero
                run = 0
        else:
            run = 0
        i += 1
    return "".join(out), removed


# ======================================================================================
# Frame building / parsing
# ======================================================================================


@dataclass
class BuiltFrame:
    """Everything produced while building one frame (kept for display / events)."""

    ftype: int
    seq: int
    payload: bytes
    header_bits: str
    payload_bits: str
    crc_bits: str
    body_bits: str            # header + payload + CRC (before channel coding / stuffing)
    coded_bits: str           # body after optional Hamming(7,4) encoding
    stuffed_bits: str         # after stuffing (what goes between the flags)
    stuffed_positions: list[int]   # inserted zeros, indices inside stuffed_bits
    wire: str                 # FLAG + stuffed_bits + FLAG
    segments: list[dict] = field(default_factory=list)  # field map of the wire bits

    @property
    def kind(self) -> str:
        return config.TYPE_NAMES.get(self.ftype, f"0x{self.ftype:02x}")

    @property
    def wire_stuffed_positions(self) -> list[int]:
        """Stuffed-zero positions relative to the full wire frame (flags included)."""
        return [p + len(config.FLAG) for p in self.stuffed_positions]


@dataclass
class ParsedFrame:
    """Result of parsing one received frame body. ``error`` is None when the frame is good."""

    raw: str                          # stuffed bits between the flags, as received
    destuffed: str = ""               # after de-stuffing (still Hamming coded in that mode)
    removed_positions: list[int] = field(default_factory=list)
    coded_bits: str = ""
    body: str = ""                    # TYPE|SEQ|LEN|PAYLOAD|CRC after decoding
    corrected_positions: list[int] = field(default_factory=list)   # indices in coded_bits
    corrected_blocks: list[int] = field(default_factory=list)      # Hamming block numbers
    remainder: str = ""
    ftype: int | None = None
    seq: int | None = None
    payload: bytes = b""
    error: FrameError | None = None
    segments: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def kind(self) -> str:
        if self.ftype is None:
            return "?"
        return config.TYPE_NAMES.get(self.ftype, f"0x{self.ftype:02x}")

    @property
    def wire(self) -> str:
        return config.FLAG + self.raw + config.FLAG

    @property
    def corrected_raw_positions(self) -> list[int]:
        """Hamming-corrected bits as indices into ``raw`` (skipping removed stuffed zeros)."""
        if not self.corrected_positions:
            return []
        removed = set(self.removed_positions)
        kept = [i for i in range(len(self.raw)) if i not in removed]
        return [kept[p] for p in self.corrected_positions if p < len(kept)]


def field_labels(payload_len: int, code: str = config.CODE_CRC) -> list[str]:
    """Name of the field each coded body bit belongs to (TYPE, SEQ, LEN, PAYLOAD, CRC).

    In Hamming mode every 4 data bits become 7 coded bits; each 7-bit block belongs to the
    field of its 4 data bits (all fields are whole nibbles, so blocks never straddle).
    """
    labels = (["TYPE"] * config.TYPE_BITS + ["SEQ"] * config.SEQ_BITS
              + ["LEN"] * config.LEN_BITS + ["PAYLOAD"] * (payload_len * 8)
              + ["CRC"] * config.CRC_BITS)
    if code == config.CODE_HAMMING:
        labels = [labels[(j // 7) * 4] for j in range(len(labels) // 4 * 7)]
    return labels


def wire_segments(body_labels: list[str], stuffed_positions: list[int]) -> list[dict]:
    """Map the wire bits to frame fields so the dashboard can colour each block.

    ``body_labels`` labels the bits *before* stuffing; every stuffed zero inherits the
    label of the bit before it. Returns a list of ``{"field", "start", "end"}`` runs over
    the full wire frame (flags included), ``end`` exclusive.
    """
    stuffed = set(stuffed_positions)
    labels = ["FLAG"] * len(config.FLAG)
    src = 0
    total = len(body_labels) + len(stuffed_positions)
    for i in range(total):
        if i in stuffed:
            labels.append(labels[-1])
        else:
            labels.append(body_labels[src] if src < len(body_labels) else "BODY")
            src += 1
    labels += ["FLAG"] * len(config.FLAG)
    segments: list[dict] = []
    for i, name in enumerate(labels):
        if segments and segments[-1]["field"] == name:
            segments[-1]["end"] = i + 1
        else:
            segments.append({"field": name, "start": i, "end": i + 1})
    return segments


def channel_encode(body: str, code: str) -> str:
    """Optional forward error correction applied after the CRC (Hamming bonus mode)."""
    if code == config.CODE_CRC:
        return body
    raise ValueError(f"unknown code {code!r}")


def channel_decode(bits: str, code: str) -> tuple[str, list[int], list[int]]:
    """Undo :func:`channel_encode`. Returns (body, corrected positions, corrected blocks)."""
    if code == config.CODE_CRC:
        return bits, [], []
    raise ValueError(f"unknown code {code!r}")


def build_frame(ftype: int, seq: int, payload: bytes = b"",
                code: str = config.CODE_CRC) -> BuiltFrame:
    """Build a complete frame ready for the wire.

    Steps: TYPE|SEQ|LEN header + payload bits -> append CRC-16 -> (Hamming encode)
    -> bit-stuff -> add flags. ACK/NACK frames have LEN = 0 and no payload but still
    carry a CRC.
    """
    if ftype not in config.TYPE_NAMES:
        raise ValueError(f"unknown frame type {ftype}")
    if seq not in (0, 1):
        raise ValueError("Stop-and-Wait uses only sequence numbers 0 and 1")
    if len(payload) > config.MAX_PAYLOAD_LIMIT:
        raise ValueError("payload longer than 255 bytes does not fit the LEN field")
    header = (int_to_bits(ftype, config.TYPE_BITS) + int_to_bits(seq, config.SEQ_BITS)
              + int_to_bits(len(payload), config.LEN_BITS))
    payload_bits = bytes_to_bits(payload)
    crc_bits = crc.crc16_bits(header + payload_bits)
    body = header + payload_bits + crc_bits
    coded = channel_encode(body, code)
    stuffed, positions = stuff_bits(coded)
    wire = config.FLAG + stuffed + config.FLAG
    frame = BuiltFrame(ftype, seq, payload, header, payload_bits, crc_bits, body, coded,
                       stuffed, positions, wire)
    frame.segments = wire_segments(field_labels(len(payload), code), positions)
    return frame


def parse_frame(raw: str, code: str = config.CODE_CRC) -> ParsedFrame:
    """Parse the stuffed bits found between two flags. Never raises; sets ``error``.

    Order: de-stuff -> (Hamming decode/correct) -> structural checks -> CRC check ->
    field split -> LEN check. Any structural failure (bad stuffing, wrong size, unknown
    type, bad SEQ, LEN mismatch) or a non-zero CRC remainder marks the frame as corrupted.
    """
    result = ParsedFrame(raw=raw)
    try:
        result.destuffed, result.removed_positions = destuff_bits(raw)
        result.coded_bits = result.destuffed
        bits, result.corrected_positions, result.corrected_blocks = \
            channel_decode(result.destuffed, code)
        result.body = bits
        minimum = config.HEADER_BITS + config.CRC_BITS
        if len(bits) < minimum:
            raise FrameError("structural", f"frame too short ({len(bits)} < {minimum} bits)")
        if len(bits) % 8:
            raise FrameError("structural", f"frame is {len(bits)} bits, not a whole "
                                           f"number of bytes")
        result.remainder = crc.remainder(bits)
        if "1" in result.remainder:
            raise FrameError("crc", f"CRC remainder {result.remainder} is not zero")
        result.ftype = int(bits[0:8], 2)
        result.seq = int(bits[8:16], 2)
        length = int(bits[16:24], 2)
        payload_bits = bits[config.HEADER_BITS:-config.CRC_BITS]
        if result.ftype not in config.TYPE_NAMES:
            raise FrameError("structural", f"unknown frame type 0x{result.ftype:02x}")
        if result.seq not in (0, 1):
            raise FrameError("structural", f"invalid sequence number {result.seq}")
        if len(payload_bits) != length * 8:
            raise FrameError("structural", f"LEN says {length} bytes but payload has "
                                           f"{len(payload_bits) // 8}")
        result.payload = bits_to_bytes(payload_bits)
        result.segments = wire_segments(field_labels(length, code), result.removed_positions)
    except FrameError as err:
        result.error = err
        if not result.segments:
            result.segments = [
                {"field": "FLAG", "start": 0, "end": 8},
                {"field": "BODY", "start": 8, "end": 8 + len(raw)},
                {"field": "FLAG", "start": 8 + len(raw), "end": 16 + len(raw)},
            ]
    return result


# ======================================================================================
# Extracting frames from a TCP byte stream
# ======================================================================================


class StreamDeframer:
    """Recover frames from a continuous stream of '0'/'1' characters.

    TCP is a *byte stream*: one recv() may return half a frame, or three frames glued
    together. Real flag-based framing solves this: we keep a buffer, look for a FLAG, then
    for the next FLAG, and everything in between is one frame body. No newlines or length
    prefixes are used - only the flags delimit frames.
    """

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, chunk: str) -> list[str]:
        """Add received characters; return the bodies of all complete frames found."""
        self._buffer += "".join(c for c in chunk if c in "01")
        frames: list[str] = []
        flag = config.FLAG
        while True:
            start = self._buffer.find(flag)
            if start < 0:
                # Keep only a tail that could still be the beginning of a flag.
                self._buffer = self._buffer[-(len(flag) - 1):]
                break
            end = self._buffer.find(flag, start + len(flag))
            if end < 0:
                self._buffer = self._buffer[start:]  # wait for more bits
                break
            body = self._buffer[start + len(flag):end]
            if body:
                frames.append(body)
                self._buffer = self._buffer[end + len(flag):]
            else:
                # Two flags back to back: we were out of sync, the second flag is the
                # real start of the next frame.
                self._buffer = self._buffer[end:]
        return frames

    @property
    def pending_bits(self) -> int:
        return len(self._buffer)
