"""CRC-16-CCITT error detection, computed by textbook mod-2 (XOR) long division.

Idea: treat the frame bits as one huge binary number and divide it by a fixed 17-bit
*generator polynomial* using XOR instead of subtraction (modulo-2 arithmetic, no carries).
The 16-bit remainder is the CRC and is appended to the frame. The receiver divides the
whole codeword (data + CRC) by the same generator: a remainder of all zeros means "no error
detected"; anything else means the frame was damaged on the way.

Generator: x^16 + x^12 + x^5 + 1  ->  0x1021  ->  10001000000100001 (17 bits).
Parameters: initial value 0, no bit reflection, no final XOR - the plain textbook version,
which gives exactly the same result as ``binascii.crc_hqx(data, 0)`` for whole bytes.

CRC-16 detects every single-bit error, every double-bit error, every odd number of bit
errors and every burst error of length <= 16.
"""

from __future__ import annotations

from . import config

POLY = config.CRC_POLY_BITS          # 17-bit divisor
CRC_LEN = len(POLY) - 1              # 16 remainder bits


def _check_bits(bits: str) -> None:
    if any(b not in "01" for b in bits):
        raise ValueError("expected a string of '0' and '1' characters")


def mod2_divide(dividend: str) -> str:
    """Return the remainder (last 16 bits) of mod-2 long division of ``dividend`` by POLY.

    Walk from left to right; whenever the current leading bit is 1, XOR the generator
    into the dividend at that position. This is exactly long division where
    'subtract' is replaced by XOR.
    """
    _check_bits(dividend)
    work = list(dividend)
    for i in range(len(work) - CRC_LEN):
        if work[i] == "1":
            for j, p in enumerate(POLY):
                work[i + j] = "0" if work[i + j] == p else "1"   # XOR bit by bit
    return "".join(work[-CRC_LEN:]) if len(work) >= CRC_LEN else "".join(work).zfill(CRC_LEN)


def crc16_bits(bits: str) -> str:
    """CRC-16 of a bit string: append 16 zeros, divide, the 16-bit remainder is the CRC."""
    return mod2_divide(bits + "0" * CRC_LEN)


def remainder(codeword: str) -> str:
    """Remainder of a received codeword (data + CRC). All zeros = no error detected."""
    return mod2_divide(codeword)


def verify(bits_with_crc: str) -> bool:
    """True if the codeword divides evenly by the generator (remainder == 0)."""
    return "1" not in remainder(bits_with_crc)


def crc16_steps(bits: str) -> list[dict]:
    """The individual XOR steps of the long division, for display in a viva.

    Each step: ``pos`` (where the divisor is aligned), ``window`` (17 bits under the
    divisor before XOR), ``result`` (those bits after XOR) and ``remaining`` (the whole
    working dividend after this step).
    """
    _check_bits(bits)
    work = list(bits + "0" * CRC_LEN)
    steps = []
    for i in range(len(work) - CRC_LEN):
        if work[i] != "1":
            continue
        window = "".join(work[i:i + len(POLY)])
        for j, p in enumerate(POLY):
            work[i + j] = "0" if work[i + j] == p else "1"
        steps.append({"pos": i, "window": window, "divisor": POLY,
                      "result": "".join(work[i:i + len(POLY)]), "remaining": "".join(work)})
    return steps


def format_division(bits: str, max_steps: int = 40) -> str:
    """Pretty, textbook-style rendering of the division (used by /crcdemo)."""
    steps = crc16_steps(bits)
    dividend = bits + "0" * CRC_LEN
    crc = crc16_bits(bits)
    lines = [
        f"Data bits      : {bits}",
        f"Generator      : {POLY}  (x^16 + x^12 + x^5 + 1 = 0x1021)",
        f"Dividend       : {bits} + {'0' * CRC_LEN} (16 zeros appended)",
        "",
        "  " + dividend,
    ]
    for n, step in enumerate(steps[:max_steps], start=1):
        pad = " " * (step["pos"] + 2)
        lines.append(f"{pad}{step['divisor']}   <- XOR step {n} at bit {step['pos']}")
        lines.append(f"{pad}{'-' * len(POLY)}")
        lines.append("  " + step["remaining"])
    if len(steps) > max_steps:
        lines.append(f"  ... {len(steps) - max_steps} more XOR steps ...")
    lines += [
        "",
        f"Remainder (CRC): {crc}  = 0x{int(crc, 2):04X}",
        f"Transmitted    : {bits} {crc}",
        f"Receiver check : remainder of codeword = {remainder(bits + crc)} -> "
        f"{'OK' if verify(bits + crc) else 'ERROR'}",
    ]
    return "\n".join(lines)
