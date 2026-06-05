"""CLI surface + media_conn parsing for document sends."""

from __future__ import annotations

import asyncio

from click.testing import CliRunner

import wa.cli as cli
from wa.cli import _fetch_media_conn, send
from wa.wabinary.encoder import encode_node
from wa.wabinary.node import Node


def test_send_rejects_empty_with_no_doc(monkeypatch):
    # A paired device with neither TEXT nor --doc is a usage error. Stub
    # Device.load so the check under test (not pairing) is what fails.
    class _Dev:
        @staticmethod
        def is_paired():
            return True

    monkeypatch.setattr(cli.Device, "load", staticmethod(lambda: _Dev()))
    result = CliRunner().invoke(send, ["pierre"])
    assert "nothing to send" in result.output


def test_send_doc_missing_file_errors():
    # click.Path(exists=True) rejects a non-existent --doc before any network.
    result = CliRunner().invoke(send, ["pierre", "--doc", "/no/such/file.pdf"])
    assert result.exit_code != 0


class _IdentityNS:
    """Noise stub: encrypt/decrypt are identity so we can hand-feed frames."""

    def encrypt_frame(self, b):
        return b

    def decrypt_frame(self, b):
        return b


class _ReplyFS:
    """FrameSocket stub that answers a media_conn IQ echoing the sent id."""

    def __init__(self, media_conn: Node):
        self._media_conn = media_conn
        self._reply: bytes | None = None

    async def send(self, frame):
        sent = cli.decode_node(frame)
        reply = Node(
            tag="iq",
            attrs={"id": sent.attrs["id"], "type": "result"},
            content=[self._media_conn],
        )
        self._reply = encode_node(reply)

    async def recv(self, timeout=0):
        return self._reply


def _run_media_conn(media_conn: Node):
    return asyncio.run(_fetch_media_conn(_ReplyFS(media_conn), _IdentityNS()))


def test_fetch_media_conn_parses_hosts():
    mc = Node(
        tag="media_conn",
        attrs={"auth": "AUTHTOKEN", "ttl": "3600"},
        content=[
            Node(tag="host", attrs={"hostname": "mmg.whatsapp.net"}),
            Node(tag="host", attrs={"hostname": "media.cdn.whatsapp.net"}),
        ],
    )
    hosts, _auth = _run_media_conn(mc)
    assert hosts == ["mmg.whatsapp.net", "media.cdn.whatsapp.net"]


def test_fetch_media_conn_returns_auth():
    mc = Node(
        tag="media_conn",
        attrs={"auth": "AUTHTOKEN"},
        content=[Node(tag="host", attrs={"hostname": "mmg.whatsapp.net"})],
    )
    _hosts, auth = _run_media_conn(mc)
    assert auth == "AUTHTOKEN"
