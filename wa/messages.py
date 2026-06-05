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
