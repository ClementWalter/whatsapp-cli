"""Media references out of a Message, and the download-side decrypt."""

from __future__ import annotations

import base64

from wa.media import decrypt_media, encrypt_media, media_extension
from wa.messages import extract_media, wrap_device_sent
from wa.peerreq import _length_delim, _string_field, _varint_field


def audio_message(url: str = "https://mmg.whatsapp.net/v/t62.7117-24/abc.enc") -> bytes:
    """Message{audioMessage(8){url, mimetype, sha256, length, seconds, ptt, mediaKey, encSha256, directPath}}."""
    body = (
        _string_field(1, url)
        + _string_field(2, "audio/ogg; codecs=opus")
        + _length_delim(3, b"s" * 32)
        + _varint_field(4, 12345)
        + _varint_field(5, 7)
        + _varint_field(6, 1)
        + _length_delim(7, b"k" * 32)
        + _length_delim(8, b"e" * 32)
        + _string_field(9, "/v/t62.7117-24/abc.enc")
    )
    return _length_delim(8, body)


def test_audio_message_yields_audio_kind():
    assert extract_media(audio_message())["kind"] == "audio"


def test_audio_media_key_is_base64():
    assert base64.b64decode(extract_media(audio_message())["media_key"]) == b"k" * 32


def test_audio_seconds_and_ptt_are_read():
    ref = extract_media(audio_message())
    assert (ref["seconds"], ref["ptt"]) == (7, 1)


def test_audio_through_device_sent_wrapper():
    assert extract_media(wrap_device_sent(audio_message(), "77253627113618@lid"))["direct_path"] == "/v/t62.7117-24/abc.enc"


def test_plain_text_has_no_media():
    assert extract_media(_string_field(1, "hello")) is None


def test_extract_text_labels_voice_note_as_audio():
    from wa.cli import _extract_text

    assert _extract_text(audio_message()) == "[audio]"


def test_decrypt_reverses_encrypt():
    enc = encrypt_media(b"opus bytes " * 100, "audio")
    assert decrypt_media(enc.media_key, enc.ciphertext, "audio", enc.file_enc_sha256) == b"opus bytes " * 100


def test_decrypt_rejects_tampered_blob():
    import pytest

    enc = encrypt_media(b"opus bytes", "audio")
    with pytest.raises(ValueError):
        decrypt_media(enc.media_key, enc.ciphertext[:-1] + b"\x00", "audio")


def test_opus_extension():
    assert media_extension({"mimetype": "audio/ogg; codecs=opus"}) == ".ogg"


def test_device_sent_destination_names_real_chat():
    from wa.messages import device_sent_destination

    assert device_sent_destination(wrap_device_sent(_string_field(1, "hi"), "33612345678@s.whatsapp.net")) == "33612345678@s.whatsapp.net"


def test_incoming_message_has_no_device_sent_destination():
    from wa.messages import device_sent_destination

    assert device_sent_destination(_string_field(1, "hi")) == ""
