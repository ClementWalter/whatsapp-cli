"""Voice-note transcripts, produced by the `transcribe` CLI and cached per message.

`wa read` hands each audio message it shows to `transcribe file --json`
(transcribe-cli, local Whisper / parakeet) and stores the result in
``store/transcripts.json`` keyed by ``msg_id``, so a note is transcribed once
and every later read is instant. transcribe-cli is an external program found
on ``PATH`` or through ``WA_TRANSCRIBE_BIN``; without it, audio stays ``[audio]``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Iterable

from wa.cache import CACHE_DIR, CachedMessage

log = logging.getLogger(__name__)

TRANSCRIPTS_PATH = CACHE_DIR / "transcripts.json"
# The first run downloads the speech model (about 1.5 GB), so one note can take minutes.
TRANSCRIBE_TIMEOUT = 900


def is_audio(media: dict | None) -> bool:
    """A voice-note bubble, or an audio file shared as a document."""
    if not media:
        return False
    if media.get("kind") == "audio":
        return True
    return media.get("kind") == "document" and (media.get("mimetype") or "").startswith("audio/")


def transcribe_bin() -> str | None:
    """Path of the `transcribe` executable, or None when transcribe-cli is not installed."""
    return os.environ.get("WA_TRANSCRIBE_BIN") or shutil.which("transcribe")


def load_transcripts(path: Path | None = None) -> dict[str, dict]:
    path = path or TRANSCRIPTS_PATH
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _save(path: Path, store: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, indent=2, ensure_ascii=False))
    tmp.replace(path)


def _transcribe_one(
    msg: CachedMessage,
    binary: str,
    download: Callable[[dict], bytes],
    run: Callable[..., subprocess.CompletedProcess],
) -> dict | None:
    """Download one note to a temp file and run `transcribe file` on it; None on failure."""
    from wa.media import media_extension

    with tempfile.TemporaryDirectory(prefix="wa-transcribe-") as tmp:
        audio = Path(tmp) / f"{msg.msg_id}{media_extension(msg.media)}"
        audio.write_bytes(download(msg.media))
        result = run(
            [binary, "file", str(audio), "--json"],
            capture_output=True,
            text=True,
            timeout=TRANSCRIBE_TIMEOUT,
        )
    if result.returncode != 0:
        log.warning("transcribe failed for %s: %s", msg.msg_id, result.stderr.strip()[-300:])
        return None
    out = json.loads(result.stdout)
    return {"text": out["text"].strip(), "language": out.get("language")}


def transcribe_messages(
    messages: Iterable[CachedMessage],
    *,
    binary: str | None,
    download: Callable[[dict], bytes] | None = None,
    run: Callable[..., subprocess.CompletedProcess] | None = None,
    path: Path | None = None,
) -> dict[str, dict]:
    """Transcripts of the audio among ``messages``, transcribing the ones not cached yet.

    A failed note is logged and left out, so the next read retries it. The
    store is written after every success, so an interrupted run keeps its work.
    """
    path = path or TRANSCRIPTS_PATH
    run = run or subprocess.run
    store = load_transcripts(path)
    audio = [m for m in messages if m.msg_id and is_audio(m.media)]
    if binary is not None:
        if download is None:
            from wa.media import download_media as download
        for msg in audio:
            if msg.msg_id in store:
                continue
            try:
                transcript = _transcribe_one(msg, binary, download, run)
            except Exception as exc:  # CDN expiry, decode error, timeout: skip this note only
                log.warning("cannot transcribe %s: %s", msg.msg_id, exc)
                continue
            if transcript is not None:
                store[msg.msg_id] = transcript
                _save(path, store)
    return {m.msg_id: store[m.msg_id] for m in audio if m.msg_id in store}
