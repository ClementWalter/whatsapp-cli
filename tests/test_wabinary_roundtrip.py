"""Binary XML codec tests against the whatsmeow oracle.

Three layers of assertion, each catching a different class of bug:

1. ``test_encoder_bytes_single_attr`` — byte equality for 0- or 1-attr nodes.
   Go's map iteration is non-deterministic for >1 attrs, so byte equality is
   only reliable for small attribute counts. This catches wire-level bugs
   like off-by-ones in length prefixes and wrong tag bytes.

2. ``test_encoder_semantic`` — Python encodes, oracle decodes, semantic
   equality with the original Node. Covers all cases regardless of attr
   count. Catches encoding bugs that still produce valid bytes (e.g. wrong
   JID tag, bogus packing).

3. ``test_decoder_roundtrip`` — Oracle encodes, Python decodes, Python
   re-encodes, oracle decodes the re-encoded. Semantic equal to original.
   Catches decoder bugs.
"""

from __future__ import annotations

import base64

import pytest

from wa.wabinary import JID, Node, decode_node, encode_node


def _to_oracle_json(node: Node) -> dict:
    out: dict = {"Tag": node.tag}
    if node.attrs:
        attrs: dict = {}
        for k, v in node.attrs.items():
            if isinstance(v, JID):
                attrs[k] = str(v)
            elif isinstance(v, bool):
                attrs[k] = "true" if v else "false"
            elif isinstance(v, int):
                attrs[k] = str(v)
            else:
                attrs[k] = v
        out["Attrs"] = attrs
    if node.content is None:
        pass
    elif isinstance(node.content, (bytes, bytearray)):
        out["Content"] = base64.b64encode(bytes(node.content)).decode()
    elif isinstance(node.content, list):
        out["Content"] = [_to_oracle_json(c) for c in node.content]
    else:
        raise TypeError(f"unexpected content: {type(node.content)}")
    return out


def _from_oracle_json(data: dict) -> Node:
    """Reconstruct a Node from the oracle's JSON shape for semantic comparison."""
    tag = data.get("Tag", "")
    attrs_in = data.get("Attrs") or {}
    attrs: dict = {}
    for k, v in attrs_in.items():
        # Oracle emits JIDs via fmt.Stringer → "user@server" form.
        # For comparison purposes we keep whatever string came back and
        # normalize on the caller side.
        attrs[k] = v
    content_in = data.get("Content")
    content: object = None
    if isinstance(content_in, str):
        content = base64.b64decode(content_in)
    elif isinstance(content_in, list):
        content = [_from_oracle_json(c) for c in content_in]
    return Node(tag=tag, attrs=attrs, content=content)


def _canonicalize(node: Node) -> tuple:
    """A hashable canonical form of a Node for semantic comparison.

    - attrs become a sorted tuple of (key, stringified value)
    - JIDs normalize via str()
    - ints/bools normalize to strings (matches wire-level string storage)
    - content recurses for lists, bytes stay as bytes
    """
    attr_items = []
    for k, v in node.attrs.items():
        if isinstance(v, JID):
            sv = str(v)
        elif isinstance(v, bool):
            sv = "true" if v else "false"
        elif isinstance(v, int):
            sv = str(v)
        else:
            sv = v
        attr_items.append((k, sv))
    attr_items.sort()

    c = node.content
    if isinstance(c, list):
        c_canon: object = tuple(_canonicalize(x) for x in c)
    elif isinstance(c, (bytes, bytearray)):
        c_canon = bytes(c)
    else:
        c_canon = c
    return (node.tag, tuple(attr_items), c_canon)


# --- Canonical node corpus -------------------------------------------------


def _corpus() -> list[tuple[str, Node]]:
    return [
        ("trivial_empty", Node(tag="ping")),
        (
            "iq_get_ping",
            Node(
                tag="iq",
                attrs={
                    "type": "get",
                    "id": "abc",
                    "to": JID(user="", server="s.whatsapp.net"),
                },
                content=[Node(tag="ping")],
            ),
        ),
        (
            "single_byte_tokens_only",
            Node(
                tag="message",
                attrs={"from": JID(user="", server="s.whatsapp.net"), "type": "text"},
            ),
        ),
        (
            "raw_string_attr",
            Node(
                tag="message",
                attrs={"id": "XYZ_1234", "from": JID(user="", server="s.whatsapp.net")},
            ),
        ),
        (
            "nibble_packed_user",
            Node(
                tag="iq",
                attrs={"to": JID(user="15551234567", server="s.whatsapp.net")},
            ),
        ),
        ("hex_packed_message_id", Node(tag="message", attrs={"id": "AABBCCDD1122"})),
        ("double_byte_token", Node(tag="presence", attrs={"type": "active"})),
        (
            "binary_content_small",
            Node(
                tag="enc",
                attrs={"v": "2", "type": "msg"},
                content=b"\x01\x02\x03\x04",
            ),
        ),
        (
            "binary_content_large",
            Node(
                tag="enc",
                attrs={"v": "2", "type": "msg"},
                content=b"\xab" * 300,
            ),
        ),
        (
            "jid_pair_server_only",
            Node(tag="iq", attrs={"to": JID(user="", server="s.whatsapp.net")}),
        ),
        (
            "ad_jid",
            Node(
                tag="receipt",
                attrs={
                    "participant": JID(
                        user="15551234567", server="s.whatsapp.net", device=2
                    )
                },
            ),
        ),
        (
            "nested_children",
            Node(
                tag="iq",
                attrs={"type": "set", "id": "X1"},
                content=[
                    Node(tag="add"),
                    Node(tag="remove", attrs={"id": "foo"}),
                    Node(tag="ping"),
                ],
            ),
        ),
    ]


_IDS = [n for n, _ in _corpus()]


def _is_small_attr(node: Node) -> bool:
    return len(node.attrs) <= 1 and all(
        _is_small_attr(c) if isinstance(c, Node) else True
        for c in (node.content if isinstance(node.content, list) else [])
    )


# --- Tests -----------------------------------------------------------------


@pytest.mark.parametrize(
    "name,node",
    [(n, x) for n, x in _corpus() if _is_small_attr(x)],
    ids=[n for n, x in _corpus() if _is_small_attr(x)],
)
def test_encoder_bytes_single_attr(oracle, name: str, node: Node) -> None:
    """Byte-level equality against the oracle for 0/1 attr nodes."""
    py_bytes = encode_node(node)
    oracle_bytes = oracle.encode_node(_to_oracle_json(node))
    assert py_bytes == oracle_bytes, (
        f"[{name}]\n  py:     {py_bytes.hex()}\n  oracle: {oracle_bytes.hex()}"
    )


@pytest.mark.parametrize("name,node", _corpus(), ids=_IDS)
def test_encoder_semantic(oracle, name: str, node: Node) -> None:
    """Python-encoded bytes must decode (via oracle) to the same Node."""
    py_bytes = encode_node(node)
    decoded = _from_oracle_json(oracle.decode_node(py_bytes))
    assert _canonicalize(decoded) == _canonicalize(node), f"[{name}] decoded={decoded}"


@pytest.mark.parametrize("name,node", _corpus(), ids=_IDS)
def test_decoder_roundtrip(oracle, name: str, node: Node) -> None:
    """Oracle-encoded bytes decoded by Python then re-encoded round-trip semantically."""
    oracle_bytes = oracle.encode_node(_to_oracle_json(node))
    decoded = decode_node(oracle_bytes)
    assert _canonicalize(decoded) == _canonicalize(node), f"[{name}] decoded={decoded}"
    # And the re-encoded bytes should themselves be decodable back to the same.
    re_encoded = encode_node(decoded)
    decoded_again = _from_oracle_json(oracle.decode_node(re_encoded))
    assert _canonicalize(decoded_again) == _canonicalize(node)
