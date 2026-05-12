"""QR pairing: handle ``pair-device`` / ``pair-success`` IQs, render QR.

Flow:

1. Server sends ``<iq from="s.whatsapp.net"><pair-device><ref>...</ref>*</pair-device></iq>``.
   We ACK with ``<iq type="result" id="…" to="s.whatsapp.net"/>`` and show
   the QR to the user.
2. Phone scans QR, does its own crypto, server sends
   ``<iq><pair-success><device-identity>…</device-identity><device/><biz/>
   <platform/></pair-success></iq>``.
3. We validate the HMAC + account signature, add our device signature,
   persist the new JID/LID/Account, and reply with ``<iq type="result">
   <pair-device-sign><device-identity key-index="N">…</device-identity>
   </pair-device-sign></iq>``.
4. Server disconnects. We reconnect as a paired device.
"""

from __future__ import annotations

import base64
import hmac
import hashlib
import logging

from wa.crypto.xeddsa import verify as xeddsa_verify
from wa.crypto.xeddsa_sign import xeddsa_sign
from wa.proto import waAdv_pb2
from wa.store import Device
from wa.wabinary import JID, Node

log = logging.getLogger(__name__)

# Prefixes are per libsignal ADV convention: domain-separation bytes folded
# into the HMAC / signed message so signatures can't be reused across
# contexts.
ADV_ACCOUNT_SIG_PREFIX = b"\x06\x00"
ADV_DEVICE_SIG_PREFIX = b"\x06\x01"
ADV_HOSTED_ACCOUNT_SIG_PREFIX = b"\x06\x05"
ADV_HOSTED_DEVICE_SIG_PREFIX = b"\x06\x06"


def make_qr_payload(ref: str, device: Device) -> str:
    """The comma-separated string encoded into the QR code.

    Decoded by the phone's WhatsApp, it yields our three public keys plus
    the pairing ref. The phone uses these to construct the encrypted
    ADVSignedDeviceIdentity it will send back in ``pair-success``.
    """
    parts = [
        ref,
        base64.b64encode(device.noise_key.pub).decode(),
        base64.b64encode(device.identity_key.pub).decode(),
        base64.b64encode(device.adv_secret).decode(),
    ]
    return ",".join(parts)


def extract_pair_refs(node: Node) -> list[str]:
    """Pull ref strings out of a ``<pair-device>`` IQ."""
    pair_device = node.get_child_by_tag("pair-device")
    if pair_device is None:
        return []
    refs: list[str] = []
    for child in pair_device.get_children():
        if child.tag != "ref":
            continue
        if isinstance(child.content, (bytes, bytearray)):
            refs.append(bytes(child.content).decode("utf-8"))
        elif isinstance(child.content, str):
            refs.append(child.content)
    return refs


class PairError(Exception):
    pass


def build_pair_device_ack(pair_device_iq: Node) -> Node:
    """Build the ACK we must send immediately after receiving a pair-device IQ.

    The server won't deliver pair-success until we've ACKed the initial
    pair-device IQ. Attributes must echo the original id and from-address.
    """
    iq_id = pair_device_iq.attrs.get("id", "")
    iq_from = pair_device_iq.attrs.get("from", "s.whatsapp.net")
    return Node(
        tag="iq",
        attrs={"to": iq_from, "id": iq_id, "type": "result"},
    )


def handle_pair_success(node: Node, device: Device) -> Node:
    """Validate the pair-success IQ and build the response ``<iq>`` node.

    Side effects: mutates ``device`` to record the paired JID, LID, platform,
    business name, and the self-signed ADVSignedDeviceIdentity bytes. Caller
    is responsible for persisting via ``device.save()``.
    """
    pair_success = node.get_child_by_tag("pair-success")
    if pair_success is None:
        raise PairError("no <pair-success> child")

    dev_id = pair_success.get_child_by_tag("device-identity")
    if dev_id is None or not isinstance(dev_id.content, (bytes, bytearray)):
        raise PairError("missing <device-identity> bytes")
    identity_hmac = waAdv_pb2.ADVSignedDeviceIdentityHMAC()
    identity_hmac.ParseFromString(bytes(dev_id.content))

    # Step 1: verify the HMAC the server attached to the (encrypted) identity.
    hosted = identity_hmac.accountType == waAdv_pb2.HOSTED
    prefix = ADV_HOSTED_ACCOUNT_SIG_PREFIX if hosted else b""
    mac = hmac.new(device.adv_secret, digestmod=hashlib.sha256)
    if prefix:
        mac.update(prefix)
    mac.update(identity_hmac.details)
    if mac.digest() != identity_hmac.HMAC:
        raise PairError("ADVSignedDeviceIdentityHMAC HMAC mismatch")

    signed_identity = waAdv_pb2.ADVSignedDeviceIdentity()
    signed_identity.ParseFromString(identity_hmac.details)

    inner = waAdv_pb2.ADVDeviceIdentity()
    inner.ParseFromString(signed_identity.details)

    # Step 2: verify the AccountSignature (signed by the phone's account key).
    acc_prefix = (
        ADV_HOSTED_ACCOUNT_SIG_PREFIX
        if inner.deviceType == waAdv_pb2.HOSTED
        else ADV_ACCOUNT_SIG_PREFIX
    )
    acc_msg = acc_prefix + bytes(signed_identity.details) + device.identity_key.pub
    if not xeddsa_verify(
        bytes(signed_identity.accountSignatureKey),
        acc_msg,
        bytes(signed_identity.accountSignature),
    ):
        raise PairError("AccountSignature invalid")

    # Step 3: sign the identity with our own IdentityKey so the server can
    # bind this device to our noise static.
    dev_prefix = (
        ADV_HOSTED_DEVICE_SIG_PREFIX
        if inner.deviceType == waAdv_pb2.HOSTED
        else ADV_DEVICE_SIG_PREFIX
    )
    dev_msg = (
        dev_prefix
        + bytes(signed_identity.details)
        + device.identity_key.pub
        + bytes(signed_identity.accountSignatureKey)
    )
    signed_identity.deviceSignature = xeddsa_sign(device.identity_key.priv, dev_msg)

    # Step 4: extract our new JID/LID/platform from neighbouring children.
    device_node = pair_success.get_child_by_tag("device")
    biz_node = pair_success.get_child_by_tag("biz")
    platform_node = pair_success.get_child_by_tag("platform")

    if device_node is not None:
        jid_val = device_node.attrs.get("jid")
        if isinstance(jid_val, JID):
            device.jid = str(jid_val)
        elif isinstance(jid_val, str):
            device.jid = jid_val
        lid_val = device_node.attrs.get("lid")
        if isinstance(lid_val, JID):
            device.lid = str(lid_val)
        elif isinstance(lid_val, str):
            device.lid = lid_val
    if biz_node is not None:
        bn = biz_node.attrs.get("name")
        if isinstance(bn, str):
            device.business_name = bn
    if platform_node is not None:
        pl = platform_node.attrs.get("name")
        if isinstance(pl, str):
            device.platform = pl

    # Step 5: persist — drop the accountSignatureKey from what we re-send
    # (whatsmeow nulls it before marshalling) and save the full identity
    # under Account for future reconnects.
    device.account = signed_identity.SerializeToString()
    send_copy = waAdv_pb2.ADVSignedDeviceIdentity()
    send_copy.CopyFrom(signed_identity)
    send_copy.ClearField("accountSignatureKey")
    send_bytes = send_copy.SerializeToString()

    iq_id = node.attrs.get("id", "")
    return Node(
        tag="iq",
        attrs={"to": node.attrs.get("from", "s.whatsapp.net"), "type": "result", "id": iq_id},
        content=[
            Node(
                tag="pair-device-sign",
                content=[
                    Node(
                        tag="device-identity",
                        attrs={"key-index": str(inner.keyIndex)},
                        content=send_bytes,
                    )
                ],
            )
        ],
    )


def render_qr_ansi(data: str) -> str:
    """Render the QR payload as an ANSI-friendly block of ASCII art.

    Uses the ``qrcode`` package if present, falling back to a short message
    that just prints the raw payload (the user can then use any QR tool).
    """
    try:
        import qrcode
    except ImportError:
        return f"[qrcode package missing — raw payload below]\n{data}\n"

    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=1,
        border=1,
    )
    qr.add_data(data)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    # Use UTF-8 half-blocks for terminal compactness: each row pair → 1 text line.
    lines: list[str] = []
    rows = len(matrix)
    for r in range(0, rows, 2):
        row1 = matrix[r]
        row2 = matrix[r + 1] if r + 1 < rows else [False] * len(row1)
        line = []
        for a, b in zip(row1, row2):
            if a and b:
                line.append("█")
            elif a and not b:
                line.append("▀")
            elif not a and b:
                line.append("▄")
            else:
                line.append(" ")
        lines.append("".join(line))
    return "\n".join(lines) + "\n"
