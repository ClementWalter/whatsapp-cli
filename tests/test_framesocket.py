"""FrameParser unit tests — no network, all deterministic."""

from __future__ import annotations

from wa.transport.framesocket import FrameParser, WA_HEADER, encode_frame


def _len_prefix(n: int) -> bytes:
    return bytes([(n >> 16) & 0xFF, (n >> 8) & 0xFF, n & 0xFF])


def test_wa_header_constant() -> None:
    # DictVersion=3 is embedded; fails fast if anyone bumps the table
    # without also bumping this assertion.
    assert WA_HEADER == b"WA\x06\x03"


def test_parser_single_frame() -> None:
    parser = FrameParser()
    payload = b"hello"
    assert parser.feed(_len_prefix(5) + payload) == [payload]


def test_parser_two_frames_one_feed() -> None:
    parser = FrameParser()
    frames = parser.feed(_len_prefix(1) + b"a" + _len_prefix(2) + b"bc")
    assert frames == [b"a", b"bc"]


def test_parser_partial_length_prefix() -> None:
    parser = FrameParser()
    # Send only 2 of the 3 length bytes; no frame yet.
    assert parser.feed(b"\x00\x00") == []
    # Now push the last length byte + payload — single frame pops out.
    assert parser.feed(b"\x04abcd") == [b"abcd"]


def test_parser_split_across_feeds() -> None:
    parser = FrameParser()
    assert parser.feed(_len_prefix(10) + b"part") == []
    assert parser.feed(b"y") == []
    assert parser.feed(b"payload!!") == [b"partypayload!"[:10]]


def test_parser_length_prefix_crosses_chunk_boundary() -> None:
    parser = FrameParser()
    # Only 1 length byte in first chunk.
    assert parser.feed(b"\x00") == []
    # Remaining 2 length bytes + payload in second chunk.
    assert parser.feed(b"\x00\x03xyz") == [b"xyz"]


def test_encode_frame_roundtrip() -> None:
    payload = bytes(range(37))
    framed = encode_frame(payload)
    parser = FrameParser()
    assert parser.feed(framed) == [payload]


def test_parser_large_frame() -> None:
    payload = bytes([0xAB]) * 70_000  # >64KB → needs the full 3-byte length
    parser = FrameParser()
    assert parser.feed(encode_frame(payload)) == [payload]
