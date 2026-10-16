"""Verify Signal routing, inbox isolation, safe sends, and native-client integration."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from wa.signal_cli import NATIVE_ROOT, Client, cli, native_node

NATIVE_RUN = Client.run


@pytest.fixture
def client(tmp_path):
    return Client(tmp_path, "+12025550100")


@pytest.fixture
def backend(monkeypatch, tmp_path):
    calls = []

    def run(self, *args, **kwargs):
        calls.append(args)
        return {
            "listAccounts": [{"number": "+12025550100", "aci": "self-id"}],
            "listContacts": [
                {"number": "+12025550101", "uuid": "alice-id", "name": "Alice"}
            ],
            "listGroups": [{"id": "group-id", "name": "Friends"}],
            "send": {"timestamp": 123},
        }[args[0]]

    monkeypatch.setattr(Client, "run", run)
    monkeypatch.setenv("BRAIN_SUPPORT", str(tmp_path / "policy"))
    return calls


@pytest.fixture
def event():
    return {
        "envelope": {
            "sourceUuid": "alice-id",
            "timestamp": 123,
            "dataMessage": {"timestamp": 123, "message": "hello"},
        }
    }


@pytest.mark.parametrize(
    "command",
    [
        "login",
        "status",
        "list",
        "chats",
        "pull",
        "sync",
        "read",
        "send",
        "setup",
        "backfill",
        "history",
    ],
)
def test_command_help_has_example(command):
    assert "Example:" in CliRunner().invoke(cli, [command, "--help"]).output


def test_native_command_places_global_options_first(client, monkeypatch):
    monkeypatch.setattr("wa.signal_cli.native_node", lambda: "node")
    assert client.command("--output", "json", "receive") == [
        "node",
        "--disable-warning=ExperimentalWarning",
        str(NATIVE_ROOT / "runtime.mjs"),
        "--data-dir",
        str(client.data_dir),
        "--account",
        "+12025550100",
        "--output",
        "json",
        "receive",
    ]


def test_link_omits_account(client, monkeypatch):
    monkeypatch.setattr("wa.signal_cli.native_node", lambda: "node")
    assert "--account" not in client.command("link", account=False)


def test_external_backend_setting_is_ignored(monkeypatch):
    monkeypatch.setenv("SIGNAL_CLI_BACKEND", "/untrusted/backend")
    monkeypatch.setattr("wa.signal_cli.node_runtime", lambda: "node")
    assert native_node() == "node"


def test_missing_node_is_actionable(monkeypatch):
    monkeypatch.setattr("wa.signal_cli.shutil.which", lambda name, **kwargs: None)
    assert "Node.js" in CliRunner().invoke(cli, ["status"]).output


def test_account_without_phone_uses_aci(client, monkeypatch):
    client.account = None
    monkeypatch.setattr(
        client, "run", lambda *a, **k: [{"number": None, "aci": "self-id"}]
    )
    assert client.select_account() == "self-id"


def test_multiple_accounts_require_selection(client, monkeypatch):
    client.account = None
    monkeypatch.setattr(
        client, "run", lambda *a, **k: [{"number": "+1"}, {"number": "+2"}]
    )
    with pytest.raises(click.ClickException, match="select one linked account"):
        client.select_account()


def test_resolve_contact_name(client, backend):
    assert client.resolve("alice")["id"] == "+12025550101"


def test_resolve_group(client, backend):
    assert client.resolve("Friends")["group"] is True


def test_ambiguous_recipient_fails(client, monkeypatch):
    monkeypatch.setattr(
        client,
        "peers",
        lambda: [
            {"id": "+1", "name": "Alice", "aliases": []},
            {"id": "+2", "name": "Alice Smith", "aliases": []},
        ],
    )
    with pytest.raises(click.ClickException, match="Ambiguous"):
        client.resolve("Alice")


def test_save_incoming_text(client, event):
    with client.database() as db:
        client.save(db, event)
        assert db.execute("SELECT body FROM messages").fetchone()[0] == "hello"


def test_replayed_message_is_deduplicated(client, event):
    with client.database() as db:
        client.save(db, event)
        client.save(db, event)
        assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 1


@pytest.mark.parametrize("field,value", [("expiresInSeconds", 60), ("viewOnce", True)])
def test_ephemeral_content_is_not_archived(client, event, field, value):
    event["envelope"]["dataMessage"][field] = value
    with client.database() as db:
        client.save(db, event)
        assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_receipts_are_not_messages(client):
    with client.database() as db:
        client.save(db, {"envelope": {"receiptMessage": {"timestamps": [123]}}})
        assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_sent_sync_uses_destination(client):
    with client.database() as db:
        client.save(
            db,
            {
                "envelope": {
                    "sourceUuid": "self-id",
                    "syncMessage": {
                        "sentMessage": {
                            "destinationUuid": "alice-id",
                            "timestamp": 124,
                            "message": "outgoing",
                        }
                    },
                }
            },
        )
        assert db.execute("SELECT chat FROM messages").fetchone()[0] == "alice-id"


def test_group_uses_group_id(client, event):
    event["envelope"]["dataMessage"]["groupInfo"] = {"groupId": "group-id"}
    with client.database() as db:
        client.save(db, event)
        assert db.execute("SELECT chat FROM messages").fetchone()[0] == "group-id"


def test_remote_delete_removes_authors_message(client, event):
    with client.database() as db:
        client.save(db, event)
        client.save(
            db,
            {
                "envelope": {
                    "sourceUuid": "alice-id",
                    "dataMessage": {
                        "timestamp": 124,
                        "remoteDelete": {"targetTimestamp": 123},
                    },
                }
            },
        )
        assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_remote_delete_cannot_remove_other_authors_message(client, event):
    event["envelope"]["dataMessage"]["groupInfo"] = {"groupId": "group-id"}
    with client.database() as db:
        client.save(db, event)
        client.save(
            db,
            {
                "envelope": {
                    "sourceUuid": "other-id",
                    "dataMessage": {
                        "timestamp": 124,
                        "groupInfo": {"groupId": "group-id"},
                        "remoteDelete": {"targetTimestamp": 123},
                    },
                }
            },
        )
        assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 1


def test_database_has_private_permissions(client):
    client.database().close()
    assert (client.data_dir / "inbox.sqlite3").stat().st_mode & 0o777 == 0o600


def test_read_returns_cached_text(client, event, backend):
    with client.database() as db:
        client.save(db, event)
    result = CliRunner().invoke(
        cli, ["--data-dir", str(client.data_dir), "read", "Alice", "--json"]
    )
    assert json.loads(result.output)[0]["body"] == "hello"


def test_read_isolates_accounts(client, event, backend):
    client.account = "+12025550999"
    with client.database() as db:
        client.save(db, event)
    result = CliRunner().invoke(
        cli, ["--data-dir", str(client.data_dir), "read", "Alice", "--json"]
    )
    assert json.loads(result.output) == []


def test_send_defaults_to_preview(backend):
    CliRunner().invoke(cli, ["send", "Alice", "hello", "--json"])
    assert not any(call[0] == "send" for call in backend)


def test_send_preview_resolves_recipient(backend):
    result = CliRunner().invoke(cli, ["send", "Alice", "hello", "--json"])
    assert json.loads(result.output)["recipient"] == "+12025550101"


@pytest.mark.parametrize(
    "peer,ending",
    [("Alice", ("--", "+12025550101")), ("Friends", ("--group-id", "group-id"))],
)
def test_send_routes_to_exact_destination(backend, peer, ending):
    CliRunner().invoke(cli, ["send", peer, "hello", "--yes"])
    assert backend[-1] == ("send", "--message=hello", *ending)


def test_send_attachment(backend, tmp_path):
    document = tmp_path / "report.txt"
    document.write_text("report")
    CliRunner().invoke(cli, ["send", "Alice", "--doc", str(document), "--yes"])
    assert backend[-1] == ("send", "--attachment", str(document), "--", "+12025550101")


def test_send_stdin(backend):
    CliRunner().invoke(cli, ["send", "Alice", "--stdin", "--yes"], input="hello\n")
    assert backend[-1][1] == "--message=hello\n"


def test_empty_send_is_rejected(backend):
    assert CliRunner().invoke(cli, ["send", "Alice"]).exit_code == 2


def test_send_preserves_leading_dash(backend):
    CliRunner().invoke(cli, ["send", "--yes", "--", "Alice", "--hello"])
    assert backend[-1] == ("send", "--message=--hello", "--", "+12025550101")


@pytest.fixture
def receiver(monkeypatch, backend):
    stub_run = Client.run

    def dispatch(self, *args, **kwargs):
        if args[0] == "listAccounts":
            return stub_run(self, *args, **kwargs)
        return NATIVE_RUN(self, *args, **kwargs)

    monkeypatch.setattr(Client, "run", dispatch)

    def configure(event, *, exit_code=0, trailer=""):
        # A real child exercises the receive stream without contacting Signal.
        output = json.dumps(event) + "\n" + trailer
        code = f"import sys; sys.stdout.write({output!r}); sys.stdout.flush(); sys.exit({exit_code})"
        monkeypatch.setattr(
            Client, "command", lambda *a, **k: [sys.executable, "-c", code]
        )

    return configure


def test_native_subprocess_returns_json(client, receiver, event):
    receiver(event)
    assert client.run("receive") == event


def test_pull_reports_received_events(client, receiver, event):
    receiver({"received_events": 1})
    result = CliRunner().invoke(
        cli, ["--data-dir", str(client.data_dir), "pull", "--json"]
    )
    assert json.loads(result.output) == {"received_events": 1}


def test_native_processes_are_serialized(client):
    with (
        client.lock(),
        pytest.raises(click.ClickException, match="Another Signal command"),
        client.lock(),
    ):
        pass


@pytest.mark.parametrize("exit_code,trailer", [(1, ""), (0, "invalid-json\n")])
def test_failed_native_process_returns_error(
    client, receiver, event, exit_code, trailer
):
    receiver(event, exit_code=exit_code, trailer=trailer)
    result = CliRunner().invoke(cli, ["--data-dir", str(client.data_dir), "pull"])
    assert result.exit_code == 1


def test_read_only_policy_blocks_send(backend, monkeypatch, tmp_path):
    (tmp_path / "connector-permissions.json").write_text(
        '{"signal":{"read_only":true}}'
    )
    monkeypatch.setenv("BRAIN_SUPPORT", str(tmp_path))
    assert (
        "read-only"
        in CliRunner().invoke(cli, ["send", "Alice", "hello", "--yes"]).output
    )


def test_launcher_help_end_to_end():
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "bin/signal-cli"),
            "--help",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "WhatsApp-style" in result.stdout


def test_native_unpaired_status_end_to_end(tmp_path):
    try:
        native_node()
    except click.ClickException:
        pytest.skip("Official libsignal dependency is not installed")
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "bin/signal-cli"),
            "--data-dir",
            str(tmp_path),
            "status",
            "--json",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == []
