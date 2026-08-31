"""One inbound reader for a live WhatsApp connection.

The server dumps the offline mailbox as soon as the device is
authenticated. A second ``fs.recv`` loop — waiting on an IQ, a send
``<ack>``, group-info, media_conn, usync, prekeys — consumes those
frames and cannot decrypt or ACK them. This session is the only
consumer of ``recv`` after ``<success/>``: messages always go through
the decrypt/ack/receipt path, IQ and ack waiters subscribe by id, and
pings get a result so the server does not drop a quiet socket.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Callable
from typing import Any

from wa.wabinary import JID, Node, decode_node, encode_node

log = logging.getLogger(__name__)

# WhatsApp closes a companion that never pings; real clients send this
# xmlns on a ~20s cadence (whatsmeow ``KeepAliveIntervalSec``).
KEEPALIVE_S = 20.0
_SERVER = JID(server="s.whatsapp.net")


def _is_server_ping(node: Node) -> bool:
    if node.tag != "iq" or node.attrs.get("type") != "get":
        return False
    xmlns = node.attrs.get("xmlns", "")
    if xmlns in ("urn:xmpp:ping", "w:p"):
        return True
    return any(child.tag == "ping" for child in node.get_children())


class InboundSession:
    """Demux one Noise-wrapped WebSocket into waiters + the message path.

    ``fs`` / ``ns`` are the already-handshaked FrameSocket and Noise
    state. ``device`` supplies the own PN/LID used to classify receipts.
    ``on_message`` is invoked for every ``<message>`` *before* the
    transport ack — typically ``_try_decrypt_message``.
    """

    def __init__(
        self,
        fs: Any,
        ns: Any,
        device: Any,
        *,
        on_message: Callable[[Node], None] | None = None,
    ) -> None:
        self.fs = fs
        self.ns = ns
        self._on_message = on_message
        self._own_pn = JID.parse(device.jid) if getattr(device, "jid", None) else JID()
        lid = getattr(device, "lid", None)
        self._own_lid = JID.parse(lid) if lid else None
        self._iq: dict[str, asyncio.Future] = {}
        self._ack: dict[str, asyncio.Future] = {}
        self._task: asyncio.Task | None = None
        self._stopped = False
        self.frames_seen = 0
        self.disconnected = False
        self.last_unsolicited_ts = asyncio.get_event_loop().time()

    def start(self) -> None:
        if self._task is None:
            self._stopped = False
            self._task = asyncio.get_event_loop().create_task(self._read_loop())

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self._fail_waiters(ConnectionError("session closed"))

    async def send_node(self, node: Node) -> None:
        await self.fs.send(self.ns.encrypt_frame(encode_node(node)))

    async def request_iq(self, node: Node, timeout: float) -> Node:
        """Send ``node`` and wait for the IQ reply with the same ``id``."""
        iq_id = str(node.attrs["id"])
        fut = self._make_future(self._iq, iq_id)
        try:
            await self.send_node(node)
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._iq.pop(iq_id, None)

    async def wait_ack(self, msg_id: str, timeout: float) -> Node:
        """Wait for the server ``<ack class=message id=...>`` of an outgoing send."""
        fut = self._make_future(self._ack, msg_id)
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._ack.pop(msg_id, None)

    async def drain(self, *, seconds: float, idle: float) -> int:
        """Block until unsolicited silence, the hard cap, or a disconnect.

        Keepalive IQ replies do not reset the idle clock — they are
        answers to our own pings, not mailbox traffic.
        """
        started = asyncio.get_event_loop().time()
        self.last_unsolicited_ts = started
        hard_deadline = started + seconds
        last_ka = started
        while True:
            now = asyncio.get_event_loop().time()
            if self.disconnected:
                log.info("socket closed after %d frames", self.frames_seen)
                break
            if now >= hard_deadline:
                log.info(
                    "hit hard cap of %.0fs (received %d frames)",
                    seconds,
                    self.frames_seen,
                )
                break
            if now - self.last_unsolicited_ts >= idle:
                log.info(
                    "queue idle for %.1fs after %d frames — caught up",
                    idle,
                    self.frames_seen,
                )
                break
            if now - last_ka >= KEEPALIVE_S:
                await self._send_keepalive()
                last_ka = now
            wait = min(
                idle - (now - self.last_unsolicited_ts),
                hard_deadline - now,
                KEEPALIVE_S - (now - last_ka),
            )
            await asyncio.sleep(max(0.05, min(wait, 0.25)))
        return self.frames_seen

    async def handle(self, node: Node) -> None:
        """Dispatch one already-decoded stanza (also used to replay early frames)."""
        self.frames_seen += 1
        iq_id = str(node.attrs.get("id", "") or "")
        if node.tag == "iq" and iq_id in self._iq:
            fut = self._iq[iq_id]
            if not fut.done():
                fut.set_result(node)
            return
        if node.tag == "ack" and iq_id in self._ack:
            fut = self._ack[iq_id]
            if not fut.done():
                fut.set_result(node)
            return
        self.last_unsolicited_ts = asyncio.get_event_loop().time()
        if node.tag == "iq" and (
            _is_server_ping(node) or node.attrs.get("type") == "set"
        ):
            await self.send_node(
                Node(
                    tag="iq",
                    attrs={
                        "to": node.attrs.get("from", _SERVER),
                        "id": node.attrs.get("id", ""),
                        "type": "result",
                    },
                )
            )
            return
        if node.tag == "message":
            if self._on_message is not None:
                try:
                    self._on_message(node)
                except Exception as e:
                    log.warning("on_message failed: %s", e)
            await self._ack_stanza(node)
            await self._send_receipt(node)
            return
        if node.tag in ("notification", "call", "receipt"):
            await self._ack_stanza(node)

    async def _read_loop(self) -> None:
        while not self._stopped:
            try:
                ct = await self.fs.recv(timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except ConnectionError:
                self.disconnected = True
                self._fail_waiters(ConnectionError("socket closed"))
                return
            try:
                node = decode_node(self.ns.decrypt_frame(ct))
            except Exception as e:
                log.warning("failed to decode frame: %s", e)
                continue
            log.debug("inbound %s id=%s", node.tag, node.attrs.get("id"))
            try:
                await self.handle(node)
            except Exception as e:
                log.warning("handle failed for <%s>: %s", node.tag, e)

    async def _ack_stanza(self, node: Node) -> None:
        ack_attrs = {
            "class": node.tag,
            "id": node.attrs.get("id", ""),
            "to": node.attrs.get("from", _SERVER),
        }
        for k in ("participant", "recipient"):
            if k in node.attrs:
                ack_attrs[k] = node.attrs[k]
        if node.tag != "message" and "type" in node.attrs:
            ack_attrs["type"] = node.attrs["type"]
        await self.send_node(Node(tag="ack", attrs=ack_attrs))

    async def _send_receipt(self, node: Node) -> None:
        sender = node.attrs.get("participant") or node.attrs.get("from")
        is_own_echo = isinstance(sender, JID) and sender.user in {
            self._own_pn.user,
            self._own_lid.user if self._own_lid else None,
        }
        receipt_attrs = {
            "id": node.attrs.get("id", ""),
            "to": node.attrs.get("from", _SERVER),
        }
        for k in ("participant", "recipient"):
            if k in node.attrs:
                receipt_attrs[k] = node.attrs[k]
        if is_own_echo:
            receipt_attrs["type"] = "sender"
        await self.send_node(Node(tag="receipt", attrs=receipt_attrs))

    async def _send_keepalive(self) -> None:
        iq_id = f"ka-{secrets.token_hex(4)}"
        self._make_future(self._iq, iq_id)
        try:
            await self.send_node(
                Node(
                    tag="iq",
                    attrs={
                        "to": _SERVER,
                        "type": "get",
                        "id": iq_id,
                        "xmlns": "w:p",
                    },
                    content=[Node(tag="ping")],
                )
            )
        except Exception as e:
            log.debug("keepalive send failed: %s", e)

    def _make_future(self, bucket: dict[str, asyncio.Future], key: str) -> asyncio.Future:
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        bucket[key] = fut
        return fut

    def _fail_waiters(self, exc: BaseException) -> None:
        for bucket in (self._iq, self._ack):
            for fut in list(bucket.values()):
                if not fut.done():
                    fut.set_exception(exc)
            bucket.clear()
