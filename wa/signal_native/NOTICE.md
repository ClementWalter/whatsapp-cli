# Signal protocol sources

The files in `protos/` are from Signal Desktop commit
`f3040f3e13b6dca5021c3db149320e0f7a771f0a`:
https://github.com/signalapp/Signal-Desktop/tree/f3040f3e13b6dca5021c3db149320e0f7a771f0a/protos

They retain their original copyright and AGPL-3.0-only notices. The accompanying
license is in `protos/LICENSE`. The official `@signalapp/libsignal-client`
dependency is also licensed under AGPL-3.0-only. Dependency versions and hashes
are recorded in `package-lock.json`.

Provisioning, sealed-sender trust roots, backup framing, and service endpoints
follow the Signal Desktop sources at that commit. The JavaScript modules here
implement this repository's client; no external Signal executable is launched.
`signal-ca.pem` is the `certificateAuthority` value from its `config/default.json`.

The account-data test fixture is derived from the libsignal `v0.105.1`
`rust/message-backup/tests/res/test-cases/valid/account-data.jsonproto` fixture,
also licensed under AGPL-3.0-only.
