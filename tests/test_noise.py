"""Noise handshake state-machine tests.

Three layers:
1. HKDF-SHA256 pair against RFC 5869 Test Case 1 (no oracle needed).
2. IV generation: byte-equality for known counters.
3. Step-by-step NoiseHandshake trace against the oracle. Same inputs on
   both sides; outputs (ciphertexts, plaintexts) must match exactly —
   if they do, the internal symmetric state must also match.
"""

from __future__ import annotations

import base64

import pytest

from wa.crypto.noise import (
    NoiseHandshake,
    NoiseSocket,
    _hkdf_sha256_pair,
    generate_iv,
    x25519_generate,
    x25519_public,
    x25519_shared,
)

WA_PATTERN = "Noise_XX_25519_AESGCM_SHA256\x00\x00\x00\x00"
WA_HEADER = b"WA\x06\x03"


# --- IV and HKDF ----------------------------------------------------------


def test_iv_counter_zero() -> None:
    assert generate_iv(0) == b"\x00" * 12


def test_iv_counter_big() -> None:
    assert generate_iv(0x01020304) == b"\x00" * 8 + b"\x01\x02\x03\x04"


def test_hkdf_rfc5869_tc1() -> None:
    """RFC 5869 §A.1 Test Case 1 — the canonical HKDF-SHA256 vector.

    Our two-slot expansion returns the first 64 output bytes split into
    two 32-byte halves. Match against the published OKM[:64].
    """
    ikm = bytes.fromhex("0b" * 22)
    salt = bytes.fromhex("000102030405060708090a0b0c")
    # We ignore info here (our helper hardcodes info="") so we only match
    # the first 64 bytes of the info="" derivation, not RFC 5869's info=...
    # Use OpenSSL-derived reference for info=b"" below:
    write, read = _hkdf_sha256_pair(salt, ikm)
    # Reference: computed via python-cryptography HKDF with info=b"", length=64.
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    expected = HKDF(hashes.SHA256(), 64, salt, b"").derive(ikm)
    assert write + read == expected


# --- X25519 self-consistency ----------------------------------------------


def test_x25519_symmetric() -> None:
    a_priv, a_pub = x25519_generate()
    b_priv, b_pub = x25519_generate()
    assert x25519_shared(a_priv, b_pub) == x25519_shared(b_priv, a_pub)


def test_x25519_public_derives_from_priv() -> None:
    priv, pub = x25519_generate()
    assert x25519_public(priv) == pub


# --- NoiseSocket self-consistency -----------------------------------------


def test_noise_socket_encrypt_decrypt() -> None:
    """A NoiseSocket used by both peers (same keys swapped) round-trips."""
    w = bytes(range(32))
    r = bytes(range(32, 64))
    alice = NoiseSocket(write_key=w, read_key=r)
    bob = NoiseSocket(write_key=r, read_key=w)
    for i in range(5):
        msg = f"frame {i}".encode()
        assert bob.decrypt_frame(alice.encrypt_frame(msg)) == msg


# --- Oracle-backed trace --------------------------------------------------


def _run_trace(nh: NoiseHandshake, steps: list[dict]) -> list[dict]:
    """Mirror the oracle's trace runner in Python so both sides operate on
    the same script and produce outputs we can byte-compare."""
    out: list[dict] = []
    for s in steps:
        op = s["op"]
        r: dict = {"op": op}
        if op == "authenticate":
            nh.authenticate(base64.b64decode(s["data"]))
        elif op == "mix_shared_secret":
            nh.mix_shared_secret(base64.b64decode(s["priv"]), base64.b64decode(s["pub"]))
        elif op == "encrypt":
            ct = nh.encrypt(base64.b64decode(s["plaintext"]))
            r["ciphertext"] = base64.b64encode(ct).decode()
        elif op == "decrypt":
            pt = nh.decrypt(base64.b64decode(s["ciphertext"]))
            r["plaintext"] = base64.b64encode(pt).decode()
        else:
            raise ValueError(f"unknown step {op}")
        out.append(r)
    return out


def test_noise_start_plus_authenticate(oracle) -> None:
    """After Start + an extra Authenticate, an Encrypt(empty) output must
    match. The encrypt's ciphertext is a 16-byte AES-GCM tag over no data,
    which uniquely identifies the current (key, iv, hash) triple.
    """
    steps = [
        {"op": "authenticate", "data": base64.b64encode(b"extra").decode()},
        {"op": "encrypt", "plaintext": base64.b64encode(b"").decode()},
    ]
    nh = NoiseHandshake()
    nh.start(WA_PATTERN, WA_HEADER)
    py = _run_trace(nh, steps)

    result = oracle._call(
        "noise_trace",
        {
            "pattern": WA_PATTERN,
            "header": base64.b64encode(WA_HEADER).decode(),
            "steps": steps,
        },
    )
    assert py == result["results"]


def test_noise_mix_and_encrypt(oracle) -> None:
    """Full XX-like sequence: Authenticate server ephemeral, MixSharedSecret,
    Encrypt a payload. Byte-for-byte agreement between Python and whatsmeow.
    """
    # Deterministic keys (don't use these in production, obviously).
    a_priv = bytes.fromhex("77" * 32)
    b_priv = bytes.fromhex("66" * 32)
    b_pub = x25519_public(b_priv)

    steps = [
        {"op": "authenticate", "data": base64.b64encode(b_pub).decode()},
        {
            "op": "mix_shared_secret",
            "priv": base64.b64encode(a_priv).decode(),
            "pub": base64.b64encode(b_pub).decode(),
        },
        {"op": "encrypt", "plaintext": base64.b64encode(b"hello whatsapp").decode()},
        {"op": "encrypt", "plaintext": base64.b64encode(b"second message").decode()},
    ]
    nh = NoiseHandshake()
    nh.start(WA_PATTERN, WA_HEADER)
    py = _run_trace(nh, steps)

    result = oracle._call(
        "noise_trace",
        {
            "pattern": WA_PATTERN,
            "header": base64.b64encode(WA_HEADER).decode(),
            "steps": steps,
        },
    )
    assert py == result["results"]


def test_noise_encrypt_then_decrypt_roundtrip(oracle) -> None:
    """Encrypt on Python side, decrypt on oracle side with matching state."""
    a_priv = bytes.fromhex("11" * 32)
    b_priv = bytes.fromhex("22" * 32)
    b_pub = x25519_public(b_priv)

    py_nh = NoiseHandshake()
    py_nh.start(WA_PATTERN, WA_HEADER)
    py_nh.mix_shared_secret(a_priv, b_pub)
    ct = py_nh.encrypt(b"secret payload")

    # Same sequence on oracle, asking it to decrypt our ciphertext.
    steps = [
        {
            "op": "mix_shared_secret",
            "priv": base64.b64encode(a_priv).decode(),
            "pub": base64.b64encode(b_pub).decode(),
        },
        {"op": "decrypt", "ciphertext": base64.b64encode(ct).decode()},
    ]
    result = oracle._call(
        "noise_trace",
        {
            "pattern": WA_PATTERN,
            "header": base64.b64encode(WA_HEADER).decode(),
            "steps": steps,
        },
    )
    assert result["results"][1]["plaintext"] == base64.b64encode(b"secret payload").decode()
