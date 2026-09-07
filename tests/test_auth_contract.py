"""Device authentication metadata must never export paired identity material."""

import json
from click.testing import CliRunner
from wa.cli import cli
from wa.store import Device


def test_unpaired_status(monkeypatch):
    monkeypatch.setattr(Device, "load", lambda: None)
    result = CliRunner().invoke(cli, ["auth-status", "--json"])
    assert json.loads(result.output)["pairing_required"] is True


def test_sync_never_exports_keys():
    result = CliRunner().invoke(cli, ["auth-sync"])
    assert json.loads(result.output)["portable"] is False
