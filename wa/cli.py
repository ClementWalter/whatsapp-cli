"""WhatsApp user-level CLI.

Subcommands: ``status``, ``login``, ``chats``, ``read``, ``ingest``,
``import-contacts``. Wired into the ``wa`` console script via the entry
point declared in ``pyproject.toml``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import click

from wa.clientpayload import build_login_payload, build_registration_payload, device_label
from wa.handshake import do_handshake
from wa.pair import (
    build_pair_device_ack,
    extract_pair_refs,
    handle_pair_success,
    make_qr_payload,
    render_qr_ansi,
)
from wa.pbutil import decode_fields, summarize
from wa.prekeys import INITIAL_UPLOAD_COUNT, build_upload_iq, generate_prekey_batch
from wa.signal import SignalSession
from wa.store import DEFAULT_DEVICE_PATH, Device
from wa.transport.framesocket import FrameSocket
from wa.wabinary import JID, Node, decode_node, encode_node


def _pretty(node: Node, indent: int = 0) -> str:
    """Format a Node as an indented tree for debug dumps."""
    pad = "  " * indent
    parts = [f"{pad}<{node.tag}"]
    for k, v in node.attrs.items():
        parts.append(f" {k}={v!r}")
    if node.content is None:
        parts.append("/>")
        return "".join(parts)
    parts.append(">")
    if isinstance(node.content, (bytes, bytearray)):
        parts.append(f" [{len(node.content)} bytes]")
        parts.append(f"</{node.tag}>")
        return "".join(parts)
    lines = ["".join(parts)]
    for child in node.content:
        lines.append(_pretty(child, indent + 1))
    lines.append(f"{pad}</{node.tag}>")
    return "\n".join(lines)


async def _send_presence_available(fs, ns, device: Device) -> None:
    """Announce the companion as online on every connect.

    Without this stanza, the server treats the device as a passive
    background companion: the active-IQ flips the routing flag but
    never advances the phone's "last activity" timestamp or resets
    the ~14-day linked-device retention timer. A device that only
    ever does Noise + active-IQ + drain looks dormant and gets its
    offline mailbox purged. Sending presence once per connect is
    what real clients (whatsmeow, web.whatsapp.com) do and is what
    keeps the link healthy across long-running CLI usage.

    ``name`` is the public display name the server caches for this
    device. We prefer the persisted ``push_name`` (set if the user
    or app-state sync has populated it); otherwise we use the same
    host-derived label as the companion's ``os`` field so the two
    stay consistent on the phone's UI.
    """
    name = device.push_name or device_label()
    presence = Node(
        tag="presence",
        attrs={"name": name, "type": "available"},
    )
    try:
        await fs.send(ns.encrypt_frame(encode_node(presence)))
    except Exception as e:
        # Presence is best-effort housekeeping — never fail the user
        # action over it.
        logging.getLogger("login").debug("presence send failed: %s", e)


def _setup_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@click.group()
@click.option("--debug", is_flag=True, help="Verbose logging")
@click.pass_context
def cli(ctx: click.Context, debug: bool) -> None:
    _setup_logging(debug)
    ctx.ensure_object(dict)


@cli.command()
@click.option(
    "--download",
    is_flag=True,
    help="Also download CDN-hosted history chunks via media key/directPath",
)
def ingest(download: bool) -> None:
    """Process saved HistorySyncNotification blobs into the local cache.

    Walks ``~/.cache/whatsapp-cli/blobs/*-protocolMessage-*.bin`` and
    extracts conversations, pushnames, and messages into ``store/`` as
    JSON / JSONL. Run this once after a fresh ``login`` got the bootstrap
    history, then read any chat with the ``read`` command.
    """
    from wa.cache import (
        CachedMessage,
        append_messages,
        known_message_ids,
        load_chats,
        load_contacts,
        load_lidmap,
        save_chats,
        save_contacts,
        save_lidmap,
        upsert_chat,
    )
    from wa.history import (
        conversation_name,
        decode_history_sync_notification,
        iter_conversation_messages,
        parse_history_sync,
    )

    blob_dir = Path.home() / ".cache" / "whatsapp-cli" / "blobs"
    pm_files = sorted(blob_dir.glob("*-protocolMessage-*.bin"))
    if not pm_files:
        click.echo(f"no protocolMessage blobs in {blob_dir}", err=True)
        return

    chats = load_chats()
    contacts = load_contacts()
    lidmap = load_lidmap()
    seen_ids = known_message_ids()
    new_messages: list = []
    new_chats = 0
    new_contacts = 0
    new_lid = 0
    skipped_dup = 0

    for pm_path in pm_files:
        try:
            pm = decode_fields(pm_path.read_bytes())
        except Exception as e:
            click.echo(f"  {pm_path.name}: unparseable ({e})", err=True)
            continue
        hsn_raw = pm.get(6, [None])[0]
        if not isinstance(hsn_raw, (bytes, bytearray)):
            continue
        hsn = decode_history_sync_notification(bytes(hsn_raw))
        payload = None
        if isinstance(hsn.get("inline_payload"), (bytes, bytearray)):
            payload = bytes(hsn["inline_payload"])
        elif isinstance(hsn.get("direct_path"), (bytes, bytearray)) and download:
            try:
                from wa.media import MediaRef, download_and_decrypt_history

                ref = MediaRef(
                    direct_path=bytes(hsn["direct_path"]).decode("utf-8"),
                    media_key=bytes(hsn["media_key"] or b""),
                    file_enc_sha256=bytes(hsn["file_enc_sha256"] or b""),
                )
                payload = download_and_decrypt_history(ref)
            except Exception as e:
                click.echo(f"  {pm_path.name}: CDN download failed ({e})", err=True)
                continue
        if payload is None:
            continue

        try:
            hs = parse_history_sync(payload)
        except Exception as e:
            click.echo(f"  {pm_path.name}: parse failed ({e})", err=True)
            continue

        # Pushname sync — fold into contacts.
        for jid, pushname in hs["pushnames"]:
            if jid and pushname and contacts.get(jid) != pushname:
                contacts[jid] = pushname
                new_contacts += 1
        # LID → phone-number mapping (from history field 15). Index by the
        # local part of the LID since that's what we store as message sender.
        for lid_jid, pn_jid in hs.get("lid_to_pn", []):
            local = lid_jid.split("@")[0]
            if local and pn_jid and lidmap.get(local) != pn_jid:
                lidmap[local] = pn_jid
                new_lid += 1

        for conv_bytes in hs["conversations"]:
            chat_jid, name, last_ts = conversation_name(conv_bytes)
            if not chat_jid:
                continue
            if chat_jid not in chats:
                new_chats += 1
            existing = chats.get(chat_jid, {})
            chats[chat_jid] = {
                "name": name or existing.get("name", ""),
                "last_ts": max(existing.get("last_ts", 0), last_ts),
            }
            for m in iter_conversation_messages(conv_bytes):
                if m.msg_id and m.msg_id in seen_ids:
                    skipped_dup += 1
                    continue
                seen_ids.add(m.msg_id)
                new_messages.append(
                    CachedMessage(
                        ts=m.timestamp,
                        chat=m.chat_jid,
                        sender=m.sender_jid,
                        sender_name=m.sender_name,
                        text=m.text,
                        from_me=m.from_me,
                        msg_id=m.msg_id,
                    )
                )

    save_chats(chats)
    save_contacts(contacts)
    save_lidmap(lidmap)
    append_messages(new_messages)
    click.echo(
        click.style(
            f"ingested: +{len(new_messages)} messages "
            f"(+{new_chats} chats, +{new_contacts} contacts, "
            f"+{new_lid} lid mappings, skipped {skipped_dup} dup)",
            fg="green",
        ),
        err=True,
    )


@cli.command(name="import-contacts")
def import_contacts_cmd() -> None:
    """Pull names from macOS Contacts.app and merge into the local cache.

    macOS Contacts (iCloud-synced from iPhone) stores the address-book
    labels for everyone in your phone's address book. WhatsApp's protocol
    doesn't transmit those labels to linked devices, so this command bridges
    that gap by reading them via Apple's Contacts framework.
    """
    from wa.cache import load_contacts, save_contacts
    from wa.macos_contacts import dump_macos_contacts

    click.echo("dumping macOS Contacts (Swift)...", err=True)
    book = dump_macos_contacts()
    click.echo(f"  {len(book)} unique numbers in Contacts", err=True)
    contacts = load_contacts()
    new_count = 0
    for digits, name in book.items():
        jid_key = f"{digits}@s.whatsapp.net"
        if contacts.get(jid_key) != name:
            contacts[jid_key] = name
            new_count += 1
    save_contacts(contacts)
    click.echo(click.style(f"merged {new_count} new names into contacts", fg="green"), err=True)


@cli.command(name="migrate")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would change without writing anything to disk.",
)
def migrate_cmd(dry_run: bool) -> None:
    """Merge duplicate chats from the LID/PN identity migration.

    WhatsApp's 2024 rollout routes most DMs through ``@lid`` privacy
    identifiers instead of ``@s.whatsapp.net`` phone-number JIDs, leaving
    the same conversation split between two cache entries. WhatsApp Web
    fuses them transparently using a server-maintained mapping; this CLI
    used to show both halves as separate rows. This command does the same
    fuse locally: it rewrites ``chats.json``, ``messages.jsonl``, and
    ``contacts.json`` so every conversation appears under a single
    canonical JID.

    Two cases are merged:

    1. Polluted rows — a now-fixed bug stored ``<lid>@s.whatsapp.net``.
       The local part for these is >13 digits (not a real phone number);
       they get rewritten to ``<lid>@lid``.
    2. Genuine PN/LID twins — a contact you had before the migration
       has both a ``<phone>@s.whatsapp.net`` entry and a ``<lid>@lid``
       entry. The inverted ``lidmap.json`` resolves which is which; the
       PN side gets folded into the LID side.

    Idempotent and safe to re-run.
    """
    import time as _time

    from wa.cache import (
        MESSAGES_PATH,
        canonical_jid,
        load_chats,
        load_contacts,
        load_lidmap,
        save_chats,
        save_contacts,
    )

    chats = load_chats()
    contacts = load_contacts()
    lidmap = load_lidmap()
    # Invert lidmap once so the canonical-JID helper doesn't re-read it
    # for every entry.
    pn_to_lid_user = {pn_jid: lid_user for lid_user, pn_jid in lidmap.items()}
    canonical = lambda j: canonical_jid(j, lidmap_inverted=pn_to_lid_user)

    # --- chats.json ----------------------------------------------------
    new_chats: dict[str, dict] = {}
    chat_renames: dict[str, str] = {}  # old → new
    for jid, info in chats.items():
        target = canonical(jid)
        if target != jid:
            chat_renames[jid] = target
        existing = new_chats.get(target)
        if existing is None:
            new_chats[target] = dict(info)
        else:
            # Merge: pick the higher last_ts, the first non-empty name.
            existing["last_ts"] = max(
                existing.get("last_ts", 0), info.get("last_ts", 0)
            )
            if not existing.get("name") and info.get("name"):
                existing["name"] = info["name"]

    # --- contacts.json -------------------------------------------------
    new_contacts: dict[str, str] = {}
    contact_renames = 0
    for jid, name in contacts.items():
        target = canonical(jid)
        if target != jid:
            contact_renames += 1
        # First write wins (typically the @lid entry, which we want as
        # canonical); the other writes are duplicate-but-equal names.
        new_contacts.setdefault(target, name)

    # --- messages.jsonl ------------------------------------------------
    msgs_rewritten = 0
    msgs_total = 0
    new_msgs_text = []
    if MESSAGES_PATH.exists():
        with MESSAGES_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                msgs_total += 1
                try:
                    obj = json.loads(line)
                except Exception:
                    new_msgs_text.append(line)
                    continue
                changed = False
                for key in ("chat", "sender"):
                    if key in obj and isinstance(obj[key], str):
                        new_v = canonical(obj[key])
                        if new_v != obj[key]:
                            obj[key] = new_v
                            changed = True
                if changed:
                    msgs_rewritten += 1
                new_msgs_text.append(json.dumps(obj, ensure_ascii=False))

    summary = (
        f"chats:    {len(chats)} → {len(new_chats)} "
        f"({len(chat_renames)} renames, {len(chats) - len(new_chats)} merges)\n"
        f"contacts: {len(contacts)} → {len(new_contacts)} ({contact_renames} renames)\n"
        f"messages: {msgs_total} total, {msgs_rewritten} rewritten"
    )

    if dry_run:
        click.echo(click.style("dry-run: would apply\n", fg="yellow") + summary)
        # Show a handful of example renames so the user can sanity-check.
        if chat_renames:
            click.echo("\nexample chat merges:")
            for old, new in list(chat_renames.items())[:5]:
                click.echo(f"  {old}  →  {new}")
        return

    save_chats(new_chats)
    save_contacts(new_contacts)
    if MESSAGES_PATH.exists():
        # Atomic rewrite: write to .tmp then rename. Keeps a .bak in case
        # something looks wrong after — easy rollback.
        backup = MESSAGES_PATH.with_suffix(f".jsonl.bak.{int(_time.time())}")
        MESSAGES_PATH.rename(backup)
        with MESSAGES_PATH.open("w", encoding="utf-8") as f:
            f.write("\n".join(new_msgs_text))
            if new_msgs_text:
                f.write("\n")
        click.echo(f"backup written to {backup.name}")
    click.echo(click.style("done.\n", fg="green") + summary)


@cli.command()
@click.option("--limit", type=int, default=20, help="Max chats to show")
@click.option("--json", "json_out", is_flag=True, help="Emit JSON")
def chats(limit: int, json_out: bool) -> None:
    """List known chats sorted by most recent activity."""
    from wa.cache import load_chats, load_contacts

    contacts = load_contacts()
    items = sorted(load_chats().items(), key=lambda kv: kv[1].get("last_ts", 0), reverse=True)
    items = items[:limit]

    def _display_name(jid: str, info: dict) -> str:
        # Groups always have an app-state name. DMs don't — WhatsApp doesn't
        # sync address-book labels to linked devices, so fall back to whatever
        # `import-contacts` or pushname syncs wrote into contacts.json.
        if name := (info.get("name") or contacts.get(jid)):
            return name
        # Legacy rows: a now-fixed bug used to store @lid peers as
        # `<lid>@s.whatsapp.net`. Phone numbers are <=13 digits; a longer
        # local part is almost certainly a LID. Probe contacts under the
        # correct @lid key so historical chats still resolve.
        local = jid.split("@")[0]
        if jid.endswith("@s.whatsapp.net") and local.isdigit() and len(local) > 13:
            if name := contacts.get(f"{local}@lid"):
                return name
        return "(unnamed)"

    if json_out:
        click.echo(
            json.dumps(
                [{"jid": j, **info, "display_name": _display_name(j, info)} for j, info in items],
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    from datetime import datetime

    for jid, info in items:
        ts = info.get("last_ts", 0)
        ts_s = datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "—"
        kind = "group" if jid.endswith("@g.us") else "dm   "
        name = _display_name(jid, info)
        click.echo(f"{ts_s}  {kind}  {name:<30s}  {jid}")


@cli.command()
@click.argument("query")
@click.option("--limit", type=int, default=20, help="Max messages to show")
@click.option("--json", "json_out", is_flag=True, help="Emit JSON")
@click.option(
    "--no-extend",
    is_flag=True,
    help="Don't auto-fetch more history from your phone if the cache is short",
)
@click.option(
    "--match",
    "match_index",
    type=int,
    default=None,
    help="When the query is ambiguous, pick the Nth match (1-based) instead "
    "of the first. Use after seeing the ambiguity list.",
)
def read(
    query: str,
    limit: int,
    json_out: bool,
    no_extend: bool,
    match_index: int | None,
) -> None:
    """Show recent messages from a chat (matched by name or JID substring).

    Auto-extends: if the local cache has fewer than ``--limit`` messages for
    the chat, the CLI connects to WhatsApp, asks your phone for more
    history, ingests the response, and then displays. ``--no-extend`` skips
    the network round-trip and shows whatever's cached.

    \b
    Examples:
      wa read famille
      wa read 33123456789 --limit 50 --json
      wa read famille --no-extend                 # offline, fast
      wa read alice --match 2                     # pick 2nd of ambiguous matches
      wa read 16677492244589@lid                  # exact JID — never ambiguous
    """
    from wa.cache import find_chat, iter_messages, load_chats, load_contacts, load_lidmap

    matches = find_chat(query)
    if not matches:
        click.echo(f"no chat matching {query!r}", err=True)
        raise SystemExit(1)
    exact_name = any(
        info.get("name", "").lower() == query.lower() for _, info in matches
    )
    if match_index is not None:
        if match_index < 1 or match_index > len(matches):
            click.echo(
                f"--match {match_index} out of range: {len(matches)} matches for {query!r}",
                err=True,
            )
            raise SystemExit(1)
        chat_jid, chat_info = matches[match_index - 1]
    elif len(matches) > 1 and not exact_name:
        # List every match (numbered) so the user can re-run with --match N
        # or copy a full JID. Substring match against the JID also picks a
        # single chat, so the message tells them both options.
        lines = [
            f"  [{i + 1}] {j}  {info.get('name') or ''}".rstrip()
            for i, (j, info) in enumerate(matches)
        ]
        click.echo(
            f"ambiguous: {len(matches)} chats match {query!r}. Showing first.\n"
            + "\n".join(lines)
            + "\n  → rerun with `--match N` or pass a full JID like "
            + matches[1][0],
            err=True,
        )
        chat_jid, chat_info = matches[0]
    else:
        chat_jid, chat_info = matches[0]

    # Auto-extend: count cached messages for this chat; if short, fetch more.
    if not no_extend and limit > 0:
        cached = sum(1 for m in iter_messages() if m.chat == chat_jid)
        if cached < limit:
            need = limit - cached + 5  # margin so next read may not need to refetch
            click.echo(
                click.style(
                    f"cache has {cached}/{limit}; fetching {need} more from your phone...",
                    fg="yellow",
                ),
                err=True,
            )
            try:
                from wa.cache import connection_lock

                with connection_lock():
                    added = asyncio.run(_extend_chat(chat_jid, need))
                if added > 0:
                    click.echo(
                        click.style(f"fetched +{added} new messages", fg="green"),
                        err=True,
                    )
                else:
                    click.echo(
                        click.style(
                            "phone didn't reply — likely chat-history sync is paused on your phone\n"
                            "  (WhatsApp on phone → Settings → Linked Devices → resume Chat History sync)\n"
                            "  showing what's cached.",
                            fg="yellow",
                        ),
                        err=True,
                    )
            except Exception as e:
                click.echo(
                    click.style(f"fetch failed (showing cached only): {e}", fg="red"),
                    err=True,
                )
    contacts = load_contacts()
    chats_idx = load_chats()
    lidmap = load_lidmap()

    def _candidates(jid: str) -> list[str]:
        """Variations to try when looking up a sender across our caches.

        Cached data uses three formats that don't always agree:
          - bare ``<local>@lid``
          - agent-tagged ``<local>.1@lid`` (whatsmeow JID stringification)
          - bare ``<local>`` (just the LID number)
        """
        local = jid.split("@")[0].split(".")[0].split(":")[0]
        out = [jid, local]
        if "@lid" in jid or jid.isdigit():
            out += [f"{local}@lid", f"{local}.1@lid"]
        if "@s.whatsapp.net" in jid:
            out.append(local)
        return out

    def _format_pn(pn_local: str) -> str:
        """Render a phone-number local part as a tappable-ish display.

        ``33608652084`` → ``+33 6 08 65 20 84``. Falls back to ``+<digits>``
        for non-French numbers. Better than a 15-digit LID for the eye.
        """
        if not pn_local.isdigit():
            return pn_local
        if pn_local.startswith("33") and len(pn_local) == 11:
            cc, rest = pn_local[:2], pn_local[2:]
            return f"+{cc} {rest[0]} {rest[1:3]} {rest[3:5]} {rest[5:7]} {rest[7:9]}"
        return f"+{pn_local}"

    def resolve(jid: str, fallback: str) -> str:
        # 1. Direct lookup across all known formats.
        for k in _candidates(jid):
            if name := contacts.get(k):
                return name
        # 2. LID → PN translation, then retry contacts/chats with the PN.
        local = jid.split("@")[0].split(".")[0].split(":")[0]
        pn_local = local
        if jid.endswith("@lid") or "@" not in jid or "@lid" in jid:
            pn = lidmap.get(local)
            if pn:
                if name := contacts.get(pn):
                    return name
                if (dm := chats_idx.get(pn)) and dm.get("name"):
                    return dm["name"]
                # Use the phone-number local part for a cleaner fallback.
                pn_local = pn.split("@")[0]
        # 3. Address-book name from this exact JID's DM chat (set by user
        #    via the WhatsApp app's "saved as ..." mechanism).
        if (dm := chats_idx.get(jid)) and dm.get("name"):
            return dm["name"]
        # 4. Message-time pushName.
        if fallback:
            return fallback
        # 5. Pretty phone number (best we can do without an address book).
        return _format_pn(pn_local)

    rows = [m for m in iter_messages() if m.chat == chat_jid]
    rows.sort(key=lambda m: m.ts)
    rows = rows[-limit:] if limit > 0 else rows

    if json_out:
        click.echo(
            json.dumps(
                {
                    "chat": chat_jid,
                    "name": chat_info.get("name", ""),
                    "messages": [
                        {
                            "ts": m.ts,
                            "sender": m.sender,
                            "sender_name": resolve(m.sender, m.sender_name),
                            "text": m.text,
                            "from_me": m.from_me,
                            "msg_id": m.msg_id,
                        }
                        for m in rows
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    from datetime import datetime

    chat_label = chat_info.get("name") or chat_jid
    click.echo(click.style(f"# {chat_label}  ({chat_jid}) — {len(rows)} messages", fg="green"))
    for m in rows:
        ts = datetime.fromtimestamp(m.ts).strftime("%Y-%m-%d %H:%M")
        who = "me" if m.from_me else resolve(m.sender, m.sender_name)
        text = m.text or "[non-text]"
        click.echo(f"{ts}  {who}: {text}")


@cli.command()
def status() -> None:
    """Show whether we have a paired device on disk."""
    dev = Device.load()
    if dev is None:
        click.echo(f"not logged in (no device at {DEFAULT_DEVICE_PATH})")
        return
    if dev.is_paired():
        click.echo(f"paired as {dev.jid} ({dev.push_name or '?'}) on {dev.platform}")
    else:
        click.echo("keys generated but not paired — run `login` to scan a QR")
        return

    from wa.cache import load_sync_state

    sync_state = load_sync_state()
    last_ts = sync_state.get("last_sync_ts", 0)
    if last_ts:
        import time as _time
        from datetime import datetime

        age_s = int(_time.time() - last_ts)
        if age_s < 60:
            age = f"{age_s}s ago"
        elif age_s < 3600:
            age = f"{age_s // 60}m ago"
        elif age_s < 86400:
            age = f"{age_s // 3600}h ago"
        else:
            age = f"{age_s // 86400}d ago"
        when = datetime.fromtimestamp(last_ts).strftime("%Y-%m-%d %H:%M")
        click.echo(f"last sync: {when} ({age}, {sync_state.get('last_frames', 0)} frames)")
    else:
        click.echo("last sync: never — run `wa sync`")


@cli.command()
@click.option(
    "--reset", is_flag=True, help="Discard any existing device state and start fresh"
)
def login(reset: bool) -> None:
    """Connect to WhatsApp and pair via QR (scan with your phone)."""
    dev = None if reset else Device.load()
    if dev is None or reset:
        click.echo("generating fresh device keys...")
        dev = Device.new()
        dev.save()
    from wa.cache import connection_lock

    with connection_lock():
        asyncio.run(_login_async(dev))


@cli.command()
@click.option(
    "--seconds",
    type=float,
    default=120.0,
    show_default=True,
    help="Hard cap on total drain time, for pathological queues.",
)
@click.option(
    "--idle",
    type=float,
    default=3.0,
    show_default=True,
    help="Stop after this many seconds of silence — i.e. when the offline "
    "queue is empty and we're caught up to 'latest sync'.",
)
@click.option(
    "--refresh-groups",
    is_flag=True,
    help="Also re-query every cached group's participant list (slow: "
    "~150ms per group, capped by WA_GROUPINFO_CAP).",
)
def sync(seconds: float, idle: float, refresh_groups: bool) -> None:
    """Reconnect with existing keys and ingest queued messages.

    The CLI has no daemon: every minute you're not connected, the server
    queues new messages and ``chats`` shows stale activity timestamps.
    ``sync`` opens the WebSocket; the server replays everything queued
    since the last connection; the listener decrypts each ``<message>``
    via the stored Signal sessions and writes to ``chats.json`` /
    ``messages.jsonl``. Exits as soon as the server goes quiet for
    ``--idle`` seconds (default 3) — so a quick check is ~3s, a long
    catch-up runs until the queue drains. No QR; device must already be
    paired.
    """
    dev = Device.load()
    if dev is None or not dev.is_paired():
        click.echo("not paired — run `login` first", err=True)
        raise SystemExit(1)
    from wa.cache import connection_lock

    with connection_lock():
        asyncio.run(
            _login_handshake(
                dev, seconds=seconds, idle=idle, fetch_groups=refresh_groups
            )
        )


@cli.command()
@click.argument("peer")
@click.argument("text")
def send(peer: str, text: str) -> None:
    """Send a text message to a 1:1 chat.

    ``peer`` is a name (resolved against your cached chats, same matcher
    as ``wa read``) or a full JID like ``33123456789@s.whatsapp.net``.
    For first-contact sends, pre-key bundle fetch is wired up — no setup
    required as long as you've authenticated.

    \b
    Examples:
      wa send 33123456789@s.whatsapp.net "test from CLI"
      wa send pierre "hi"
    """
    dev = Device.load()
    if dev is None or not dev.is_paired():
        click.echo("not paired — run `login` first", err=True)
        raise SystemExit(1)

    from wa.cache import find_chat

    matches = find_chat(peer)
    if not matches and "@" in peer:
        # Bare JID — accept as-is.
        peer_jid = peer
    elif not matches:
        click.echo(f"no chat matching {peer!r}", err=True)
        raise SystemExit(1)
    else:
        # Prefer exact name match; otherwise pick the most recent.
        exact = [m for m in matches if m[1].get("name", "").lower() == peer.lower()]
        peer_jid = (exact[0][0] if exact else matches[0][0])
        if len(matches) > 1 and not exact:
            click.echo(
                f"ambiguous: {len(matches)} matches; sending to most recent "
                f"({peer_jid}). Pass a full JID to be explicit.",
                err=True,
            )

    from wa.cache import connection_lock

    with connection_lock():
        if peer_jid.endswith("@g.us"):
            asyncio.run(_send_group_async(dev, peer_jid, text))
        else:
            asyncio.run(_send_async(dev, peer_jid, text))


def _phash(devices: list[JID]) -> str:
    """Compute the participant-list hash whatsmeow calls ``phash``.

    The server uses this to verify the sender's device-list view is
    current — a mismatch means a device was added/removed since our
    last usync, and the message will be rejected with `phash-mismatch`.
    Format: ``"2:" + base64(sha256(sorted_ad_strings_concatenated)[:6])``,
    standard base64 with padding stripped. Must match whatsmeow's
    ``participantListHashV2`` byte-for-byte.
    """
    import base64
    import hashlib

    ad_strs = sorted(d.ad_string() for d in devices)
    digest = hashlib.sha256("".join(ad_strs).encode()).digest()[:6]
    return "2:" + base64.b64encode(digest).decode().rstrip("=")


async def _fetch_prekey_bundles(
    fs: FrameSocket, ns, devices: list[JID]
) -> dict[str, dict]:
    """Fetch X3DH prekey bundles for a set of device JIDs.

    Returns ``{ad_string: {registration_id, identity_key, signed_pre_key_*,
    pre_key_*}}``. Each entry is what
    :func:`SignalSession.install_prekey_bundle` consumes. Devices missing
    from the response (server error or unknown) are simply absent from
    the dict — the caller skips them.

    Schema mirrors whatsmeow's ``fetchPreKeys``:

    .. code-block:: xml

        <iq xmlns="encrypt" type="get" to="s.whatsapp.net">
          <key>
            <user jid="<device-AD-jid>" reason="identity"/>
            ...
          </key>
        </iq>

    The reply has ``<list><user jid=...><registration/><identity/>
    <skey><id/><value/><signature/></skey>[<key><id/><value/></key>]</user></list>``.
    """
    import secrets

    iq_id = f"prekey-{secrets.token_hex(4)}"
    iq = Node(
        tag="iq",
        attrs={
            "to": JID(server="s.whatsapp.net"),
            "type": "get",
            "id": iq_id,
            "xmlns": "encrypt",
        },
        content=[
            Node(
                tag="key",
                content=[
                    Node(tag="user", attrs={"jid": d, "reason": "identity"})
                    for d in devices
                ],
            )
        ],
    )
    await fs.send(ns.encrypt_frame(encode_node(iq)))

    deadline = asyncio.get_event_loop().time() + 10.0
    while asyncio.get_event_loop().time() < deadline:
        try:
            ct = await fs.recv(
                timeout=max(0.1, deadline - asyncio.get_event_loop().time())
            )
        except (asyncio.TimeoutError, ConnectionError):
            break
        try:
            node = decode_node(ns.decrypt_frame(ct))
        except Exception:
            continue
        if node.tag != "iq" or node.attrs.get("id") != iq_id:
            continue
        if node.attrs.get("type") == "error":
            err = node.get_child_by_tag("error")
            raise RuntimeError(
                f"prekey IQ rejected: {err.attrs if err else node.attrs}"
            )
        lst = node.get_child_by_tag("list")
        if lst is None:
            return {}
        out: dict[str, dict] = {}
        for user_node in lst.get_children():
            if user_node.tag != "user":
                continue
            user_jid = user_node.attrs.get("jid")
            if not isinstance(user_jid, JID):
                continue
            try:
                bundle = _parse_prekey_user_node(user_node)
            except Exception as e:
                logging.getLogger("send").warning(
                    "couldn't parse bundle for %s: %s", user_jid, e
                )
                continue
            out[user_jid.ad_string()] = bundle
        return out
    raise TimeoutError("prekey IQ reply timeout")


def _parse_prekey_user_node(user_node: Node) -> dict:
    """Extract a single bundle from one ``<user>`` child of the IQ reply."""
    import struct

    def _bytes(tag: str) -> bytes | None:
        child = user_node.get_child_by_tag(tag)
        if child is None:
            return None
        c = child.content
        if isinstance(c, (bytes, bytearray)):
            return bytes(c)
        return None

    def _key_node_to_record(child: Node | None) -> dict | None:
        if child is None:
            return None
        id_node = child.get_child_by_tag("id")
        value_node = child.get_child_by_tag("value")
        if id_node is None or value_node is None:
            return None
        id_bytes = id_node.content if isinstance(id_node.content, (bytes, bytearray)) else b""
        pub = value_node.content if isinstance(value_node.content, (bytes, bytearray)) else b""
        if not id_bytes or not pub:
            return None
        # WhatsApp encodes key IDs as 3-byte BE; pad to 4 for struct.
        key_id = struct.unpack(">I", b"\x00" + bytes(id_bytes))[0]
        sig_node = child.get_child_by_tag("signature")
        sig = None
        if sig_node is not None and isinstance(sig_node.content, (bytes, bytearray)):
            sig = bytes(sig_node.content)
        return {"key_id": key_id, "pub": bytes(pub), "sig": sig}

    reg_bytes = _bytes("registration") or b"\x00\x00\x00\x00"
    if len(reg_bytes) != 4:
        raise ValueError(f"bad registration length {len(reg_bytes)}")
    registration_id = struct.unpack(">I", reg_bytes)[0]

    identity = _bytes("identity")
    if identity is None or len(identity) != 32:
        raise ValueError("missing/invalid identity")

    # Older responses inline the keys; newer ones wrap them in `<keys>`.
    keys_container = user_node.get_child_by_tag("keys") or user_node
    skey = _key_node_to_record(keys_container.get_child_by_tag("skey"))
    if skey is None or skey["sig"] is None:
        raise ValueError("missing or unsigned skey")
    opk = _key_node_to_record(keys_container.get_child_by_tag("key"))

    return {
        "registration_id": registration_id,
        "identity_key_pub": identity,
        "signed_pre_key_id": skey["key_id"],
        "signed_pre_key_pub": skey["pub"],
        "signed_pre_key_signature": skey["sig"],
        "pre_key_id": opk["key_id"] if opk else None,
        "pre_key_pub": opk["pub"] if opk else None,
    }


async def _usync_devices(
    fs: FrameSocket, ns, queried: list[JID]
) -> list[JID]:
    """Query the server for every JID's device list.

    Returns the union of every ``<device-list><device id="N"/></device-list>``,
    as fully-specified AD JIDs (with the right ``device`` field set).
    Mirrors whatsmeow's ``GetUserDevices``.
    """
    import secrets

    sid = secrets.token_hex(8)
    iq_id = f"usync-dev-{sid[:6]}"
    iq = Node(
        tag="iq",
        attrs={
            "to": JID(server="s.whatsapp.net"),
            "type": "get",
            "id": iq_id,
            "xmlns": "usync",
        },
        content=[
            Node(
                tag="usync",
                attrs={
                    "sid": sid,
                    "mode": "query",
                    "last": "true",
                    "index": "0",
                    "context": "message",
                },
                content=[
                    Node(
                        tag="query",
                        content=[Node(tag="devices", attrs={"version": "2"})],
                    ),
                    Node(
                        tag="list",
                        content=[Node(tag="user", attrs={"jid": j}) for j in queried],
                    ),
                ],
            )
        ],
    )
    await fs.send(ns.encrypt_frame(encode_node(iq)))
    deadline = asyncio.get_event_loop().time() + 8.0
    while asyncio.get_event_loop().time() < deadline:
        try:
            ct = await fs.recv(timeout=max(0.1, deadline - asyncio.get_event_loop().time()))
        except (asyncio.TimeoutError, ConnectionError):
            break
        try:
            node = decode_node(ns.decrypt_frame(ct))
        except Exception:
            continue
        if node.tag != "iq" or node.attrs.get("id") != iq_id:
            continue
        if node.attrs.get("type") == "error":
            err = node.get_child_by_tag("error")
            raise RuntimeError(f"usync rejected: {err.attrs if err else node.attrs}")
        lst = node.get_child_by_tag("usync", "list")
        if lst is None:
            return []
        out: list[JID] = []
        for user_node in lst.get_children():
            if user_node.tag != "user":
                continue
            user_jid = user_node.attrs.get("jid")
            if not isinstance(user_jid, JID):
                continue
            dl = user_node.get_child_by_tag("devices", "device-list")
            if dl is None:
                continue
            for dev_node in dl.get_children():
                if dev_node.tag != "device":
                    continue
                try:
                    dev_id = int(dev_node.attrs.get("id", 0))
                except (TypeError, ValueError):
                    continue
                out.append(
                    JID(
                        user=user_jid.user,
                        server=user_jid.server,
                        agent=user_jid.agent,
                        device=dev_id,
                    )
                )
        return out
    raise TimeoutError("usync device query timed out")


def _build_message_plaintext(text: str) -> bytes:
    """Encode a minimal ``waE2E.Message{conversation: text}`` on the wire.

    Field 1 is the ``conversation`` string. We hand-roll the proto wire
    format rather than vendor a .proto, because this is the only outbound
    structure we currently produce.
    """
    from wa.peerreq import _string_field

    return _string_field(1, text)


def _build_dsm_plaintext(text: str, destination_jid: str) -> bytes:
    """Wrap a Message in ``deviceSentMessage`` for own-other-device fan-out.

    Schema: ``Message { deviceSentMessage = 31: DeviceSentMessage {
    destinationJID = 1: string; message = 2: Message } }``. Encrypted
    copy goes to every linked device of *yours* so they show the
    outgoing message in their UI.
    """
    from wa.peerreq import _length_delim, _string_field

    inner = _string_field(1, text)  # nested Message{conversation: text}
    dsm = _string_field(1, destination_jid) + _length_delim(2, inner)
    return _length_delim(31, dsm)


async def _send_async(device: Device, peer_jid: str, text: str) -> None:
    """Single-shot send: login, usync devices, encrypt per device, transmit, ack.

    Implements the same protocol whatsmeow uses for ``sendDM``:

    1. ``usync`` the peer + own JID → list of AD JIDs (every device).
    2. Encrypt the bare ``Message`` for each peer device, the wrapping
       ``deviceSentMessage`` for each of our own other devices. Skip
       the current device (this CLI) — we don't echo to ourselves.
    3. Build ``<message><participants><to jid=...><enc/></to>...</participants></message>``
       with the ``phash`` set so the server accepts our device-list view.
    4. Send and await ``<ack class="message" id="...">``.

    Devices for which we have no Signal session yet are skipped with a
    warning; pre-key bundle fetch isn't wired up. The peer's primary
    phone (device 0) almost always has a session after any prior `wa
    sync`, which is the only one that matters for delivery to the
    recipient's UI.
    """
    import secrets
    import time as _time

    from wa.cache import CachedMessage, append_messages, upsert_chat
    from wa.peerreq import pad_for_signal

    log = logging.getLogger("send")

    peer_local, _, peer_server = peer_jid.partition("@")
    if not peer_server:
        click.echo(f"malformed peer JID: {peer_jid!r}", err=True)
        raise SystemExit(1)

    own_pn = JID.parse(device.jid)
    own_lid = JID.parse(device.lid) if device.lid else None
    own_self_device = own_pn.device  # this CLI's device number

    # Resolve peer to PN + LID forms. For self-sends we use LID end-to-end
    # (matching what the phone itself does on its outgoing flow); for
    # peer sends we use PN addressing with LID encryption identity
    # (matching whatsmeow's behavior for non-self recipients).
    from wa.cache import load_lidmap

    lidmap = load_lidmap()
    if peer_server == "s.whatsapp.net":
        peer_pn_jid = JID(user=peer_local, server="s.whatsapp.net")
        peer_lid_user = next(
            (lu for lu, pn in lidmap.items() if pn == str(peer_pn_jid)),
            None,
        )
    elif peer_server == "lid":
        peer_lid_user = peer_local
        pn_str = lidmap.get(peer_local)
        peer_pn_jid = JID.parse(pn_str) if pn_str else None
    else:
        click.echo(f"unsupported recipient server {peer_server!r}", err=True)
        raise SystemExit(1)

    is_self_send = (
        peer_pn_jid is not None and peer_pn_jid.user == own_pn.user
    ) or (own_lid is not None and peer_lid_user == own_lid.user)

    if is_self_send and own_lid is not None:
        # End-to-end LID addressing: query LID, encrypt under LID, address
        # the stanza with LID. Mirrors what the phone does for self-sends.
        wire_to = JID(user=own_lid.user, server="lid")
        dsm_dest = str(wire_to)
    elif peer_pn_jid is not None:
        # Standard peer send: PN addressing, LID encryption identity.
        wire_to = peer_pn_jid
        dsm_dest = str(peer_pn_jid)
    else:
        click.echo(
            click.style(
                f"don't know the PN for {peer_jid} — only the LID is in lidmap.\n"
                f"  hint: this happens when you've never been in a group with the "
                f"peer (group participant lists carry both ids). For peer sends the "
                f"CLI currently needs a known PN.",
                fg="red",
            ),
            err=True,
        )
        raise SystemExit(1)

    msg_plaintext = _build_message_plaintext(text)
    dsm_plaintext = _build_dsm_plaintext(text, dsm_dest)

    async with FrameSocket() as fs:
        await fs.connect()
        try:
            ns = await do_handshake(fs, device, build_login_payload(device))
        except Exception as e:
            click.echo(click.style(f"handshake failed: {e}", fg="red"), err=True)
            raise SystemExit(1)
        while True:
            try:
                ct = await fs.recv(timeout=15.0)
            except (asyncio.TimeoutError, ConnectionError):
                click.echo(click.style("no <success/> received", fg="red"), err=True)
                raise SystemExit(1)
            node = decode_node(ns.decrypt_frame(ct))
            if node.tag == "success":
                break
            if node.tag == "failure":
                click.echo(
                    click.style(f"login rejected: {_pretty(node)}", fg="red"),
                    err=True,
                )
                raise SystemExit(1)

        active = Node(
            tag="iq",
            attrs={
                "to": JID(server="s.whatsapp.net"),
                "type": "set",
                "id": "send-active",
                "xmlns": "passive",
            },
            content=[Node(tag="active")],
        )
        await fs.send(ns.encrypt_frame(encode_node(active)))
        active_deadline = asyncio.get_event_loop().time() + 5.0
        while asyncio.get_event_loop().time() < active_deadline:
            try:
                ct = await fs.recv(timeout=0.5)
            except (asyncio.TimeoutError, ConnectionError):
                break
            n = decode_node(ns.decrypt_frame(ct))
            if n.tag == "iq" and n.attrs.get("id") == "send-active":
                break
        await _send_presence_available(fs, ns, device)

        # For self-sends we query usync with our LID JID so the response
        # is a list of LID-form devices — matching the LID-addressed
        # stanza we'll build below. For peer sends, query PN.
        if is_self_send and own_lid is not None:
            queried = [JID(user=own_lid.user, server="lid")]
        else:
            queried = [peer_pn_jid]
            if peer_pn_jid.user != own_pn.user:
                queried.append(JID(user=own_pn.user, server="s.whatsapp.net"))
        try:
            all_devices = await _usync_devices(fs, ns, queried)
        except Exception as e:
            click.echo(click.style(f"device list query failed: {e}", fg="red"), err=True)
            raise SystemExit(1)
        if not all_devices:
            click.echo(
                click.style(
                    "no devices returned — peer may have no WhatsApp account",
                    fg="red",
                ),
                err=True,
            )
            raise SystemExit(1)
        log.info("device list (PN form): %s", [str(d) for d in all_devices])

        signal = SignalSession(device)
        # Probe each device for an existing session; for any device that
        # has none, fetch a prekey bundle so the first encrypt becomes a
        # pkmsg via X3DH instead of failing with "session not found".
        no_session_devices: list[JID] = []
        for dev in all_devices:
            is_own = (
                (is_self_send and own_lid is not None and dev.user == own_lid.user)
                or (not is_self_send and dev.user == own_pn.user)
            )
            if is_own and dev.device == own_self_device:
                continue
            if is_self_send:
                enc_user = dev.user
            elif is_own and own_lid is not None:
                enc_user = own_lid.user
            elif (not is_own) and peer_lid_user is not None:
                enc_user = peer_lid_user
            else:
                enc_user = dev.user
            if not signal.has_session(enc_user, dev.device):
                no_session_devices.append(dev)
        if no_session_devices:
            log.info(
                "fetching prekey bundles for %d device(s) with no session",
                len(no_session_devices),
            )
            try:
                bundles = await _fetch_prekey_bundles(fs, ns, no_session_devices)
            except Exception as e:
                log.warning("prekey fetch failed: %s — some devices will be skipped", e)
                bundles = {}
            for dev in no_session_devices:
                bundle = bundles.get(dev.ad_string())
                if bundle is None:
                    log.debug("no bundle returned for %s", dev)
                    continue
                # Use the same encryption-identity rules as below.
                is_own = (
                    (is_self_send and own_lid is not None and dev.user == own_lid.user)
                    or (not is_self_send and dev.user == own_pn.user)
                )
                if is_self_send:
                    enc_user = dev.user
                elif is_own and own_lid is not None:
                    enc_user = own_lid.user
                elif (not is_own) and peer_lid_user is not None:
                    enc_user = peer_lid_user
                else:
                    enc_user = dev.user
                try:
                    signal.install_prekey_bundle(enc_user, dev.device, **bundle)
                    log.debug("installed bundle for %s:%d", enc_user, dev.device)
                except Exception as e:
                    log.warning(
                        "couldn't install bundle for %s:%d: %s",
                        enc_user, dev.device, e,
                    )

        participant_children: list[Node] = []
        missing_sessions: list[str] = []
        any_pkmsg = False
        for dev in all_devices:
            # "Own device" check: depends on which namespace we queried.
            # For self-sends we queried LID, so all devices have user ==
            # own_lid.user. For peer sends we queried PN.
            if is_self_send:
                is_own = own_lid is not None and dev.user == own_lid.user
            else:
                is_own = dev.user == own_pn.user
            if is_own and dev.device == own_self_device:
                continue
            plaintext = dsm_plaintext if is_own else msg_plaintext
            padded = pad_for_signal(plaintext)
            # Encryption identity. For self-sends the device list IS
            # already LID, so encrypt under dev.user directly. For peer
            # sends, dev.user is PN; translate to the peer's LID local
            # part (which is where active sessions are stored).
            if is_self_send:
                enc_user = dev.user
            elif is_own and own_lid is not None:
                enc_user = own_lid.user
            elif (not is_own) and peer_lid_user is not None:
                enc_user = peer_lid_user
            else:
                enc_user = dev.user
            try:
                ct, kind = signal.encrypt_msg(enc_user, dev.device, padded)
            except Exception as e:
                missing_sessions.append(f"{enc_user}:{dev.device}")
                log.debug("no session with %s:%d (%s)", enc_user, dev.device, e)
                continue
            if kind == "pkmsg":
                any_pkmsg = True
            # Wire identity matches what we'll address the stanza with:
            # LID for self-sends, PN for peer sends.
            participant_children.append(
                Node(
                    tag="to",
                    attrs={"jid": dev},
                    content=[
                        Node(tag="enc", attrs={"v": "2", "type": kind}, content=ct)
                    ],
                )
            )
        if not participant_children:
            click.echo(
                click.style(
                    "no encryptable devices — every target lacked a Signal session.\n"
                    "  hint: receive any message from the peer first (so their session "
                    "is bootstrapped), then retry. Pre-key bundle fetch for "
                    "first-contact sends is not yet implemented.",
                    fg="red",
                ),
                err=True,
            )
            raise SystemExit(1)
        if missing_sessions:
            log.warning(
                "skipped %d device(s) with no session: %s",
                len(missing_sessions), ", ".join(missing_sessions),
            )

        # The participant-list hash (`phash`) is computed for diagnostics
        # but deliberately NOT placed on the DM stanza — whatsmeow only
        # puts it on group sends, and including it on DMs makes the
        # server quietly drop the message. Same for `addressing_mode`,
        # which is a group-specific attribute.
        msg_id = "3EB0" + secrets.token_hex(9).upper()
        content_nodes: list[Node] = [
            Node(tag="participants", content=participant_children)
        ]
        if any_pkmsg and device.account:
            # When any per-device `<enc>` was a pkmsg (first message in
            # that session direction), the recipient needs our signed
            # ADV identity to verify the prekey signal message. The blob
            # is `ADVSignedDeviceIdentity`, captured during pairing.
            content_nodes.append(
                Node(tag="device-identity", content=device.account)
            )
        stanza = Node(
            tag="message",
            attrs={"to": wire_to, "type": "text", "id": msg_id},
            content=content_nodes,
        )
        await fs.send(ns.encrypt_frame(encode_node(stanza)))
        log.info(
            "sent text id=%s to=%s participants=%d any_pkmsg=%s",
            msg_id, wire_to, len(participant_children), any_pkmsg,
        )

        ack_deadline = asyncio.get_event_loop().time() + 15.0
        while asyncio.get_event_loop().time() < ack_deadline:
            try:
                ct = await fs.recv(
                    timeout=max(0.1, ack_deadline - asyncio.get_event_loop().time())
                )
            except (asyncio.TimeoutError, ConnectionError):
                break
            try:
                node = decode_node(ns.decrypt_frame(ct))
            except Exception:
                continue
            if node.tag == "ack" and node.attrs.get("id") == msg_id:
                # Canonicalize before persisting: a user-typed peer JID
                # may be in PN form even when canonical form is @lid.
                # Without this we'd write a duplicate row in chats.json.
                from wa.cache import canonical_jid

                cache_jid = canonical_jid(peer_jid)
                ts = int(_time.time())
                append_messages([
                    CachedMessage(
                        ts=ts,
                        chat=cache_jid,
                        sender=str(own_pn) if own_pn.user else "",
                        sender_name="me",
                        text=text,
                        from_me=True,
                        msg_id=msg_id,
                    )
                ])
                upsert_chat(cache_jid, last_ts=ts)
                click.echo(click.style(f"sent: {msg_id}", fg="green"))
                return
            log.debug("ignoring frame while waiting for ack: %s", node.tag)
        click.echo(
            click.style(
                "no ack within 15s — message may or may not have been queued",
                fg="yellow",
            ),
            err=True,
        )


async def _fetch_group_members(
    fs: FrameSocket, ns, group_jid: JID
) -> tuple[list[JID], str]:
    """Get the current participant list for ``group_jid``.

    Returns ``(participants, addressing_mode)`` where ``participants`` is
    a list of per-member JIDs (typically @lid for modern groups) without
    device numbers, and ``addressing_mode`` is the group's own
    ``"lid"`` / ``"pn"`` attribute. Mirrors the same IQ we use in
    ``_fetch_group_participants`` but returns the raw structure so the
    send path can iterate.
    """
    import secrets

    iq_id = f"gi-{secrets.token_hex(4)}"
    iq = Node(
        tag="iq",
        attrs={"to": group_jid, "type": "get", "id": iq_id, "xmlns": "w:g2"},
        content=[Node(tag="query", attrs={"request": "interactive"})],
    )
    await fs.send(ns.encrypt_frame(encode_node(iq)))
    deadline = asyncio.get_event_loop().time() + 8.0
    while asyncio.get_event_loop().time() < deadline:
        try:
            ct = await fs.recv(
                timeout=max(0.1, deadline - asyncio.get_event_loop().time())
            )
        except (asyncio.TimeoutError, ConnectionError):
            break
        try:
            node = decode_node(ns.decrypt_frame(ct))
        except Exception:
            continue
        if node.tag != "iq" or node.attrs.get("id") != iq_id:
            continue
        if node.attrs.get("type") == "error":
            err = node.get_child_by_tag("error")
            raise RuntimeError(f"group info IQ rejected: {err.attrs if err else node.attrs}")
        grp = node.get_child_by_tag("group")
        if grp is None:
            raise RuntimeError("group info reply has no <group>")
        addressing_mode = grp.attrs.get("addressing_mode", "pn") or "pn"
        participants: list[JID] = []
        for child in grp.get_children():
            if child.tag != "participant":
                continue
            pjid = child.attrs.get("jid")
            if isinstance(pjid, JID):
                participants.append(pjid)
        return participants, addressing_mode
    raise TimeoutError("group info IQ timed out")


async def _send_group_async(device: Device, group_jid: str, text: str) -> None:
    """Send a text message to a group via Sender Keys + skmsg.

    Per-message flow (mirrors whatsmeow's ``sendGroup``):

    1. Fetch the group's participant list + addressing mode.
    2. ``usync`` the participants to expand to per-device AD JIDs.
    3. Generate (or refresh) our sender key for this group.
    4. Wrap the SKDM in ``Message{senderKeyDistributionMessage: {...}}``,
       Signal-encrypt that to every participant device — pre-key bundle
       fetch fills missing sessions.
    5. ``group_encrypt`` the actual plaintext to a single ``skmsg`` blob.
    6. Build ``<message to=<group> type=text id=...><participants>...
       </participants><enc type=skmsg/>[<device-identity/>]</message>``,
       send, await ack.
    """
    import secrets
    import time as _time

    from wa.cache import CachedMessage, append_messages, upsert_chat
    from wa.peerreq import _length_delim, _string_field, pad_for_signal

    log = logging.getLogger("send")

    own_pn = JID.parse(device.jid)
    own_lid = JID.parse(device.lid) if device.lid else None
    own_self_device = own_pn.device

    group_jid_obj = JID(user=group_jid.split("@")[0], server="g.us")

    async with FrameSocket() as fs:
        await fs.connect()
        try:
            ns = await do_handshake(fs, device, build_login_payload(device))
        except Exception as e:
            click.echo(click.style(f"handshake failed: {e}", fg="red"), err=True)
            raise SystemExit(1)
        while True:
            try:
                ct = await fs.recv(timeout=15.0)
            except (asyncio.TimeoutError, ConnectionError):
                click.echo(click.style("no <success/>", fg="red"), err=True)
                raise SystemExit(1)
            node = decode_node(ns.decrypt_frame(ct))
            if node.tag == "success":
                break
            if node.tag == "failure":
                click.echo(
                    click.style(f"login rejected: {_pretty(node)}", fg="red"),
                    err=True,
                )
                raise SystemExit(1)

        active = Node(
            tag="iq",
            attrs={
                "to": JID(server="s.whatsapp.net"),
                "type": "set",
                "id": "send-active",
                "xmlns": "passive",
            },
            content=[Node(tag="active")],
        )
        await fs.send(ns.encrypt_frame(encode_node(active)))
        active_dl = asyncio.get_event_loop().time() + 5.0
        while asyncio.get_event_loop().time() < active_dl:
            try:
                ct = await fs.recv(timeout=0.5)
            except (asyncio.TimeoutError, ConnectionError):
                break
            n = decode_node(ns.decrypt_frame(ct))
            if n.tag == "iq" and n.attrs.get("id") == "send-active":
                break
        await _send_presence_available(fs, ns, device)

        # 1. Group participants + addressing mode.
        try:
            participants, addressing_mode = await _fetch_group_members(
                fs, ns, group_jid_obj
            )
        except Exception as e:
            click.echo(click.style(f"group info failed: {e}", fg="red"), err=True)
            raise SystemExit(1)
        if not participants:
            click.echo(
                click.style("group has no participants — are you still in it?", fg="red"),
                err=True,
            )
            raise SystemExit(1)
        log.info(
            "group %s: %d participants, addressing_mode=%s",
            group_jid, len(participants), addressing_mode,
        )

        # 2. Expand to AD JIDs via usync. Whatsmeow uses the participant
        # list as-is (LIDs for LID-addressed groups).
        try:
            all_devices = await _usync_devices(fs, ns, participants)
        except Exception as e:
            click.echo(
                click.style(f"device list query failed: {e}", fg="red"), err=True
            )
            raise SystemExit(1)
        # Drop our own current device from the recipient set.
        sender_user = own_lid.user if (addressing_mode == "lid" and own_lid) else own_pn.user
        targets = [
            d for d in all_devices
            if not (d.user == sender_user and d.device == own_self_device)
        ]
        log.info("targets: %d device(s) across %d participants", len(targets), len(participants))

        # 3. Generate the SKDM for this group + our LID-keyed sender addr.
        signal = SignalSession(device)
        skdm_bytes = signal.create_sender_key_distribution(
            group_id=str(group_jid_obj), sender_user=sender_user, sender_device=own_self_device
        )

        # 4. Wrap in Message{senderKeyDistributionMessage{groupID, axolotl_bytes}}
        skdm_inner = _string_field(1, str(group_jid_obj)) + _length_delim(2, skdm_bytes)
        skdm_message_proto = _length_delim(2, skdm_inner)
        skdm_padded = pad_for_signal(skdm_message_proto)

        # Fetch bundles for sessionless devices in one batch.
        no_session_devices = [
            d for d in targets if not signal.has_session(d.user, d.device)
        ]
        if no_session_devices:
            log.info("fetching prekey bundles for %d device(s)", len(no_session_devices))
            try:
                bundles = await _fetch_prekey_bundles(fs, ns, no_session_devices)
            except Exception as e:
                log.warning("prekey fetch failed: %s", e)
                bundles = {}
            for d in no_session_devices:
                b = bundles.get(d.ad_string())
                if not b:
                    continue
                try:
                    signal.install_prekey_bundle(d.user, d.device, **b)
                except Exception as e:
                    log.warning("bundle install failed for %s: %s", d, e)

        participant_children: list[Node] = []
        any_pkmsg = False
        skipped: list[str] = []
        for dev in targets:
            try:
                ct, kind = signal.encrypt_msg(dev.user, dev.device, skdm_padded)
            except Exception as e:
                skipped.append(f"{dev.user}:{dev.device}")
                log.debug("skipping %s: %s", dev, e)
                continue
            if kind == "pkmsg":
                any_pkmsg = True
            participant_children.append(
                Node(
                    tag="to",
                    attrs={"jid": dev},
                    content=[Node(tag="enc", attrs={"v": "2", "type": kind}, content=ct)],
                )
            )
        if skipped:
            log.warning("skipped %d device(s) with no encryptable session", len(skipped))
        if not participant_children:
            click.echo(
                click.style("no encryptable participants — aborting", fg="red"),
                err=True,
            )
            raise SystemExit(1)

        # 5. Encrypt the actual user message via group cipher.
        message_plaintext = _build_message_plaintext(text)
        skmsg_padded = pad_for_signal(message_plaintext)
        try:
            skmsg_ct = signal.group_encrypt(
                str(group_jid_obj), sender_user, own_self_device, skmsg_padded
            )
        except Exception as e:
            click.echo(click.style(f"group encrypt failed: {e}", fg="red"), err=True)
            raise SystemExit(1)

        # 6. Build stanza. Group sends DO set `phash` (unlike DMs), and
        # set `addressing_mode` to match the group's mode.
        phash = _phash(all_devices)
        msg_id = "3EB0" + secrets.token_hex(9).upper()
        stanza_attrs: dict = {
            "to": group_jid_obj,
            "type": "text",
            "id": msg_id,
            "phash": phash,
            "addressing_mode": addressing_mode,
        }
        content_nodes: list[Node] = [
            Node(tag="participants", content=participant_children),
            Node(tag="enc", attrs={"v": "2", "type": "skmsg"}, content=skmsg_ct),
        ]
        if any_pkmsg and device.account:
            content_nodes.append(Node(tag="device-identity", content=device.account))
        stanza = Node(tag="message", attrs=stanza_attrs, content=content_nodes)
        await fs.send(ns.encrypt_frame(encode_node(stanza)))
        log.info(
            "sent skmsg id=%s to=%s participants=%d any_pkmsg=%s",
            msg_id, group_jid, len(participant_children), any_pkmsg,
        )

        ack_dl = asyncio.get_event_loop().time() + 15.0
        while asyncio.get_event_loop().time() < ack_dl:
            try:
                ct = await fs.recv(
                    timeout=max(0.1, ack_dl - asyncio.get_event_loop().time())
                )
            except (asyncio.TimeoutError, ConnectionError):
                break
            try:
                node = decode_node(ns.decrypt_frame(ct))
            except Exception:
                continue
            if node.tag == "ack" and node.attrs.get("id") == msg_id:
                ts = int(_time.time())
                append_messages([
                    CachedMessage(
                        ts=ts,
                        chat=group_jid,
                        sender=str(own_pn) if own_pn.user else "",
                        sender_name="me",
                        text=text,
                        from_me=True,
                        msg_id=msg_id,
                    )
                ])
                upsert_chat(group_jid, last_ts=ts)
                click.echo(click.style(f"sent: {msg_id}", fg="green"))
                return
        click.echo(
            click.style("no ack within 15s — message may or may not have been queued", fg="yellow"),
            err=True,
        )


async def _login_async(device: Device) -> None:
    # Fast path for already-paired devices: skip the QR dance and go
    # straight to the login-handshake. This is also what runs after a
    # successful pair, to consummate the link.
    if device.is_paired():
        await _login_handshake(device)
        return

    click.echo("connecting to web.whatsapp.com ...")
    async with FrameSocket() as fs:
        await fs.connect()
        payload = build_registration_payload(device)
        try:
            ns = await do_handshake(fs, device, payload)
        except Exception as e:
            click.echo(click.style(f"handshake failed: {e}", fg="red"), err=True)
            raise SystemExit(1)
        click.echo(click.style("handshake OK — waiting for pair-device IQ...", fg="green"))

        # First frame post-handshake is <iq><pair-device>…</pair-device></iq>.
        ct = await fs.recv(timeout=20.0)
        pair_device_iq = decode_node(ns.decrypt_frame(ct))
        logging.getLogger("login").debug(
            "first post-handshake frame:\n%s", _pretty(pair_device_iq)
        )
        refs = extract_pair_refs(pair_device_iq)
        if not refs:
            click.echo(
                click.style(
                    f"unexpected first frame: {pair_device_iq.tag}", fg="red"
                ),
                err=True,
            )
            return

        # Server needs the ACK before it delivers pair-success.
        ack = build_pair_device_ack(pair_device_iq)
        await fs.send(ns.encrypt_frame(encode_node(ack)))

        click.echo(f"got {len(refs)} pair refs; showing first")
        click.echo("\n" + render_qr_ansi(make_qr_payload(refs[0], device)))
        click.echo("scan this with WhatsApp → Linked Devices → Link a device")

        # Wait for pair-success. The server also sends a new <ref> every ~30s;
        # we rotate the QR by re-rendering when we see one.
        ref_index = 1
        while True:
            try:
                ct = await fs.recv(timeout=180.0)
            except (asyncio.TimeoutError, ConnectionError):
                click.echo(click.style("timed out waiting for scan", fg="red"), err=True)
                return
            node = decode_node(ns.decrypt_frame(ct))
            ps = node.get_child_by_tag("pair-success")
            if ps is not None:
                reply = handle_pair_success(node, device)
                await fs.send(ns.encrypt_frame(encode_node(reply)))
                device.save()
                click.echo(
                    click.style(
                        f"paired as {device.jid} on {device.platform or '?'}"
                        + (f" ({device.business_name})" if device.business_name else ""),
                        fg="green",
                    )
                )
                # Give the server a moment to close the socket after our reply,
                # then reconnect to consummate the pair (phone dialog only
                # closes once WE complete a login-handshake).
                break
            pd = node.get_child_by_tag("pair-device")
            if pd is not None:
                # Server refreshed refs — either scan the new one or wait.
                new_refs = extract_pair_refs(node)
                if new_refs and ref_index < len(new_refs):
                    click.echo("\n" + render_qr_ansi(make_qr_payload(new_refs[0], device)))
                    ref_index += 1
                ack = build_pair_device_ack(node)
                await fs.send(ns.encrypt_frame(encode_node(ack)))
                continue
            # Ignore anything else (stream:error, unrelated IQs) and keep waiting.
            log = logging.getLogger("login")
            log.debug("ignoring frame:\n%s", _pretty(node))

    # Fall through to the login-handshake so the phone's dialog closes.
    click.echo("reconnecting to consummate pair...")
    # Brief pause to let the server tear down the registration stream.
    await asyncio.sleep(0.5)
    await _login_handshake(device)


def _extract_text(plaintext: bytes, _depth: int = 0) -> str:
    """Pull the first bit of human-readable text out of a decrypted Message.

    Recurses through common wrappers — ``deviceSentMessage`` (sent from your
    phone to sync across linked devices), ``ephemeralMessage`` (disappearing),
    ``viewOnceMessage`` (single-view), etc. — because the actual content
    lives inside a nested Message proto.
    """
    if _depth > 4:
        return ""
    try:
        top = decode_fields(plaintext)
    except Exception:
        return ""

    def _first_bytes(field: int) -> bytes | None:
        if field in top and isinstance(top[field][0], (bytes, bytearray)):
            return bytes(top[field][0])
        return None

    def _utf8(b: bytes) -> str | None:
        try:
            return b.decode("utf-8")
        except UnicodeDecodeError:
            return None

    # Plain text — conversation (field 1, wire=2 — string)
    if (b := _first_bytes(1)) is not None and (s := _utf8(b)) is not None:
        return s

    # ExtendedTextMessage — field 6, inner text at field 1
    if (b := _first_bytes(6)) is not None:
        try:
            inner = decode_fields(b)
            cand = inner.get(1, [None])[0]
            if isinstance(cand, (bytes, bytearray)) and (s := _utf8(bytes(cand))):
                return s
        except Exception:
            pass

    # Media captions — image (3), video (26), both have caption at inner 7.
    for f, label in ((3, "image"), (26, "video")):
        if (b := _first_bytes(f)) is not None:
            try:
                inner = decode_fields(b)
                cap = inner.get(7, [b""])[0]
                if isinstance(cap, (bytes, bytearray)) and cap:
                    if (s := _utf8(bytes(cap))):
                        return f"[{label}] {s}"
            except Exception:
                pass
            return f"[{label}]"

    # Content types without text — just flag them
    for f, label in ((23, "audio"), (7, "document"), (25, "sticker")):
        if _first_bytes(f) is not None:
            return f"[{label}]"

    # Reaction (field 38 = ReactionMessage, inner text at field 2)
    if (b := _first_bytes(38)) is not None:
        try:
            inner = decode_fields(b)
            cand = inner.get(2, [None])[0]
            if isinstance(cand, (bytes, bytearray)) and (s := _utf8(bytes(cand))):
                return f"[reaction] {s}"
        except Exception:
            pass

    # Wrappers that nest another Message — recurse into it.
    #   31 = DeviceSentMessage { field 2 = nested Message }
    #   25 = EphemeralMessage, 24 = ViewOnceMessage, 36 = ViewOnceMessageV2,
    #   53 = DocumentWithCaptionMessage — each has nested Message at field 1.
    _WRAPPERS = [(31, 2), (25, 1), (24, 1), (36, 1), (53, 1)]
    for outer_field, inner_field in _WRAPPERS:
        if (b := _first_bytes(outer_field)) is not None:
            try:
                wrap = decode_fields(b)
                cand = wrap.get(inner_field, [None])[0]
                if isinstance(cand, (bytes, bytearray)):
                    deeper = _extract_text(bytes(cand), _depth + 1)
                    if deeper:
                        return deeper
            except Exception:
                continue
    return ""


def _try_decrypt_message(node: Node, signal: SignalSession) -> None:
    """Attempt to Signal-decrypt the ``<enc>`` children of a ``<message>``.

    For 1:1 ``<message>`` the Signal address is the ``from`` JID. For group
    messages ``from`` is the GROUP JID (``@g.us``) — the real sender is in
    the ``participant`` attribute, and that's who owns the Signal identity
    key we need to authenticate.
    """
    log = logging.getLogger("decrypt")
    sender = node.attrs.get("participant") or node.attrs.get("from")
    if isinstance(sender, JID):
        jid_user = sender.user
        device_num = sender.device
    elif isinstance(sender, str):
        parts = sender.split("@")[0]
        jid_user, _, dev = parts.partition(":")
        device_num = int(dev) if dev else 0
    else:
        log.warning("message has no usable from/participant: %s", type(sender))
        return
    # For group messages, the <from> JID is the group — we need it to key
    # the sender-key store so skmsg can be decrypted.
    from_jid = node.attrs.get("from")
    group_id = (
        from_jid.user
        if isinstance(from_jid, JID) and from_jid.server == "g.us"
        else None
    )

    # Opportunistically harvest sender identity. Group messages carry the
    # sender's pushName in `notify=`, the sender's @lid as `participant=`,
    # and the matching phone-number JID as `participant_pn=`. We persist
    # all three so subsequent `read` calls can resolve the LID to a name
    # without needing a separate PUSH_NAME sync chunk.
    notify = node.attrs.get("notify", "")
    # Group msgs use participant + participant_pn; DMs from @lid users use
    # the source JID (`from`) as the LID and `sender_pn` for the phone form.
    participant = node.attrs.get("participant") or node.attrs.get("from")
    participant_pn = node.attrs.get("participant_pn") or node.attrs.get("sender_pn")
    if notify and participant is not None:
        try:
            from wa.cache import (
                load_contacts,
                load_lidmap,
                save_contacts,
                save_lidmap,
            )

            contacts = load_contacts()
            lidmap = load_lidmap()
            changed = False
            # Normalize the LID JID to bare ``<local>@lid`` (no agent, no
            # device) since that's what historical messages use. Mixing
            # the three formats — bare ``X@lid``, agent-tagged ``X.1@lid``,
            # and just-local ``X`` — would break contact lookup.
            if isinstance(participant, JID) and participant.server == "lid":
                lid_key = f"{participant.user}@lid"
            elif isinstance(participant, str) and participant.endswith("@lid"):
                local_only = participant.split("@")[0].split(".")[0].split(":")[0]
                lid_key = f"{local_only}@lid"
            else:
                lid_key = ""
            if lid_key and contacts.get(lid_key) != notify:
                contacts[lid_key] = notify
                changed = True
            # LID → PN mapping if participant_pn alongside.
            if isinstance(participant, JID) and isinstance(participant_pn, JID):
                local = participant.user
                pn_jid = f"{participant_pn.user}@s.whatsapp.net"
                if local and lidmap.get(local) != pn_jid:
                    lidmap[local] = pn_jid
                    changed = True
                if pn_jid and contacts.get(pn_jid) != notify:
                    contacts[pn_jid] = notify
                    changed = True
            if changed:
                save_contacts(contacts)
                save_lidmap(lidmap)
        except Exception as e:
            log.warning("contact harvest failed: %s", e)
    for enc in node.get_children():
        if enc.tag != "enc":
            continue
        enc_type = enc.attrs.get("type", "")
        if not isinstance(enc.content, (bytes, bytearray)):
            continue
        ct = bytes(enc.content)
        try:
            if enc_type == "pkmsg":
                pt = signal.decrypt_pkmsg(jid_user, device_num, ct)
                # If the plaintext is a Message{senderKeyDistributionMessage{...}},
                # install the sender key so the next skmsg decrypts.
                if group_id is not None:
                    try:
                        outer = decode_fields(pt)
                        skdm_container = outer.get(2, [None])[0]
                        if isinstance(skdm_container, (bytes, bytearray)):
                            inner = decode_fields(bytes(skdm_container))
                            # field 2 is axolotlSenderKeyDistributionMessage
                            axolotl = inner.get(2, [None])[0]
                            if isinstance(axolotl, (bytes, bytearray)):
                                signal.process_sender_key_distribution(
                                    group_id, jid_user, device_num, bytes(axolotl)
                                )
                                log.debug(
                                    "installed sender key for group=%s sender=%s:%d",
                                    group_id, jid_user, device_num,
                                )
                    except Exception as e:
                        log.warning("failed to install sender key: %s", e)
            elif enc_type == "msg":
                pt = signal.decrypt_msg(jid_user, device_num, ct)
            elif enc_type == "skmsg" and group_id is not None:
                pt = signal.decrypt_skmsg(group_id, jid_user, device_num, ct)
            else:
                log.debug("skipping enc type=%s", enc_type)
                continue
        except Exception as e:
            log.warning(
                "decrypt %s from %s:%d failed (%d bytes): %s",
                enc_type, jid_user, device_num, len(ct), e,
            )
            continue
        # Map field numbers at the outer Message level to sub-message names
        # from whatsmeow's waE2E.proto (for human-readable log lines).
        _FIELD_NAMES = {
            1: "conversation",
            2: "senderKeyDistributionMessage",
            3: "imageMessage",
            4: "contactMessage",
            5: "locationMessage",
            6: "extendedTextMessage",
            12: "protocolMessage",
            23: "audioMessage",
            26: "videoMessage",
            31: "deviceSentMessage",
            35: "messageContextInfo",
        }

        # Pull something humans can actually read out of the plaintext.
        pretty = _extract_text(pt)
        if pretty:
            sender_name = node.attrs.get("notify", "")
            # Preserve the original server on the JID. Hardcoding
            # `@s.whatsapp.net` for non-group chats was wrong: many DMs in
            # the post-2024 WhatsApp world come from `@lid` identifiers
            # (15-digit privacy-preserving IDs).
            if isinstance(from_jid, JID) and from_jid.server == "g.us":
                chat_jid = f"{from_jid.user}@g.us"
            elif isinstance(from_jid, JID):
                chat_jid = f"{from_jid.user}@{from_jid.server}"
            else:
                chat_jid = f"{jid_user}@s.whatsapp.net"  # last-resort fallback
            if isinstance(sender, JID):
                sender_jid = f"{sender.user}@{sender.server}"
            else:
                sender_jid = f"{jid_user}@s.whatsapp.net"
            # Auto-merge: fold any wrongly-namespaced PN entry into its
            # canonical @lid twin before persisting, so the cache never
            # accumulates the kind of duplicates that `wa migrate` cleans
            # up retroactively.
            from wa.cache import canonical_jid

            chat_jid = canonical_jid(chat_jid)
            sender_jid = canonical_jid(sender_jid)
            ts_attr = node.attrs.get("t", 0)
            try:
                ts = int(ts_attr)
            except (TypeError, ValueError):
                import time as _time

                ts = int(_time.time())
            msg_id = node.attrs.get("id", "") or ""
            # Persist to cache so the live stream feeds into `read`.
            try:
                from wa.cache import CachedMessage, append_messages, upsert_chat

                append_messages([
                    CachedMessage(
                        ts=ts,
                        chat=chat_jid,
                        sender=sender_jid,
                        sender_name=sender_name,
                        text=pretty,
                        from_me=False,
                        msg_id=msg_id,
                    )
                ])
                upsert_chat(chat_jid, last_ts=ts)
            except Exception as e:
                log.warning("cache append failed: %s", e)
            click.echo(
                click.style(
                    f"[{chat_jid}] {sender_name or jid_user}: {pretty}",
                    fg="cyan",
                ),
                err=True,
            )
        try:
            top = decode_fields(pt)
        except Exception as e:
            top = None
            log.info(
                "decrypted %s from %s:%d → %d bytes (unparseable: %s)",
                enc_type, jid_user, device_num, len(pt), e,
            )
        if top is not None:
            fields = [
                f"{_FIELD_NAMES.get(f, f'field{f}')}"
                f"[{sum(len(v) if isinstance(v, (bytes, bytearray)) else 0 for v in vs) or len(vs)}]"
                for f, vs in sorted(top.items())
            ]
            log.info(
                "decrypted %s from %s:%d → %d bytes: %s",
                enc_type, jid_user, device_num, len(pt), ", ".join(fields),
            )
            # Every protocolMessage from the phone carries history-sync or
            # app-state-key material; save each one with a stable timestamp-
            # suffixed filename so we can replay them later via `history`.
            for f, vs in top.items():
                for idx, v in enumerate(vs):
                    if not isinstance(v, (bytes, bytearray)):
                        continue
                    if f != 12 and len(v) < 1024:
                        continue  # skip small non-protocol blobs
                    dump_dir = Path.home() / ".cache" / "whatsapp-cli" / "blobs"
                    dump_dir.mkdir(parents=True, exist_ok=True)
                    name = _FIELD_NAMES.get(f, f"field{f}")
                    import time as _time

                    ts = int(_time.time() * 1000)
                    p = dump_dir / f"{ts}-{name}-{jid_user}-{device_num}-{idx}-{len(v)}.bin"
                    p.write_bytes(bytes(v))
                    log.debug("saved %d-byte %s → %s", len(v), name, p.name)


async def _fetch_group_participants(
    fs: FrameSocket, ns, *, only_unnamed: bool = False
) -> None:
    """For every cached group, query its metadata and harvest display names.

    Each ``<participant display_name="…" jid="<lid>" phone_number="<pn>">``
    is the WA pushName for that user as known by the group, plus the LID-PN
    pair. This is the only way to populate names for contacts whose
    pushNames aren't in the global PUSH_NAME sync (typically because they
    haven't shared one publicly, but their group display still has one).

    Two optimizations vs. a naive loop:

    1. **TTL cache** (``group_fetches.json``). Groups queried in the last
       ``WA_GROUPINFO_TTL_DAYS`` (default 7) are skipped — participant
       lists are essentially static between explicit ``w:gp2``
       notifications, so re-fetching them every login is pure waste.
    2. **Pipelined IQs**. A single reader task demultiplexes replies by
       ``iq_id``; up to ``WA_GROUPINFO_CONCURRENCY`` (default 10) requests
       are in flight at once. With a typical ~150ms RTT the wall-clock
       drops from ``N × 150ms`` to roughly ``N/10 × 150ms``.

    With ``only_unnamed=True`` the function targets just groups whose
    ``chats.json`` entry has an empty ``name`` (newly-joined groups whose
    subject we've never resolved), and ignores the TTL cache so they're
    always fetched. Called from ``wa sync`` to keep names current
    without the full ~5s refresh.
    """
    import secrets
    import time as _time

    from wa.cache import (
        load_chats,
        load_contacts,
        load_group_fetches,
        load_lidmap,
        save_chats,
        save_contacts,
        save_group_fetches,
        save_lidmap,
    )

    log = logging.getLogger("groupinfo")
    chats = load_chats()
    contacts = load_contacts()
    lidmap = load_lidmap()
    fetches = load_group_fetches()
    now_ts = int(_time.time())
    ttl_days = float(os.environ.get("WA_GROUPINFO_TTL_DAYS", "7"))
    ttl_seconds = int(ttl_days * 86400)
    cap = int(os.environ.get("WA_GROUPINFO_CAP", "1000"))
    concurrency = int(os.environ.get("WA_GROUPINFO_CONCURRENCY", "10"))

    # Sort by recent activity so the most relevant groups go first when the
    # cap bites. Then filter to either (a) unnamed groups regardless of
    # TTL, or (b) groups beyond the TTL window for the full refresh.
    groups_all = sorted(
        (j for j in chats if j.endswith("@g.us")),
        key=lambda j: chats[j].get("last_ts", 0),
        reverse=True,
    )
    if only_unnamed:
        groups = [g for g in groups_all if not chats[g].get("name")][:cap]
        log.info(
            "fetching %d newly-joined groups (no name yet, concurrency=%d)",
            len(groups), concurrency,
        )
    else:
        skipped = sum(
            1 for g in groups_all if now_ts - fetches.get(g, 0) < ttl_seconds
        )
        groups = [
            g for g in groups_all if now_ts - fetches.get(g, 0) >= ttl_seconds
        ][:cap]
        log.info(
            "fetching %d groups (skipping %d cached <%.0fd old, concurrency=%d)",
            len(groups), skipped, ttl_days, concurrency,
        )
    if not groups:
        return

    pending: dict[str, asyncio.Future] = {}
    new_count = 0
    reader_stop = asyncio.Event()

    async def reader() -> None:
        # Dedicated frame reader: routes <iq> replies to their pending
        # futures by id, drops anything else (no other phase is running).
        while not reader_stop.is_set():
            try:
                ct = await fs.recv(timeout=0.5)
            except (asyncio.TimeoutError, ConnectionError):
                continue
            try:
                node = decode_node(ns.decrypt_frame(ct))
            except Exception:
                continue
            if node.tag != "iq":
                continue
            iq_id = node.attrs.get("id")
            fut = pending.get(iq_id)
            if fut is not None and not fut.done():
                fut.set_result(node)

    def _harvest(gid: str, node: Node) -> int:
        nonlocal new_count
        added = 0
        grp = node.get_child_by_tag("group")
        if grp is None:
            return 0
        # The group's own display name (subject) lives on the <group> tag.
        # Persist it to chats.json so `wa chats` stops showing `(unnamed)`
        # for newly-joined groups.
        subject = grp.attrs.get("subject", "")
        if subject and chats.get(gid, {}).get("name") != subject:
            entry = chats.get(gid, {})
            entry["name"] = subject
            entry.setdefault("last_ts", 0)
            chats[gid] = entry
        for child in grp.get_children():
            if child.tag != "participant":
                continue
            disp = child.attrs.get("display_name", "")
            pjid = child.attrs.get("jid")
            pn_attr = child.attrs.get("phone_number")
            if not disp or not isinstance(pjid, JID):
                continue
            lid_key = f"{pjid.user}@lid" if pjid.server == "lid" else None
            pn_key = None
            if isinstance(pn_attr, JID) and pn_attr.user:
                pn_key = f"{pn_attr.user}@s.whatsapp.net"
            elif pjid.server == "s.whatsapp.net":
                pn_key = f"{pjid.user}@s.whatsapp.net"
            for k in (lid_key, pn_key):
                if k and contacts.get(k) != disp:
                    contacts[k] = disp
                    added += 1
            if lid_key and pn_key and lidmap.get(pjid.user) != pn_key:
                lidmap[pjid.user] = pn_key
        new_count += added
        return added

    sem = asyncio.Semaphore(concurrency)

    async def fetch_one(gid: str) -> None:
        async with sem:
            iq_id = f"gi-{secrets.token_hex(4)}"
            fut: asyncio.Future = asyncio.get_event_loop().create_future()
            pending[iq_id] = fut
            iq = Node(
                tag="iq",
                attrs={"to": JID.parse(gid), "type": "get", "id": iq_id, "xmlns": "w:g2"},
                content=[Node(tag="query", attrs={"request": "interactive"})],
            )
            try:
                await fs.send(ns.encrypt_frame(encode_node(iq)))
            except Exception as e:
                log.warning("send failed for %s: %s", gid, e)
                pending.pop(iq_id, None)
                return
            try:
                node = await asyncio.wait_for(fut, timeout=5.0)
            except asyncio.TimeoutError:
                log.debug("group %s: no reply in 5s, skipping", gid)
                return
            finally:
                pending.pop(iq_id, None)
            # Cache the fetch attempt regardless of outcome: an `<iq type="error">`
            # ("not-authorized" / "item-not-found") means you've left the group
            # or it no longer exists, and that state is very unlikely to flip
            # back during the TTL window. Re-querying every login is exactly
            # the waste we're trying to eliminate.
            fetches[gid] = int(_time.time())
            if node.attrs.get("type") == "error":
                err = node.get_child_by_tag("error")
                log.debug("group %s: error %s", gid, err.attrs if err else "?")
                return
            _harvest(gid, node)

    reader_task = asyncio.create_task(reader())
    try:
        await asyncio.gather(*(fetch_one(g) for g in groups), return_exceptions=True)
    finally:
        reader_stop.set()
        reader_task.cancel()
        try:
            await reader_task
        except (asyncio.CancelledError, Exception):
            pass

    save_chats(chats)
    save_contacts(contacts)
    save_lidmap(lidmap)
    save_group_fetches(fetches)
    log.info(
        "groupinfo: %d display_name entries from %d groups",
        new_count, len(groups),
    )


async def _fetch_pushnames(fs: FrameSocket, ns) -> None:
    """Ask the server for pushNames of every PN in lidmap not already named.

    The reply has ``<user jid=... notify="…">`` per phone-number — that
    ``notify`` attribute is the WhatsApp display name. Works without the
    phone needing chat-history sync enabled, because this is a server-side
    query, not a phone-mediated history flow.
    """
    import secrets

    from wa.cache import load_contacts, load_lidmap, save_contacts

    log = logging.getLogger("usync")
    contacts = load_contacts()
    lidmap = load_lidmap()

    # Targets: every PN we know via the LID mapping that isn't yet named.
    targets = sorted({pn for pn in lidmap.values() if pn and pn not in contacts})
    if not targets:
        log.debug("no pushNames to fetch")
        return
    log.info("fetching pushNames for %d unknown contacts...", len(targets))

    async def _query_batch(batch: list[str]) -> dict[str, str]:
        sid = secrets.token_hex(8)
        iq_id = f"usync-{sid[:6]}"
        users = [
            Node(tag="user", attrs={"jid": JID.parse(pn)})
            for pn in batch
        ]
        iq = Node(
            tag="iq",
            attrs={
                "to": JID(server="s.whatsapp.net"),
                "type": "get",
                "id": iq_id,
                "xmlns": "usync",
            },
            content=[
                Node(
                    tag="usync",
                    attrs={
                        "sid": sid,
                        "mode": "query",
                        "last": "true",
                        "index": "0",
                        "context": "interactive",
                    },
                    content=[
                        Node(tag="query", content=[Node(tag="contact")]),
                        Node(tag="list", content=users),
                    ],
                )
            ],
        )
        await fs.send(ns.encrypt_frame(encode_node(iq)))
        deadline = asyncio.get_event_loop().time() + 8.0
        while asyncio.get_event_loop().time() < deadline:
            try:
                ct = await fs.recv(timeout=max(0.1, deadline - asyncio.get_event_loop().time()))
            except (asyncio.TimeoutError, ConnectionError):
                return {}
            try:
                node = decode_node(ns.decrypt_frame(ct))
            except Exception:
                continue
            if node.tag != "iq" or node.attrs.get("id") != iq_id:
                continue
            # Dump the response once so we can see what the server actually
            # returned vs what we expect. Crucial for debugging when 0 names
            # come back: the issue is usually wrong query sub-tags.
            log.debug("usync reply:\n%s", _pretty(node))
            if node.attrs.get("type") == "error":
                err_node = node.get_child_by_tag("error")
                log.warning(
                    "usync IQ rejected: %s",
                    err_node.attrs if err_node else "no <error/>",
                )
                return {}
            found: dict[str, str] = {}
            for child_user in (
                node.get_child_by_tag("usync", "list") or Node(tag="")
            ).get_children():
                if child_user.tag != "user":
                    continue
                jid_attr = child_user.attrs.get("jid")
                notify_attr = child_user.attrs.get("notify", "")
                if isinstance(jid_attr, JID):
                    found[f"{jid_attr.user}@s.whatsapp.net"] = notify_attr
                elif isinstance(jid_attr, str):
                    found[jid_attr] = notify_attr
            return found
        return {}

    # Cap initial run at a small batch so the debug-log response shape is
    # visible quickly. Once we know the right query subtags, this opens up
    # to all targets in 50-batch chunks like whatsmeow does.
    targets = targets[:10]
    new_count = 0
    for i in range(0, len(targets), 50):
        batch = targets[i : i + 50]
        try:
            found = await _query_batch(batch)
        except Exception as e:
            log.warning("usync batch %d: %s", i, e)
            continue
        for jid, name in found.items():
            if name and contacts.get(jid) != name:
                contacts[jid] = name
                new_count += 1
    save_contacts(contacts)
    log.info("usync added %d pushNames to contacts", new_count)


async def _upload_prekeys(fs: FrameSocket, ns, device: Device) -> None:
    """First-pair pre-key upload — sends the IQ and waits for the result.

    On success persists ``device.prekeys_uploaded = True`` so we don't repeat
    on subsequent reconnects. Without this batch the phone keeps spinning
    on "connecting…" because its UI uses our pre-key count to decide whether
    we are reachable for new Signal sessions.
    """
    log = logging.getLogger("prekeys")
    batch = generate_prekey_batch(device.next_prekey_id, INITIAL_UPLOAD_COUNT)
    iq_id = f"prekey-upload-{device.next_prekey_id}"
    iq = build_upload_iq(
        iq_id=iq_id,
        registration_id=device.registration_id,
        identity_key_pub=device.identity_key.pub,
        one_time_prekeys=batch,
        signed_prekey={
            "key_id": device.signed_pre_key.key_id,
            "pub": device.signed_pre_key.pub,
            "signature": device.signed_pre_key.signature,
        },
    )
    log.info("uploading %d one-time pre-keys (id %d…%d)",
             len(batch), batch[0]["key_id"], batch[-1]["key_id"])
    await fs.send(ns.encrypt_frame(encode_node(iq)))
    # Poll for the matching IQ result; timebox to avoid hanging the connect.
    deadline = asyncio.get_event_loop().time() + 10.0
    while asyncio.get_event_loop().time() < deadline:
        try:
            ct = await fs.recv(timeout=max(0.1, deadline - asyncio.get_event_loop().time()))
        except (asyncio.TimeoutError, ConnectionError):
            log.warning("pre-key upload IQ — no result within timeout")
            return
        node = decode_node(ns.decrypt_frame(ct))
        if node.tag == "iq" and node.attrs.get("id") == iq_id:
            if node.attrs.get("type") == "result":
                device.prekeys_uploaded = True
                device.next_prekey_id += INITIAL_UPLOAD_COUNT
                # Persist the private halves so future processes can decrypt
                # incoming pkmsgs that reference these prekey IDs.
                device.one_time_prekeys.extend(batch)
                device.save()
                log.info("pre-key upload accepted (%d privates persisted)", len(batch))
            else:
                log.warning("pre-key upload rejected:\n%s", _pretty(node))
            return
        # Stash anything else for the main post-success handler to deal with;
        # for simplicity we just drop them here. Real client would queue.
        log.debug("dropping out-of-order frame during prekey upload:\n%s", _pretty(node))


async def _post_success(
    fs: FrameSocket,
    ns,
    device: Device,
    *,
    seconds: float = 60.0,
    idle: float = 3.0,
    fetch_groups: bool = True,
) -> None:
    """Behave like a real client after <success/>, drain the offline queue.

    Sends the active-IQ the phone is waiting on, ACKs server-initiated IQs,
    and attempts to Signal-decrypt any ``<enc>`` payloads so we can see the
    peer content (typically app-state keys and history sync).

    Termination is whichever happens first:

    - ``idle`` seconds pass without any frame from the server (queue is
      drained — we're caught up to "latest sync"). This is the normal exit.
    - ``seconds`` total elapse (safety cap for pathological queues).

    ``fetch_groups`` controls whether to query every cached group's
    participant list before draining. Disabled by `wa sync`.
    """
    from wa.wabinary.jid import JID as _JID

    log = logging.getLogger("login")
    server_jid = _JID(server="s.whatsapp.net")
    signal = SignalSession(device)
    # Cached own JIDs for self-vs-peer classification on incoming
    # `<message>` frames when we send `<receipt>` back.
    own_pn = JID.parse(device.jid) if device.jid else JID()
    own_lid = JID.parse(device.lid) if device.lid else None

    # First-pair: upload one-time prekeys so the phone's UI can resolve.
    # The phone's "Linked Devices" dialog stays in "connecting…" state
    # until the server reports we have a healthy pre-key bundle on file.
    if not device.prekeys_uploaded:
        await _upload_prekeys(fs, ns, device)

    active_iq = Node(
        tag="iq",
        attrs={
            "to": server_jid,
            "type": "set",
            "id": "set-active-1",
            "xmlns": "passive",
        },
        content=[Node(tag="active")],
    )
    await fs.send(ns.encrypt_frame(encode_node(active_iq)))
    # Wait for the server to confirm our active state. Group-info IQs sent
    # before this is acknowledged get silently dropped (server still treats
    # us as passive at the time of receipt).
    active_deadline = asyncio.get_event_loop().time() + 5.0
    while asyncio.get_event_loop().time() < active_deadline:
        try:
            ct = await fs.recv(timeout=0.5)
        except (asyncio.TimeoutError, ConnectionError):
            break
        try:
            n = decode_node(ns.decrypt_frame(ct))
        except Exception:
            continue
        if n.tag == "iq" and n.attrs.get("id") == "set-active-1":
            log.debug("active state confirmed")
            break
        log.debug("pre-active drain: %s id=%s", n.tag, n.attrs.get("id"))
    await _send_presence_available(fs, ns, device)
    if fetch_groups:
        click.echo(
            click.style(
                "authenticated — fetching group participants for name resolution...",
                fg="green",
            )
        )
        await _fetch_group_participants(fs, ns)
    else:
        # Skip the full TTL-aware refresh but still resolve names for any
        # groups we've recently joined (chats.json has the JID but no
        # subject). Cheap — typically 0 groups, occasionally a couple.
        await _fetch_group_participants(fs, ns, only_unnamed=True)
        click.echo(click.style("authenticated — draining offline queue...", fg="green"))

    # Idle-timeout drain. We don't know in advance how much queued mail
    # the server will replay, so commit to a hard wall (`seconds`) but
    # exit early as soon as the server goes quiet for `idle` seconds —
    # at that point the offline queue is empty and we're caught up.
    started = asyncio.get_event_loop().time()
    hard_deadline = started + seconds
    last_frame_ts = started
    frames_seen = 0
    while True:
        now = asyncio.get_event_loop().time()
        if now >= hard_deadline:
            log.info("hit hard cap of %.0fs (received %d frames)", seconds, frames_seen)
            break
        if now - last_frame_ts >= idle:
            log.info(
                "queue idle for %.1fs after %d frames — caught up",
                idle, frames_seen,
            )
            break
        # Wake at whichever boundary comes first (idle or hard cap), so we
        # never block past either.
        wait = min(idle - (now - last_frame_ts), hard_deadline - now)
        try:
            ct = await fs.recv(timeout=max(0.1, wait))
        except (asyncio.TimeoutError, ConnectionError):
            continue
        try:
            node = decode_node(ns.decrypt_frame(ct))
        except Exception as e:
            log.warning("failed to decode frame: %s", e)
            continue
        last_frame_ts = asyncio.get_event_loop().time()
        frames_seen += 1
        log.debug("post-success frame:\n%s", _pretty(node))
        # Try to decrypt any <enc> inside <message>; log plaintext metadata.
        if node.tag == "message":
            _try_decrypt_message(node, signal)
        # Server-initiated messages and notifications need an <ack> with
        # class=<tag>; this is the transport-level confirm that dequeues
        # offline deliveries. Without it the server keeps redelivering.
        if node.tag in ("message", "notification", "call", "receipt"):
            ack_attrs = {
                "class": node.tag,
                "id": node.attrs.get("id", ""),
                "to": node.attrs.get("from", server_jid),
            }
            for k in ("participant", "recipient"):
                if k in node.attrs:
                    ack_attrs[k] = node.attrs[k]
            if node.tag != "message" and "type" in node.attrs:
                ack_attrs["type"] = node.attrs["type"]
            ack = Node(tag="ack", attrs=ack_attrs)
            await fs.send(ns.encrypt_frame(encode_node(ack)))
        # The sender's "delivered ✓✓" indicator requires a <receipt> back
        # to them — separate from the transport <ack> above. Without
        # this, WhatsApp keeps re-queueing the same messages on every
        # connection because it never learns we received them. Type
        # `sender` for our own device-sent messages (echoes from other
        # linked devices), no type for peer messages = "delivered".
        if node.tag == "message":
            sender = node.attrs.get("participant") or node.attrs.get("from")
            is_own_echo = (
                isinstance(sender, JID)
                and sender.user
                in {own_pn.user, (own_lid.user if own_lid else None)}
            )
            receipt_attrs = {
                "id": node.attrs.get("id", ""),
                "to": node.attrs.get("from", server_jid),
            }
            for k in ("participant", "recipient"):
                if k in node.attrs:
                    receipt_attrs[k] = node.attrs[k]
            if is_own_echo:
                receipt_attrs["type"] = "sender"
            await fs.send(
                ns.encrypt_frame(encode_node(Node(tag="receipt", attrs=receipt_attrs)))
            )
        # ACK every server-initiated IQ so we don't look unresponsive.
        if node.tag == "iq" and node.attrs.get("type") == "set":
            ack = Node(
                tag="iq",
                attrs={
                    "to": node.attrs.get("from", server_jid),
                    "id": node.attrs.get("id", ""),
                    "type": "result",
                },
            )
            await fs.send(ns.encrypt_frame(encode_node(ack)))
    # Record successful drain so `wa status` can show how stale we are.
    try:
        import time as _time

        from wa.cache import save_sync_state

        save_sync_state({"last_sync_ts": int(_time.time()), "last_frames": frames_seen})
    except Exception as e:
        log.debug("could not persist sync state: %s", e)
    click.echo(
        click.style(f"session closed — drained {frames_seen} frames.", fg="green")
    )


async def _extend_chat(chat_jid: str, count: int) -> int:
    """Connect, request more history for ``chat_jid``, ingest, return msg count gained.

    Sends a ``peerDataOperationRequestMessage`` (Signal-encrypted) to our
    own phone with the oldest cached message ID for the chat. The phone
    replies with a fresh ``HistorySyncNotification`` (encrypted peer
    message) which our standard decrypt+save path captures as a blob, and
    we then run the existing ingest pipeline on disk.
    """
    import secrets

    from wa.cache import iter_messages
    from wa.peerreq import build_peer_data_request, pad_for_signal

    log = logging.getLogger("extend")

    device = Device.load()
    if device is None or not device.is_paired():
        raise RuntimeError("no paired device on disk; run `login` first")

    # Find the oldest cached message for this chat to anchor the request.
    rows = [m for m in iter_messages() if m.chat == chat_jid]
    if not rows:
        raise RuntimeError(f"no cached messages for {chat_jid}; need an anchor")
    rows.sort(key=lambda m: m.ts)
    anchor = rows[0]

    log.info(
        "anchor: ts=%d id=%s from_me=%s — requesting %d msgs older than this",
        anchor.ts, anchor.msg_id, anchor.from_me, count,
    )

    request_plaintext = build_peer_data_request(
        chat_jid=chat_jid,
        oldest_msg_id=anchor.msg_id,
        oldest_ts=anchor.ts,
        oldest_from_me=anchor.from_me,
        count=count,
    )
    padded = pad_for_signal(request_plaintext)

    blobs_before = _count_blob_files()

    async with FrameSocket() as fs:
        await fs.connect()
        ns = await do_handshake(fs, device, build_login_payload(device))
        # Wait for <success/>
        while True:
            ct = await fs.recv(timeout=15.0)
            node = decode_node(ns.decrypt_frame(ct))
            if node.tag == "success":
                break
            if node.tag == "failure":
                raise RuntimeError("login failed")

        # Active IQ.
        active = Node(
            tag="iq",
            attrs={
                "to": JID(server="s.whatsapp.net"),
                "type": "set",
                "id": "ext-act",
                "xmlns": "passive",
            },
            content=[Node(tag="active")],
        )
        await fs.send(ns.encrypt_frame(encode_node(active)))
        # Drain until active is confirmed (so the request isn't dropped).
        active_dl = asyncio.get_event_loop().time() + 5.0
        while asyncio.get_event_loop().time() < active_dl:
            try:
                ct = await fs.recv(timeout=0.5)
            except (asyncio.TimeoutError, ConnectionError):
                break
            n = decode_node(ns.decrypt_frame(ct))
            if n.tag == "iq" and n.attrs.get("id") == "ext-act":
                break
        await _send_presence_available(fs, ns, device)

        # Encrypt to our own *LID* device 0 (whatsmeow does the same — peer
        # messages go to the LID identity, even though the wire-level `to`
        # attribute is the PN form). Two session entries exist in our store:
        # the PN-keyed one and the LID-keyed one. We pick LID per the
        # whatsmeow reference.
        signal = SignalSession(device)
        own_pn = JID.parse(device.jid)
        own_lid = JID.parse(device.lid) if device.lid else None
        if own_lid is not None:
            ciphertext, kind = signal.encrypt_msg(own_lid.user, 0, padded)
        else:
            ciphertext, kind = signal.encrypt_msg(own_pn.user, 0, padded)
        peer_msg = Node(
            tag="message",
            attrs={
                "to": JID(user=own_pn.user, server="s.whatsapp.net"),
                "category": "peer",
                "type": "text",
                # WhatsApp message IDs are uppercase hex of length 22 prefixed
                # with 3EB0 for outgoing client-generated. The server filters
                # off-format IDs from peer routing.
                "id": "3EB0" + secrets.token_hex(9).upper(),
                # On-demand history requests need these flags or the phone
                # quietly drops them (per whatsmeow preparePeerMessageNode).
                "push_priority": "high_force",
                "privacy_sensitive": "1",
            },
            content=[
                Node(tag="meta", attrs={"appdata": "default"}),
                Node(tag="enc", attrs={"v": "2", "type": kind}, content=ciphertext),
            ],
        )
        await fs.send(ns.encrypt_frame(encode_node(peer_msg)))
        log.info("on-demand request sent (%s, %d-byte payload)", kind, len(ciphertext))

        # Drain incoming messages for ~20s. We only care about peer
        # messages whose decryption yields a HistorySyncNotification blob;
        # the regular _try_decrypt_message hook persists those to disk.
        signal_for_decrypt = SignalSession(device)
        log.info("listening for history reply (60s)...")
        listen_dl = asyncio.get_event_loop().time() + 60.0
        while asyncio.get_event_loop().time() < listen_dl:
            try:
                ct = await fs.recv(timeout=max(0.1, listen_dl - asyncio.get_event_loop().time()))
            except (asyncio.TimeoutError, ConnectionError):
                break
            try:
                node = decode_node(ns.decrypt_frame(ct))
            except Exception as e:
                log.debug("decode skip: %s", e)
                continue
            log.debug(
                "rx %s id=%s from=%s type=%s category=%s",
                node.tag,
                node.attrs.get("id"),
                node.attrs.get("from"),
                node.attrs.get("type"),
                node.attrs.get("category"),
            )
            if node.tag == "message":
                _try_decrypt_message(node, signal_for_decrypt)
                # ack so server stops redelivering
                ack = Node(
                    tag="ack",
                    attrs={
                        "class": "message",
                        "id": node.attrs.get("id", ""),
                        "to": node.attrs.get("from", JID(server="s.whatsapp.net")),
                    },
                )
                if "participant" in node.attrs:
                    ack.attrs["participant"] = node.attrs["participant"]
                await fs.send(ns.encrypt_frame(encode_node(ack)))

    # Now process any new blobs into the cache.
    blobs_after = _count_blob_files()
    log.info("blobs delta: %d → %d", blobs_before, blobs_after)
    return await _ingest_now(chat_jid)


def _count_blob_files() -> int:
    p = Path.home() / ".cache" / "whatsapp-cli" / "blobs"
    if not p.exists():
        return 0
    return sum(1 for _ in p.iterdir() if _.is_file())


async def _ingest_now(chat_jid: str) -> int:
    """Run ingest in-process and return how many new messages landed for chat_jid."""
    from wa.cache import iter_messages

    before = sum(1 for m in iter_messages() if m.chat == chat_jid)
    # Re-use the ingest CLI command body via a direct call. The command is
    # decorated with @cli.command(), but its callback is accessible.
    ingest.callback(download=False)  # type: ignore[attr-defined]
    after = sum(1 for m in iter_messages() if m.chat == chat_jid)
    return after - before


async def _login_handshake(
    device: Device,
    *,
    seconds: float = 60.0,
    idle: float = 3.0,
    fetch_groups: bool = True,
) -> None:
    """Handshake with the login payload (not registration) and wait for <success/>.

    The phone's Linked Devices dialog only closes once this step completes
    against the server; it's also the normal startup path for an already-
    paired device. ``seconds``, ``idle``, and ``fetch_groups`` forward to
    :func:`_post_success` for the post-auth drain phase.
    """
    async with FrameSocket() as fs:
        await fs.connect()
        try:
            ns = await do_handshake(fs, device, build_login_payload(device))
        except Exception as e:
            click.echo(
                click.style(f"login handshake failed: {e}", fg="red"), err=True
            )
            raise SystemExit(1)
        while True:
            try:
                ct = await fs.recv(timeout=15.0)
            except (asyncio.TimeoutError, ConnectionError):
                click.echo(
                    click.style("no <success/> received on login", fg="red"),
                    err=True,
                )
                return
            node = decode_node(ns.decrypt_frame(ct))
            if node.tag == "success":
                await _post_success(
                    fs,
                    ns,
                    device,
                    seconds=seconds,
                    idle=idle,
                    fetch_groups=fetch_groups,
                )
                return
            if node.tag == "failure":
                click.echo(
                    click.style(f"login rejected: {_pretty(node)}", fg="red"),
                    err=True,
                )
                return
            logging.getLogger("login").debug(
                "login-phase frame (ignored):\n%s", _pretty(node)
            )


if __name__ == "__main__":
    cli()
