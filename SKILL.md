---
name: whatsapp-cli
description: "Read and write your own WhatsApp chats from the terminal via the bundled `bin/wa` command. Pairs as a linked device (one-time QR scan); then `bin/wa sync` catches up the offline queue, `bin/wa chats` lists conversations, `bin/wa read <name>` shows a thread, `bin/wa send <peer> \"<text>\"` sends a 1:1 or group message (text only). Reads end-to-end-encrypted personal chats — not the Cloud Business API. Use when the user asks to interact with their personal WhatsApp account from a shell or script."
---

# WhatsApp User CLI

Terminal access to WhatsApp via protocol-level reimplementation. Pairs as a
linked device (same path WhatsApp Web uses), decrypts E2E chats locally, and
sends new messages. No browser automation, no Cloud Business API.

## How to invoke

All commands run through the bundled launcher next to this file: **`bin/wa`**
(PEP 723 — `uv` resolves Python deps inline on first run, no install step).
Examples in this doc are written as `bin/wa <cmd>`; resolve `bin/wa` against
this skill's own directory. From any other working directory the same script
can be invoked with its absolute path.

If the user has installed the CLI globally (`uv tool install --from <skill-dir>
whatsapp-cli`), `wa` on PATH is interchangeable with `bin/wa`.

## When to use

Trigger this skill when the user wants to:

- read their own WhatsApp messages (`bin/wa read alice`)
- catch up after being offline (`bin/wa sync`)
- send a 1:1 or group message (`bin/wa send "Pierre" "hello"`)
- list, search, or count their chats programmatically (`bin/wa chats --json`)
- check whether the CLI is paired or how recent the last sync was (`bin/wa status`)

Do **not** use it when the user wants to operate on someone else's account,
use the WhatsApp Business API, or scrape a web UI.

## First-time setup

```bash
# Pair as a linked device — prints a QR code in the terminal. The user
# scans it from their phone (WhatsApp → Settings → Linked Devices → Link).
bin/wa login

# Verify.
bin/wa status
# → paired as 33123456789:23@s.whatsapp.net (...) on iphone
#   last sync: 2026-05-12 16:40 (3m ago, 119 frames)
```

State lives in:
- `~/.config/whatsapp-cli/device.json` — pairing keys (keep private)
- `~/.config/whatsapp-cli/signal.json` — Signal ratchet + sender keys
- `~/.cache/whatsapp-cli/store/` — chats, contacts, messages cache

## Commands

All commands print human-readable output to stdout and structured logs to
stderr. Use `--json` where supported for machine parsing. Pass `--debug` on
the top level (`bin/wa --debug ...`) for verbose protocol logs.

### `bin/wa status`
Show pairing state and last sync. Use to verify the user is set up before
running any other command.

### `bin/wa sync [--seconds N] [--idle N] [--refresh-groups]`
Reconnect, drain the offline-message queue, exit when the queue goes idle for
`--idle` seconds (default 3). `--seconds` is a hard cap (default 120). Use
before `bin/wa chats` / `bin/wa read` if the user wants fresh data.

```bash
bin/wa sync                  # quick catch-up, ~3-30s depending on backlog
bin/wa sync --seconds 300    # bigger cap after weeks offline
```

### `bin/wa chats [--limit N] [--json]`
List conversations sorted by most recent activity. Default limit 20.

```
2026-05-12  dm     Alice               123456789012345@lid
2026-05-12  group  Football            123456789012345678@g.us
…
```

`--json` emits `[{jid, name, last_ts, display_name}, ...]`. The
`display_name` field falls back to `contacts.json` when the app-state name
is empty (DMs).

### `bin/wa read <query> [--limit N] [--match N] [--no-extend] [--json]`
Show messages from a single chat. `<query>` is a fuzzy substring match
against chat names, JIDs, and contact names (case-insensitive). Ambiguous
queries print a numbered list — pick with `--match N` or pass a full JID.

```bash
bin/wa read alice                   # most likely match
bin/wa read alice --match 2         # pick the 2nd ambiguous match
bin/wa read 123456789012345@lid     # exact JID, never ambiguous
bin/wa read famille --limit 100     # bigger window
bin/wa read alice --no-extend       # offline, cached only — no network
bin/wa read alice --json            # machine-readable
```

Auto-extends from the user's phone if the cache is shorter than `--limit`;
pass `--no-extend` to disable that network round-trip.

### `bin/wa send <peer> "<text>"`
Send a text message. `<peer>` is a fuzzy match (same matcher as `read`) or
a full JID. Supports 1:1 and group sends.

```bash
bin/wa send "Pierre" "running late"
bin/wa send 33123456789@s.whatsapp.net "test"     # self-send
bin/wa send "Football" "see you at 7pm"           # group (Sender Keys)
```

Limitations:
- Text only. Media, replies, reactions, edits not implemented.
- Server `<ack>` means "queued for delivery", not "actually delivered".
- For a brand-new contact (you've never received from them), the CLI
  fetches their prekey bundle automatically — no setup needed.

### `bin/wa import-contacts` (macOS only)
Pull display names from `Contacts.app` and merge into the local contacts
cache. Required to resolve LID-identified DMs to human names. Run once
after `bin/wa login` and re-run whenever the user adds new contacts to
their phone.

### `bin/wa migrate [--dry-run]`
One-shot cache cleanup: folds `@s.whatsapp.net` / `@lid` duplicate chats
into their canonical form (post-2024 WhatsApp routes most DMs via LIDs,
leaving legacy PN entries). Idempotent and safe to re-run; `--dry-run`
shows the plan.

### `bin/wa login [--reset]`
Pair as a linked device. First time: scans a QR code. With `--reset`:
wipes existing keys and starts fresh (forces a new QR). For already-paired
devices, this runs the same drain as `bin/wa sync` but with a full
group-info refresh that resolves all contact display names from group
participants (slower — ~5s cold, ~0s warm).

## Common workflows

### "Catch me up, what's been happening?"
```bash
bin/wa sync && bin/wa chats --limit 30
```

### "Show me messages from <someone>"
```bash
bin/wa sync && bin/wa read "<their name>" --limit 100
```

### "Send a message to <someone>"
```bash
bin/wa send "<peer name or JID>" "<message text>"
```

### "Find a chat by partial name"
```bash
bin/wa chats --json --limit 500 | jq '.[] | select(.display_name | test("theo"; "i"))'
```

### "Get all messages from a chat as JSON for further processing"
```bash
bin/wa read "alice" --limit 1000 --json --no-extend
```

## Output and exit codes

- All commands write to stdout/stderr in UTF-8.
- Exit code 0 on success, 1 on error (e.g. not paired, no chat matching
  query).
- `--json` mode emits a single JSON document; everything else is rendered
  for human reading.

## Things to know

- **Single connection at a time.** A file lock prevents two `bin/wa`
  processes from connecting simultaneously; the second one blocks until
  the first finishes.
- **No daemon.** Messages only land locally when you run `bin/wa sync`
  (or any other connecting command). There's no push.
- **`(unnamed)` chats are normal initially.** WhatsApp's app-state
  doesn't ship contact labels to linked devices; run
  `bin/wa import-contacts` (macOS) or let group-info backfill resolve
  them via `bin/wa login`.
- **Some group `skmsg` decrypt warnings are expected** ("invalid send key
  id"). They mean the sender first distributed their group key while we
  were disconnected, so we never received it. Future messages from that
  sender in that group will decrypt fine once they redistribute.
- **Reverse-engineered protocol.** Meta has historically sent C&D letters
  to similar projects (Baileys). Scoped to single-user personal tooling,
  not a service. The user should not run this on a number they can't
  afford to have banned.

## Project layout (for development questions)

```
whatsapp-cli/
  SKILL.md            # this file
  bin/wa              # PEP 723 launcher — invoke this
  wa/
    cli.py            # click commands — main entry point
    wabinary/         # binary XML codec, oracle-validated
    crypto/           # Noise XX, X25519, XEdDSA, HKDF
    transport/        # WebSocket + WA frame format
    signal/           # libsignal binding glue
    proto/            # vendored .proto schemas
    pair.py, handshake.py, clientpayload.py, store.py
    cache.py, history.py, peerreq.py, prekeys.py
  tools/oracle/       # Go binary wrapping whatsmeow for byte-diff tests
  tests/              # pytest, oracle-diffed
  pyproject.toml      # console-script entrypoint: wa = wa.cli:cli
```
