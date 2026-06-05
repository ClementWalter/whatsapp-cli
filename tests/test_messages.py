"""DocumentMessage / deviceSentMessage proto wire encoding."""

from __future__ import annotations

import pytest

from wa.messages import (
    DocumentInfo,
    build_document_dsm_plaintext,
    build_document_message,
    build_document_plaintext,
)
from wa.pbutil import decode_fields


@pytest.fixture
def doc() -> DocumentInfo:
    return DocumentInfo(
        url="https://mmg.whatsapp.net/d/f/abc.enc",
        direct_path="/v/t62/abc.enc",
        mimetype="application/pdf",
        file_name="report.pdf",
        media_key=b"k" * 32,
        file_sha256=b"s" * 32,
        file_enc_sha256=b"e" * 32,
        file_length=12345,
        media_key_timestamp=1700000000,
        caption="see attached",
        title="report.pdf",
    )


def test_document_message_url(doc):
    fields = decode_fields(build_document_message(doc))
    assert fields[1][0].decode() == doc.url


def test_document_message_mimetype(doc):
    fields = decode_fields(build_document_message(doc))
    assert fields[2][0].decode() == "application/pdf"


def test_document_message_file_sha256(doc):
    fields = decode_fields(build_document_message(doc))
    assert fields[4][0] == doc.file_sha256


def test_document_message_file_length(doc):
    fields = decode_fields(build_document_message(doc))
    assert fields[5][0] == doc.file_length


def test_document_message_media_key(doc):
    fields = decode_fields(build_document_message(doc))
    assert fields[7][0] == doc.media_key


def test_document_message_file_name(doc):
    fields = decode_fields(build_document_message(doc))
    assert fields[8][0].decode() == "report.pdf"


def test_document_message_file_enc_sha256(doc):
    fields = decode_fields(build_document_message(doc))
    assert fields[9][0] == doc.file_enc_sha256


def test_document_message_direct_path(doc):
    fields = decode_fields(build_document_message(doc))
    assert fields[10][0].decode() == doc.direct_path


def test_document_message_media_key_timestamp(doc):
    fields = decode_fields(build_document_message(doc))
    assert fields[11][0] == doc.media_key_timestamp


def test_document_message_caption(doc):
    fields = decode_fields(build_document_message(doc))
    assert fields[20][0].decode() == "see attached"


def test_document_message_no_thumbnail_field_when_absent(doc):
    fields = decode_fields(build_document_message(doc))
    assert 16 not in fields


def test_document_message_jpeg_thumbnail(doc):
    with_thumb = DocumentInfo(
        **{**doc.__dict__, "jpeg_thumbnail": b"\xff\xd8jpeg", "thumbnail_width": 400, "thumbnail_height": 300}
    )
    fields = decode_fields(build_document_message(with_thumb))
    assert fields[16][0] == b"\xff\xd8jpeg"


def test_document_message_thumbnail_dimensions(doc):
    with_thumb = DocumentInfo(
        **{**doc.__dict__, "jpeg_thumbnail": b"\xff\xd8jpeg", "thumbnail_width": 400, "thumbnail_height": 300}
    )
    fields = decode_fields(build_document_message(with_thumb))
    assert (fields[19][0], fields[18][0]) == (400, 300)


def test_document_message_cdn_thumbnail_direct_path(doc):
    with_cdn = DocumentInfo(
        **{
            **doc.__dict__,
            "thumbnail_direct_path": "/v/t62/thumb.enc",
            "thumbnail_sha256": b"t" * 32,
            "thumbnail_enc_sha256": b"u" * 32,
        }
    )
    fields = decode_fields(build_document_message(with_cdn))
    assert fields[13][0].decode() == "/v/t62/thumb.enc"


def test_document_message_cdn_thumbnail_hashes(doc):
    with_cdn = DocumentInfo(
        **{
            **doc.__dict__,
            "thumbnail_direct_path": "/v/t62/thumb.enc",
            "thumbnail_sha256": b"t" * 32,
            "thumbnail_enc_sha256": b"u" * 32,
        }
    )
    fields = decode_fields(build_document_message(with_cdn))
    assert (fields[14][0], fields[15][0]) == (b"t" * 32, b"u" * 32)


def test_document_message_caption_omitted_when_absent(doc):
    no_caption = DocumentInfo(**{**doc.__dict__, "caption": None})
    fields = decode_fields(build_document_message(no_caption))
    assert 20 not in fields


def test_document_plaintext_wraps_field_7(doc):
    fields = decode_fields(build_document_plaintext(doc))
    assert 7 in fields


def test_document_plaintext_nested_url(doc):
    outer = decode_fields(build_document_plaintext(doc))
    inner = decode_fields(outer[7][0])
    assert inner[1][0].decode() == doc.url


def test_dsm_wraps_field_31(doc):
    fields = decode_fields(build_document_dsm_plaintext(doc, "33600000000@s.whatsapp.net"))
    assert 31 in fields


def test_dsm_carries_destination_jid(doc):
    outer = decode_fields(build_document_dsm_plaintext(doc, "33600000000@s.whatsapp.net"))
    dsm = decode_fields(outer[31][0])
    assert dsm[1][0].decode() == "33600000000@s.whatsapp.net"


def test_dsm_nested_message_has_document(doc):
    outer = decode_fields(build_document_dsm_plaintext(doc, "33600000000@s.whatsapp.net"))
    dsm = decode_fields(outer[31][0])
    nested_message = decode_fields(dsm[2][0])
    assert 7 in nested_message
