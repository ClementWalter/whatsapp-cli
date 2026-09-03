"""Hand-rolled ``waE2E.Message`` builders for outbound media.

We don't vendor the full waE2E proto graph (it imports ~30 files), so —
like :mod:`wa.peerreq` — we emit the wire bytes directly. Field numbers
mirror whatsmeow's ``WAWebProtobufsE2E`` bindings:

``Message`` (outer):
    7  documentMessage (length-delimited)
    31 deviceSentMessage (length-delimited)

``DocumentMessage``:
    1  URL (string)            9  fileEncSHA256 (bytes)
    2  mimetype (string)       10 directPath (string)
    3  title (string)          11 mediaKeyTimestamp (int64, seconds)
    4  fileSHA256 (bytes)      16 jpegThumbnail (bytes)
    5  fileLength (uint64)     18 thumbnailHeight (uint32)
    6  pageCount (uint32)      19 thumbnailWidth (uint32)
    7  mediaKey (bytes)        20 caption (string)
    8  fileName (string)
"""

from __future__ import annotations

from dataclasses import dataclass

from wa.peerreq import _length_delim, _string_field, _varint_field


@dataclass(frozen=True)
class DocumentInfo:
    """Fully-resolved document descriptor (post-upload)."""

    url: str
    direct_path: str
    mimetype: str
    file_name: str
    media_key: bytes
    file_sha256: bytes
    file_enc_sha256: bytes
    file_length: int
    media_key_timestamp: int
    caption: str | None = None
    title: str | None = None
    jpeg_thumbnail: bytes | None = None  # inline JPEG preview WhatsApp renders
    thumbnail_width: int | None = None
    thumbnail_height: int | None = None


def _bytes_field(field_num: int, b: bytes) -> bytes:
    return _length_delim(field_num, b)


def build_document_message(doc: DocumentInfo) -> bytes:
    """Encode a ``DocumentMessage`` body (the value of Message field 7)."""
    body = b""
    body += _string_field(1, doc.url)
    body += _string_field(2, doc.mimetype)
    if doc.title:
        body += _string_field(3, doc.title)
    body += _bytes_field(4, doc.file_sha256)
    body += _varint_field(5, doc.file_length)
    body += _bytes_field(7, doc.media_key)
    body += _string_field(8, doc.file_name)
    body += _bytes_field(9, doc.file_enc_sha256)
    body += _string_field(10, doc.direct_path)
    body += _varint_field(11, doc.media_key_timestamp)
    if doc.jpeg_thumbnail:
        body += _bytes_field(16, doc.jpeg_thumbnail)
        if doc.thumbnail_height:
            body += _varint_field(18, doc.thumbnail_height)
        if doc.thumbnail_width:
            body += _varint_field(19, doc.thumbnail_width)
    if doc.caption:
        body += _string_field(20, doc.caption)
    return body


def build_document_plaintext(doc: DocumentInfo) -> bytes:
    """``Message{documentMessage = 7}`` — the plaintext sent to the peer."""
    return _length_delim(7, build_document_message(doc))


def build_document_dsm_plaintext(doc: DocumentInfo, destination_jid: str) -> bytes:
    """``Message{deviceSentMessage = 31}`` wrapping the document.

    Sent to our own other linked devices so they render the outgoing file,
    matching :func:`wa.cli._build_dsm_plaintext` for text.
    """
    inner = build_document_plaintext(doc)  # nested Message{documentMessage}
    dsm = _string_field(1, destination_jid) + _length_delim(2, inner)
    return _length_delim(31, dsm)


# --- Quoted replies -------------------------------------------------------
#
# A WhatsApp "reply" is an ordinary content message carrying a ContextInfo
# (waE2E.proto): ExtendedTextMessage/ImageMessage/VideoMessage/Document/
# AudioMessage all hold it at field 17, with ``stanzaID`` (1) = id of the
# quoted message, ``participant`` (2) = its sender, ``quotedMessage`` (3) =
# a copy of it for preview. Text replies always come as extendedTextMessage,
# never as a bare ``conversation``.

_CONTEXT_INFO_FIELD = 17
# Message sub-messages that may carry a ContextInfo, and the wrappers that
# nest another Message (same table as ``wa.cli._extract_text``).
_CONTENT_WITH_CONTEXT = (6, 3, 9, 7, 8, 26)
_WRAPPERS = ((31, 2), (40, 1), (37, 1), (55, 1), (53, 1))

# Field numbers of the download-relevant attributes per media sub-message
# (waE2E.proto): url, mimetype, fileSha256, fileLength, mediaKey,
# fileEncSha256, directPath, plus `seconds` for audio/video.
_MEDIA_LAYOUT = {
    8: ("audio", {"url": 1, "mimetype": 2, "file_sha256": 3, "file_length": 4, "seconds": 5, "ptt": 6, "media_key": 7, "file_enc_sha256": 8, "direct_path": 9}),
    3: ("image", {"url": 1, "mimetype": 2, "file_sha256": 4, "file_length": 5, "media_key": 8, "file_enc_sha256": 9, "direct_path": 11}),
    9: ("video", {"url": 1, "mimetype": 2, "file_sha256": 3, "file_length": 4, "seconds": 5, "media_key": 6, "file_enc_sha256": 11, "direct_path": 13}),
    7: ("document", {"url": 1, "mimetype": 2, "file_sha256": 4, "file_length": 5, "media_key": 7, "file_name": 8, "file_enc_sha256": 9, "direct_path": 10}),
}
_BYTES_KEYS = ("media_key", "file_sha256", "file_enc_sha256")


def extract_media(plaintext: bytes, _depth: int = 0) -> dict | None:
    """Return the CDN reference of an audio/image/video/document message, else None.

    Binary keys are base64 so the dict round-trips through the JSONL cache;
    ``wa download`` turns it back into bytes.
    """
    import base64

    from wa.pbutil import decode_fields

    if _depth > 4:
        return None
    try:
        top = decode_fields(plaintext)
    except Exception:
        return None
    for field, (kind, layout) in _MEDIA_LAYOUT.items():
        if (content := _first_bytes(top, field)) is None:
            continue
        try:
            inner = decode_fields(content)
        except ValueError:
            return None
        ref: dict = {"kind": kind}
        for name, num in layout.items():
            values = inner.get(num)
            if not values:
                continue
            value = values[0]
            if isinstance(value, (bytes, bytearray)):
                value = base64.b64encode(bytes(value)).decode() if name in _BYTES_KEYS else bytes(value).decode("utf-8", "replace")
            ref[name] = value
        return ref if ref.get("media_key") and (ref.get("url") or ref.get("direct_path")) else None
    for outer, inner_field in _WRAPPERS:
        if (wrapped := _first_bytes(top, outer)) is None:
            continue
        try:
            nested = _first_bytes(decode_fields(wrapped), inner_field)
        except ValueError:
            continue
        if nested is not None:
            return extract_media(nested, _depth + 1)
    return None


def _first_bytes(fields: dict, field: int) -> bytes | None:
    values = fields.get(field)
    if values and isinstance(values[0], (bytes, bytearray)):
        return bytes(values[0])
    return None


def extract_quoted_id(plaintext: bytes, _depth: int = 0) -> str:
    """Return the stanza id this message quotes, or "" when it is not a reply."""
    from wa.pbutil import decode_fields

    if _depth > 4:
        return ""
    try:
        top = decode_fields(plaintext)
    except Exception:
        return ""
    for field in _CONTENT_WITH_CONTEXT:
        if (content := _first_bytes(top, field)) is None:
            continue
        try:
            ctx = _first_bytes(decode_fields(content), _CONTEXT_INFO_FIELD)
            if ctx is None:
                return ""
            stanza = _first_bytes(decode_fields(ctx), 1)
            return stanza.decode("utf-8") if stanza else ""
        except (ValueError, UnicodeDecodeError):
            return ""
    for outer, inner in _WRAPPERS:
        if (wrapped := _first_bytes(top, outer)) is None:
            continue
        try:
            nested = _first_bytes(decode_fields(wrapped), inner)
        except ValueError:
            continue
        if nested is not None:
            return extract_quoted_id(nested, _depth + 1)
    return ""


@dataclass(frozen=True)
class QuotedRef:
    """What a reply needs to know about the message it quotes."""

    msg_id: str
    participant: str  # bare JID of the quoted sender, no ":device" suffix
    text: str

    @classmethod
    def from_cached(cls, msg_id: str, sender: str, text: str) -> "QuotedRef":
        # Cached senders carry the device ("336...:25@s.whatsapp.net"); ContextInfo wants the bare JID.
        user, _, server = sender.partition("@")
        return cls(msg_id=msg_id, participant=f"{user.split(':')[0]}@{server}" if server else sender, text=text)


def device_sent_destination(plaintext: bytes) -> str:
    """For a copy of our own phone's message (deviceSentMessage), the chat it really went to; "" otherwise."""
    from wa.pbutil import decode_fields

    try:
        dsm = _first_bytes(decode_fields(plaintext), 31)
        if dsm is None:
            return ""
        dest = _first_bytes(decode_fields(dsm), 1)
    except ValueError:
        return ""
    return dest.decode("utf-8", "replace") if dest else ""


def build_reply_plaintext(text: str, quoted_id: str, quoted_participant: str, quoted_text: str) -> bytes:
    """Encode ``Message{extendedTextMessage{text, contextInfo{...}}}`` quoting another message.

    ``quoted_participant`` is the bare JID of the quoted message's sender (no
    device suffix); ``quoted_text`` feeds the preview the phone renders above
    the reply.
    """
    quoted = _string_field(1, quoted_text)  # quotedMessage: Message{conversation}
    context = _string_field(1, quoted_id) + _string_field(2, quoted_participant) + _length_delim(3, quoted)
    extended = _string_field(1, text) + _length_delim(_CONTEXT_INFO_FIELD, context)
    return _length_delim(6, extended)


def wrap_device_sent(message_plaintext: bytes, destination_jid: str) -> bytes:
    """Wrap any Message in ``deviceSentMessage`` (field 31) for own-device fan-out."""
    return _length_delim(31, _string_field(1, destination_jid) + _length_delim(2, message_plaintext))
