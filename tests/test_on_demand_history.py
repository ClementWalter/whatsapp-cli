"""On-demand history: paging, reply matching, coverage notes and receipts.

The phone answers a ``HISTORY_SYNC_ON_DEMAND`` request with one CDN-hosted
``HistorySyncNotification`` per request, echoing the request id. These
tests pin the pieces that turn that into a paged walk back through a chat
and a loud note when the walk falls short, without opening a socket.
"""

from __future__ import annotations

import asyncio

import pytest

from wa import cli
from wa.cache import CachedMessage
from wa.history import decode_history_sync_notification, is_history_sync_notification
from wa.peerreq import _length_delim, _string_field, _varint_field
from wa.session import InboundSession
from wa.wabinary import JID, Node, decode_node
from wa.wabinary.encoder import encode_node


def _hsn(request_id: str = "", sync_type: int = 6) -> bytes:
    """HistorySyncNotification: syncType (6) and peerDataRequestSessionID (12)."""
    body = _varint_field(6, sync_type)
    if request_id:
        body += _string_field(12, request_id)
    return body


def _protocol_message(hsn: bytes | None) -> bytes:
    """ProtocolMessage bytes, as saved in a ``*-protocolMessage-*.bin`` blob."""
    body = _varint_field(2, 5)
    if hsn is not None:
        body += _length_delim(6, hsn)
    return body


def _message(protocol: bytes) -> bytes:
    """Top-level Message carrying ``protocolMessage`` (field 12)."""
    return _length_delim(12, protocol)


@pytest.fixture
def blob_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_blob_dir", lambda: tmp_path)
    return tmp_path


def _save_blob(blob_dir, ms: int, payload: bytes):
    path = blob_dir / f"{ms}-protocolMessage-10000000000000-0-0-{len(payload)}.bin"
    path.write_bytes(payload)
    return path


# --- notification parsing ---------------------------------------------------


def test_history_sync_notification_is_detected():
    assert is_history_sync_notification(_message(_protocol_message(_hsn())))


def test_protocol_message_without_notification_is_not_history():
    assert not is_history_sync_notification(_message(_protocol_message(None)))


def test_text_message_is_not_history():
    assert not is_history_sync_notification(_string_field(1, "hello"))


def test_notification_exposes_request_id():
    assert decode_history_sync_notification(_hsn("3EB0ABC"))["request_id"] == b"3EB0ABC"


# --- reply matching ---------------------------------------------------------


def test_reply_blob_matches_its_request_id(blob_dir):
    path = _save_blob(blob_dir, 1000, _protocol_message(_hsn("3EB0AAA")))
    assert cli._reply_blobs([path], {"3EB0AAA"}) == [path]


def test_late_reply_to_an_earlier_request_matches(blob_dir):
    path = _save_blob(blob_dir, 1000, _protocol_message(_hsn("3EB0OLD")))
    assert cli._reply_blobs([path], {"3EB0OLD", "3EB0NEW"}) == [path]


def test_reply_blob_for_another_request_is_ignored(blob_dir):
    path = _save_blob(blob_dir, 1000, _protocol_message(_hsn("3EB0BBB")))
    assert cli._reply_blobs([path], {"3EB0AAA"}) == []


def test_blobs_saved_before_the_cutoff_are_skipped(blob_dir):
    _save_blob(blob_dir, 1000, _protocol_message(_hsn("old")))
    new = _save_blob(blob_dir, 2000, _protocol_message(_hsn("new")))
    assert cli._history_blobs_since(1500) == [new]


def test_new_history_ingest_skips_non_history_blobs(blob_dir, monkeypatch):
    _save_blob(blob_dir, 1000, _protocol_message(None))
    seen: list = []
    monkeypatch.setattr(
        cli, "_ingest_history_blobs", lambda files, download: seen.extend(files)
    )
    cli._ingest_new_history(0)
    assert seen == []


def test_new_history_ingest_downloads_cdn_chunks(blob_dir, monkeypatch):
    _save_blob(blob_dir, 1000, _protocol_message(_hsn("3EB0AAA")))
    calls: list = []
    monkeypatch.setattr(
        cli,
        "_ingest_history_blobs",
        lambda files, download: calls.append(download) or 0,
    )
    cli._ingest_new_history(0)
    assert calls == [True]


def test_new_history_ingest_skips_already_ingested_replies(blob_dir, monkeypatch):
    path = _save_blob(blob_dir, 1000, _protocol_message(_hsn("3EB0AAA")))
    seen: list = []
    monkeypatch.setattr(
        cli, "_ingest_history_blobs", lambda files, download: seen.extend(files)
    )
    cli._ingest_new_history(0, skip={path})
    assert seen == []


# --- paging -----------------------------------------------------------------


def _msg(ts: int) -> CachedMessage:
    return CachedMessage(
        ts=ts,
        chat="c@lid",
        sender="c@lid",
        sender_name="",
        text="",
        from_me=False,
        msg_id=f"m{ts}",
    )


class StubPhone:
    """A chat whose phone-side history is ``history`` (newest last).

    The cache starts with the newest ``cached`` messages; each request
    moves up to ``n`` older ones into the cache, like an ON_DEMAND reply.
    ``silent`` makes the phone never answer.
    """

    def __init__(self, history: list[int], cached: int, silent: bool = False) -> None:
        self.phone_only = (
            [_msg(ts) for ts in history[:-cached]]
            if cached
            else [_msg(t) for t in history]
        )
        self.cache = [_msg(ts) for ts in history[-cached:]] if cached else []
        self.silent = silent
        self.requests: list[tuple[int, int]] = []

    def oldest(self) -> CachedMessage | None:
        return min(self.cache, key=lambda m: m.ts) if self.cache else None

    async def request(self, anchor: CachedMessage, n: int) -> int | None:
        self.requests.append((anchor.ts, n))
        if self.silent:
            return None
        older = [m for m in self.phone_only if m.ts < anchor.ts][-n:]
        self.phone_only = [m for m in self.phone_only if m not in older]
        self.cache.extend(older)
        return len(older)


def _walk(phone: StubPhone, count: int, since_ts: int | None = None) -> cli.PageResult:
    return asyncio.run(
        cli._page_on_demand(
            oldest=phone.oldest, request=phone.request, count=count, since_ts=since_ts
        )
    )


def test_paging_asks_at_most_one_page_per_request():
    phone = StubPhone(list(range(1, 201)), cached=10)
    _walk(phone, 120)
    assert [n for _, n in phone.requests] == [50, 50, 20]


def test_paging_reanchors_on_the_oldest_stored_message():
    phone = StubPhone(list(range(1, 201)), cached=10)
    _walk(phone, 120)
    assert [ts for ts, _ in phone.requests] == [191, 141, 91]


def test_paging_gains_what_was_asked():
    phone = StubPhone(list(range(1, 201)), cached=10)
    assert _walk(phone, 120).gained == 120


@pytest.mark.parametrize(
    ("phone", "count", "since_ts", "stop"),
    [
        (StubPhone(list(range(1, 201)), cached=10), 120, None, "count"),
        (StubPhone(list(range(1, 31)), cached=10), 120, None, "no_older"),
        (StubPhone(list(range(1, 201)), cached=10, silent=True), 120, None, "no_reply"),
        (StubPhone(list(range(1, 201)), cached=10), 5000, 100, "since"),
        (StubPhone(list(range(1, 201)), cached=0), 120, None, "no_anchor"),
    ],
    ids=["count", "start-of-chat", "silent-phone", "since", "empty-cache"],
)
def test_paging_reports_why_it_stopped(phone, count, since_ts, stop):
    assert _walk(phone, count, since_ts).stop == stop


def test_paging_to_a_date_stops_once_the_cache_reaches_it():
    phone = StubPhone(list(range(1, 201)), cached=10)
    _walk(phone, 5000, since_ts=100)
    assert phone.oldest().ts == 91


# --- coverage ---------------------------------------------------------------


def _coverage(**overrides) -> dict:
    kwargs = {
        "cached": 15,
        "oldest_ts": 1772565338,
        "limit": 500,
        "since_ts": None,
        "stop": "no_reply",
    }
    kwargs.update(overrides)
    return cli._coverage(**kwargs)


def test_short_cache_note_names_the_oldest_cached_date():
    assert cli._date(1772565338) in _coverage()["note"]


def test_short_cache_note_says_the_phone_did_not_answer():
    assert "did not answer" in _coverage()["note"]


def test_short_cache_note_is_flagged_partial():
    assert _coverage()["note"].startswith("PARTIAL")


def test_full_page_has_no_note():
    assert _coverage(cached=600, stop=None)["note"] is None


def test_cache_older_than_since_has_no_note():
    assert (
        _coverage(limit=0, since_ts=1700000000, oldest_ts=1600000000, stop="since")[
            "note"
        ]
        is None
    )


def test_cache_newer_than_since_has_a_note():
    assert _coverage(limit=0, since_ts=1600000000, stop="no_reply")["note"] is not None


def test_start_of_chat_on_phone_is_complete():
    assert _coverage(stop="no_older")["complete"] is True


def test_start_of_chat_on_phone_has_no_note():
    assert _coverage(stop="no_older")["note"] is None


# --- receipts ---------------------------------------------------------------


class _IdentityNS:
    def encrypt_frame(self, b):
        return b

    def decrypt_frame(self, b):
        return b


class _FakeDevice:
    jid = "33123456789:23@s.whatsapp.net"
    lid = "12345:23@lid"


class _FakeFS:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    async def send(self, frame: bytes) -> None:
        self.sent.append(frame)


def _receipt_types(node: Node, on_message_result: list[str]) -> list[str]:
    async def _inner():
        fs = _FakeFS()
        session = InboundSession(
            fs, _IdentityNS(), _FakeDevice(), on_message=lambda n: on_message_result
        )
        await session.handle(node)
        return [
            n.attrs.get("type")
            for n in (decode_node(f) for f in fs.sent)
            if n.tag == "receipt"
        ]

    return asyncio.run(_inner())


def _from_own_phone(category: str | None = None) -> Node:
    attrs = {"id": "3A1", "from": JID.parse("12345@lid"), "t": "1"}
    if category:
        attrs["category"] = category
    return Node(tag="message", attrs=attrs)


def test_peer_message_from_own_phone_gets_peer_msg_receipt():
    assert "peer_msg" in _receipt_types(_from_own_phone("peer"), [])


def test_history_notification_gets_hist_sync_receipt():
    assert "hist_sync" in _receipt_types(_from_own_phone("peer"), ["hist_sync"])


def test_contact_message_gets_no_protocol_receipt():
    node = Node(
        tag="message", attrs={"id": "3A2", "from": JID.parse("999@lid"), "t": "1"}
    )
    assert _receipt_types(node, ["hist_sync"]) == [None]


def test_protocol_receipt_goes_to_own_phone_number():
    async def _inner():
        fs = _FakeFS()
        session = InboundSession(
            fs, _IdentityNS(), _FakeDevice(), on_message=lambda n: []
        )
        await session.handle(_from_own_phone("peer"))
        sent = [decode_node(f) for f in fs.sent]
        return next(n for n in sent if n.attrs.get("type") == "peer_msg").attrs["to"]

    assert str(asyncio.run(_inner())) == "33123456789@s.whatsapp.net"


def test_encode_roundtrip_keeps_category():
    # The session reads `category` off the decoded stanza.
    node = _from_own_phone("peer")
    assert decode_node(encode_node(node)).attrs["category"] == "peer"


# --- chat JID forms ---------------------------------------------------------

LID_CHAT = "100000000000001@lid"
PN_CHAT = "33600000000@s.whatsapp.net"


@pytest.mark.parametrize(
    ("chat", "lidmap", "expected"),
    [
        (LID_CHAT, {"100000000000001": PN_CHAT}, [PN_CHAT, LID_CHAT]),
        (LID_CHAT, {}, [LID_CHAT]),
        (
            "120363000000000000@g.us",
            {"100000000000001": PN_CHAT},
            ["120363000000000000@g.us"],
        ),
    ],
    ids=["lid-with-known-number", "lid-without-number", "group"],
)
def test_on_demand_chat_jid_forms(chat, lidmap, expected):
    assert cli._on_demand_chat_jids(chat, lidmap) == expected


@pytest.fixture
def store(tmp_path, monkeypatch):
    """An isolated cache store whose lidmap knows LID_CHAT's phone number."""
    import json

    from wa import cache

    store_dir = tmp_path / "store"
    store_dir.mkdir()
    for name, path in {
        "CACHE_DIR": store_dir,
        "MESSAGES_PATH": store_dir / "messages.jsonl",
        "CHATS_PATH": store_dir / "chats.json",
        "CONTACTS_PATH": store_dir / "contacts.json",
        "LIDMAP_PATH": store_dir / "lidmap.json",
    }.items():
        monkeypatch.setattr(cache, name, path)
    (store_dir / "lidmap.json").write_text(json.dumps({"100000000000001": PN_CHAT}))
    return cache


@pytest.fixture
def phone_number_history(monkeypatch):
    """A parsed ON_DEMAND chunk whose single conversation is filed under PN_CHAT."""
    from wa import history
    from wa.history import HistoryMessage

    monkeypatch.setattr(
        history,
        "parse_history_sync",
        lambda payload: {"pushnames": [], "lid_to_pn": [], "conversations": [b"conv"]},
    )
    monkeypatch.setattr(
        history, "conversation_name", lambda conv: (PN_CHAT, "", 1663300000)
    )
    monkeypatch.setattr(
        history,
        "iter_conversation_messages",
        lambda conv: [
            HistoryMessage(
                chat_jid=PN_CHAT,
                sender_jid=PN_CHAT,
                sender_name="",
                timestamp=1663300000,
                text="pitch deck advice",
                from_me=False,
                msg_id="3AOLD",
            )
        ],
    )


def test_ingested_phone_number_history_is_filed_under_the_lid_chat(
    blob_dir, store, phone_number_history
):
    inline = _varint_field(6, 6) + _length_delim(11, b"payload")
    path = _save_blob(blob_dir, 1000, _protocol_message(inline))
    cli._ingest_history_blobs([path], download=False)
    assert [m.chat for m in store.iter_messages()] == [LID_CHAT]
