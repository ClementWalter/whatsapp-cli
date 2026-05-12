"""The Noise XX handshake choreography as WhatsApp uses it.

Mirrors whatsmeow/handshake.go:doHandshake. Separates the transport-agnostic
cert verification from the network I/O so the logic can be unit-tested
offline with captured ServerHello bytes.
"""

from __future__ import annotations

import logging

from wa.crypto.noise import (
    NoiseHandshake,
    NoiseSocket,
    x25519_generate,
    x25519_public,
)
from wa.crypto.xeddsa import verify as xeddsa_verify
from wa.proto import waCert_pb2, waWa6_pb2
from wa.store import Device
from wa.transport.framesocket import WA_HEADER, FrameSocket

log = logging.getLogger(__name__)

NOISE_PATTERN = "Noise_XX_25519_AESGCM_SHA256\x00\x00\x00\x00"

# Hardcoded WhatsApp root certificate public key (Curve25519 / XEdDSA).
# Same 32 bytes as whatsmeow's WACertPubKey. Rotating this would require a
# code update; it hasn't changed in years.
WA_CERT_PUB_KEY = bytes([
    0x14, 0x23, 0x75, 0x57, 0x4d, 0x0a, 0x58, 0x71,
    0x66, 0xaa, 0xe7, 0x1e, 0xbe, 0x51, 0x64, 0x37,
    0xc4, 0xa2, 0x8b, 0x73, 0xe3, 0x69, 0x5c, 0x6c,
    0xe1, 0xf7, 0xf9, 0x54, 0x5d, 0xa8, 0xee, 0x6b,
])
WA_CERT_ISSUER_SERIAL = 0


class HandshakeError(Exception):
    pass


def _verify_cert_chain(cert_bytes: bytes, server_static: bytes) -> None:
    """Validate the two-level cert chain the server sends inside ServerHello.

    The chain structure is: root (implicit, hardcoded pub key) → intermediate
    → leaf. Leaf's key must equal the server's static Noise key bytes we
    just decrypted.
    """
    chain = waCert_pb2.CertChain()
    chain.ParseFromString(cert_bytes)

    intermediate = chain.intermediate
    leaf = chain.leaf
    if not intermediate.details or not intermediate.signature:
        raise HandshakeError("missing intermediate cert parts")
    if not leaf.details or not leaf.signature:
        raise HandshakeError("missing leaf cert parts")
    if len(intermediate.signature) != 64 or len(leaf.signature) != 64:
        raise HandshakeError("unexpected signature length")

    # Intermediate signed by the hardcoded root.
    if not xeddsa_verify(WA_CERT_PUB_KEY, intermediate.details, intermediate.signature):
        raise HandshakeError("intermediate cert signature invalid")

    inter_details = waCert_pb2.CertChain.NoiseCertificate.Details()
    inter_details.ParseFromString(intermediate.details)
    if inter_details.issuerSerial != WA_CERT_ISSUER_SERIAL:
        raise HandshakeError(
            f"unexpected intermediate issuer serial {inter_details.issuerSerial}"
        )
    if len(inter_details.key) != 32:
        raise HandshakeError("intermediate cert key wrong length")

    # Leaf signed by the intermediate.
    if not xeddsa_verify(bytes(inter_details.key), leaf.details, leaf.signature):
        raise HandshakeError("leaf cert signature invalid")

    leaf_details = waCert_pb2.CertChain.NoiseCertificate.Details()
    leaf_details.ParseFromString(leaf.details)
    if leaf_details.issuerSerial != inter_details.serial:
        raise HandshakeError(
            f"leaf issuer serial mismatch: {leaf_details.issuerSerial} vs {inter_details.serial}"
        )
    if bytes(leaf_details.key) != server_static:
        raise HandshakeError("leaf cert key doesn't match decrypted server static")


async def do_handshake(
    fs: FrameSocket, device: Device, client_payload: bytes
) -> NoiseSocket:
    """Run the full XX handshake against a connected FrameSocket.

    Returns a NoiseSocket ready for traffic. Raises HandshakeError on any
    server-side rejection or crypto failure.
    """
    nh = NoiseHandshake()
    nh.start(NOISE_PATTERN, WA_HEADER)

    # --- 1. Client → Server: ClientHello{ephemeral} ---
    ephemeral_priv, ephemeral_pub = x25519_generate()
    nh.authenticate(ephemeral_pub)
    msg = waWa6_pb2.HandshakeMessage()
    msg.clientHello.ephemeral = ephemeral_pub
    await fs.send(msg.SerializeToString())

    # --- 2. Server → Client: ServerHello{ephemeral, static, payload} ---
    resp = await fs.recv(timeout=20.0)
    reply = waWa6_pb2.HandshakeMessage()
    reply.ParseFromString(resp)
    sh = reply.serverHello
    if not sh.ephemeral or not sh.static or not sh.payload:
        raise HandshakeError("ServerHello missing fields")
    server_eph = bytes(sh.ephemeral)
    if len(server_eph) != 32:
        raise HandshakeError(f"ServerHello.ephemeral wrong length {len(server_eph)}")

    nh.authenticate(server_eph)
    nh.mix_shared_secret(ephemeral_priv, server_eph)  # ee
    server_static = nh.decrypt(bytes(sh.static))
    if len(server_static) != 32:
        raise HandshakeError(f"server static wrong length {len(server_static)}")
    nh.mix_shared_secret(ephemeral_priv, server_static)  # es

    cert_bytes = nh.decrypt(bytes(sh.payload))
    _verify_cert_chain(cert_bytes, server_static)

    # --- 3. Client → Server: ClientFinish{static, payload=ClientPayload} ---
    static_ct = nh.encrypt(device.noise_key.pub)
    nh.mix_shared_secret(device.noise_key.priv, server_eph)  # se
    payload_ct = nh.encrypt(client_payload)

    msg = waWa6_pb2.HandshakeMessage()
    msg.clientFinish.static = static_ct
    msg.clientFinish.payload = payload_ct
    await fs.send(msg.SerializeToString())

    # --- 4. Derive traffic keys ---
    write_key, read_key = nh.finish()
    return NoiseSocket(write_key=write_key, read_key=read_key)
