"""One-time pre-key bundle generation and upload.

WhatsApp expects every linked device to publish a batch of Signal one-time
pre-keys (X3DH OPKs) so other clients can initiate fresh sessions without
needing the phone to mediate. On a new pair the phone's UI waits for this
upload before considering our device fully online — without it the
"connecting…" dialog spins forever.

Whatsmeow uploads 812 keys on initial pair, then 50 at a time when the
server reports < 5 remaining. We follow the same shape but with a smaller
default batch for first-pair (fewer Curve25519 keypairs to generate).
"""

from __future__ import annotations

import secrets
import struct

from wa.crypto.noise import x25519_public
from wa.wabinary import JID, Node

# Drop one-time pre-keys count for now: 30 is well above whatsmeow's
# MinPreKeyCount (5), and uploading 812 on a slow Python implementation
# adds latency without meaningful security benefit for a personal client.
INITIAL_UPLOAD_COUNT = 30
ECC_DJB_TYPE = 0x05


def generate_prekey_batch(start_id: int, count: int) -> list[dict]:
    """Generate ``count`` Curve25519 one-time pre-keys starting at id ``start_id``.

    Each entry is a dict ``{"key_id": int, "priv": bytes, "pub": bytes}``.
    Pre-keys are NOT signed (only the SignedPreKey is). Server demands a
    contiguous range starting at the next available id.
    """
    out: list[dict] = []
    for i in range(count):
        priv = secrets.token_bytes(32)
        # Curve25519 private keys are clamped at use time; pyca handles that
        # internally when we call X25519PrivateKey.from_private_bytes, so we
        # don't need to clamp here. Still, the public derivation must match
        # what the server will see when others run X25519 against this key.
        pub = x25519_public(priv)
        out.append({"key_id": start_id + i, "priv": priv, "pub": pub})
    return out


def _key_id_24bit(key_id: int) -> bytes:
    return struct.pack(">I", key_id)[1:]


def build_upload_iq(
    iq_id: str,
    registration_id: int,
    identity_key_pub: bytes,
    one_time_prekeys: list[dict],
    signed_prekey: dict,
) -> Node:
    """Construct the ``<iq xmlns='encrypt' type='set'>`` payload for upload.

    Wire format mirrors whatsmeow/prekeys.go:uploadPreKeys exactly:

        <iq to=s.whatsapp.net type=set id=… xmlns=encrypt>
          <registration>{4-byte BE u32}</registration>
          <type>{0x05}</type>
          <identity>{32-byte pub}</identity>
          <list>
            <key><id>{3-byte BE}</id><value>{32-byte pub}</value></key>
            …
          </list>
          <skey>
            <id>{3-byte BE}</id>
            <value>{32-byte pub}</value>
            <signature>{64-byte sig}</signature>
          </skey>
        </iq>
    """
    list_children = [
        Node(
            tag="key",
            content=[
                Node(tag="id", content=_key_id_24bit(k["key_id"])),
                Node(tag="value", content=k["pub"]),
            ],
        )
        for k in one_time_prekeys
    ]
    skey = Node(
        tag="skey",
        content=[
            Node(tag="id", content=_key_id_24bit(signed_prekey["key_id"])),
            Node(tag="value", content=signed_prekey["pub"]),
            Node(tag="signature", content=signed_prekey["signature"]),
        ],
    )
    return Node(
        tag="iq",
        attrs={
            "to": JID(server="s.whatsapp.net"),
            "type": "set",
            "id": iq_id,
            "xmlns": "encrypt",
        },
        content=[
            Node(tag="registration", content=struct.pack(">I", registration_id)),
            Node(tag="type", content=bytes([ECC_DJB_TYPE])),
            Node(tag="identity", content=identity_key_pub),
            Node(tag="list", content=list_children),
            skey,
        ],
    )


def build_get_count_iq(iq_id: str) -> Node:
    """Ask the server how many pre-keys it currently has on file for us."""
    return Node(
        tag="iq",
        attrs={
            "to": JID(server="s.whatsapp.net"),
            "type": "get",
            "id": iq_id,
            "xmlns": "encrypt",
        },
        content=[Node(tag="count")],
    )
