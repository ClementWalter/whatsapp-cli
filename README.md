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

Two ways:

**A — Bundled launcher.** No install step. Clone the repo and invoke
`bin/wa` directly; the `#!/usr/bin/env -S uv run --script` shebang and
PEP 723 inline metadata make `uv` pull deps on the first run.

```bash
git clone <repo> whatsapp-cli
cd whatsapp-cli
./bin/wa login                          # pair (one-time QR scan)
./bin/wa status
```

**B — Global tool install.** Puts a `wa` command on PATH; the rest of
the docs use that shorter form interchangeably with `bin/wa`.

```bash
uv tool install --from ./whatsapp-cli whatsapp-cli
wa login
```

State lives under `~/.config/whatsapp-user-cli/` (keys) and
`~/.cache/whatsapp-user-cli/` (chats, contacts, message history). To
uninstall cleanly, `uv tool uninstall whatsapp-cli` and `rm -rf` both
directories.

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

The repo ships with a `SKILL.md` and a self-contained launcher at `bin/wa`,
both at the project root. The skill is published in the
[Vercel Labs `skills`](https://github.com/vercel-labs/skills) format — same
YAML frontmatter + markdown body that the `npx skills` CLI installs, and
the same layout other agentic systems consume.

### Install via `npx skills` (once the repo is on GitHub)

```bash
# Replace <owner>/<repo> with the repo you publish to.
npx skills add <owner>/<repo>
```

This drops the skill under `~/.agents/skills/whatsapp-user-cli/` and
symlinks it into every supported agent runtime that's installed on your
machine (Claude Code, Cursor, Windsurf, Codex, Gemini CLI, …). Agents
then drive the CLI by invoking the bundled `bin/wa` script directly.

### Install locally (development / unpublished)

```bash
# Option 1 — symlink so the skill picks up live edits.
mkdir -p ~/.claude/skills
ln -s "$(pwd)" ~/.claude/skills/whatsapp-user-cli

# Option 2 — copy a frozen snapshot.
cp -R . ~/.claude/skills/whatsapp-user-cli
```

Either way, agents that scan `~/.claude/skills/` (or
`~/.agents/skills/`) will load `SKILL.md` and resolve commands against
the sibling `bin/wa` launcher. No separate `uv tool install` needed —
`bin/wa` is self-contained via PEP 723.

Trigger phrases include "send a WhatsApp message", "read my WhatsApp
chats", "catch me up on WhatsApp".

## Development

```bash
# One-time: build the Go oracle (used as the byte-for-byte reference for tests).
cd tools/oracle && go build -o oracle .

# Run the test suite. uv resolves dev deps from pyproject.toml's `test` group.
uv run --group test pytest tests/ -v

# Iterate without reinstalling — PEP 723 launcher pulls deps inline.
./bin/wa chats

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
