---
name: whatsapp-cli
description: "Read and write your own WhatsApp chats from the terminal via the bundled `wa` command. Pairs as a linked device (one-time QR scan); then `wa pull` (alias `sync`) catches up the offline queue (or `wa listen` streams live), `wa list` (alias `chats`) lists conversations, `wa read <name>` shows a thread, `wa send <peer> \"<text>\"` sends a 1:1 or group message, and `wa send <peer> --doc <path>` shares a document (PDF or any file). Reads end-to-end-encrypted personal chats — not the Cloud Business API. Use when the user asks to interact with their personal WhatsApp account from a shell or script."
---

# WhatsApp User CLI

Terminal access to WhatsApp via protocol-level reimplementation. Pairs as a
linked device (same path WhatsApp Web uses), decrypts E2E chats locally, and
sends new messages. No browser automation, no Cloud Business API.

## How to invoke

Invoke it as **`wa`** — on `$PATH` via a symlink in `~/.local/bin` onto this
repo's `bin/wa`, so it always runs the current checkout: a `git pull`, or even
an uncommitted edit, takes effect immediately with nothing to reinstall.

```bash
wa list
```

Examples in this doc are written that way. If `wa` is not on `$PATH`, run the
bundled launcher `bin/wa` resolved against this skill's own directory (PEP 723
— `uv` resolves deps inline on first run), or link it once:

```bash
ln -sfn <skill-dir>/bin/wa ~/.local/bin/wa
```

**Do not `uv tool install` this package.** That copies a snapshot, so `wa` keeps
running install-day code while the checkout moves on, and a newly added command
fails with `Error: No such command '<name>'` under `wa` only. It is also measurably
slower to start than the PEP 723 launcher, which reuses uv's warm dependency cache
either way. The `pyproject.toml` here exists for the test group
(`uv run --group test pytest`), not for installing the CLI.

## When to use

Trigger this skill when the user wants to:

- read their own WhatsApp messages (`wa read alice`)
- catch up after being offline (`wa pull`)
- send a 1:1 or group message (`wa send "Pierre" "hello"`)
- share a document / PDF (`wa send "Pierre" --doc report.pdf`)
- list, search, or count their chats programmatically (`wa list --json`)
- check whether the CLI is paired or how recent the last sync was (`wa status`)

Do **not** use it when the user wants to operate on someone else's account,
use the WhatsApp Business API, or scrape a web UI.

## First-time setup

```bash
# Pair as a linked device — prints a QR code in the terminal. The user
# scans it from their phone (WhatsApp → Settings → Linked Devices → Link).
wa login

# Verify.
wa status
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
the top level (`wa --debug ...`) for verbose protocol logs.

### `wa status`
Show pairing state and last sync. Use to verify the user is set up before
running any other command.

### `wa pull` / `wa sync` `[--seconds N] [--idle N] [--refresh-groups]`
Reconnect, drain the **server** mailbox, exit when the queue goes idle for
`--idle` seconds (default 3). `--seconds` is a hard cap (default 120). Use
before `wa list` / `wa read` if the user wants fresh data. `sync` is an alias.

```bash
wa pull                  # quick catch-up, ~3-30s depending on backlog
wa pull --seconds 300    # bigger cap after weeks offline
```

`pull` only ingests what the **server** has queued. It reports `0 frames —
caught up` and exits after `--idle` seconds of silence, which is correct for
queued mail but useless when you are waiting on the **phone** — use
`wa backfill` for that.

### `wa backfill` / `wa history` `[--minutes N]`
Hold the socket open (default 15 min, idle exit disabled) so the phone can
push chat-history sync bundles; whatever it pushes is downloaded and ingested
when the window closes (so are bundles that arrive during `pull`/`listen`). Tell the user to open WhatsApp on their phone
**in the foreground with the screen kept awake** for the whole window — the
phone only serves history while both ends are live, and `pull`'s 3-second
idle exit closes the socket long before it starts pushing. `history` is an alias.

```bash
wa backfill                 # 15-minute window
wa backfill --minutes 30    # longer, for a big backfill
```

Reach for it when a chat is visibly missing recent messages. See
**Recovering missing messages** below for when it can't help.

### `wa listen [--exec CMD]`
Push instead of pull. Stays connected for hours, decrypts and caches every
message as it arrives, and prints each one as a JSON line (the `wa read --json`
shape). With `--exec CMD`, runs CMD per message with that JSON on stdin — the
inbox heartbeat's trigger. Reconnects with backoff when the server hangs up.

WhatsApp allows one connection per linked device, so while a listener runs:
`wa pull` is a no-op ("cache is already live"), and `wa send` to a 1:1 chat is
relayed over `~/.cache/whatsapp-cli/listen.sock` onto the listener's connection
(~0.7 s instead of a full handshake); group sends are refused until the listener
stops. `wa read` never needed the socket. Run it under a supervisor (launchd
KeepAlive, systemd), one per device.

### `wa list` / `wa chats` `[--limit N] [--json]`
List the local conversation cache, sorted by most recent activity. Default
limit 20. Offline — does not open a socket. `chats` is an alias.

```
2026-05-12  dm     Alice               123456789012345@lid
2026-05-12  group  Football            123456789012345678@g.us
…
```

`--json` emits `[{jid, name, last_ts, display_name}, ...]`. The
`display_name` field falls back to `contacts.json` when the app-state name
is empty (DMs).

### `wa read <query> [--limit N] [--since YYYY-MM-DD] [--match N] [--no-extend] [--wait S] [--json]`
Show messages from a single chat. `<query>` is a fuzzy substring match
against chat names, JIDs, and contact names (case-insensitive). Ambiguous
queries print a numbered list — pick with `--match N` or pass a full JID.

```bash
wa read alice                   # most likely match
wa read alice --match 2         # pick the 2nd ambiguous match
wa read 123456789012345@lid     # exact JID, never ambiguous
wa read famille --limit 100     # bigger window
wa read alice --no-extend       # offline, cached only — no network
wa read alice --json            # machine-readable
wa read alice --since 2022-01-01 --limit 0 --json   # page back to 2022, all of it
```

Each `--json` message carries `msg_id`, `quoted_id` and `media`: `quoted_id`
is the `msg_id` of the message a reply quotes (the "reply" bubble on the
phone), `""` otherwise; `media` is the attachment's CDN reference (kind,
mimetype, seconds…) consumed by `wa download`, `null` for text. Plain output renders a quote as `↩︎ «snippet» text`. That is how a
script tells "he answered *this* message" from "he wrote in the same chat".

Auto-extends from the user's phone when the cache is shorter than `--limit`
or does not reach back to `--since`: it pages older history 50 messages per
request, re-anchoring on the oldest cached message each time, until it has
enough, reaches the date, or the phone has nothing older. Each request waits
up to `--wait` seconds (default 180) for the phone. `--no-extend` skips the
network round-trip.

**Check `coverage` before concluding anything is absent.** `--json` output
carries `coverage: {cached, oldest_date, stop, complete, note}`; text mode
prints the same `note` on stderr. A non-null `note` starts with `PARTIAL:`
and means the cache does not reach as far back as was asked — never report
"not found" from a `PARTIAL` read. `stop` says why paging ended: `count` /
`since` (got what was asked), `no_older` (`complete: true`, the phone says
the chat starts there), `no_access` (the phone says older messages exist but
refuses to serve them), `nothing_new` (the phone answered without anything
new), `no_reply` (the phone stayed silent), `skipped` (`--no-extend`).

**The auto-extend only walks backwards.** It asks the phone for messages
*older* than the oldest it already holds, so raising `--limit` never fetches
recent messages — no matter how high you push it. For those, see
**Recovering missing messages**.

### `wa send <peer> "<text>"`
Send a text message — or a document with `--doc`. `<peer>` is a fuzzy match
(same matcher as `read`) or a full JID. Supports 1:1 and group sends.

> **🚨 RÈGLE ABSOLUE : TOUJOURS lire avant d'envoyer, et imiter exactement le
> fil.** Avant **chaque** envoi (`wa send`), lire d'abord les messages
> précédents de cette conversation exacte avec `wa read <peer> --limit 30`
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
wa send "Pierre" "running late"
wa send 33123456789@s.whatsapp.net "test"     # self-send (explicit JID)
wa send "Football" "see you at 7pm"           # group (Sender Keys)
wa send "Pierre" --doc report.pdf             # share a document (any file)
wa send "Pierre" "see attached" --doc report.pdf   # document + caption
wa send "Pierre" "yes, go ahead" --reply-to 3EB0ABCDEF0123456789AB   # quote a message (msg_id from `wa read --json`)
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
  `wa send 33612345678 "..."` may match that group instead of the
  contact. It picks the most-recent match and only warns on stderr.
- **To message yourself, use the explicit PN JID
  `<your-number>@s.whatsapp.net`** (e.g. `wa send
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

### `wa download <msg_id> [-o PATH]`
Download and decrypt an attachment — voice note, image, video or document —
from a cached message (`media` not null in `wa read --json`, text shown as
`[audio]`, `[image]`…). Fetched from WhatsApp's CDN with the message's own
media key, verified and decrypted; prints the written path. Voice notes are
Ogg/Opus (`.ogg`), which `afconvert` cannot read: decode with libsndfile
(`soundfile`) or ffmpeg.

```bash
wa read "Clément Walter" --json | jq '.messages[] | select(.media.kind=="audio") | .msg_id'
wa download 3A0E8ACA6B24EA044A13 -o /tmp/notes/      # → /tmp/notes/3A0E8ACA6B24EA044A13.ogg
```

### `wa import-contacts` (macOS only)
Pull display names from `Contacts.app` and merge into the local contacts
cache. Required to resolve LID-identified DMs to human names. Run once
after `wa login` and re-run whenever the user adds new contacts to
their phone.

### `wa migrate [--dry-run]`
One-shot cache cleanup: folds `@s.whatsapp.net` / `@lid` duplicate chats
into their canonical form (post-2024 WhatsApp routes most DMs via LIDs,
leaving legacy PN entries). Idempotent and safe to re-run; `--dry-run`
shows the plan.

### `wa login [--reset]`
Pair as a linked device. First time: scans a QR code. With `--reset`:
wipes existing keys and starts fresh (forces a new QR). For already-paired
devices, this runs the same drain as `wa pull` but with a full
group-info refresh that resolves all contact display names from group
participants (slower — ~5s cold, ~0s warm).

## Common workflows

### "Catch me up, what's been happening?"
```bash
wa pull && wa list --limit 30
```

### "A chat is missing its latest messages"
```bash
wa backfill --minutes 15    # user must foreground WhatsApp on the phone now
wa read "<name>" --no-extend
```
See **Recovering missing messages** — if `backfill` returns nothing new, the
messages were never encrypted for this device and only the phone has them.

### "Show me messages from <someone>"
```bash
wa pull && wa read "<their name>" --limit 100
```

### "Send a message to <someone>"
```bash
# STEP 1 — ALWAYS read the thread first to match its tone/language/register:
wa read "<peer name or JID>" --limit 30
# STEP 2 — only then compose and send, in the tone observed above:
wa send "<peer name or JID>" "<message text>"
# When the exact target matters, pass a full JID (a bare number can match a group):
wa send "<number>@s.whatsapp.net" "<message text>"   # 1:1 / self-send
```

### "Find a chat by partial name"
```bash
wa list --json --limit 500 | jq '.[] | select(.display_name | test("theo"; "i"))'
```

### "Get all messages from a chat as JSON for further processing"
```bash
wa read "alice" --limit 1000 --json --no-extend
```

### "What did <someone> tell me about <topic>?" (search old history)
```bash
# Page the whole chat in first, then search the JSON. Check coverage.note.
wa read "<name or JID>" --since 2015-01-01 --limit 0 --json > /tmp/chat.json
jq '.coverage' /tmp/chat.json
jq -r '.messages[] | select(.text | test("pitch|deck"; "i")) | "\(.ts|todate) \(.sender_name): \(.text)"' /tmp/chat.json
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
unfixable, so don't burn rounds re-running `pull`.

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

**Older than the cache** (the chat is current but starts too late, e.g. a
2022 thread when the cache begins in 2026): `wa read "<chat>" --since
<date> --limit 0` pages it in from the phone, 50 messages per request. What
is true of this path (verified live, 2026-10-26):

- The phone answers on-demand requests with a CDN-hosted ON_DEMAND bundle,
  usually within seconds but sometimes minutes late; a late answer that lands
  during any later connection is ingested then. CDN links expire within
  days, which is why ingestion happens on receipt.
- 1:1 chats must be requested by their **phone-number JID**: the phone
  silently ignores a request naming the `@lid` form. `read` uses the
  `lidmap.json` twin first and falls back to the `@lid` form; a 1:1 chat with
  no known phone number may get no answer at all.
- The phone sometimes goes quiet for a few minutes mid-walk, then answers
  again. Rerunning the same `read --since` resumes from the oldest cached
  message, so a `no_reply` stop is worth one or two retries a few minutes
  apart, with WhatsApp foregrounded.
- Each answer carries the phone's verdict
  (`Conversation.endOfHistoryTransferType`), and paging stops on it:
  `1` = the chat really starts here (`no_older`, `complete: true`);
  `3` = **older messages exist on the phone but it will not serve them to a
  linked device** (`no_access`). Type 3 is a hard phone-side cutoff, not
  sleep. Retrying, changing the anchor, or flipping the JID form does not
  move it. For `no_access`, go straight to the fallback below.
- **Undecryptable placeholders block the walk.** Messages the phone itself
  never decrypted (the "Waiting for this message" bubbles, `messageStubType`
  CIPHERTEXT) are served as `[waiting for this message]` rows. The phone
  answers type 3 for any request anchored on the oldest of a run of them,
  whatever id or timestamp is sent, so nothing older than the placeholders
  can be paged in. `coverage` marks such a chat as never `complete` and says
  so in its `note`. Verified on 2026-10-26: one 1:1 chat stops at six
  2023-05-15 placeholders (msgOrderID 292, so about 291 older messages
  exist on the phone, back to 2022).

**Fallback when the phone will not serve it:** on the phone, open the chat →
contact name → *Export chat* → *Without media*, and save/share the `.txt`
somewhere readable (Files, Drive, email to self); read that file directly.
For a handful of messages, forward them to yourself ("Message yourself"),
then `wa pull` and `wa read <own number>`.

1. **Chat-history sync paused on the phone** → fixable. `wa backfill`,
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

- **Single connection at a time.** A file lock prevents two `wa`
  processes from connecting simultaneously; the second one blocks until
  the first finishes. This includes `backfill`, which holds it for its whole
  window — don't try to `read --limit` in parallel, and stub
  `wa.cache.connection_lock` in tests so the suite never waits on a live
  session.
- **No daemon.** Messages only land locally when you run `wa pull`
  (or any other connecting command). There's no push. Consequence worth
  internalising: the phone only serves chat history while a companion is
  *concurrently* connected, so any window this CLI opens is a few seconds
  wide unless you ask for one explicitly (`wa backfill`).
- **`(unnamed)` chats are normal initially.** WhatsApp's app-state
  doesn't ship contact labels to linked devices; run
  `wa import-contacts` (macOS) or let group-info backfill resolve
  them via `wa login`.
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
