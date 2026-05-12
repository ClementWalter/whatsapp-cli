"""Noise_XX_25519_AESGCM_SHA256 handshake state machine and post-handshake socket.

Mirrors whatsmeow/socket/noisehandshake.go and noisesocket.go. The WA-specific
handshake pattern string is "Noise_XX_25519_AESGCM_SHA256\\x00\\x00\\x00\\x00"
(exactly 32 bytes), so the initial hash is set to those bytes directly rather
than being SHA-256'd (whatsmeow short-circuits when len(pattern) == 32).

Split from the XX choreography (in handshake.py) so this module holds only
the symmetric-state machinery plus X25519 mixing: any bugs here fail the
oracle diff before live network code runs.
"""

from __future__ import annotations

import hashlib
import hmac
import struct
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PrivateFormat,
    PublicFormat,
    NoEncryption,
)


def generate_iv(counter: int) -> bytes:
    """Noise/WA nonce: 12 bytes, big-endian counter in the last 4 bytes."""
    return b"\x00" * 8 + struct.pack(">I", counter & 0xFFFFFFFF)


def _hkdf_sha256_pair(salt: bytes, ikm: bytes) -> tuple[bytes, bytes]:
    """Two-output HKDF-SHA256 expansion with empty info: returns (write, read)
    32-byte slots. Matches golang.org/x/crypto/hkdf.New used by whatsmeow."""
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    t1 = hmac.new(prk, b"\x01", hashlib.sha256).digest()
    t2 = hmac.new(prk, t1 + b"\x02", hashlib.sha256).digest()
    return t1, t2


def x25519_generate() -> tuple[bytes, bytes]:
    """Return (private 32B, public 32B) suitable for a Noise ephemeral."""
    sk = X25519PrivateKey.generate()
    priv = sk.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    pub = sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return priv, pub


def x25519_public(priv: bytes) -> bytes:
    return (
        X25519PrivateKey.from_private_bytes(priv)
        .public_key()
        .public_bytes(Encoding.Raw, PublicFormat.Raw)
    )


def x25519_shared(priv: bytes, pub: bytes) -> bytes:
    return X25519PrivateKey.from_private_bytes(priv).exchange(
        X25519PublicKey.from_public_bytes(pub)
    )


@dataclass
class NoiseHandshake:
    """Symmetric + key-derivation state of the Noise XX handshake.

    Invariants after Start():
      - hash:    32-byte transcript hash (grows with each Authenticate/Encrypt)
      - salt:    32-byte chaining value (rotated each MixIntoKey)
      - aead:    current AES-GCM cipher (nonce derived from counter)
      - counter: resets to 0 at every MixIntoKey
    """

    hash: bytes = b""
    salt: bytes = b""
    aead: AESGCM | None = None
    counter: int = 0

    def start(self, pattern: str, header: bytes) -> None:
        """Initialize from the Noise pattern string and WA prologue bytes."""
        data = pattern.encode("utf-8")
        if len(data) == 32:
            h = data
        else:
            h = hashlib.sha256(data).digest()
        self.hash = h
        self.salt = h
        self.aead = AESGCM(h)
        self.counter = 0
        self.authenticate(header)

    def authenticate(self, data: bytes) -> None:
        """Fold ``data`` into the transcript hash (non-crypto mix)."""
        self.hash = hashlib.sha256(self.hash + data).digest()

    def _post_inc_counter(self) -> int:
        c = self.counter
        self.counter = c + 1
        return c

    def encrypt(self, plaintext: bytes) -> bytes:
        assert self.aead is not None
        ct = self.aead.encrypt(generate_iv(self._post_inc_counter()), plaintext, self.hash)
        self.authenticate(ct)
        return ct

    def decrypt(self, ciphertext: bytes) -> bytes:
        assert self.aead is not None
        pt = self.aead.decrypt(generate_iv(self._post_inc_counter()), ciphertext, self.hash)
        # Authenticate the ciphertext on success — must happen AFTER decrypt so
        # the next op's AAD reflects what we committed to.
        self.authenticate(ciphertext)
        return pt

    def mix_into_key(self, data: bytes) -> None:
        """Derive a new chaining value and AEAD key from current salt + ``data``."""
        self.counter = 0
        write, read = _hkdf_sha256_pair(self.salt, data)
        self.salt = write
        self.aead = AESGCM(read)

    def mix_shared_secret(self, priv: bytes, pub: bytes) -> None:
        """X25519(priv, pub) into the symmetric state."""
        self.mix_into_key(x25519_shared(priv, pub))

    def finish(self) -> tuple[bytes, bytes]:
        """After the last handshake message, derive (write_key, read_key).

        These 32-byte keys feed a NoiseSocket for the rest of the session.
        """
        write, read = _hkdf_sha256_pair(self.salt, b"")
        return write, read


class NoiseSocket:
    """Post-handshake authenticated-encryption stream.

    Each direction has its own AES-GCM key and counter. Nonces are the same
    12-byte BE-counter format used during the handshake, but with no AAD.
    """

    __slots__ = ("_write_key", "_read_key", "_write_ctr", "_read_ctr")

    def __init__(self, write_key: bytes, read_key: bytes) -> None:
        self._write_key = AESGCM(write_key)
        self._read_key = AESGCM(read_key)
        self._write_ctr = 0
        self._read_ctr = 0

    def encrypt_frame(self, plaintext: bytes) -> bytes:
        ct = self._write_key.encrypt(generate_iv(self._write_ctr), plaintext, None)
        self._write_ctr += 1
        return ct

    def decrypt_frame(self, ciphertext: bytes) -> bytes:
        pt = self._read_key.decrypt(generate_iv(self._read_ctr), ciphertext, None)
        self._read_ctr += 1
        return pt
