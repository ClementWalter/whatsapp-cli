"""XEdDSA verification tests against the oracle.

We only implement verify; signing stays in the oracle (cert / ADV signatures
are produced by the server / phone, not by us). For each test: ask the
oracle to sign, then Python verifies. If our Edwards conversion or sign-bit
handling is off, the verification fails immediately.
"""

from __future__ import annotations

import base64

import pytest

from wa.crypto.noise import x25519_generate
from wa.crypto.xeddsa import verify


@pytest.mark.parametrize(
    "message",
    [b"", b"hello", b"A" * 1, b"WhatsApp certificate details", b"\x00" * 64],
    ids=["empty", "short", "single", "ascii-64", "zeros"],
)
def test_xeddsa_sign_and_python_verify(oracle, message: bytes) -> None:
    priv, pub = x25519_generate()
    signed = oracle._call(
        "xeddsa_sign",
        {
            "priv": base64.b64encode(priv).decode(),
            "message": base64.b64encode(message).decode(),
        },
    )
    assert base64.b64decode(signed["pub"]) == pub, "oracle-derived pub must match"
    signature = base64.b64decode(signed["signature"])
    assert verify(pub, message, signature) is True


def test_xeddsa_reject_tampered_signature(oracle) -> None:
    priv, pub = x25519_generate()
    message = b"original"
    signed = oracle._call(
        "xeddsa_sign",
        {
            "priv": base64.b64encode(priv).decode(),
            "message": base64.b64encode(message).decode(),
        },
    )
    sig = bytearray(base64.b64decode(signed["signature"]))
    sig[0] ^= 0xFF
    assert verify(pub, message, bytes(sig)) is False


def test_xeddsa_reject_wrong_message(oracle) -> None:
    priv, pub = x25519_generate()
    signed = oracle._call(
        "xeddsa_sign",
        {
            "priv": base64.b64encode(priv).decode(),
            "message": base64.b64encode(b"msg-a").decode(),
        },
    )
    assert verify(pub, b"msg-b", base64.b64decode(signed["signature"])) is False


def test_xeddsa_rejects_wrong_length_inputs() -> None:
    assert verify(b"\x00" * 31, b"x", b"\x00" * 64) is False
    assert verify(b"\x00" * 32, b"x", b"\x00" * 63) is False
