"""Signal linked-device commands backed by AsamK/signal-cli and a local inbox."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import uuid
from contextlib import closing
from pathlib import Path

import click


def backend_path() -> str:
    """Find the native client without recursively invoking this wrapper."""
    explicit = os.environ.get("SIGNAL_CLI_BACKEND")
    candidates = (
        [explicit]
        if explicit
        else [str(Path(directory) / "signal-cli") for directory in os.get_exec_path()]
    )
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if not path.is_file() or not os.access(path, os.X_OK):
            continue
        # Both the checkout launcher and installed entry point import this module.
        with path.open("rb") as stream:
            if b"wa.signal_cli" in stream.read(8192):
                continue
        return str(path.resolve())
    raise click.ClickException(
        "Signal backend not found. Install AsamK/signal-cli (macOS: brew install signal-cli), "
        "or set SIGNAL_CLI_BACKEND to its executable."
    )


class Client:
    """Keep native account selection and cached inboxes isolated by data directory."""

    def __init__(self, data_dir: Path, account: str | None):
        self.data_dir = data_dir.expanduser().resolve()
        self.account = account

    def command(self, *args: str, account: bool = True) -> list[str]:
        command = [backend_path(), "--data-dir", str(self.data_dir)]
        if account and self.account:
            command += ["--account", self.account]
        return [*command, *args]

    def run(self, *args: str, account: bool = True):
        try:
            result = subprocess.run(
                self.command("--output", "json", *args, account=account),
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise click.ClickException(str(error)) from error
        if result.returncode:
            raise click.ClickException(result.stderr.strip() or "Signal backend failed")
        try:
            return json.loads(result.stdout)
        except ValueError as error:
            raise click.ClickException(
                "Signal backend returned invalid JSON"
            ) from error

    def select_account(self) -> str:
        accounts = self.run("listAccounts", account=False)
        matches = [
            a
            for a in accounts
            if not self.account or self.account in (a.get("number"), a.get("aci"))
        ]
        if len(matches) != 1:
            raise click.ClickException(
                "Link a phone with signal-cli login, or select one linked account with --account."
            )
        self.account = matches[0].get("number") or matches[0]["aci"]
        return self.account

    def database(self) -> sqlite3.Connection:
        # Account selection must precede cache access to prevent cross-account reads.
        if not self.account:
            self.select_account()
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.data_dir / "inbox.sqlite3"
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        db = sqlite3.connect(path)
        db.execute(
            "CREATE TABLE IF NOT EXISTS messages (account TEXT, chat TEXT, sender TEXT, timestamp INTEGER, body TEXT, PRIMARY KEY(account, chat, sender, timestamp))"
        )
        db.row_factory = sqlite3.Row
        return db

    def peers(self) -> list[dict]:
        contacts = self.run("listContacts", "--all-recipients")
        groups = self.run("listGroups")
        peers = []
        for contact in contacts:
            profile = contact.get("profile") or {}
            identity = contact.get("number") or contact.get("uuid")
            if identity:
                peers.append(
                    {
                        "id": identity,
                        "aliases": [contact.get("number"), contact.get("uuid")],
                        "name": contact.get("name")
                        or profile.get("givenName")
                        or identity,
                        "group": False,
                    }
                )
        for group in groups:
            peers.append(
                {
                    "id": group["id"],
                    "aliases": [],
                    "name": group.get("name") or group["id"],
                    "group": True,
                }
            )
        return peers

    def resolve(self, query: str) -> dict:
        peers = self.peers()
        exact = [p for p in peers if query == p["id"] or query in p["aliases"]]
        matches = exact or [
            p for p in peers if query.casefold() in p["name"].casefold()
        ]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise click.ClickException(
                "Ambiguous recipient; use an exact ID: "
                + ", ".join(p["id"] for p in matches)
            )
        try:
            uuid.UUID(query.removeprefix("PNI:"))
            direct = True
        except ValueError:
            direct = query.startswith("u:") or (
                query.startswith("+") and query[1:].isdigit()
            )
        if direct:
            return {"id": query, "aliases": [], "name": query, "group": False}
        raise click.ClickException(
            "No matching contact or group. Run signal-cli pull, then signal-cli list."
        )

    def save(self, db: sqlite3.Connection, event: dict) -> None:
        envelope = event.get("envelope", event)
        sent = (envelope.get("syncMessage") or {}).get("sentMessage")
        message = sent or envelope.get("dataMessage")
        if not message:
            return
        sender = (
            envelope.get("sourceUuid")
            or envelope.get("sourceNumber")
            or envelope.get("source")
        )
        destination = (
            (
                sent.get("destinationUuid")
                or sent.get("destinationNumber")
                or sent.get("destination")
            )
            if sent
            else sender
        )
        group = message.get("groupInfo") or {}
        chat = group.get("groupId") or destination
        if not chat or not sender:
            return
        deleted = message.get("remoteDelete")
        if deleted:
            db.execute(
                "DELETE FROM messages WHERE account=? AND chat=? AND sender=? AND timestamp=?",
                (
                    self.account,
                    chat,
                    sender,
                    deleted.get("targetTimestamp", deleted.get("timestamp")),
                ),
            )
        # Ephemeral content is excluded instead of extending its lifetime in an archive.
        if (
            message.get("expiresInSeconds")
            or message.get("viewOnce")
            or not message.get("message")
        ):
            db.commit()
            return
        db.execute(
            "INSERT OR IGNORE INTO messages VALUES (?, ?, ?, ?, ?)",
            (self.account, chat, sender, message["timestamp"], message["message"]),
        )
        # Commit each event before receiving another; interrupted pulls retain prior messages.
        db.commit()


def emit(value, as_json: bool) -> None:
    """Keep JSON output parseable while rendering concise terminal rows."""
    if as_json:
        click.echo(json.dumps(value, ensure_ascii=False))
    elif isinstance(value, list):
        for row in value:
            click.echo(
                "  ".join(
                    str(row.get(k, ""))
                    for k in ("timestamp", "name", "id", "sender", "body")
                    if k in row
                )
            )
    else:
        click.echo(json.dumps(value, ensure_ascii=False))


@click.group()
@click.option(
    "--account", envvar="SIGNAL_ACCOUNT", help="Linked account number or UUID."
)
@click.option(
    "--data-dir",
    type=click.Path(path_type=Path),
    default=lambda: (
        Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
        / "signal-cli"
    ),
)
@click.pass_context
def cli(ctx, account, data_dir):
    """Signal with WhatsApp-style commands. Example: signal-cli pull."""
    ctx.obj = Client(data_dir, account)


@cli.command()
@click.option("--name", default="personal-cli", show_default=True)
@click.pass_obj
def login(client, name):
    """Link your phone by QR code. Example: signal-cli login --name laptop."""
    # Inherited terminal output lets the native client render its pairing QR code.
    result = subprocess.run(
        client.command("link", "--name", name, account=False), check=False
    )
    if result.returncode:
        raise click.ClickException("Signal linking failed; retry signal-cli login.")


@cli.command()
@click.option("--json", "as_json", is_flag=True)
@click.pass_obj
def status(client, as_json):
    """Show local linked accounts. Example: signal-cli status --json."""
    emit(client.run("listAccounts", account=False), as_json)


@cli.command("list")
@click.option("--json", "as_json", is_flag=True)
@click.option("--limit", type=click.IntRange(min=1), default=20)
@click.pass_obj
def list_chats(client, as_json, limit):
    """List known contacts and groups. Example: signal-cli list --json."""
    client.select_account()
    emit(client.peers()[:limit], as_json)


@cli.command()
@click.option("--seconds", type=click.IntRange(1, 60), default=5, show_default=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_obj
def pull(client, seconds, as_json):
    """Receive queued text into the inbox. Example: signal-cli pull --seconds 10."""
    client.select_account()
    command = client.command(
        "--output",
        "json",
        "receive",
        "--timeout",
        str(seconds),
        "--ignore-attachments",
        "--ignore-stories",
        "--ignore-avatars",
        "--ignore-stickers",
    )
    count = 0
    with closing(client.database()) as db:
        try:
            with subprocess.Popen(
                command, stdout=subprocess.PIPE, text=True
            ) as process:
                try:
                    for line in process.stdout:
                        client.save(db, json.loads(line))
                        count += 1
                except BaseException:
                    # A parser failure or interruption must not leave a mailbox consumer running.
                    if process.poll() is None:
                        process.terminate()
                    raise
                if process.wait():
                    raise click.ClickException(
                        "Signal receive failed; previously cached messages are retained."
                    )
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise click.ClickException(
                "Invalid receive event; previously cached messages are retained."
            ) from error
        except OSError as error:
            raise click.ClickException(str(error)) from error
    emit({"received_events": count}, as_json)


@cli.command()
@click.argument("peer")
@click.option("--limit", type=click.IntRange(min=1), default=50)
@click.option("--json", "as_json", is_flag=True)
@click.pass_obj
def read(client, peer, limit, as_json):
    """Read cached text; pull first. Example: signal-cli read Alice --json."""
    client.select_account()
    recipient = client.resolve(peer)
    identities = [recipient["id"], *filter(None, recipient["aliases"])]
    placeholders = ",".join("?" for _ in identities)
    with closing(client.database()) as db:
        rows = db.execute(
            f"SELECT chat, sender, timestamp, body FROM messages WHERE account=? AND chat IN ({placeholders}) ORDER BY timestamp DESC LIMIT ?",
            (client.account, *identities, limit),
        ).fetchall()
    emit([dict(row) for row in reversed(rows)], as_json)


@cli.command()
@click.argument("peer")
@click.argument("text", required=False)
@click.option("--doc", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--stdin", "from_stdin", is_flag=True, help="Read message text from stdin."
)
@click.option("--yes", is_flag=True, help="Actually send; otherwise preview only.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_obj
def send(client, peer, text, doc, from_stdin, yes, as_json):
    """Preview or send text/files. Example: signal-cli send Alice hello --yes."""
    if from_stdin:
        if text is not None:
            raise click.UsageError("Use TEXT or --stdin, not both.")
        text = click.get_text_stream("stdin").read()
    if not text and not doc:
        raise click.UsageError("Provide TEXT, --stdin, or --doc.")
    client.select_account()
    recipient = client.resolve(peer)
    if not yes:
        emit(
            {
                "dry_run": True,
                "recipient": recipient["id"],
                "text": text,
                "doc": str(doc) if doc else None,
            },
            as_json,
        )
        return
    args = ["send"]
    if text:
        # Equals syntax preserves messages beginning with a dash in the native parser.
        args += [f"--message={text}"]
    if doc:
        args += ["--attachment", str(doc.resolve())]
    if recipient["group"]:
        args += ["--group-id", recipient["id"]]
    else:
        args += ["--", recipient["id"]]
    emit(client.run(*args), as_json)


cli.add_command(pull, "sync")
cli.add_command(list_chats, "chats")

# Signal follows the same app-managed write restrictions as the other connectors.
from wa.onebrain_policy import install as _install_policy

_install_policy(cli, "signal")
