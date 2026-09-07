"""Primary names are list / pull / backfill; old names stay as aliases."""

from __future__ import annotations

import contextlib

import pytest
from click.testing import CliRunner

import wa.cache as cache
import wa.cli as cli
from wa.cli import cli as wa_cli


class _PairedDevice:
    @staticmethod
    def is_paired():
        return True


@pytest.fixture
def handshake_kwargs(monkeypatch):
    captured: dict = {}

    async def _fake_handshake(dev, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(cli.Device, "load", staticmethod(lambda: _PairedDevice()))
    monkeypatch.setattr(cli, "_login_handshake", _fake_handshake)
    monkeypatch.setattr(cache, "connection_lock", contextlib.nullcontext)
    monkeypatch.setattr(cache, "iter_messages", lambda: iter(()))
    # A developer's resident listener must not bypass the test handshake.
    monkeypatch.setattr(cli, "_daemon_alive", lambda: False)
    return captured


def test_help_lists_the_new_primary_names():
    result = CliRunner().invoke(wa_cli, ["--help"])
    assert "list" in result.output
    assert "pull" in result.output
    assert "backfill" in result.output


def test_list_and_chats_are_the_same_command():
    assert wa_cli.commands["list"] is wa_cli.commands["chats"]


def test_pull_and_sync_are_the_same_command():
    assert wa_cli.commands["pull"] is wa_cli.commands["sync"]


def test_backfill_and_history_are_the_same_command():
    assert wa_cli.commands["backfill"] is wa_cli.commands["history"]


def test_backfill_holds_the_full_window(handshake_kwargs):
    CliRunner().invoke(wa_cli, ["backfill", "--minutes", "4"])
    assert handshake_kwargs["idle"] == handshake_kwargs["seconds"] == 240.0


def test_history_alias_still_opens_the_window(handshake_kwargs):
    CliRunner().invoke(wa_cli, ["history", "--minutes", "4"])
    assert handshake_kwargs["seconds"] == 240.0


def test_pull_uses_the_short_idle(handshake_kwargs):
    CliRunner().invoke(wa_cli, ["pull"])
    assert handshake_kwargs["idle"] == 3.0
    assert handshake_kwargs["seconds"] == 120.0


def test_sync_alias_still_pulls(handshake_kwargs):
    CliRunner().invoke(wa_cli, ["sync", "--seconds", "9", "--idle", "1"])
    assert handshake_kwargs["seconds"] == 9.0
    assert handshake_kwargs["idle"] == 1.0
