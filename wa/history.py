"""History sync extraction.

WhatsApp sends linked devices a series of ``HistorySyncNotification`` IQs
after pairing. Each notification either inlines an encrypted payload
(``initialHistBootstrapInlinePayload``) or points to a CDN blob via
``directPath`` + ``mediaKey`` + ``fileEncSHA256``.

This module:

- decodes a HistorySyncNotification proto (via our minimal proto walker),
- decompresses / decrypts its payload depending on which variant is set,
- walks the resulting HistorySync / Conversation / HistorySyncMsg tree,
- extracts readable ``(chat_jid, sender_name, timestamp, text)`` tuples.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from wa.pbutil import decode_fields

log = logging.getLogger(__name__)

# HistorySyncType values from waHistorySync.proto — useful in logs.
SYNC_TYPES = {
    0: "INITIAL_BOOTSTRAP",
    1: "INITIAL_STATUS_V3",
    2: "FULL",
    3: "RECENT",
    4: "PUSH_NAME",
    5: "NON_BLOCKING_DATA",
    6: "ON_DEMAND",
    7: "FULL_WITH_MEDIA_METADATA",
}


@dataclass
class HistoryMessage:
    chat_jid: str
    sender_jid: str
    sender_name: str
    timestamp: int
    text: str
    from_me: bool
    msg_id: str = ""


def _aes_cbc_decrypt(key: bytes, iv: bytes, ct: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(ct) + decryptor.finalize()
    pad = padded[-1]
    return padded[:-pad]


def _hkdf_expand(ikm: bytes, info: bytes, length: int) -> bytes:
    # HKDF-SHA256 with empty salt, as used by all WhatsApp media keys.
    prk = hmac.new(b"\x00" * 32, ikm, hashlib.sha256).digest()
    output = b""
    t = b""
    counter = 1
    while len(output) < length:
        t = hmac.new(prk, t + info + bytes([counter]), hashlib.sha256).digest()
        output += t
        counter += 1
    return output[:length]


def decrypt_history_payload(
    media_key: bytes, ciphertext: bytes, file_enc_sha256: bytes | None
) -> bytes:
    """AES-CBC decrypt + HMAC verify a CDN-hosted history blob.

    Matches the standard WA media scheme: HKDF(mediaKey, info="WhatsApp
    History Keys", 112 bytes) → iv[16]|cipherKey[32]|macKey[32]|refKey[32].
    Last 10 bytes of ``ciphertext`` are the HMAC-SHA256(iv||rest)[:10].
    """
    expanded = _hkdf_expand(media_key, b"WhatsApp History Keys", 112)
    iv, cipher_key, mac_key = expanded[:16], expanded[16:48], expanded[48:80]
    if len(ciphertext) <= 10:
        raise ValueError("history ciphertext too short")
    body, mac = ciphertext[:-10], ciphertext[-10:]
    expected = hmac.new(mac_key, iv + body, hashlib.sha256).digest()[:10]
    if not hmac.compare_digest(expected, mac):
        raise ValueError("history HMAC mismatch")
    if file_enc_sha256 and hashlib.sha256(ciphertext).digest() != file_enc_sha256:
        raise ValueError("history fileEncSHA256 mismatch")
    return _aes_cbc_decrypt(cipher_key, iv, body)


def decode_history_sync_notification(hsn_bytes: bytes) -> dict:
    """Return the fields we care about from a HistorySyncNotification proto."""
    f = decode_fields(hsn_bytes)

    def first(tag, default=None):
        if tag in f and f[tag]:
            return f[tag][0]
        return default

    return {
        "file_sha256": first(1),
        "file_length": first(2),
        "media_key": first(3),
        "file_enc_sha256": first(4),
        "direct_path": first(5),
        "sync_type": first(6),
        "chunk_order": first(7),
        "original_message_id": first(8),
        "progress": first(9),
        "inline_payload": first(11),
    }


def parse_history_sync(payload: bytes) -> dict:
    """Decompress the zlib payload and parse it as a HistorySync proto.

    Returns a dict with ``sync_type``, ``conversations`` (list of inner
    Conversation bytes), ``pushnames`` (list of (jid, name) tuples).
    """
    decompressed = zlib.decompress(payload)
    f = decode_fields(decompressed)
    pushnames: list[tuple[str, str]] = []
    for pn_bytes in f.get(7, []):
        if not isinstance(pn_bytes, (bytes, bytearray)):
            continue
        pn = decode_fields(bytes(pn_bytes))
        jid = pn.get(1, [b""])[0]
        name = pn.get(2, [b""])[0]
        try:
            pushnames.append(
                (
                    bytes(jid).decode("utf-8") if isinstance(jid, (bytes, bytearray)) else "",
                    bytes(name).decode("utf-8") if isinstance(name, (bytes, bytearray)) else "",
                )
            )
        except UnicodeDecodeError:
            continue
    conversations = [
        bytes(c) for c in f.get(2, []) if isinstance(c, (bytes, bytearray))
    ]
    # PhoneNumberToLIDMapping (field 15): groups use @lid sender JIDs that
    # don't match the @s.whatsapp.net keys our pushnames are stored under.
    # This mapping lets us translate one to the other so contact lookup works
    # for group senders too.
    lid_to_pn: list[tuple[str, str]] = []
    for m_bytes in f.get(15, []):
        if not isinstance(m_bytes, (bytes, bytearray)):
            continue
        m = decode_fields(bytes(m_bytes))
        pn = m.get(1, [b""])[0]
        lid = m.get(2, [b""])[0]
        try:
            pn_s = bytes(pn).decode("utf-8") if isinstance(pn, (bytes, bytearray)) else ""
            lid_s = bytes(lid).decode("utf-8") if isinstance(lid, (bytes, bytearray)) else ""
        except UnicodeDecodeError:
            continue
        if pn_s and lid_s:
            lid_to_pn.append((lid_s, pn_s))
    sync_type = f.get(1, [None])[0]
    return {
        "sync_type": sync_type,
        "sync_type_name": SYNC_TYPES.get(sync_type, f"UNKNOWN({sync_type})"),
        "conversations": conversations,
        "pushnames": pushnames,
        "lid_to_pn": lid_to_pn,
        "raw_size": len(decompressed),
    }


def conversation_name(conv_bytes: bytes) -> tuple[str, str, int]:
    """Return ``(chat_jid, name, last_msg_timestamp)`` from a Conversation proto."""
    conv = decode_fields(conv_bytes)
    chat_id = ""
    if 1 in conv and isinstance(conv[1][0], (bytes, bytearray)):
        try:
            chat_id = bytes(conv[1][0]).decode("utf-8")
        except UnicodeDecodeError:
            pass
    name = ""
    if 13 in conv and isinstance(conv[13][0], (bytes, bytearray)):
        try:
            name = bytes(conv[13][0]).decode("utf-8")
        except UnicodeDecodeError:
            pass
    last_ts = 0
    if 5 in conv and isinstance(conv[5][0], int):
        last_ts = conv[5][0]
    return chat_id, name, last_ts


def iter_conversation_messages(conv_bytes: bytes) -> Iterable[HistoryMessage]:
    """Walk a Conversation proto and emit ``HistoryMessage`` rows.

    Conversation (waHistorySync.proto):
      1  string id           — chat JID
      2  HistorySyncMsg messages (repeated)
      5  uint64 lastMsgTimestamp
      13 string name         — group display name (and some DM custom names)

    HistorySyncMsg:
      1  WebMessageInfo message
         key={1 remoteJid, 2 fromMe, 3 id, 4 participant}
         message=Message     (same as live, field 2)
         messageTimestamp=3  (uint64)
         participant=5       (string — same value as key.participant in groups)
         pushName=19         (string — sender display name at time of msg)
    """
    chat_id = conversation_name(conv_bytes)[0]
    conv = decode_fields(conv_bytes)

    for hsm_bytes in conv.get(2, []):
        if not isinstance(hsm_bytes, (bytes, bytearray)):
            continue
        hsm = decode_fields(bytes(hsm_bytes))
        if 1 not in hsm or not isinstance(hsm[1][0], (bytes, bytearray)):
            continue
        wmi = decode_fields(bytes(hsm[1][0]))
        key = (
            decode_fields(bytes(wmi[1][0]))
            if 1 in wmi and isinstance(wmi[1][0], (bytes, bytearray))
            else {}
        )
        from_me = bool(key.get(2, [0])[0])

        def _utf8(field: int, src: dict) -> str:
            v = src.get(field, [b""])[0]
            if isinstance(v, (bytes, bytearray)):
                try:
                    return bytes(v).decode("utf-8")
                except UnicodeDecodeError:
                    return ""
            return ""

        participant = _utf8(4, key) or _utf8(5, wmi)
        msg_id = _utf8(3, key)
        timestamp = wmi.get(3, [0])[0] if 3 in wmi else 0
        push_name = _utf8(19, wmi)

        text = ""
        if 2 in wmi and isinstance(wmi[2][0], (bytes, bytearray)):
            # Local import sidesteps a circular import with the entry script
            # (it imports from this module too).
            from scripts.whatsapp_user_cli import _extract_text

            text = _extract_text(bytes(wmi[2][0]))

        yield HistoryMessage(
            chat_jid=chat_id,
            sender_jid=participant if participant else chat_id,
            sender_name=push_name,
            timestamp=int(timestamp),
            text=text,
            from_me=from_me,
            msg_id=msg_id,
        )
