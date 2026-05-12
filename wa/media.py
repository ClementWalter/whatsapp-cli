"""Media downloader for WhatsApp CDN-hosted blobs (history, images, audio).

WhatsApp's media CDN takes a ``directPath`` (relative URL) and a host name
we first fetch via an ``<iq><media_conn/></iq>`` IQ. For our purposes we
cache the host list after the first successful lookup.

Encryption scheme is uniform across media types — AES-CBC with an
HKDF-derived key + IV + HMAC truncated to 10 bytes, keyed per media type
via the HKDF ``info`` string (e.g. ``WhatsApp Image Keys``,
``WhatsApp History Keys``).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import urllib.parse
import urllib.request
from dataclasses import dataclass

from wa.history import decrypt_history_payload  # same scheme

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MediaRef:
    direct_path: str
    media_key: bytes
    file_enc_sha256: bytes
    file_sha256: bytes | None = None


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
