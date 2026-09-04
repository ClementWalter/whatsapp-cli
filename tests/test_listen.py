"""The listener's local socket protocol degrades to "no listener" without a socket."""

from __future__ import annotations

import wa.cli as cli
from wa import cache


def test_no_socket_means_no_listener(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "LISTEN_SOCK", tmp_path / "listen.sock")
    assert cli._daemon_request({"op": "ping"}) is None


def test_stale_socket_file_means_no_listener(tmp_path, monkeypatch):
    sock = tmp_path / "listen.sock"
    sock.write_text("")  # a leftover regular file, nobody serving it
    monkeypatch.setattr(cache, "LISTEN_SOCK", sock)
    assert cli._daemon_alive() is False


def test_live_connection_holds_its_parts():
    conn = cli.LiveConnection("fs", "ns", "session", "signal")
    assert (conn.fs, conn.session, conn.signal) == ("fs", "session", "signal")
