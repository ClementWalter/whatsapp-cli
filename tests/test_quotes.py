"""Quoted-reply ContextInfo: encoding a reply and reading the quoted id back."""

from __future__ import annotations

from wa.messages import build_reply_plaintext, extract_quoted_id, wrap_device_sent
from wa.pbutil import decode_fields
from wa.peerreq import _string_field

QUOTED_ID = "3EB0074119DBF1235E05E4"


def reply() -> bytes:
    return build_reply_plaintext("oui vas-y", QUOTED_ID, "33612345678@s.whatsapp.net", "🎙️ rapport")


def test_reply_is_extended_text_message():
    assert list(decode_fields(reply())) == [6]


def test_reply_keeps_text_at_field_one():
    extended = decode_fields(decode_fields(reply())[6][0])
    assert extended[1][0].decode() == "oui vas-y"


def test_extract_quoted_id_from_reply():
    assert extract_quoted_id(reply()) == QUOTED_ID


def test_extract_quoted_id_through_device_sent_wrapper():
    assert extract_quoted_id(wrap_device_sent(reply(), "10000000000000@lid")) == QUOTED_ID


def test_plain_conversation_is_not_a_reply():
    assert extract_quoted_id(_string_field(1, "hello")) == ""


def test_extended_text_without_context_is_not_a_reply():
    from wa.peerreq import _length_delim

    assert extract_quoted_id(_length_delim(6, _string_field(1, "link-preview text"))) == ""


def test_garbage_is_not_a_reply():
    assert extract_quoted_id(b"\xff\xff\xff") == ""


def test_context_participant_is_bare_jid():
    extended = decode_fields(decode_fields(reply())[6][0])
    context = decode_fields(extended[17][0])
    assert context[2][0].decode() == "33612345678@s.whatsapp.net"


def test_quoted_ref_strips_device_suffix():
    from wa.messages import QuotedRef

    assert QuotedRef.from_cached("id", "33612345678:25@s.whatsapp.net", "t").participant == "33612345678@s.whatsapp.net"
