"""On-disk cache of chats, contacts, and messages.

Layout under ``~/.cache/whatsapp-cli/store/``:

- ``chats.json``     — { jid: {name, last_ts, unread, archived} }
- ``contacts.json``  — { jid: push_name }
- ``messages.jsonl`` — append-only newline-delimited JSON, one message per line:
                       {ts, chat, sender, sender_name, text, from_me, msg_id}

Two writers (``history`` ingest and live ``login`` decrypt) coexist by
appending to the JSONL with O_APPEND and rewriting the JSON files
atomically. Order of messages is timestamp on read, not append order.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

_LEGACY_CACHE_ROOT = Path.home() / ".cache" / "whatsapp-user-cli"
_CACHE_ROOT = Path.home() / ".cache" / "whatsapp-cli"
# Pre-publish naming was inconsistent: the skill name was `whatsapp-user-cli`
# while the project, repo, and `pyproject.toml` were already `whatsapp-cli`.
# Existing users have state under the old path — rename in place so we don't
# orphan their pairing keys and cached chats. Idempotent: only runs if the
# old directory exists and the new one doesn't.
if _LEGACY_CACHE_ROOT.exists() and not _CACHE_ROOT.exists():
    _LEGACY_CACHE_ROOT.rename(_CACHE_ROOT)

CACHE_DIR = _CACHE_ROOT / "store"
LOCK_PATH = _CACHE_ROOT / ".connection.lock"
MESSAGES_PATH = CACHE_DIR / "messages.jsonl"
CHATS_PATH = CACHE_DIR / "chats.json"
CONTACTS_PATH = CACHE_DIR / "contacts.json"
LIDMAP_PATH = CACHE_DIR / "lidmap.json"
GROUP_FETCHES_PATH = CACHE_DIR / "group_fetches.json"
SYNC_STATE_PATH = CACHE_DIR / "sync_state.json"


@dataclass
class CachedMessage:
    ts: int
    chat: str
    sender: str
    sender_name: str
    text: str
    from_me: bool
    msg_id: str = ""


def _ensure_dir() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _atomic_write_json(path: Path, data: dict) -> None:
    _ensure_dir()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(path)


def load_chats() -> dict[str, dict]:
    if not CHATS_PATH.exists():
        return {}
    return json.loads(CHATS_PATH.read_text())


def load_contacts() -> dict[str, str]:
    if not CONTACTS_PATH.exists():
        return {}
    return json.loads(CONTACTS_PATH.read_text())


def save_chats(chats: dict[str, dict]) -> None:
    _atomic_write_json(CHATS_PATH, chats)


def save_contacts(contacts: dict[str, str]) -> None:
    _atomic_write_json(CONTACTS_PATH, contacts)


def load_lidmap() -> dict[str, str]:
    """``lid_jid_local_part → pn_jid`` (e.g. ``42997622276200`` → ``33687776779@s.whatsapp.net``).

    The local part of the LID is what appears as the "sender" of group
    messages in our cached messages.jsonl, so we key by that for fast
    lookup during ``read``.
    """
    if not LIDMAP_PATH.exists():
        return {}
    return json.loads(LIDMAP_PATH.read_text())


def save_lidmap(lidmap: dict[str, str]) -> None:
    _atomic_write_json(LIDMAP_PATH, lidmap)


def load_group_fetches() -> dict[str, int]:
    """``group_jid → unix_ts`` of the last successful participant-list fetch.

    Lets the login post-success step skip groups it recently queried,
    cutting wall-clock time on warm reconnects from ~38s to ~0s.
    """
    if not GROUP_FETCHES_PATH.exists():
        return {}
    return json.loads(GROUP_FETCHES_PATH.read_text())


def save_group_fetches(fetches: dict[str, int]) -> None:
    _atomic_write_json(GROUP_FETCHES_PATH, fetches)


def load_sync_state() -> dict:
    """Last-sync bookkeeping: ``{last_sync_ts: int, last_frames: int}``."""
    if not SYNC_STATE_PATH.exists():
        return {}
    return json.loads(SYNC_STATE_PATH.read_text())


def save_sync_state(state: dict) -> None:
    _atomic_write_json(SYNC_STATE_PATH, state)


class connection_lock:
    """File-lock around the one WebSocket session this CLI holds.

    WhatsApp's server treats each linked-device JID as having a single
    live connection; if two `wa` invocations connect at once, the older
    one is silently kicked. Worse, both sides write to ``signal.json``
    on session updates, so a racing pair can leave the ratchet state
    torn. Wrap every command that opens a FrameSocket in this CM:
    ``with connection_lock():`` blocks if another process holds it (or
    fails fast with ``blocking=False``).
    """

    def __init__(self, *, blocking: bool = True, stale_seconds: float = 300.0) -> None:
        self._blocking = blocking
        self._stale_seconds = stale_seconds
        self._fh = None

    def __enter__(self) -> "connection_lock":
        import fcntl
        import time as _time

        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        # A pre-existing lock file with a stale mtime is fine to reuse;
        # what matters is whether `flock` grants us the advisory lock.
        if LOCK_PATH.exists():
            age = _time.time() - LOCK_PATH.stat().st_mtime
            if age > self._stale_seconds:
                log.debug("lock file is %.0fs old; will reacquire if owner is gone", age)
        self._fh = open(LOCK_PATH, "w")
        flags = fcntl.LOCK_EX
        if not self._blocking:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(self._fh.fileno(), flags)
        except BlockingIOError:
            self._fh.close()
            self._fh = None
            raise RuntimeError(
                f"another `wa` process holds the connection lock ({LOCK_PATH}). "
                f"Wait for it to finish or kill the other process."
            )
        self._fh.write(f"{os.getpid()}\n")
        self._fh.flush()
        return self

    def __exit__(self, *exc) -> None:
        import fcntl

        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            self._fh.close()
            self._fh = None


def canonical_jid(
    jid: str, *, lidmap_inverted: dict[str, str] | None = None
) -> str:
    """Map a peer JID to the canonical form used across the cache.

    WhatsApp's post-2024 identity rollout routes most DMs through ``@lid``
    privacy identifiers instead of ``@s.whatsapp.net`` phone-number JIDs,
    so the same conversation can arrive under both. We prefer ``@lid`` as
    canonical: it's what the live decrypt path receives today, and it
    matches what WhatsApp Web stores. Two cases get folded:

    - Polluted: 14+-digit ``@s.whatsapp.net`` locals are LIDs that the
      old JID-server bug stored in the wrong namespace.
    - Genuine PN/LID twins: a phone-number JID whose LID counterpart is
      known via the inverted ``lidmap``.

    Groups, already-@lid, and PN entries with no known LID twin are
    returned unchanged. Passing ``lidmap_inverted`` avoids re-reading
    ``lidmap.json`` on every call when batching (e.g. in ``migrate``).
    """
    if not jid.endswith("@s.whatsapp.net"):
        return jid
    local = jid.split("@")[0]
    if local.isdigit() and len(local) > 13:
        return f"{local}@lid"
    if lidmap_inverted is None:
        lidmap_inverted = {pn: lid for lid, pn in load_lidmap().items()}
    if (lid := lidmap_inverted.get(jid)):
        return f"{lid}@lid"
    return jid


def append_messages(messages: list[CachedMessage]) -> None:
    """Append a batch to ``messages.jsonl``. Existing duplicates are not
    deduplicated here — the caller is expected to skip msg_ids already seen.
    """
    if not messages:
        return
    _ensure_dir()
    with MESSAGES_PATH.open("a", encoding="utf-8") as f:
        for m in messages:
            f.write(json.dumps(asdict(m), ensure_ascii=False) + "\n")


def iter_messages() -> Iterator[CachedMessage]:
    if not MESSAGES_PATH.exists():
        return iter(())
    with MESSAGES_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield CachedMessage(**json.loads(line))
            except Exception as e:
                log.warning("skipping malformed message line: %s", e)


def known_message_ids() -> set[str]:
    """For deduplication when re-ingesting history blobs across runs."""
    seen: set[str] = set()
    for m in iter_messages():
        if m.msg_id:
            seen.add(m.msg_id)
    return seen


def upsert_chat(jid: str, name: str = "", last_ts: int = 0) -> None:
    """Add or update a chat entry. Doesn't clobber existing ``name`` with empty."""
    chats = load_chats()
    cur = chats.get(jid, {})
    if name:
        cur["name"] = name
    if last_ts and last_ts > cur.get("last_ts", 0):
        cur["last_ts"] = last_ts
    cur.setdefault("name", "")
    cur.setdefault("last_ts", 0)
    chats[jid] = cur
    save_chats(chats)


def find_chat(query: str) -> list[tuple[str, dict]]:
    """Return chats whose JID, app-state name, or contact name contains ``query``.

    DMs have no app-state name (WhatsApp doesn't sync address-book labels
    to linked devices), so we also match against ``contacts.json`` — that's
    where ``import-contacts`` and pushname syncs land. Sorted by last_ts
    descending so the most active match comes first.
    """
    q = query.lower()
    chats = load_chats()
    contacts = load_contacts()
    matches = []
    for jid, info in chats.items():
        contact_name = contacts.get(jid, "")
        haystack = (jid + " " + (info.get("name") or "") + " " + contact_name).lower()
        if q in haystack:
            matches.append((jid, info))
    matches.sort(key=lambda kv: kv[1].get("last_ts", 0), reverse=True)
    return matches
