"""Encode a Node into WhatsApp's binary XML byte stream.

Mirrors whatsmeow/binary/encoder.go. Output always begins with a leading
0x00 byte (a reserved compression flag that is always 0 in current use).
"""

from __future__ import annotations

from typing import Any

from . import tokens as T
from .jid import JID
from .node import Attrs, Node


class _Writer:
    __slots__ = ("buf",)

    def __init__(self) -> None:
        # Leading 0x00 is the reserved compression flag (always 0 here).
        self.buf = bytearray([0])

    def push_byte(self, b: int) -> None:
        self.buf.append(b & 0xFF)

    def push(self, data: bytes) -> None:
        self.buf.extend(data)

    def push_u8(self, v: int) -> None:
        self.buf.append(v & 0xFF)

    def push_u16_be(self, v: int) -> None:
        self.buf.extend(int(v & 0xFFFF).to_bytes(2, "big"))

    def push_u20_be(self, v: int) -> None:
        # 20-bit big-endian: high nibble in the top of byte 0
        self.buf.append((v >> 16) & 0x0F)
        self.buf.append((v >> 8) & 0xFF)
        self.buf.append(v & 0xFF)

    def push_u32_be(self, v: int) -> None:
        self.buf.extend(int(v & 0xFFFFFFFF).to_bytes(4, "big"))


def encode_node(node: Node) -> bytes:
    """Serialize a Node into WhatsApp binary XML."""
    w = _Writer()
    _write_node(w, node)
    return bytes(w.buf)


def encode_bytes(data: bytes) -> bytes:
    """Encode raw bytes as a binary value (Binary8/20/32 + payload). Rare standalone use."""
    w = _Writer()
    _write_byte_length(w, len(data))
    w.push(data)
    return bytes(w.buf)


def _write_node(w: _Writer, n: Node) -> None:
    # Special case: a node with tag "0" serializes as List8 + ListEmpty.
    if n.tag == "0":
        w.push_byte(T.LIST_8)
        w.push_byte(T.LIST_EMPTY)
        return

    attr_count = _count_attrs(n.attrs)
    has_content = 1 if n.content is not None else 0
    _write_list_start(w, 2 * attr_count + 1 + has_content)
    _write_string(w, n.tag)
    _write_attrs(w, n.attrs)
    if n.content is not None:
        _write_any(w, n.content)


def _write_any(w: _Writer, value: Any) -> None:
    if value is None:
        w.push_byte(T.LIST_EMPTY)
        return
    if isinstance(value, JID):
        _write_jid(w, value)
        return
    if isinstance(value, str):
        _write_string(w, value)
        return
    if isinstance(value, bool):
        _write_string(w, "true" if value else "false")
        return
    if isinstance(value, int):
        _write_string(w, str(value))
        return
    if isinstance(value, (bytes, bytearray)):
        _write_bytes(w, bytes(value))
        return
    if isinstance(value, list):
        # List of Nodes
        _write_list_start(w, len(value))
        for child in value:
            _write_node(w, child)
        return
    raise TypeError(f"cannot encode value of type {type(value).__name__}")


def _count_attrs(attrs: Attrs) -> int:
    return sum(1 for v in attrs.values() if v is not None and v != "")


def _write_attrs(w: _Writer, attrs: Attrs) -> None:
    for key, val in attrs.items():
        if val is None or val == "":
            continue
        _write_string(w, key)
        _write_any(w, val)


def _write_list_start(w: _Writer, size: int) -> None:
    if size == 0:
        w.push_byte(T.LIST_EMPTY)
    elif size < 256:
        w.push_byte(T.LIST_8)
        w.push_u8(size)
    else:
        w.push_byte(T.LIST_16)
        w.push_u16_be(size)


def _write_byte_length(w: _Writer, length: int) -> None:
    if length < 256:
        w.push_byte(T.BINARY_8)
        w.push_u8(length)
    elif length < (1 << 20):
        w.push_byte(T.BINARY_20)
        w.push_u20_be(length)
    elif length < (1 << 31):
        w.push_byte(T.BINARY_32)
        w.push_u32_be(length)
    else:
        raise ValueError(f"length too large: {length}")


def _write_bytes(w: _Writer, data: bytes) -> None:
    _write_byte_length(w, len(data))
    w.push(data)


def _write_string(w: _Writer, s: str) -> None:
    # Priority: single-byte token → double-byte token → nibble pack → hex pack → raw.
    idx = T.single_byte_index(s)
    if idx is not None:
        w.push_byte(idx)
        return
    dbl = T.double_byte_index(s)
    if dbl is not None:
        dict_idx, tok_idx = dbl
        w.push_byte(T.DICTIONARY_0 + dict_idx)
        w.push_byte(tok_idx)
        return
    if _valid_nibble(s):
        _write_packed(w, s, T.NIBBLE_8)
        return
    if _valid_hex(s):
        _write_packed(w, s, T.HEX_8)
        return
    _write_string_raw(w, s)


def _write_string_raw(w: _Writer, s: str) -> None:
    data = s.encode("utf-8")
    _write_byte_length(w, len(data))
    w.push(data)


def _write_jid(w: _Writer, jid: JID) -> None:
    if jid.is_ad():
        w.push_byte(T.AD_JID)
        w.push_u8(jid.actual_agent())
        w.push_u8(jid.device)
        _write_string(w, jid.user)
        return
    if jid.server == "msgr":
        w.push_byte(T.FB_JID)
        _write_any(w, jid.user)
        w.push_u16_be(jid.device)
        _write_any(w, jid.server)
        return
    if jid.server == "interop":
        w.push_byte(T.INTEROP_JID)
        _write_any(w, jid.user)
        w.push_u16_be(jid.device)
        w.push_u16_be(jid.integrator)
        _write_any(w, jid.server)
        return
    w.push_byte(T.JID_PAIR)
    if not jid.user:
        w.push_byte(T.LIST_EMPTY)
    else:
        _write_any(w, jid.user)
    _write_any(w, jid.server)


# --- packed (nibble / hex) encoding ---------------------------------------

_NIBBLE_ALPHABET = set("0123456789-.")
_HEX_ALPHABET = set("0123456789ABCDEF")


def _valid_nibble(s: str) -> bool:
    if not s or len(s) > T.PACKED_MAX:
        return False
    return all(c in _NIBBLE_ALPHABET for c in s)


def _valid_hex(s: str) -> bool:
    if not s or len(s) > T.PACKED_MAX:
        return False
    return all(c in _HEX_ALPHABET for c in s)


def _pack_nibble(c: str) -> int:
    if c == "-":
        return 10
    if c == ".":
        return 11
    if c == "\x00":
        return 15
    if "0" <= c <= "9":
        return ord(c) - ord("0")
    raise ValueError(f"cannot pack as nibble: {c!r}")


def _pack_hex(c: str) -> int:
    if "0" <= c <= "9":
        return ord(c) - ord("0")
    if "A" <= c <= "F":
        return 10 + ord(c) - ord("A")
    if c == "\x00":
        return 15
    raise ValueError(f"cannot pack as hex: {c!r}")


def _write_packed(w: _Writer, s: str, data_type: int) -> None:
    w.push_byte(data_type)
    rounded = (len(s) + 1) // 2
    header = rounded
    if len(s) % 2 != 0:
        header |= 0x80
    w.push_u8(header)
    packer = _pack_nibble if data_type == T.NIBBLE_8 else _pack_hex
    half = len(s) // 2
    for i in range(half):
        a = packer(s[2 * i])
        b = packer(s[2 * i + 1])
        w.push_byte((a << 4) | b)
    if len(s) % 2 != 0:
        a = packer(s[-1])
        b = packer("\x00")
        w.push_byte((a << 4) | b)
