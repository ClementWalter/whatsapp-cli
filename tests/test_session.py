"""InboundSession demux — messages must survive IQ/ack waits.

These tests pin the contract that used to be broken: a waiter sitting
on `fs.recv` for usync / media_conn / active / send-ack used to drop
the offline mailbox. One assertion per test.
"""

from __future__ import annotations

import asyncio

from wa.session import InboundSession
from wa.wabinary import JID, Node, decode_node
from wa.wabinary.encoder import encode_node


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
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.sent: list[bytes] = []

    async def send(self, frame: bytes) -> None:
        self.sent.append(frame)

    async def recv(self, timeout: float | None = None) -> bytes:
        frame = await asyncio.wait_for(self.incoming.get(), timeout)
        if frame is None:
            raise ConnectionError("socket closed")
        return frame

    def push(self, node: Node) -> None:
        self.incoming.put_nowait(encode_node(node))

    def close(self) -> None:
        self.incoming.put_nowait(None)

    def sent_nodes(self) -> list[Node]:
        return [decode_node(f) for f in self.sent]


def _peer_message(msg_id: str = "m1") -> Node:
    return Node(
        tag="message",
        attrs={
            "id": msg_id,
            "from": JID.parse("999@lid"),
            "t": "1",
        },
    )


def _run(coro):
    return asyncio.run(coro)


def test_message_during_iq_wait_is_acked():
    async def _inner():
        fs = _FakeFS()
        session = InboundSession(fs, _IdentityNS(), _FakeDevice())
        session.start()
        iq = Node(
            tag="iq",
            attrs={"id": "q1", "type": "get", "to": JID(server="s.whatsapp.net")},
        )

        async def _peer_then_reply() -> None:
            await asyncio.sleep(0.02)
            fs.push(_peer_message("m1"))
            fs.push(Node(tag="iq", attrs={"id": "q1", "type": "result"}))

        asyncio.create_task(_peer_then_reply())
        await session.request_iq(iq, timeout=1.0)
        await asyncio.sleep(0.05)
        tags = [n.tag for n in fs.sent_nodes()]
        await session.stop()
        return tags

    assert "ack" in _run(_inner())


def test_request_iq_returns_the_matching_result():
    async def _inner():
        fs = _FakeFS()
        session = InboundSession(fs, _IdentityNS(), _FakeDevice())
        session.start()
        iq = Node(
            tag="iq",
            attrs={"id": "q1", "type": "get", "to": JID(server="s.whatsapp.net")},
        )

        async def _peer_then_reply() -> None:
            await asyncio.sleep(0.02)
            fs.push(_peer_message("m1"))
            fs.push(Node(tag="iq", attrs={"id": "q2", "type": "result"}))
            fs.push(Node(tag="iq", attrs={"id": "q1", "type": "result"}))

        asyncio.create_task(_peer_then_reply())
        node = await session.request_iq(iq, timeout=1.0)
        await session.stop()
        return node.attrs.get("id")

    assert _run(_inner()) == "q1"


def test_on_message_runs_before_ack():
    async def _inner():
        seen: list[str] = []
        fs = _FakeFS()
        session = InboundSession(
            fs,
            _IdentityNS(),
            _FakeDevice(),
            on_message=lambda n: seen.append(n.attrs.get("id", "")),
        )
        session.start()
        fs.push(_peer_message("m42"))
        await asyncio.sleep(0.05)
        await session.stop()
        return seen

    assert _run(_inner()) == ["m42"]


def test_ping_iq_gets_a_result():
    async def _inner():
        fs = _FakeFS()
        session = InboundSession(fs, _IdentityNS(), _FakeDevice())
        session.start()
        fs.push(
            Node(
                tag="iq",
                attrs={
                    "id": "p1",
                    "type": "get",
                    "xmlns": "urn:xmpp:ping",
                    "from": JID(server="s.whatsapp.net"),
                },
                content=[Node(tag="ping")],
            )
        )
        await asyncio.sleep(0.05)
        replies = fs.sent_nodes()
        await session.stop()
        return replies

    replies = _run(_inner())
    assert any(n.tag == "iq" and n.attrs.get("id") == "p1" and n.attrs.get("type") == "result" for n in replies)


def test_server_set_iq_gets_a_result():
    async def _inner():
        fs = _FakeFS()
        session = InboundSession(fs, _IdentityNS(), _FakeDevice())
        session.start()
        fs.push(
            Node(
                tag="iq",
                attrs={"id": "set-1", "type": "set", "from": JID(server="s.whatsapp.net")},
            )
        )
        await asyncio.sleep(0.05)
        replies = fs.sent_nodes()
        await session.stop()
        return replies

    replies = _run(_inner())
    assert any(n.attrs.get("id") == "set-1" and n.attrs.get("type") == "result" for n in replies)


def test_wait_ack_resolves_matching_ack():
    async def _inner():
        fs = _FakeFS()
        session = InboundSession(fs, _IdentityNS(), _FakeDevice())
        session.start()

        async def _arrive() -> None:
            await asyncio.sleep(0.02)
            fs.push(_peer_message("noise"))
            fs.push(Node(tag="ack", attrs={"id": "3EB0ABC", "class": "message"}))

        asyncio.create_task(_arrive())
        node = await session.wait_ack("3EB0ABC", timeout=1.0)
        await session.stop()
        return node.attrs.get("id")

    assert _run(_inner()) == "3EB0ABC"


def test_drain_exits_on_idle_with_no_frames():
    async def _inner():
        fs = _FakeFS()
        session = InboundSession(fs, _IdentityNS(), _FakeDevice())
        session.start()
        n = await session.drain(seconds=2.0, idle=0.15)
        await session.stop()
        return n

    assert _run(_inner()) == 0


def test_drain_stops_when_socket_closes():
    async def _inner():
        fs = _FakeFS()
        session = InboundSession(fs, _IdentityNS(), _FakeDevice())
        session.start()
        fs.close()
        await session.drain(seconds=5.0, idle=5.0)
        closed = session.disconnected
        await session.stop()
        return closed

    assert _run(_inner()) is True


def test_keepalive_reply_does_not_refresh_idle_clock():
    async def _inner():
        fs = _FakeFS()
        session = InboundSession(fs, _IdentityNS(), _FakeDevice())
        stamp = session.last_unsolicited_ts
        session._make_future(session._iq, "ka-test")
        await session.handle(
            Node(tag="iq", attrs={"id": "ka-test", "type": "result"})
        )
        unchanged = session.last_unsolicited_ts == stamp
        await session.stop()
        return unchanged

    assert _run(_inner()) is True
