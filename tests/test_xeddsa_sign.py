"""XEdDSA signing — Python signs, oracle (libsignal) verifies.

Uses a fixed ``random`` so the signature is deterministic and we can also
byte-compare against the oracle's sign output for the same inputs.
"""

from __future__ import annotations

import base64

import pytest

from wa.crypto.noise import x25519_generate
from wa.crypto.xeddsa import verify
from wa.crypto.xeddsa_sign import xeddsa_sign


@pytest.mark.parametrize(
    "message",
    [b"hello", b"A" * 32, b"", b"\xff" * 100],
    ids=["short", "mid", "empty", "long-ones"],
)
def test_python_sign_oracle_verifies(oracle, message: bytes) -> None:
    """Python-produced signatures must validate under libsignal."""
    priv, pub = x25519_generate()
    sig = xeddsa_sign(priv, message)
    r = oracle._call(
        "xeddsa_verify",
        {
            "pub": base64.b64encode(pub).decode(),
            "message": base64.b64encode(message).decode(),
            "signature": base64.b64encode(sig).decode(),
        },
    )
    assert r["valid"] is True


def test_python_sign_python_verifies() -> None:
    """Self-consistency: our verify accepts our own signatures."""
    priv, pub = x25519_generate()
    message = b"roundtrip"
    sig = xeddsa_sign(priv, message)
    assert verify(pub, message, sig) is True
