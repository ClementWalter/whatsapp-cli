---
name: whatsapp-user-cli
description:
  "Python WhatsApp Web client with CLI. Reads your own chats programmatically
  (no Meta Business API, no Playwright, no Baileys/whatsmeow runtime dep). The
  protocol layer is reimplemented in Python and oracle-diffed against whatsmeow;
  Signal session decryption uses the `signal-protocol` Rust binding. Status:
  pairing, history sync, and read-only chat browsing work end-to-end; media
  download and live app-state sync are partial."
allowed-tools:
  - Bash
  - Read
---

# WhatsApp User CLI

Terminal access to WhatsApp using protocol-level reimplementation — reads
end-to-end-encrypted chats by pairing as a linked device, same path a phone or
WhatsApp Web uses.

This is **not** the Cloud Business API (which cannot read personal chats) and
**not** a Playwright browser scraper. The client speaks WhatsApp's native
protocol: Noise handshake over WebSocket, Signal Double Ratchet for messages,
LtHash-verified app-state sync for chat metadata.

## Status

| Stage | Scope                                           | State       |
| ----- | ----------------------------------------------- | ----------- |
| 1     | Binary XML codec (tokens, nodes, JIDs, packing) | ✅ complete |
| 2     | Noise handshake + WebSocket transport           | ✅ complete |
| 3     | QR pairing + device registration                | ✅ complete |
| 4     | Login, keepalive, IQ round-trip                 | ✅ complete |
| 5     | Signal session, message decrypt                 | ✅ via `signal-protocol` binding |
| 6     | CLI surface (status/login/chats/read/ingest)    | ✅ complete |
| 7     | App-state sync (chat names, mutes, archives)    | partial — bootstrap history works, live sync TBD |
| 8     | Media download (AES-CBC + HMAC)                 | stub        |

## Dependencies

Not dep-free. Runtime requirements (declared in `scripts/whatsapp_user_cli.py`
via PEP 723 and in `pyproject.toml`):

- `aiohttp` — WebSocket transport
- `cryptography` — X25519, AES-GCM, HKDF for Noise
- `signal-protocol` — Rust-backed libsignal binding (Double Ratchet, Sender Keys)
- `protobuf` — wire format for handshake / app-state messages
- `click` — CLI framework
- `qrcode[pil]` — render pairing QR in the terminal
- `pyobjc-framework-Contacts` — macOS-only, for `import-contacts`

Only `wa/wabinary/` is pure stdlib.

## Layout

```
whatsapp-cli/
  wa/
    wabinary/      # binary XML codec  (stage 1)
    crypto/        # Noise, XEdDSA, key derivation
    transport/     # WebSocket + framing
    signal/        # libsignal glue (Double Ratchet + Sender Keys)
    proto/         # vendored .proto schemas (MPL-2.0 from whatsmeow, MIT from wa-proto)
    pair.py        # QR pairing + device registration
    handshake.py   # Noise XX choreography
    clientpayload.py, store.py, cache.py, history.py, ...
  tools/oracle/    # Go binary wrapping whatsmeow — stdio JSON for tests
  tests/           # pytest, each assertion byte- or semantic-equal to oracle
  scripts/whatsapp_user_cli.py  # CLI entry point
```

## Development

All validation is oracle-diffed: every Python layer compares against a
known-good reference (whatsmeow) byte-for-byte before any network code runs.
Build the oracle once, then iterate in Python:

```bash
# one-time oracle build
cd tools/oracle && go build -o oracle .

# test loop (uv resolves deps from pyproject.toml's [test] group)
uv run --group test pytest tests/ -v
```

## Legal posture

Reverse-engineered clients run against WhatsApp's ToS. Meta has sent
cease-and-desist letters to popular libs (e.g. Baileys). This skill is scoped to
**single-user personal tooling**, not a service. Use on a number you can afford
to have banned; expect periodic breakage when WA bumps protocol tokens; no Meta
branding anywhere.
