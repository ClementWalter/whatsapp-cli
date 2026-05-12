# whatsapp-cli

A pure-Python WhatsApp client that pairs as a **linked device** and gives you
your own chats from the terminal:

```bash
wa sync                 # catch up
wa chats                # 1 row per conversation
wa read "alice"         # show a thread
wa send "alice" "hi"    # send a text (1:1 or group)
```

Not the Cloud Business API (which cannot read personal chats), not a Playwright
browser scraper. The client speaks WhatsApp's native protocol: Noise XX over
WebSocket, Signal Double Ratchet for messages, Sender Keys for groups,
LtHash-verified app-state sync for chat metadata.

## Install

Requires Python 3.11+ and [uv](https://github.com/astral-sh/uv).

```bash
# Clone and install as a user-wide tool. Pulls in deps (aiohttp, cryptography,
# protobuf, signal-protocol, click, qrcode, pyobjc-framework-Contacts on macOS).
git clone <repo> whatsapp-cli
uv tool install --from ./whatsapp-cli whatsapp-cli

# Pair: scan the QR code with your phone (WhatsApp → Linked Devices → Link a Device).
wa login

# Verify.
wa status
# → paired as 33629442167:23@s.whatsapp.net (?) on iphone
#   last sync: 2026-05-12 17:21 (5s ago, 22 frames)
```

State lives under `~/.config/whatsapp-user-cli/` (keys) and
`~/.cache/whatsapp-user-cli/` (chats, contacts, message history). To uninstall
cleanly, `uv tool uninstall whatsapp-cli` and `rm -rf` both directories.

## Daily usage

```bash
wa sync                              # ~3s if nothing new, longer for a backlog
wa chats                             # 20 most-recent conversations
wa chats --limit 200 --json          # everything, machine-readable
wa read "alice"                      # show alice's thread; auto-fetches more if cache is short
wa read "alice" --match 2            # disambiguate when multiple "alices" match
wa read 33687776779@s.whatsapp.net   # exact JID, never ambiguous
wa send "alice" "running 10 min late"
wa send "Football" "see you at 7pm"  # groups work too
wa import-contacts                   # macOS only: pull names from Contacts.app
```

`wa --help` lists every subcommand; `wa <cmd> --help` for per-command options
including `--json`, `--limit`, `--seconds`, `--idle`, `--refresh-groups`,
`--no-extend`, `--match`, `--reset`, `--dry-run`.

## Use as an LLM skill

The repo ships with a `SKILL.md` that documents the commands in a form an LLM
agent can drive. Install it as a Claude Code skill (works with any tool that
follows the `~/.claude/skills/<name>/SKILL.md` convention, including the
`npx`-style skill loaders):

```bash
# Symlink for local development — the skill picks up your latest edits.
mkdir -p ~/.claude/skills
ln -s "$(pwd)" ~/.claude/skills/whatsapp-user-cli

# OR copy if you prefer a frozen snapshot.
cp -R . ~/.claude/skills/whatsapp-user-cli
```

The skill is then available to any LLM agent that scans `~/.claude/skills/`.
Trigger phrases include "send a WhatsApp message", "read my WhatsApp chats",
"catch me up on WhatsApp". Inside the agent the surface is exactly `wa <cmd>`
— there's no separate API layer.

## Development

```bash
# One-time: build the Go oracle (used as the byte-for-byte reference for tests).
cd tools/oracle && go build -o oracle .

# Run the test suite. uv resolves dev deps from pyproject.toml's `test` group.
uv run --group test pytest tests/ -v

# Iterate without reinstalling — PEP 723 launcher pulls deps inline.
./scripts/whatsapp_user_cli.py chats

# After editing source, reinstall the console script.
# (`--reinstall` is required: uv sees the version unchanged and won't rebuild
# under plain `--force`.)
uv tool install --reinstall --from . whatsapp-cli
```

## Status

| Stage | Scope                                                | State |
| ----- | ---------------------------------------------------- | ----- |
| 1     | Binary XML codec (tokens, nodes, JIDs, packing)      | ✅    |
| 2     | Noise XX handshake + WebSocket transport             | ✅    |
| 3     | QR pairing + device registration                     | ✅    |
| 4     | Login, keepalive, IQ round-trip                      | ✅    |
| 5     | Signal session, message decrypt (DM + group)         | ✅    |
| 6     | CLI surface (status/login/sync/chats/read/send/migrate) | ✅    |
| 7     | App-state sync — bootstrap history works             | partial |
| 8     | Media download / upload (AES-CBC + HMAC)             | not yet |
| 9     | Replies, reactions, edits, deletes                   | not yet |

Reliability features implemented: process lock (prevents racing `wa`
invocations from corrupting `signal.json`), delivery receipts (sender's `✓✓`
indicator updates, prevents server from re-queueing the same messages),
persistent Signal sessions and Sender Keys across runs.

## Dependencies

Not dep-free. Hard runtime requirements (declared in `pyproject.toml`):

- `aiohttp` — WebSocket transport
- `cryptography` — X25519, AES-GCM, HKDF for Noise
- `signal-protocol` — Rust-backed libsignal binding (Double Ratchet, Sender Keys)
- `protobuf` — wire format for handshake / app-state messages
- `click` — CLI framework
- `qrcode[pil]` — render pairing QR in the terminal
- `pyobjc-framework-Contacts` — macOS-only, used by `wa import-contacts`

Only `wa/wabinary/` is pure stdlib.

## Legal posture

Reverse-engineered clients run against WhatsApp's ToS. Meta has historically
sent cease-and-desist letters to popular libs (e.g. Baileys). This project is
scoped to **single-user personal tooling**, not a service. Use on a number
you can afford to have banned; expect periodic breakage when WhatsApp bumps
protocol tokens; no Meta branding anywhere.
