"""Minimal protobuf wire-format inspector.

Avoids dragging in the 30-file waE2E proto graph for cases where we just
want to see *what fields are populated* (protocolMessage, historySyncNotification
appStateSyncKeyShare, etc.) without decoding their full contents.

Returns a dict of ``{field_number: [values...]}`` where each value is either
an int (varint, 32/64-bit fixed) or bytes (length-delimited — nested message
or raw bytes/string — caller decides).
"""

from __future__ import annotations

WIRE_VARINT = 0
WIRE_64BIT = 1
WIRE_LEN_DELIM = 2
WIRE_32BIT = 5


def _read_varint(data: bytes, i: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        b = data[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, i
        shift += 7
        if shift >= 64:
            raise ValueError("varint too long")


def decode_fields(data: bytes) -> dict[int, list]:
    """Walk the protobuf wire stream once.

    Repeated fields land in the same list in their encoded order. Groups
    (deprecated wire types 3/4) raise; we don't expect them in WA's schemas.
    """
    out: dict[int, list] = {}
    i = 0
    while i < len(data):
        tag, i = _read_varint(data, i)
        field = tag >> 3
        wire = tag & 0x07
        if wire == WIRE_VARINT:
            v, i = _read_varint(data, i)
        elif wire == WIRE_64BIT:
            v = int.from_bytes(data[i : i + 8], "little")
            i += 8
        elif wire == WIRE_LEN_DELIM:
            length, i = _read_varint(data, i)
            v = data[i : i + length]
            i += length
        elif wire == WIRE_32BIT:
            v = int.from_bytes(data[i : i + 4], "little")
            i += 4
        else:
            raise ValueError(f"unsupported wire type {wire} at field {field}")
        out.setdefault(field, []).append(v)
    return out


def summarize(data: bytes, max_bytes: int = 64) -> str:
    """Human-readable one-line-per-field summary — good for log dumps."""
    try:
        fields = decode_fields(data)
    except Exception as e:
        return f"(not protobuf: {e})"
    lines = []
    for f in sorted(fields):
        for v in fields[f]:
            if isinstance(v, int):
                lines.append(f"  field {f}: varint = {v}")
            else:
                body = v[:max_bytes].hex()
                more = f" ...(+{len(v) - max_bytes})" if len(v) > max_bytes else ""
                lines.append(f"  field {f}: bytes[{len(v)}] = {body}{more}")
    return "\n".join(lines)
