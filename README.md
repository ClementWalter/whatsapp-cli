# WhatsApp & Signal CLI

Read and send your personal **WhatsApp and Signal messages from the terminal**.
One repository, two commands, separate linked-device accounts and local stores:

| | WhatsApp | Signal |
| --- | --- | --- |
| Command | `wa` | `signal-cli` |
| Pairing | Scan a QR with WhatsApp | Scan a QR with Signal |
| Receive messages | `wa pull` | `signal-cli pull` |
| List chats | `wa list` | `signal-cli list` |
| Read a thread | `wa read "Alice"` | `signal-cli read "Alice"` |
| Send text or files | Sends immediately | Preview first; add `--yes` to send |
| Older messages | Phone history sync and read auto-extension | Phone history transfer during pairing |
| Runtime | Python 3.11+ and uv | Python 3.11+, uv, Node.js 24.18.1+, npm |

Both clients connect directly to their respective services and decrypt messages
locally. WhatsApp uses a Python protocol implementation; Signal uses this
repository's JavaScript client with Signal's official native `libsignal` library.
Signal requires no third-party backend executable or daemon. The `signal-cli`
command here belongs to this project; it is separate from AsamK's `signal-cli`.

## Get started

Install [uv](https://github.com/astral-sh/uv) and Python 3.11+. For Signal, also
install Node.js 24.18.1+ with npm.

```bash
git clone https://github.com/ClementWalter/whatsapp-cli.git
cd whatsapp-cli
```

The launchers in `bin/` use PEP 723 metadata, so uv resolves their Python
dependencies automatically. No Python package installation is required.

### WhatsApp

```bash
./bin/wa login
./bin/wa status
./bin/wa pull
./bin/wa list
./bin/wa read "Alice"
```

Scan the pairing QR from **WhatsApp → Settings → Linked Devices**.

### Signal

```bash
./bin/signal-cli setup
./bin/signal-cli login
./bin/signal-cli status --json
./bin/signal-cli pull
./bin/signal-cli list --json
./bin/signal-cli read "Alice" --limit 50
```

`setup` installs the exact locked Node dependencies. Scan the QR from
**Signal → Settings → Linked Devices**, then accept **Transfer Message History**
to import older messages. Keep the login command running until the transfer
finishes. Use `login --no-history` to link without importing history.

To save a pairing image you can open separately:

```bash
./bin/signal-cli login --qr-file pairing.png
```

QR codes expire; generate a new one if pairing closes before phone approval.

### Put both commands on PATH

From the checkout, link the launchers into a directory on your PATH:

```bash
mkdir -p ~/.local/bin
ln -s "$(pwd)/bin/wa" ~/.local/bin/wa
ln -s "$(pwd)/bin/signal-cli" ~/.local/bin/signal-cli
```

The examples below use these shorter command names. Symlinks run the current
checkout, so pulling updates does not require reinstalling the commands.

## Daily use

### WhatsApp

```bash
wa pull                              # receive the queued mailbox
wa list --limit 200 --json            # cached conversations
wa read "Alice" --limit 50
wa read "Alice" --match 2             # choose a match after an ambiguity warning
wa read "Alice" --no-extend --json    # cached messages only
wa listen                            # stay connected; stream messages as JSON lines
wa backfill --minutes 15              # keep the connection open for phone history
wa import-contacts                   # macOS Contacts.app names
```

For phone history sync, keep WhatsApp open in the foreground on the phone
while `backfill` runs. `pull` drains the server queue and normally exits after
three seconds of silence; it does not copy the phone's entire database.

### Signal

```bash
signal-cli pull --seconds 10 --json   # receive queued text into the local inbox
signal-cli list --limit 100 --json    # known groups and contacts; groups first
signal-cli read "Alice" --limit 50
signal-cli read "Alice" --json
signal-cli backfill --json            # retry a pending pairing history import
```

`read` displays cached text in chronological order. Run `pull` first for new
messages. Names must resolve uniquely; exact phone numbers, UUIDs, `u:`
usernames, and known group IDs are also accepted.

Use `--account NUMBER_OR_ACI` before the command, or set `SIGNAL_ACCOUNT`,
when multiple Signal accounts are linked. `--data-dir PATH` selects an
independent account store and inbox.

Both commands provide `sync` as an alias for `pull`, `chats` for `list`, and
`history` for `backfill`. Their options and history behavior differ; use
`wa <command> --help` or `signal-cli <command> --help` for details.

### Understanding history and message limits

`--limit 50` means **up to 50 available messages**, not a guarantee that the
client can retrieve 50 messages from the service.

| | WhatsApp | Signal |
| --- | --- | --- |
| New messages | `pull` receives the server mailbox | `pull` receives the server mailbox |
| Reading older messages | `read` requests older phone history when the cache is short; `--no-extend` disables this | `read` queries the local inbox only |
| Importing phone history | `backfill` holds a connection open for phone history pushes | Accept Transfer Message History during `login` |
| Retrying history | Run `backfill` with the phone foregrounded | Run `backfill` to retry the pending pairing archive |

A Signal device paired without a transfer key must pair again to request
phone history. Increasing `--limit` or rerunning `pull` cannot recover that
history. Once an archive has been imported, `backfill` reports it as already
imported. An empty server queue does not establish that either client's local
history matches everything on the phone.

## Send messages and files

WhatsApp sends immediately. Use an exact JID when the destination matters:
a bare phone number can match a legacy group JID containing that number.

```bash
wa send 33123456789@s.whatsapp.net "Running late"
wa send "Football" "See you at 7pm"
wa send "Alice" "See attached" --doc report.pdf
wa send "Alice" "Agreed" --reply-to MESSAGE_ID
wa download MESSAGE_ID -o downloads/
```

`MESSAGE_ID` comes from `wa read <chat> --json`. Documents are encrypted and
uploaded before sending; `download` decrypts attachments from cached messages.

Signal previews sends by default. Add `--yes` to transmit:

```bash
signal-cli send "Alice" "Running late"
signal-cli send "Alice" "Running late" --yes
signal-cli send "Alice" "See attached" --doc report.pdf --yes
```

Signal also accepts `--stdin` for message text. Text and file sends support
direct conversations and known groups. Live pairing, text reception, and
history transfer have been verified; live file uploads still need verification.

## Storage and current scope

| | WhatsApp | Signal |
| --- | --- | --- |
| Credentials | `~/.config/whatsapp-cli/` | Private `inbox.sqlite3` in the Signal data directory |
| Cached messages | `~/.cache/whatsapp-cli/` | The same account-scoped SQLite database |
| Default Signal data directory | — | `$XDG_DATA_HOME/signal-cli`, or `~/.local/share/signal-cli` |
| Continuous reception | `wa listen` | Run `pull` to receive queued messages |
| Incoming attachments | Download with `wa download` | Not downloaded |

Signal's receive queue commits an encrypted copy before acknowledging delivery.
Failed decryption remains queued for retry. Receiving does not send read receipts.
The Signal inbox excludes disappearing and view-once content; edits, reactions,
and stories are not presented. The phone may currently show the linked device
as unnamed.

WhatsApp implements QR pairing, Noise XX transport, direct and group message
encryption, app-state sync, history sync, text/document sends, replies, and
attachment downloads. History completeness depends on what the phone and
service deliver. Reactions and edits are not implemented.

If migrating from an earlier version that used AsamK's Signal client, native
startup imports its local credentials, ratchets, contacts, and groups without
launching that executable. Original files remain on disk. Stop using the old
client for that device: separate ratchet stores would diverge. Fresh native
pairing preserves the inbox and retains previous native credentials privately.

## Use with coding agents

The existing [SKILL.md](SKILL.md) documents WhatsApp workflows and can be
installed with:

```bash
npx skills add ClementWalter/whatsapp-cli
```

The bundled launchers are `bin/wa` and `bin/signal-cli`, relative to the
checkout or skill directory. Agents can discover Signal commands through
`bin/signal-cli --help`. Signal read commands support `--json`; WhatsApp
supports it on `list` and `read`. Signal pairing with `login --json` emits
JSON events, including the QR URI and transfer result.

## Development

Run focused tests first, then the full suite. Native Signal tests exercise
the official library without requiring an account:

```bash
./bin/signal-cli setup
uv run --group test pytest tests/test_signal_cli.py -q
npm test --prefix wa/signal_native
uv run --group test pytest tests/ -q
```

| Path | Purpose |
| --- | --- |
| `bin/wa`, `bin/signal-cli` | Checkout launchers |
| `wa/cli.py` | WhatsApp command interface |
| `wa/signal_cli.py` | Signal command interface and inbox reads |
| `wa/signal_native/` | Signal provisioning, transport, crypto stores, messaging, history |
| `wa/wabinary/`, `wa/crypto/`, `wa/transport/`, `wa/signal/` | WhatsApp protocol implementation |
| `tests/` | Python tests |
| `wa/signal_native/test/` | Native Signal tests |
| `tools/oracle/` | Go reference implementation for WhatsApp codec tests |

Python dependencies are pinned in `pyproject.toml` and `uv.lock`. They include
Click, qrcode, aiohttp, cryptography, protobuf, and the Rust-backed
`signal-protocol` binding used by WhatsApp. macOS contact import additionally
uses PyObjC. Signal's official `@signalapp/libsignal-client` and `protobufjs`
dependencies are locked in `wa/signal_native/package-lock.json`.

These are unofficial clients for personal accounts. Service protocol changes
can require client updates.
