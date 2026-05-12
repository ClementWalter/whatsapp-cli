"""SignalSession — glue between our Device + wabinary world and libsignal.

Responsibilities:

- Convert the Device's raw Curve25519 keys into libsignal's IdentityKeyPair.
- Populate the signed pre-key record at id 1 (what we advertise to the
  server during pairing).
- Decrypt incoming ``<enc type="pkmsg">`` / ``<enc type="msg">`` payloads
  to plaintext bytes.
- Persist and reload Signal state (sessions, pre-keys) across runs so the
  ratchet is durable.

Session state lives in ``~/.config/whatsapp-user-cli/signal.json``. Each
session is keyed by ``user:device`` (e.g. ``33629442167:0``), matching the
address libsignal uses for its ProtocolAddress.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path

from signal_protocol import (
    address,
    curve,
    group_cipher,
    identity_key,
    protocol,
    sender_keys,
    session,
    session_cipher,
    state,
    storage,
)

from wa.store import DEFAULT_CONFIG_DIR, Device

log = logging.getLogger(__name__)

DEFAULT_SIGNAL_PATH = DEFAULT_CONFIG_DIR / "signal.json"


def _unpad(plaintext: bytes) -> bytes:
    """Strip WhatsApp's trailing padding (last byte = padding length).

    Matches whatsmeow's unpadMessage: each padding byte equals the count,
    so the final byte tells us how many bytes to drop.
    """
    if not plaintext:
        return plaintext
    pad = plaintext[-1]
    if 0 < pad <= len(plaintext):
        return plaintext[:-pad]
    return plaintext


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _unb64(s: str) -> bytes:
    return base64.b64decode(s)


class SignalSession:
    """libsignal-backed session store seeded from our Device.

    Concurrency-unsafe; intended for single-threaded use inside the
    asyncio event loop.
    """

    def __init__(self, device: Device, signal_path: Path = DEFAULT_SIGNAL_PATH):
        self._device = device
        self._path = signal_path
        self._store = self._build_store()
        self._sessions: dict[str, bytes] = {}
        self._load_sessions()

    # --- store construction ------------------------------------------------

    def _build_store(self) -> storage.InMemSignalProtocolStore:
        """Convert our 32-byte Curve25519 keys into libsignal's types.

        libsignal public keys are 33 bytes (0x05 DjbType prefix + 32-byte
        x-coordinate); private keys stay 32 bytes.
        """
        priv = curve.PrivateKey.deserialize(self._device.identity_key.priv)
        pub = identity_key.IdentityKey(b"\x05" + self._device.identity_key.pub)
        ikp = identity_key.IdentityKeyPair(pub, priv)
        store = storage.InMemSignalProtocolStore(ikp, self._device.registration_id)

        # Reconstruct our signed pre-key so peers' pkmsg can be decrypted.
        spk_priv = curve.PrivateKey.deserialize(self._device.signed_pre_key.priv)
        spk_pub = curve.PublicKey.deserialize(b"\x05" + self._device.signed_pre_key.pub)
        spk_kp = curve.KeyPair(spk_pub, spk_priv)
        spk_record = state.SignedPreKeyRecord(
            self._device.signed_pre_key.key_id,
            # libsignal accepts a timestamp; 0 is fine for our purposes.
            0,
            spk_kp,
            self._device.signed_pre_key.signature,
        )
        store.save_signed_pre_key(self._device.signed_pre_key.key_id, spk_record)

        # Re-install every one-time prekey we've previously uploaded so the
        # ratchet can decrypt fresh pkmsgs from peers that picked one of
        # them out of the server-side bundle. Without this, X3DH fails with
        # "invalid prekey identifier".
        for k in self._device.one_time_prekeys:
            try:
                opk_pub = curve.PublicKey.deserialize(b"\x05" + k["pub"])
                opk_priv = curve.PrivateKey.deserialize(k["priv"])
                opk_kp = curve.KeyPair(opk_pub, opk_priv)
                store.save_pre_key(int(k["key_id"]), state.PreKeyRecord(int(k["key_id"]), opk_kp))
            except Exception as e:
                log.warning("failed to load one-time prekey %s: %s", k.get("key_id"), e)
        return store

    # --- addresses / decryption -------------------------------------------

    @staticmethod
    def _address_from_jid(jid_user: str, device_num: int) -> "address.ProtocolAddress":
        return address.ProtocolAddress(jid_user, device_num)

    def decrypt_pkmsg(self, jid_user: str, device_num: int, ciphertext: bytes) -> bytes:
        """Decrypt a PreKeySignalMessage (``<enc type="pkmsg">``).

        First message from a new peer device; also establishes the session
        so subsequent ``msg`` types can use the ratchet without re-keying.
        """
        msg = protocol.PreKeySignalMessage.try_from(ciphertext)
        addr = self._address_from_jid(jid_user, device_num)
        plaintext = session_cipher.message_decrypt_prekey(self._store, addr, msg)
        self._persist_session(addr)
        return _unpad(bytes(plaintext))

    def process_sender_key_distribution(
        self,
        group_id: str,
        sender_user: str,
        sender_device: int,
        skdm_bytes: bytes,
    ) -> None:
        """Install a sender key from the inner axolotl SKDM bytes.

        Call this after decrypting a ``<enc type="pkmsg">`` whose plaintext
        ``Message`` has ``senderKeyDistributionMessage.axolotlSenderKeyDistributionMessage``
        populated. Future ``skmsg`` from the same sender decrypt under the
        key we save here.
        """
        skdm = protocol.SenderKeyDistributionMessage.try_from(skdm_bytes)
        skn = sender_keys.SenderKeyName(
            group_id, address.ProtocolAddress(sender_user, sender_device)
        )
        group_cipher.process_sender_key_distribution_message(skn, skdm, self._store)

    def decrypt_skmsg(
        self,
        group_id: str,
        sender_user: str,
        sender_device: int,
        ciphertext: bytes,
    ) -> bytes:
        """Decrypt an ``<enc type="skmsg">`` — the real group message content.

        Requires a prior ``process_sender_key_distribution`` for this group +
        sender. Returns the inner plaintext (typically a padded waE2E.Message).
        """
        skm = protocol.SenderKeyMessage.try_from(ciphertext)
        skn = sender_keys.SenderKeyName(
            group_id, address.ProtocolAddress(sender_user, sender_device)
        )
        plaintext = group_cipher.group_decrypt(skm.serialize(), self._store, skn)
        return _unpad(bytes(plaintext))

    def save_one_time_prekeys(self, prekeys: list[dict]) -> None:
        """Persist one-time prekeys' private halves into the libsignal store.

        After uploading the public halves to the server, the phone may
        encrypt incoming pkmsgs by referencing one of these prekey IDs.
        Decryption needs the matching private key locally — without this,
        libsignal raises ``invalid prekey identifier``.
        """
        for k in prekeys:
            kp_pub = curve.PublicKey.deserialize(b"\x05" + k["pub"])
            kp_priv = curve.PrivateKey.deserialize(k["priv"])
            kp = curve.KeyPair(kp_pub, kp_priv)
            rec = state.PreKeyRecord(k["key_id"], kp)
            self._store.save_pre_key(k["key_id"], rec)

    def create_sender_key_distribution(
        self, group_id: str, sender_user: str, sender_device: int
    ) -> bytes:
        """Build or refresh our sender key for ``group_id`` and return the SKDM.

        Returned bytes are the serialized ``AxolotlSenderKeyDistributionMessage``
        — to be wrapped in a ``Message{senderKeyDistributionMessage: ...}``
        and Signal-encrypted to each recipient device so they can install
        the key and decrypt subsequent ``skmsg`` payloads in this group.
        """
        sender_addr = self._address_from_jid(sender_user, sender_device)
        name = sender_keys.SenderKeyName(group_id, sender_addr)
        skdm = group_cipher.create_sender_key_distribution_message(name, self._store)
        return bytes(skdm.serialize())

    def group_encrypt(
        self,
        group_id: str,
        sender_user: str,
        sender_device: int,
        plaintext: bytes,
    ) -> bytes:
        """Encrypt ``plaintext`` for the group, returns the ``skmsg`` ciphertext."""
        sender_addr = self._address_from_jid(sender_user, sender_device)
        name = sender_keys.SenderKeyName(group_id, sender_addr)
        return bytes(group_cipher.group_encrypt(self._store, name, plaintext))

    def has_session(self, jid_user: str, device_num: int) -> bool:
        """True if we have an established Signal session with this address.

        Cheaper than trying to encrypt and catching the exception; used
        by the send path to decide whether to fetch a prekey bundle.
        """
        addr = self._address_from_jid(jid_user, device_num)
        try:
            return self._store.load_session(addr) is not None
        except Exception:
            return False

    def install_prekey_bundle(
        self,
        jid_user: str,
        device_num: int,
        *,
        registration_id: int,
        identity_key_pub: bytes,
        signed_pre_key_id: int,
        signed_pre_key_pub: bytes,
        signed_pre_key_signature: bytes,
        pre_key_id: int | None = None,
        pre_key_pub: bytes | None = None,
    ) -> None:
        """Bootstrap a fresh outbound Signal session from a server-fetched bundle.

        Required for sending to a peer/device we've never received from.
        After this call, ``encrypt_msg(jid_user, device_num, ...)``
        returns a ``pkmsg`` (PreKeySignalMessage) the recipient can
        decrypt via X3DH. ``identity_key_pub`` / ``*_pub`` are the raw
        32-byte Curve25519 publics returned by the prekey IQ, **without**
        the leading ``0x05`` DJB-type byte — we add it here.
        """
        ik = identity_key.IdentityKey(b"\x05" + identity_key_pub)
        spk = curve.PublicKey.deserialize(b"\x05" + signed_pre_key_pub)
        opk = (
            curve.PublicKey.deserialize(b"\x05" + pre_key_pub)
            if pre_key_pub is not None
            else None
        )
        bundle = state.PreKeyBundle(
            registration_id,
            device_num,
            pre_key_id,
            opk,
            signed_pre_key_id,
            spk,
            signed_pre_key_signature,
            ik,
        )
        addr = self._address_from_jid(jid_user, device_num)
        session.process_prekey_bundle(addr, self._store, bundle)
        self._persist_session(addr)

    def encrypt_msg(self, jid_user: str, device_num: int, plaintext: bytes) -> tuple[bytes, str]:
        """Encrypt a Signal message for an existing peer session.

        Returns ``(ciphertext, type_str)`` where ``type_str`` is ``"pkmsg"``
        for an initial X3DH message (CiphertextMessage type 3) or ``"msg"``
        for a regular Double-Ratchet message (type 1, 2). Caller wraps in
        ``<enc type="…" v="2">``.
        """
        addr = self._address_from_jid(jid_user, device_num)
        ct = session_cipher.message_encrypt(self._store, addr, plaintext)
        self._persist_session(addr)
        # CiphertextMessage.message_type: 3 = PreKeySignalMessage, 1/2 = SignalMessage
        mtype = ct.message_type()
        kind = "pkmsg" if mtype == 3 else "msg"
        return bytes(ct.serialize()), kind

    def decrypt_msg(self, jid_user: str, device_num: int, ciphertext: bytes) -> bytes:
        """Decrypt a SignalMessage (``<enc type="msg">``).

        Assumes a session is already established via a prior pkmsg.
        """
        msg = protocol.SignalMessage.try_from(ciphertext)
        addr = self._address_from_jid(jid_user, device_num)
        plaintext = session_cipher.message_decrypt_signal(self._store, addr, msg)
        self._persist_session(addr)
        return _unpad(bytes(plaintext))

    # --- persistence ------------------------------------------------------

    def _session_key(self, addr: "address.ProtocolAddress") -> str:
        return f"{addr.name()}:{addr.device_id()}"

    def _persist_session(self, addr: "address.ProtocolAddress") -> None:
        s = self._store.load_session(addr)
        if s is None:
            return
        self._sessions[self._session_key(addr)] = s.serialize()
        self._save_sessions()

    def _load_sessions(self) -> None:
        if not self._path.exists():
            return
        data = json.loads(self._path.read_text())
        for key, b64 in data.get("sessions", {}).items():
            user, _, dev = key.rpartition(":")
            try:
                addr = self._address_from_jid(user, int(dev))
                rec = state.SessionRecord.deserialize(_unb64(b64))
                self._store.store_session(addr, rec)
                self._sessions[key] = _unb64(b64)
            except Exception as e:
                log.warning("failed to restore session %s: %s", key, e)

    def _save_sessions(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        payload = {"sessions": {k: _b64(v) for k, v in self._sessions.items()}}
        tmp.write_text(json.dumps(payload, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(self._path)
