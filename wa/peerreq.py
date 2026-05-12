"""Build ``peerDataOperationRequestMessage`` payloads for on-demand history.

Hand-crafted protobuf — we don't pull in the full waE2E generated bindings
because the schema imports ~30 other proto files. The wire format is small
and the only fields we need are well-known.

Top-level Message proto, only field 12 (protocolMessage) populated:
    Message
      └ ProtocolMessage (field 12, wire 2)
          ├ type = 16 (PEER_DATA_OPERATION_REQUEST_MESSAGE) (field 2, varint)
          └ peerDataOperationRequestMessage (field 16, wire 2)
              ├ peerDataOperationRequestType = 3 (HISTORY_SYNC_ON_DEMAND) (field 1)
              └ historySyncOnDemandRequest (field 4, wire 2)
                  ├ chatJID  (field 1, string)
                  ├ oldestMsgID (field 2, string)
                  ├ oldestMsgFromMe (field 3, bool)
                  ├ onDemandMsgCount (field 4, int32)
                  └ oldestMsgTimestampMS (field 5, int64 — actually seconds, despite the name)
"""

from __future__ import annotations


def _varint(n: int) -> bytes:
    n &= (1 << 64) - 1
    out = bytearray()
    while n >= 0x80:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n & 0x7F)
    return bytes(out)


def _tag(field_num: int, wire: int) -> bytes:
    return _varint((field_num << 3) | wire)


def _length_delim(field_num: int, body: bytes) -> bytes:
    return _tag(field_num, 2) + _varint(len(body)) + body


def _string_field(field_num: int, s: str) -> bytes:
    return _length_delim(field_num, s.encode("utf-8"))


def _varint_field(field_num: int, n: int) -> bytes:
    return _tag(field_num, 0) + _varint(n)


def build_history_sync_on_demand(
    chat_jid: str,
    oldest_msg_id: str,
    oldest_ts: int,
    oldest_from_me: bool,
    count: int,
) -> bytes:
    """Build the inner ``HistorySyncOnDemandRequest`` body."""
    body = b""
    body += _string_field(1, chat_jid)
    body += _string_field(2, oldest_msg_id)
    body += _varint_field(3, 1 if oldest_from_me else 0)
    body += _varint_field(4, count)
    body += _varint_field(5, oldest_ts)
    return body


def build_peer_data_request(
    chat_jid: str,
    oldest_msg_id: str,
    oldest_ts: int,
    oldest_from_me: bool,
    count: int = 50,
) -> bytes:
    """Top-level Message proto bytes ready to feed Signal-encrypt + pad."""
    hsod = build_history_sync_on_demand(
        chat_jid=chat_jid,
        oldest_msg_id=oldest_msg_id,
        oldest_ts=oldest_ts,
        oldest_from_me=oldest_from_me,
        count=count,
    )
    pdor = b""
    pdor += _varint_field(1, 3)  # peerDataOperationRequestType = HISTORY_SYNC_ON_DEMAND
    pdor += _length_delim(4, hsod)

    pm = b""
    pm += _varint_field(2, 16)  # ProtocolMessage.type = PEER_DATA_OPERATION_REQUEST_MESSAGE
    pm += _length_delim(16, pdor)

    msg = _length_delim(12, pm)
    return msg


def pad_for_signal(plaintext: bytes) -> bytes:
    """Apply WhatsApp's pad-to-N-bytes padding scheme used by Signal frames.

    Each padding byte equals the count, with a random length in 1..15. This
    is what whatsmeow/Baileys send and what our ``_unpad`` reverses on decrypt.
    """
    import secrets

    pad_len = secrets.randbelow(15) + 1
    return plaintext + bytes([pad_len]) * pad_len
