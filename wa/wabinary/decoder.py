"""Decode WhatsApp's binary XML byte stream into Nodes."""

from __future__ import annotations

from typing import Any

from . import tokens as T
from .jid import (
    DEFAULT_USER_SERVER,
    HIDDEN_USER_SERVER,
    HOSTED_LID_SERVER,
    HOSTED_SERVER,
    JID,
)
from .node import Node


class _Reader:
    __slots__ = ("data", "pos")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def eof(self) -> bool:
        return self.pos >= len(self.data)

    def read_u8(self) -> int:
        b = self.data[self.pos]
        self.pos += 1
        return b

    def read_u16_be(self) -> int:
        v = int.from_bytes(self.data[self.pos : self.pos + 2], "big")
        self.pos += 2
        return v

    def read_u20_be(self) -> int:
        b0 = self.data[self.pos]
        b1 = self.data[self.pos + 1]
        b2 = self.data[self.pos + 2]
        self.pos += 3
        return ((b0 & 0x0F) << 16) | (b1 << 8) | b2

    def read_u32_be(self) -> int:
        v = int.from_bytes(self.data[self.pos : self.pos + 4], "big")
        self.pos += 4
        return v

    def read(self, n: int) -> bytes:
        out = self.data[self.pos : self.pos + n]
        self.pos += n
        return out


def decode_node(data: bytes) -> Node:
    # Leading byte is a compression flag — bit 1 (mask 0x02) indicates the
    # remaining bytes are zlib-compressed. Large frames (group info, history)
    # routinely arrive compressed; without this they appear to "go missing".
    flag = data[0]
    payload = data[1:]
    if flag & 0x02:
        import zlib
        payload = zlib.decompress(payload)
    elif flag != 0:
        raise ValueError(f"unsupported binary stream flag 0x{flag:02x}")
    r = _Reader(payload)
    node = _read_node(r)
    if node is None:
        raise ValueError("expected node, got empty list")
    if r.pos != len(payload):
        raise ValueError(f"{len(payload) - r.pos} leftover bytes after decoding")
    return node


def _read_list_size(r: _Reader, tag: int) -> int:
    if tag == T.LIST_EMPTY:
        return 0
    if tag == T.LIST_8:
        return r.read_u8()
    if tag == T.LIST_16:
        return r.read_u16_be()
    raise ValueError(f"not a list tag: {tag}")


def _read_node(r: _Reader) -> Node | None:
    list_tag = r.read_u8()
    size = _read_list_size(r, list_tag)
    if size == 0:
        return None
    tag = _read_string(r, r.read_u8())
    attrs = _read_attrs(r, (size - 1) // 2)
    content: Any = None
    if size % 2 == 0:
        # Content: do NOT stringify raw binary; callers need bytes for payloads.
        content = _read_value(r, r.read_u8(), as_string=False)
    return Node(tag=tag, attrs=attrs, content=content)


def _read_attrs(r: _Reader, n: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for _ in range(n):
        key = _read_string(r, r.read_u8())
        # Attribute values are always text on the wire (JIDs are a separate
        # tag class); stringify any raw binary.
        val = _read_value(r, r.read_u8(), as_string=True)
        out[key] = val
    return out


def _read_value(r: _Reader, tag: int, *, as_string: bool) -> Any:
    """Read a value. ``as_string`` controls whether raw Binary* tags decode
    to str (attribute position) or bytes (content position)."""
    # Strings: single-byte token (1..235) or dictionary tokens (236..239).
    if 1 <= tag <= len(T.SINGLE_BYTE_TOKENS) - 1:
        return T.SINGLE_BYTE_TOKENS[tag]
    if tag == T.LIST_EMPTY:
        return ""
    if T.DICTIONARY_0 <= tag <= T.DICTIONARY_3:
        dict_idx = tag - T.DICTIONARY_0
        tok_idx = r.read_u8()
        return T.DOUBLE_BYTE_TOKENS[dict_idx][tok_idx]
    if tag == T.JID_PAIR:
        return _read_jid_pair(r)
    if tag == T.AD_JID:
        return _read_ad_jid(r)
    if tag == T.FB_JID:
        return _read_fb_jid(r)
    if tag == T.INTEROP_JID:
        return _read_interop_jid(r)
    if tag == T.NIBBLE_8:
        return _read_packed(r, _unpack_nibble)
    if tag == T.HEX_8:
        return _read_packed(r, _unpack_hex)
    if tag == T.BINARY_8:
        data = r.read(r.read_u8())
        return data.decode("utf-8") if as_string else data
    if tag == T.BINARY_20:
        data = r.read(r.read_u20_be())
        return data.decode("utf-8") if as_string else data
    if tag == T.BINARY_32:
        data = r.read(r.read_u32_be())
        return data.decode("utf-8") if as_string else data
    if tag in (T.LIST_8, T.LIST_16):
        size = _read_list_size(r, tag)
        return [_read_node_in_list(r) for _ in range(size)]
    raise ValueError(f"unknown content tag: {tag}")


def _read_node_in_list(r: _Reader) -> Node:
    """Read a node when we've already consumed the outer list header."""
    list_tag = r.read_u8()
    size = _read_list_size(r, list_tag)
    if size == 0:
        return Node(tag="")
    tag = _read_string(r, r.read_u8())
    attrs = _read_attrs(r, (size - 1) // 2)
    content: Any = None
    if size % 2 == 0:
        content = _read_value(r, r.read_u8(), as_string=False)
    return Node(tag=tag, attrs=attrs, content=content)


def _read_string(r: _Reader, tag: int) -> str:
    val = _read_value(r, tag, as_string=True)
    if isinstance(val, str):
        return val
    raise ValueError(f"expected string at tag {tag}, got {type(val).__name__}")


def _read_jid_pair(r: _Reader) -> JID:
    user_tag = r.read_u8()
    if user_tag == T.LIST_EMPTY:
        user = ""
    else:
        user = _read_string(r, user_tag)
    server = _read_string(r, r.read_u8())
    return JID(user=user, server=server)


def _read_ad_jid(r: _Reader) -> JID:
    agent = r.read_u8()
    device = r.read_u8()
    user = _read_string(r, r.read_u8())
    # Infer server from agent byte the way whatsmeow does for legacy/PN vs LID.
    # Agent=0 → DefaultUserServer (s.whatsapp.net); agent=1 → LID (lid);
    # agent>=2 → HostedServer family. Simple mapping; refine if needed.
    if agent == 0:
        server = DEFAULT_USER_SERVER
    elif agent == 1:
        server = HIDDEN_USER_SERVER
    else:
        server = HOSTED_SERVER if agent < 128 else HOSTED_LID_SERVER
    return JID(user=user, server=server, agent=agent, device=device)


def _read_fb_jid(r: _Reader) -> JID:
    user = _read_string(r, r.read_u8())
    device = r.read_u16_be()
    server = _read_string(r, r.read_u8())
    return JID(user=user, server=server, device=device)


def _read_interop_jid(r: _Reader) -> JID:
    user = _read_string(r, r.read_u8())
    device = r.read_u16_be()
    integrator = r.read_u16_be()
    server = _read_string(r, r.read_u8())
    return JID(user=user, server=server, device=device, integrator=integrator)


def _read_packed(r: _Reader, unpacker) -> str:
    # Header bit 7 = odd-length flag; bits 0..6 = byte count.
    # When odd, the low nibble of the final byte is padding (value 15)
    # and must be dropped — not mapped to an empty string.
    header = r.read_u8()
    odd = (header & 0x80) != 0
    count = header & 0x7F
    chars: list[str] = []
    for i in range(count):
        b = r.read_u8()
        chars.append(unpacker((b >> 4) & 0x0F))
        if i == count - 1 and odd:
            continue
        chars.append(unpacker(b & 0x0F))
    return "".join(chars)


def _unpack_nibble(v: int) -> str:
    if 0 <= v <= 9:
        return chr(ord("0") + v)
    if v == 10:
        return "-"
    if v == 11:
        return "."
    raise ValueError(f"invalid nibble: {v}")


def _unpack_hex(v: int) -> str:
    if 0 <= v <= 9:
        return chr(ord("0") + v)
    if 10 <= v <= 15:
        return chr(ord("A") + v - 10)
    raise ValueError(f"invalid hex: {v}")
