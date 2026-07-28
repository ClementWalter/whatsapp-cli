---
name: whatsapp-cli
description: "Read and write your own WhatsApp chats from the terminal via the bundled `bin/wa` command. Pairs as a linked device (one-time QR scan); then `bin/wa sync` catches up the offline queue, `bin/wa chats` lists conversations, `bin/wa read <name>` shows a thread, `bin/wa send <peer> \"<text>\"` sends a 1:1 or group message, and `bin/wa send <peer> --doc <path>` shares a document (PDF or any file). Reads end-to-end-encrypted personal chats — not the Cloud Business API. Use when the user asks to interact with their personal WhatsApp account from a shell or script."
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

`wa` on PATH is a symlink onto `bin/wa`, so it is the same program and cannot
drift:

```bash
ln -sfn <skill-dir>/bin/wa ~/.local/bin/wa
```

**Do not `uv tool install` this package.** That copies a snapshot, so `wa`
keeps running install-day code while `bin/wa` runs the current source, and a
newly added command fails with `Error: No such command '<name>'` under `wa`
only. It is also measurably slower to start than the PEP 723 launcher, since
`bin/wa` reuses uv's warm dependency cache either way. The `pyproject.toml`
here exists for the test group (`uv run --group test pytest`), not for
installing the CLI.

## When to use

Trigger this skill when the user wants to:

- read their own WhatsApp messages (`bin/wa read alice`)
- catch up after being offline (`bin/wa sync`)
- send a 1:1 or group message (`bin/wa send "Pierre" "hello"`)
- share a document / PDF (`bin/wa send "Pierre" --doc report.pdf`)
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

`sync` only ingests what the **server** has queued. It reports `0 frames —
caught up` and exits after `--idle` seconds of silence, which is correct for
queued mail but useless when you are waiting on the **phone** — use
`bin/wa history` for that.

### `bin/wa history [--minutes N]`
Hold a connection open (default 15 min, idle exit disabled) so the phone can
push chat-history sync bundles. Tell the user to open WhatsApp on their phone
**in the foreground with the screen kept awake** for the whole window — the
phone only serves history while both ends are live ("la synchro reprendra
lorsque WhatsApp sera ouvert sur les deux appareils"), and `sync`'s 3-second
idle exit closes the socket long before it starts pushing.

```bash
bin/wa history                 # 15-minute window
bin/wa history --minutes 30    # longer, for a big backfill
```

Reach for it when a chat is visibly missing recent messages. See
**Recovering missing messages** below for when it can't help.

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

**The auto-extend only walks backwards.** It asks the phone for messages
*older* than the oldest it already holds (`requesting N msgs older than
this`), so raising `--limit` never fetches recent messages — no matter how
high you push it. For those, see **Recovering missing messages**.

### `bin/wa send <peer> "<text>"`
Send a text message — or a document with `--doc`. `<peer>` is a fuzzy match
(same matcher as `read`) or a full JID. Supports 1:1 and group sends.

> **🚨 RÈGLE ABSOLUE : TOUJOURS lire avant d'envoyer, et imiter exactement le
> fil.** Avant **chaque** envoi (`bin/wa send`), lire d'abord les messages
> précédents de cette conversation exacte avec `bin/wa read <peer> --limit 30`
> (ou plus). Aucune exception : 1:1 comme groupe, même si on croit connaître le
> ton.
>
> Le message rédigé doit **épouser le fil sur TOUTES ses dimensions** :
>
> - **Ton et registre** : niveau de formalité, tutoiement/vouvoiement, humour,
>   blagues et références récurrentes.
> - **Langue** : celle réellement parlée par les participants (et le mélange
>   éventuel).
> - **Style et syntaxe** : longueur des phrases, façon de tourner, abréviations,
>   usage des majuscules.
> - **Ponctuation** : reproduire les usages observés (ex. « .. », « … », « ! »)
>   et ne jamais en inventer.
> - **Emojis** : n'utiliser **que** des emojis déjà apparus dans le fil, et au
>   même dosage. Si le fil n'en contient pas, n'en mettre aucun.
> - **Onomatopées et interjections** : ne reprendre **que** celles déjà écrites
>   dans le fil, à l'orthographe exacte (ex. si le fil dit « Ahaha », ne pas
>   écrire « Haha » ni « héhé »). Aucune variante, aucune invention.
>
> **Règle dure anti-innovation : aucun signe de ponctuation, aucun emoji, aucune
> onomatopée ou interjection qui n'a pas déjà été vu, à l'identique, dans cette
> conversation.** On recycle uniquement le vocabulaire de signes et
> d'expressions du fil, on n'en introduit jamais de nouveau. Un message
> au mauvais ton (ou au mauvais dosage d'emojis) est aussi grave qu'un message
> au mauvais destinataire, et un `send` est irréversible. Si le fil est vide ou
> introuvable, le signaler et demander le ton souhaité plutôt que de deviner.

```bash
bin/wa send "Pierre" "running late"
bin/wa send 33123456789@s.whatsapp.net "test"     # self-send (explicit JID)
bin/wa send "Football" "see you at 7pm"           # group (Sender Keys)
bin/wa send "Pierre" --doc report.pdf             # share a document (any file)
bin/wa send "Pierre" "see attached" --doc report.pdf   # document + caption
```

`--doc <path>` encrypts the file, uploads it to WhatsApp's media CDN, and
delivers it as a document message; the mimetype is auto-detected (a `.pdf`
arrives as `application/pdf`). `TEXT` is optional and becomes the caption.

A preview thumbnail is generated and embedded automatically when possible —
for images, PDFs (`pdftoppm`/`sips`), and HTML (`wkhtmltoimage` or headless
Chrome) — so the document shows an inline preview in the chat (WhatsApp only
auto-previews PDFs/images server-side, never HTML). Pass `--no-thumbnail` to
skip it.

**⚠️ A send is irreversible — there is no unsend/delete. When the exact
target matters, always pass a full JID, never a fuzzy name or a bare
number.** The matcher (`find_chat`) does a plain substring match against
`jid + app-state name + contact name`, so:

- **A bare phone number can silently resolve to a GROUP, not a person.**
  Legacy group JIDs embed a phone number (`<number>-<timestamp>@g.us`), so
  `bin/wa send 33612345678 "..."` may match that group instead of the
  contact. It picks the most-recent match and only warns on stderr.
- **To message yourself, use the explicit PN JID
  `<your-number>@s.whatsapp.net`** (e.g. `bin/wa send
  33612345678@s.whatsapp.net "note to self"`). The bare number does **not**
  reach your own DM: post-2024 self-chats are keyed by a `@lid` identity
  that contains no phone digits, so a bare number can't match it and will
  fall through to whatever group/contact happens to embed those digits.
- A full JID with `@` that matches no cached chat is accepted as-is and
  sent directly — this is the safe, unambiguous path.

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

### "A chat is missing its latest messages"
```bash
bin/wa history --minutes 15    # user must foreground WhatsApp on the phone now
bin/wa read "<name>" --no-extend
```
See **Recovering missing messages** — if `history` returns nothing new, the
messages were never encrypted for this device and only the phone has them.

### "Show me messages from <someone>"
```bash
bin/wa sync && bin/wa read "<their name>" --limit 100
```

### "Send a message to <someone>"
```bash
# STEP 1 — ALWAYS read the thread first to match its tone/language/register:
bin/wa read "<peer name or JID>" --limit 30
# STEP 2 — only then compose and send, in the tone observed above:
bin/wa send "<peer name or JID>" "<message text>"
# When the exact target matters, pass a full JID (a bare number can match a group):
bin/wa send "<number>@s.whatsapp.net" "<message text>"   # 1:1 / self-send
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

## Recovering missing messages

A chat that stops days or weeks before the phone shows. Diagnose it in this
order — the three causes need different fixes, and two of them are
unfixable, so don't burn rounds re-running `sync`.

**First, confirm the messages really are absent** rather than filed under a
JID you didn't read. Query the raw store, not the rendered output:

```bash
cd ~/.cache/whatsapp-cli/store
# newest message overall — is the store current at all?
jq -rc '[.ts,.chat]|@tsv' messages.jsonl | sort -rn | head -3
# newest for the suspect chat
jq -rc 'select(.chat|test("<jid-fragment>"))|[.ts,(.sender_name//"")]|@tsv' \
  messages.jsonl | sort -rn | head -3
```

If the store is current for other chats but stale for this one, delivery is
the problem, not display.

1. **Chat-history sync paused on the phone** → fixable. `bin/wa history`,
   with WhatsApp foregrounded on the phone for the whole window.
2. **The sender's client holds a stale device list** → not fixable
   retroactively. A 1:1 message only reaches a companion device if the
   *sender* encrypted a copy for it; if they didn't, no copy exists to
   fetch and no sync will ever produce one. Suspect this when groups stay
   current while specific DMs go stale — groups fan out via Sender Keys to
   every device, DMs don't. Having the user send that contact a message
   from their phone refreshes the device list for *future* messages only.
3. **Never delivered and not in the phone's push window** → read it on the
   phone. Say so plainly instead of looping.

`0 frames — caught up` means the server had nothing queued. It is **not**
evidence that the local store matches the phone.

## Things to know

- **Single connection at a time.** A file lock prevents two `bin/wa`
  processes from connecting simultaneously; the second one blocks until
  the first finishes. This includes `history`, which holds it for its whole
  window — don't try to `read --limit` in parallel, and stub
  `wa.cache.connection_lock` in tests so the suite never waits on a live
  session.
- **No daemon.** Messages only land locally when you run `bin/wa sync`
  (or any other connecting command). There's no push. Consequence worth
  internalising: the phone only serves chat history while a companion is
  *concurrently* connected, so any window this CLI opens is a few seconds
  wide unless you ask for one explicitly (`bin/wa history`).
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
