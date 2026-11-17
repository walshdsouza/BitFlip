"""Protocol constants and defaults shared by every module.

Keeping every "magic number" of the Data Link protocol in one place makes it easy to
explain the frame format in a viva and to change a default without hunting through code.
"""

# --------------------------------------------------------------------------------------
# Framing (HDLC-style)
# --------------------------------------------------------------------------------------

#: Frame delimiter. Marks the start and the end of every frame on the wire.
#: Bit stuffing guarantees this pattern can never appear *inside* a frame.
FLAG = "01111110"

#: After this many consecutive 1s the sender inserts a 0 (bit stuffing).
STUFF_RUN = 5

# Frame TYPE field values (8 bits each).
TYPE_DATA = 0x01        #: last (or only) fragment of a message
TYPE_ACK = 0x02         #: positive acknowledgement
TYPE_NACK = 0x03        #: negative acknowledgement (frame arrived corrupted)
TYPE_DATA_MORE = 0x04   #: DATA fragment with "more fragments follow"

TYPE_NAMES = {
    TYPE_DATA: "DATA",
    TYPE_ACK: "ACK",
    TYPE_NACK: "NACK",
    TYPE_DATA_MORE: "DATA_MORE",
}
DATA_TYPES = (TYPE_DATA, TYPE_DATA_MORE)

# Field widths in bits.
TYPE_BITS = 8
SEQ_BITS = 8
LEN_BITS = 8
CRC_BITS = 16
HEADER_BITS = TYPE_BITS + SEQ_BITS + LEN_BITS

#: Largest payload a single frame may carry (LEN is 8 bits, so the hard limit is 255).
#: Kept small by default so multi-frame messages are easy to demonstrate.
MAX_PAYLOAD = 32
MAX_PAYLOAD_LIMIT = 255

#: Longest message (in UTF-8 bytes) accepted from the user, to keep demos sane.
MAX_MESSAGE_BYTES = 4096

# --------------------------------------------------------------------------------------
# Error detection / correction
# --------------------------------------------------------------------------------------

#: CRC-16-CCITT generator polynomial x^16 + x^12 + x^5 + 1 (0x1021) as a 17-bit divisor.
CRC_POLY_BITS = "10001000000100001"

CODE_CRC = "crc"          #: CRC-16 only (detect errors, recover with ARQ)
CODE_HAMMING = "hamming"  #: Hamming(7,4) correction + CRC-16 detection on top
CODES = (CODE_CRC, CODE_HAMMING)

# --------------------------------------------------------------------------------------
# Stop-and-Wait ARQ
# --------------------------------------------------------------------------------------

DEFAULT_TIMEOUT = 3.0     #: seconds the sender waits for ACK/NACK before retransmitting
MAX_RETRIES = 10          #: retransmissions of one frame before giving up

# --------------------------------------------------------------------------------------
# Noise injector (simulated physical layer)
# --------------------------------------------------------------------------------------

DEFAULT_FRAME_ERROR_PROB = 0.3
DEFAULT_BITS_TO_FLIP = 1
MAX_BITS_TO_FLIP = 8

# --------------------------------------------------------------------------------------
# Transport / dashboard
# --------------------------------------------------------------------------------------

CONNECT_RETRY_SECONDS = 1.0   #: pause between attempts to reach the peer
RECV_CHUNK = 4096             #: bytes read per recv() call
DEFAULT_DELAY = 0.0           #: per-step pause for demo visibility (seconds)
EVENT_BACKLOG = 300           #: recent events replayed to a dashboard that (re)connects
SSE_PING_SECONDS = 15.0       #: keep-alive comment interval on the SSE stream
