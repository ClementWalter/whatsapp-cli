"""`wa link` without WhatsApp: QR rendering, the already-linked guard and the detached helper."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

import wa.cli as cli
from wa.cli import cli as wa_cli


class _Device:
    jid = "33100000000:1@s.whatsapp.net"
    push_name = "Alice"

    def __init__(self, paired: bool):
        self._paired = paired

    def is_paired(self):
        return self._paired


@pytest.fixture
def link_state(monkeypatch, tmp_path):
    path = tmp_path / "link.json"
    monkeypatch.setattr(cli, "_link_state_path", lambda: path)
    return path


def test_qr_svg_is_an_svg():
    assert cli._qr_svg("2@ref,key,key,adv").lstrip().startswith("<svg")


def test_link_when_paired_does_not_start_helper(monkeypatch, link_state):
    monkeypatch.setattr(cli.Device, "load", staticmethod(lambda: _Device(paired=True)))
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: pytest.fail("helper started"))
    result = CliRunner().invoke(wa_cli, ["link"])
    assert "already linked" in result.output


def test_link_reports_page_url(monkeypatch, link_state):
    monkeypatch.setattr(cli.Device, "load", staticmethod(lambda: None))
    # The helper announces its page through the state file; the stub does it at once.
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: link_state.write_text('{"url": "http://127.0.0.1:1/t/"}'))
    result = CliRunner().invoke(wa_cli, ["link"])
    assert "http://127.0.0.1:1/t/" in result.output
