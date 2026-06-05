"""Media transfer for WhatsApp CDN-hosted blobs (history, documents, images).

WhatsApp's media CDN takes a ``directPath`` (relative URL) and a host name
we first fetch via an ``<iq><media_conn/></iq>`` IQ. For our purposes we
cache the host list after the first successful lookup.

Encryption scheme is uniform across media types — AES-CBC with an
HKDF-derived key + IV + HMAC truncated to 10 bytes, keyed per media type
via the HKDF ``info`` string (e.g. ``WhatsApp Image Keys``,
``WhatsApp Document Keys``, ``WhatsApp History Keys``).

Outbound media is the mirror image: pick a random 32-byte ``mediaKey``,
derive the same iv/cipher/mac material, AES-CBC encrypt the padded
plaintext, append the truncated HMAC, then POST the resulting blob to a
``media_conn`` host. The CDN replies with a ``url`` + ``directPath`` that,
together with the mediaKey and the two SHA-256 digests, fully describe the
``DocumentMessage`` we attach to the encrypted stanza.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import urllib.parse
import urllib.request
from dataclasses import dataclass

from wa.history import _hkdf_expand  # shared HKDF-SHA256 (empty salt)
from wa.history import decrypt_history_payload  # same scheme

log = logging.getLogger(__name__)

# HKDF ``info`` strings, keyed by the CDN ``mms`` path segment. The path
# segment doubles as the media-type label everywhere it's needed.
MEDIA_HKDF_INFO = {
    "document": b"WhatsApp Document Keys",
    "image": b"WhatsApp Image Keys",
    "video": b"WhatsApp Video Keys",
    "audio": b"WhatsApp Audio Keys",
}


@dataclass(frozen=True)
class MediaRef:
    direct_path: str
    media_key: bytes
    file_enc_sha256: bytes
    file_sha256: bytes | None = None


@dataclass(frozen=True)
class EncryptedMedia:
    """Everything needed to upload a blob and describe it in a proto."""

    ciphertext: bytes  # enc || HMAC[:10] — the bytes POSTed to the CDN
    media_key: bytes
    file_sha256: bytes  # sha256(plaintext)
    file_enc_sha256: bytes  # sha256(ciphertext) — also the upload token
    file_length: int  # len(plaintext)


def _aes_cbc_encrypt(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
    """AES-256-CBC with PKCS#7 padding (inverse of history._aes_cbc_decrypt)."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    pad = 16 - (len(plaintext) % 16)
    padded = plaintext + bytes([pad]) * pad
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def encrypt_media(plaintext: bytes, media_type: str = "document") -> EncryptedMedia:
    """Encrypt a file for upload using WhatsApp's standard media scheme.

    HKDF(mediaKey, info=<media-type keys>, 112) → iv|cipherKey|macKey|refKey.
    Ciphertext is ``AES-CBC(cipherKey, iv, pkcs7(plaintext))`` followed by
    ``HMAC-SHA256(macKey, iv || enc)[:10]`` — exactly what the download path
    in :func:`decrypt_history_payload` reverses.
    """
    import secrets

    info = MEDIA_HKDF_INFO.get(media_type)
    if info is None:
        raise ValueError(f"unknown media type {media_type!r}")
    media_key = secrets.token_bytes(32)
    expanded = _hkdf_expand(media_key, info, 112)
    iv, cipher_key, mac_key = expanded[:16], expanded[16:48], expanded[48:80]
    enc = _aes_cbc_encrypt(cipher_key, iv, plaintext)
    mac = hmac.new(mac_key, iv + enc, hashlib.sha256).digest()[:10]
    ciphertext = enc + mac
    return EncryptedMedia(
        ciphertext=ciphertext,
        media_key=media_key,
        file_sha256=hashlib.sha256(plaintext).digest(),
        file_enc_sha256=hashlib.sha256(ciphertext).digest(),
        file_length=len(plaintext),
    )


def upload_media(
    enc: EncryptedMedia,
    hosts: list[str],
    auth: str,
    media_type: str = "document",
) -> tuple[str, str]:
    """POST an encrypted blob to a ``media_conn`` host; return (url, directPath).

    Mirrors whatsmeow's ``Upload``: the upload token is the URL-safe base64
    of ``fileEncSHA256``, and the path is ``/mms/<media-type>/<token>`` with
    ``auth`` + ``token`` query params. Hosts are tried in order; the first
    that returns a JSON body with a ``url`` wins.
    """
    token = base64.urlsafe_b64encode(enc.file_enc_sha256).decode()
    query = urllib.parse.urlencode({"auth": auth, "token": token})
    last_err: Exception | None = None
    for host in hosts:
        url = f"https://{host}/mms/{media_type}/{token}?{query}"
        req = urllib.request.Request(
            url,
            data=enc.ciphertext,
            method="POST",
            headers={
                "Origin": "https://web.whatsapp.com",
                "Referer": "https://web.whatsapp.com/",
                "User-Agent": "Mozilla/5.0",
                "Content-Type": "application/octet-stream",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read())
        except Exception as e:  # try the next host
            log.warning("upload to %s failed: %s", host, e)
            last_err = e
            continue
        direct_path = body.get("direct_path") or body.get("directPath")
        if body.get("url") and direct_path:
            log.info("uploaded %d bytes to %s", len(enc.ciphertext), host)
            return body["url"], direct_path
        last_err = ValueError(f"unexpected upload response: {body}")
    raise RuntimeError(f"all media hosts failed: {last_err}")


def download_and_decrypt_history(ref: MediaRef, host: str = "mmg.whatsapp.net") -> bytes:
    """Fetch an encrypted history blob from the WA CDN and decrypt it.

    Returns zlib-compressed plaintext ready for :py:func:`history.parse_history_sync`.
    """
    if not ref.direct_path.startswith("/"):
        raise ValueError(f"directPath must start with /: {ref.direct_path!r}")
    hash_url = urllib.parse.quote(
        __import__("base64").urlsafe_b64encode(ref.file_enc_sha256).decode().rstrip("=")
    )
    url = f"https://{host}{ref.direct_path}&hash={hash_url}&mms-type=md-msg-hist&__wa-mms="
    log.debug("fetching history blob from %s", url)
    req = urllib.request.Request(
        url,
        headers={
            "Origin": "https://web.whatsapp.com",
            "Referer": "https://web.whatsapp.com/",
            "User-Agent": "Mozilla/5.0",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    return decrypt_history_payload(ref.media_key, data, ref.file_enc_sha256)
