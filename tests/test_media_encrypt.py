"""Outbound media encryption: HKDF parity, AES-CBC round-trip, hashes."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from wa.history import _aes_cbc_decrypt, _hkdf_expand
from wa.media import MEDIA_HKDF_INFO, encrypt_media


@pytest.fixture
def plaintext() -> bytes:
    # 100 bytes — deliberately not a multiple of the 16-byte AES block, so
    # PKCS#7 padding is exercised.
    return b"%PDF-1.4 fake document body for tests " + b"x" * 63


@pytest.mark.parametrize("media_type", sorted(MEDIA_HKDF_INFO))
def test_hkdf_matches_oracle(oracle, media_type):
    # Our HKDF expansion must equal whatsmeow's getMediaKeys byte-for-byte.
    media_key = bytes(range(32))
    info = MEDIA_HKDF_INFO[media_type]
    expected = oracle.derive_media_keys(media_key, info.decode())
    got = _hkdf_expand(media_key, info, 112)
    assert got[:16] == expected["iv"]


def test_encrypt_media_roundtrips(plaintext):
    enc = encrypt_media(plaintext, "document")
    expanded = _hkdf_expand(enc.media_key, MEDIA_HKDF_INFO["document"], 112)
    iv, cipher_key = expanded[:16], expanded[16:48]
    recovered = _aes_cbc_decrypt(cipher_key, iv, enc.ciphertext[:-10])
    assert recovered == plaintext


def test_encrypt_media_hmac_valid(plaintext):
    enc = encrypt_media(plaintext, "document")
    expanded = _hkdf_expand(enc.media_key, MEDIA_HKDF_INFO["document"], 112)
    iv, mac_key = expanded[:16], expanded[48:80]
    body, mac = enc.ciphertext[:-10], enc.ciphertext[-10:]
    expected = hmac.new(mac_key, iv + body, hashlib.sha256).digest()[:10]
    assert mac == expected


def test_encrypt_media_file_enc_sha256(plaintext):
    enc = encrypt_media(plaintext, "document")
    assert enc.file_enc_sha256 == hashlib.sha256(enc.ciphertext).digest()


def test_encrypt_media_file_sha256(plaintext):
    enc = encrypt_media(plaintext, "document")
    assert enc.file_sha256 == hashlib.sha256(plaintext).digest()


def test_encrypt_media_file_length(plaintext):
    enc = encrypt_media(plaintext, "document")
    assert enc.file_length == len(plaintext)


def test_encrypt_media_media_key_is_32_bytes(plaintext):
    enc = encrypt_media(plaintext, "document")
    assert len(enc.media_key) == 32


def test_encrypt_media_unknown_type_raises():
    with pytest.raises(ValueError):
        encrypt_media(b"x", "spreadsheet")
