"""CLI surface for `wa history` — the chat-history sync window.

The command's whole reason to exist is that a quiet server must NOT end the
wait: it is the phone we are waiting on, not queued mail. These tests pin
the window arithmetic that guarantees that, without opening a socket.
"""

from __future__ import annotations

import contextlib

import pytest

from click.testing import CliRunner

import wa.cache as cache
import wa.cli as cli
from wa.cli import history


class _PairedDevice:
    @staticmethod
    def is_paired():
        return True


@pytest.fixture
def handshake_kwargs(monkeypatch):
    """Capture what `history` passes to the handshake, running no socket.

    Also neutralises the two process-wide resources the command touches, so
    the suite neither blocks on a lock a live `wa sync` may hold nor reads
    the developer's own message store.
    """
    captured: dict = {}

    async def _fake_handshake(dev, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(cli.Device, "load", staticmethod(lambda: _PairedDevice()))
    monkeypatch.setattr(cli, "_login_handshake", _fake_handshake)
    monkeypatch.setattr(cache, "connection_lock", contextlib.nullcontext)
    monkeypatch.setattr(cache, "iter_messages", lambda: iter(()))
    return captured


def test_history_requires_pairing(monkeypatch):
    monkeypatch.setattr(cli.Device, "load", staticmethod(lambda: None))
    result = CliRunner().invoke(history, [])
    assert "not paired" in result.output


def test_history_disables_the_idle_exit(handshake_kwargs):
    # idle == the whole window, so silence never gets read as "caught up".
    CliRunner().invoke(history, ["--minutes", "4"])
    assert handshake_kwargs["idle"] == handshake_kwargs["seconds"]


def test_history_window_is_minutes_in_seconds(handshake_kwargs):
    CliRunner().invoke(history, ["--minutes", "4"])
    assert handshake_kwargs["seconds"] == 240.0


def test_history_default_window_is_fifteen_minutes(handshake_kwargs):
    CliRunner().invoke(history, [])
    assert handshake_kwargs["seconds"] == 900.0


def test_history_skips_group_participant_refresh(handshake_kwargs):
    # Group names are irrelevant to a backfill wait and cost ~150ms per group.
    CliRunner().invoke(history, [])
    assert handshake_kwargs["fetch_groups"] is False


def test_history_tells_the_user_to_foreground_whatsapp(handshake_kwargs):
    result = CliRunner().invoke(history, [])
    assert "leave it in the foreground" in result.output
