"""Signal commands using the repository's direct client and official libsignal."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import sqlite3
import subprocess
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

import click

NATIVE_ROOT = Path(__file__).with_name("signal_native")


def node_runtime() -> str:
    """Select an installed Node version supported by the cryptography library."""
    checked = set()
    for directory in os.get_exec_path():
        node = shutil.which("node", path=directory)
        if not node or str(Path(node).resolve()) in checked:
            continue
        checked.add(str(Path(node).resolve()))
        result = subprocess.run(
            [node, "--version"], capture_output=True, text=True, check=False
        )
        try:
            version = tuple(
                int(part) for part in result.stdout.strip().removeprefix("v").split(".")
            )
        except ValueError:
            continue
        if result.returncode == 0 and version >= (24, 18, 1):
            return node
    raise click.ClickException(
        "Node.js 24.18.1+ is required for official libsignal. Install Node.js, then run signal-cli setup."
    )


def native_node() -> str:
    """Locate the runtime and installed official cryptography library."""
    node = node_runtime()
    if not (NATIVE_ROOT / "node_modules/@signalapp/libsignal-client").is_dir():
        raise click.ClickException(
            "Official libsignal is not installed. Run signal-cli setup."
        )
    return node


class Client:
    """Keep native account selection and cached inboxes isolated by data directory."""

    def __init__(self, data_dir: Path, account: str | None):
        self.data_dir = data_dir.expanduser().resolve()
        self.account = account

    def command(self, *args: str, account: bool = True) -> list[str]:
        command = [
            native_node(),
            "--disable-warning=ExperimentalWarning",
            str(NATIVE_ROOT / "runtime.mjs"),
            "--data-dir",
            str(self.data_dir),
        ]
        if account and self.account:
            command += ["--account", self.account]
        return [*command, *args]

    @contextmanager
    def lock(self):
        """Serialize ratchet changes across all commands for this store."""
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.data_dir / "native.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise click.ClickException(
                    "Another Signal command is using this account store; retry when it finishes."
                ) from error
            yield
        finally:
            os.close(fd)

    def run(self, *args: str, account: bool = True):
        try:
            with self.lock():
                result = subprocess.run(
                    self.command("--output", "json", *args, account=account),
                    capture_output=True,
                    text=True,
                    timeout=360,
                    check=False,
                )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise click.ClickException(str(error)) from error
        if result.returncode:
            raise click.ClickException(
                result.stderr.strip() or "Native Signal client failed"
            )
        try:
            return json.loads(result.stdout)
        except ValueError as error:
            raise click.ClickException(
                "Native Signal client returned invalid JSON"
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


@click.group(epilog="\b\nExamples:\n  signal-cli status\n  signal-cli list --json\n  signal-cli read Alice --limit 20")
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


@cli.command(epilog="\b\nExamples:\n  signal-cli setup")
def setup():
    """Install the official crypto library. Example: signal-cli setup."""
    node = node_runtime()
    environment = {
        **os.environ,
        "PATH": str(Path(node).parent) + os.pathsep + os.environ.get("PATH", ""),
    }
    npm = shutil.which("npm", path=environment["PATH"])
    if not npm:
        raise click.ClickException(
            "Install Node.js with npm, then run signal-cli setup."
        )
    result = subprocess.run(
        [npm, "ci", "--ignore-scripts", "--prefix", str(NATIVE_ROOT)],
        check=False,
        env=environment,
    )
    if result.returncode:
        raise click.ClickException("Official libsignal installation failed.")


@cli.command(epilog="\b\nExamples:\n  signal-cli login\n  signal-cli login --name laptop")
@click.option("--name", default="personal-cli", show_default=True)
@click.option(
    "--history/--no-history", default=True, help="Request phone history during pairing."
)
@click.option(
    "--json", "as_json", is_flag=True, help="Emit pairing events as JSON lines."
)
@click.option(
    "--qr-file", type=click.Path(path_type=Path), help="Save the pairing QR as a PNG."
)
@click.pass_obj
def login(client, name, history, as_json, qr_file):
    """Link your phone by QR code. Example: signal-cli login --name laptop."""
    import qrcode

    args = ["link", "--name", name, *([] if history else ["--no-history"])]
    with (
        client.lock(),
        subprocess.Popen(
            client.command(*args, account=False), stdout=subprocess.PIPE, text=True
        ) as process,
    ):
        try:
            for line in process.stdout:
                event = json.loads(line)
                if event.get("type") == "link":
                    code = qrcode.QRCode(border=2)
                    code.add_data(event["uri"])
                    if qr_file:
                        code.make_image().save(qr_file)
                    if as_json:
                        emit(event, True)
                    else:
                        code.print_ascii(invert=True)
                        click.echo(
                            "Scan in Signal > Settings > Linked Devices; choose Transfer Message History."
                        )
                else:
                    emit(event, True)
            if process.wait():
                raise click.ClickException(
                    "Signal linking failed; retry signal-cli login."
                )
        except BaseException:
            if process.poll() is None:
                process.terminate()
            raise


@cli.command(epilog="\b\nExamples:\n  signal-cli status --json")
@click.option("--json", "as_json", is_flag=True)
@click.pass_obj
def status(client, as_json):
    """Show local linked accounts. Example: signal-cli status --json."""
    emit(client.run("listAccounts", account=False), as_json)


@cli.command("list", epilog="\b\nExamples:\n  signal-cli list\n  signal-cli chats --json --limit 50")
@click.option("--json", "as_json", is_flag=True)
@click.option("--limit", type=click.IntRange(min=1), default=20)
@click.pass_obj
def list_chats(client, as_json, limit):
    """List known contacts and groups. Example: signal-cli list --json."""
    client.select_account()
    emit(sorted(client.peers(), key=lambda peer: not peer["group"])[:limit], as_json)


@cli.command(epilog="\b\nExamples:\n  signal-cli pull\n  signal-cli sync --seconds 10 --json")
@click.option("--seconds", type=click.IntRange(1, 60), default=5, show_default=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_obj
def pull(client, seconds, as_json):
    """Receive queued text into the inbox. Example: signal-cli pull --seconds 10."""
    client.select_account()
    emit(client.run("receive", "--timeout", str(seconds)), as_json)


@cli.command(epilog="\b\nExamples:\n  signal-cli backfill --json")
@click.option("--json", "as_json", is_flag=True)
@click.pass_obj
def backfill(client, as_json):
    """Import history offered during pairing. Example: signal-cli backfill --json."""
    client.select_account()
    emit(client.run("importHistory"), as_json)


@cli.command(epilog="\b\nExamples:\n  signal-cli read Alice\n  signal-cli read Friends --limit 20 --json")
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


@cli.command(epilog="\b\nExamples:\n  signal-cli send Alice \"hello\"\n  signal-cli send Alice \"report\" --doc report.pdf --yes")
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
cli.add_command(backfill, "history")
