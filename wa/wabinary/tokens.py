"""Token tables and tag constants for WhatsApp's binary XML format.

The single-byte and double-byte token tables are copied verbatim from
whatsmeow's `binary/token/token.go` (MPL-2.0) via a one-shot generator; see
`_tokens_data.py`. The tables must match what the server expects for the
advertised `DICT_VERSION`; mismatches silently corrupt encoding.
"""

from __future__ import annotations

from ._tokens_data import DICT_VERSION, SINGLE_BYTE_TOKENS, DOUBLE_BYTE_TOKENS

__all__ = [
    "DICT_VERSION",
    "SINGLE_BYTE_TOKENS",
    "DOUBLE_BYTE_TOKENS",
    "LIST_EMPTY",
    "DICTIONARY_0",
    "DICTIONARY_1",
    "DICTIONARY_2",
    "DICTIONARY_3",
    "INTEROP_JID",
    "FB_JID",
    "AD_JID",
    "LIST_8",
    "LIST_16",
    "JID_PAIR",
    "HEX_8",
    "BINARY_8",
    "BINARY_20",
    "BINARY_32",
    "NIBBLE_8",
    "PACKED_MAX",
    "single_byte_index",
    "double_byte_index",
]

# Tag byte constants matching whatsmeow/binary/token/token.go.
LIST_EMPTY = 0
DICTIONARY_0 = 236
DICTIONARY_1 = 237
DICTIONARY_2 = 238
DICTIONARY_3 = 239
INTEROP_JID = 245
FB_JID = 246
AD_JID = 247
LIST_8 = 248
LIST_16 = 249
JID_PAIR = 250
HEX_8 = 251
BINARY_8 = 252
BINARY_20 = 253
BINARY_32 = 254
NIBBLE_8 = 255

# Max length for nibble/hex-packed strings (7-bit length field, high bit = odd flag).
PACKED_MAX = 127

_single_lookup: dict[str, int] = {}
_double_lookup: dict[str, tuple[int, int]] = {}


def _build_indexes() -> None:
    for i, tok in enumerate(SINGLE_BYTE_TOKENS):
        if tok:
            _single_lookup[tok] = i
    for d, dct in enumerate(DOUBLE_BYTE_TOKENS):
        for i, tok in enumerate(dct):
            _double_lookup[tok] = (d, i)


_build_indexes()


def single_byte_index(s: str) -> int | None:
    """Return the single-byte token index, or None if not a token."""
    return _single_lookup.get(s)


def double_byte_index(s: str) -> tuple[int, int] | None:
    """Return (dict_index, token_index) for a double-byte token, or None."""
    return _double_lookup.get(s)
