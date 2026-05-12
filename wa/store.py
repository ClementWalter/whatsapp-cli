"""Persistent device state.

Mirrors the subset of whatsmeow/store.Device needed for pairing +
subsequent logins. Stored as JSON at
``~/.config/whatsapp-user-cli/device.json`` by default (chmod 600).

The three-secret core is:

- ``noise_key``    — Curve25519 keypair used as the Noise XX static key,
                     presented to the server on every reconnect.
- ``identity_key`` — Curve25519 keypair, the Signal identity. Used for
                     XEdDSA signatures on device identities.
- ``adv_secret``   — 32 random bytes that bind pair QR / pair-code to the
                     ADVSignedDeviceIdentity the server later returns.

Everything else (JID, account blob, platform) arrives from the server
during pairing and is added to this struct on disk.
"""

from __future__ import annotations

import json
import os
import secrets
import struct
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from wa.crypto.noise import x25519_generate, x25519_public
from wa.crypto.xeddsa import _mont_x_to_ed_y  # noqa: F401 (exercised indirectly)

DEFAULT_CONFIG_DIR = Path(os.environ.get("WA_CLI_HOME", "~/.config/whatsapp-user-cli")).expanduser()
DEFAULT_DEVICE_PATH = DEFAULT_CONFIG_DIR / "device.json"


def _b64(b: bytes) -> str:
    import base64

    return base64.b64encode(b).decode()


def _unb64(s: str) -> bytes:
    import base64

    return base64.b64decode(s)


@dataclass
class KeyPair:
    priv: bytes
    pub: bytes

    @classmethod
    def generate(cls) -> "KeyPair":
        priv, pub = x25519_generate()
        return cls(priv=priv, pub=pub)

    def to_dict(self) -> dict[str, str]:
        return {"priv": _b64(self.priv), "pub": _b64(self.pub)}

    @classmethod
    def from_dict(cls, d: dict[str, str]) -> "KeyPair":
        return cls(priv=_unb64(d["priv"]), pub=_unb64(d["pub"]))


@dataclass
class SignedPreKey:
    key_id: int
    priv: bytes
    pub: bytes
    signature: bytes  # 64-byte XEdDSA signature over ``pub`` by IdentityKey

    def to_dict(self) -> dict[str, Any]:
        return {
            "key_id": self.key_id,
            "priv": _b64(self.priv),
            "pub": _b64(self.pub),
            "signature": _b64(self.signature),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SignedPreKey":
        return cls(
            key_id=int(d["key_id"]),
            priv=_unb64(d["priv"]),
            pub=_unb64(d["pub"]),
            signature=_unb64(d["signature"]),
        )


@dataclass
class Device:
    noise_key: KeyPair
    identity_key: KeyPair
    signed_pre_key: SignedPreKey
    registration_id: int
    adv_secret: bytes  # 32 bytes

    # Populated only after a successful pair:
    jid: str = ""
    lid: str = ""
    platform: str = ""
    business_name: str = ""
    account: bytes = b""  # marshalled ADVSignedDeviceIdentity
    push_name: str = ""

    # Set true after the first successful one-time-prekey upload, so we don't
    # re-upload on every reconnect. The phone's "Linked Devices" UI uses the
    # presence of these keys to decide whether the device is fully online.
    prekeys_uploaded: bool = False
    next_prekey_id: int = 2  # 1 is reserved for the SignedPreKey

    # Private halves of every one-time prekey we've ever uploaded. The
    # server only stores the publics; when a peer encrypts a pkmsg
    # referencing one of these IDs we need the matching priv locally to
    # decrypt. List of dicts ``{key_id, priv, pub}``.
    one_time_prekeys: list[dict] = field(default_factory=list)

    @classmethod
    def new(cls) -> "Device":
        """Generate a fresh device (call once before pairing)."""
        from wa.crypto.xeddsa_sign import xeddsa_sign  # local import, avoid cycle

        noise = KeyPair.generate()
        identity = KeyPair.generate()
        spk = KeyPair.generate()
        # Signal convention: prepend the DjbType byte (0x05) before signing
        # the public key. Matches whatsmeow/util/keys/keypair.go:Sign. The
        # phone verifies this exact signed-prekey bundle on scan; if we sign
        # the raw pub instead, pair fails with server stream:error 500.
        sig = xeddsa_sign(identity.priv, bytes([0x05]) + spk.pub)
        return cls(
            noise_key=noise,
            identity_key=identity,
            signed_pre_key=SignedPreKey(key_id=1, priv=spk.priv, pub=spk.pub, signature=sig),
            # 14-bit registration ID to match whatsmeow's convention.
            registration_id=secrets.randbits(14),
            adv_secret=secrets.token_bytes(32),
        )

    # --- persistence ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "noise_key": self.noise_key.to_dict(),
            "identity_key": self.identity_key.to_dict(),
            "signed_pre_key": self.signed_pre_key.to_dict(),
            "registration_id": self.registration_id,
            "adv_secret": _b64(self.adv_secret),
            "jid": self.jid,
            "lid": self.lid,
            "platform": self.platform,
            "business_name": self.business_name,
            "account": _b64(self.account),
            "push_name": self.push_name,
            "prekeys_uploaded": self.prekeys_uploaded,
            "next_prekey_id": self.next_prekey_id,
            "one_time_prekeys": [
                {"key_id": k["key_id"], "priv": _b64(k["priv"]), "pub": _b64(k["pub"])}
                for k in self.one_time_prekeys
            ],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Device":
        return cls(
            noise_key=KeyPair.from_dict(d["noise_key"]),
            identity_key=KeyPair.from_dict(d["identity_key"]),
            signed_pre_key=SignedPreKey.from_dict(d["signed_pre_key"]),
            registration_id=int(d["registration_id"]),
            adv_secret=_unb64(d["adv_secret"]),
            jid=d.get("jid", ""),
            lid=d.get("lid", ""),
            platform=d.get("platform", ""),
            business_name=d.get("business_name", ""),
            account=_unb64(d.get("account", "")) if d.get("account") else b"",
            push_name=d.get("push_name", ""),
            prekeys_uploaded=bool(d.get("prekeys_uploaded", False)),
            next_prekey_id=int(d.get("next_prekey_id", 2)),
            one_time_prekeys=[
                {"key_id": int(k["key_id"]), "priv": _unb64(k["priv"]), "pub": _unb64(k["pub"])}
                for k in d.get("one_time_prekeys", [])
            ],
        )

    def save(self, path: Path = DEFAULT_DEVICE_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path = DEFAULT_DEVICE_PATH) -> "Device | None":
        if not path.exists():
            return None
        return cls.from_dict(json.loads(path.read_text()))

    # --- convenience ----------------------------------------------------

    def is_paired(self) -> bool:
        return bool(self.jid) and bool(self.account)

    def registration_id_bytes(self) -> bytes:
        """Big-endian 32-bit for DevicePairingData.ERegid."""
        return struct.pack(">I", self.registration_id)

    def signed_prekey_id_bytes(self) -> bytes:
        """Big-endian 24-bit (!) for DevicePairingData.ESkeyID."""
        return struct.pack(">I", self.signed_pre_key.key_id)[1:]
