"""Minimal protobuf inspector tests — no network, no oracle needed."""

from __future__ import annotations

from wa.pbutil import decode_fields, summarize


def test_single_varint() -> None:
    # Field 1, wire=0 (varint), value 42 → tag byte 0x08, value 0x2A
    assert decode_fields(b"\x08\x2a") == {1: [42]}


def test_length_delimited_bytes() -> None:
    # Field 12, wire=2 (len-delim), 3 bytes "abc"
    assert decode_fields(b"\x62\x03abc") == {12: [b"abc"]}


def test_repeated_field() -> None:
    # Field 1 appearing twice: values 1 and 2
    assert decode_fields(b"\x08\x01\x08\x02") == {1: [1, 2]}


def test_multi_byte_varint() -> None:
    # 300 in varint = 0xAC 0x02, field 1
    assert decode_fields(b"\x08\xac\x02") == {1: [300]}


def test_summarize_truncates_long_bytes() -> None:
    data = b"\x0a" + bytes([100]) + (b"x" * 100)
    out = summarize(data, max_bytes=8)
    assert "bytes[100]" in out
    assert "...(+92)" in out


def test_summarize_handles_garbage() -> None:
    assert "not protobuf" in summarize(b"\xff\xff\xff\xff")
