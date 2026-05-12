"""XEdDSA signing — split from xeddsa.py because it pulls in more bits.

Only used at key-generation time (signing our own SignedPreKey.pub with the
IdentityKey). The algorithm below follows libsignal's Go implementation at
libsignal/ecc/SignCurve25519.go.
"""

from __future__ import annotations

import hashlib
import os

# Pull edwards25519 group operations from cryptography's internal backend.
# We need scalar mul / point addition / canonical encoding — pyca doesn't
# expose those directly, so we use a small pure-Python implementation
# backed by a well-tested reference (the Ed25519 RFC 8032 primitives).

P = (1 << 255) - 19
L = (1 << 252) + 27742317777372353535851937790883648493  # group order
D = (-121665 * pow(121666, -1, P)) % P
I_CONST = pow(2, (P - 1) // 4, P)


def _inv(x: int) -> int:
    return pow(x, P - 2, P)


def _x_recover(y: int) -> int:
    xx = (y * y - 1) * _inv(D * y * y + 1)
    x = pow(xx, (P + 3) // 8, P)
    if (x * x - xx) % P != 0:
        x = (x * I_CONST) % P
    if x % 2 != 0:
        x = P - x
    return x


_BY = 4 * _inv(5) % P
_BX = _x_recover(_BY)
_B = (_BX % P, _BY % P, 1, (_BX * _BY) % P)


def _edwards_add(P1: tuple[int, int, int, int], P2: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    x1, y1, z1, t1 = P1
    x2, y2, z2, t2 = P2
    A = ((y1 - x1) * (y2 - x2)) % P
    B = ((y1 + x1) * (y2 + x2)) % P
    C = (t1 * 2 * D * t2) % P
    D_ = (z1 * 2 * z2) % P
    E = (B - A) % P
    F = (D_ - C) % P
    G = (D_ + C) % P
    H = (B + A) % P
    return (
        (E * F) % P,
        (G * H) % P,
        (F * G) % P,
        (E * H) % P,
    )


def _scalar_mul(P0: tuple[int, int, int, int], e: int) -> tuple[int, int, int, int]:
    if e == 0:
        return (0, 1, 1, 0)
    Q = _scalar_mul(P0, e // 2)
    Q = _edwards_add(Q, Q)
    if e & 1:
        Q = _edwards_add(Q, P0)
    return Q


def _encode_point(P0: tuple[int, int, int, int]) -> bytes:
    x, y, z, _ = P0
    zinv = _inv(z)
    x = (x * zinv) % P
    y = (y * zinv) % P
    bits = [(y >> i) & 1 for i in range(255)] + [x & 1]
    return bytes(sum(bits[i * 8 + j] << j for j in range(8)) for i in range(32))


def xeddsa_sign(curve25519_priv: bytes, message: bytes, random: bytes | None = None) -> bytes:
    """Produce a 64-byte XEdDSA signature of ``message`` under a Curve25519
    private key. Matches libsignal's SignCurve25519 byte-for-byte.
    """
    if len(curve25519_priv) != 32:
        raise ValueError("private key must be 32 bytes")
    if random is None:
        random = os.urandom(64)
    elif len(random) != 64:
        raise ValueError("random must be 64 bytes")

    # Clamp the scalar (standard Curve25519 clamping).
    a_bytes = bytearray(curve25519_priv)
    a_bytes[0] &= 248
    a_bytes[31] &= 127
    a_bytes[31] |= 64
    a = int.from_bytes(a_bytes, "little")

    # Public key A = aB on Edwards curve.
    A_point = _scalar_mul(_B, a)
    A = _encode_point(A_point)

    diversifier = b"\xfe" + b"\xff" * 31
    h = hashlib.sha512()
    h.update(diversifier)
    h.update(a_bytes)
    h.update(message)
    h.update(random)
    r = int.from_bytes(h.digest(), "little") % L

    R_point = _scalar_mul(_B, r)
    R = _encode_point(R_point)

    h2 = hashlib.sha512()
    h2.update(R)
    h2.update(A)
    h2.update(message)
    k = int.from_bytes(h2.digest(), "little") % L

    s = (r + k * a) % L
    s_bytes = s.to_bytes(32, "little")

    # Move the A sign bit into signature[63] top bit (XEdDSA convention).
    sig = bytearray(R + s_bytes)
    sig[63] |= A[31] & 0x80
    return bytes(sig)
