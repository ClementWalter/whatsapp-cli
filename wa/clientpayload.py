"""Build ``ClientPayload`` protobuf messages for the Noise ClientFinish.

Two shapes:

- **Registration** — for a fresh device, carrying DevicePairingData with the
  identity/prekey bundle the server uses to produce a pair-device IQ.
- **Login** — for a reconnect, identified only by JID + device number.
"""

from __future__ import annotations

import hashlib
import struct

import logging
import re
import socket
import subprocess
import sys
import urllib.request

from wa.proto import waCompanionReg_pb2, waWa6_pb2
from wa.store import Device

log = logging.getLogger(__name__)


def device_label() -> str:
    """Identifier shown on the phone's Linked Devices list and used as the
    presence ``name`` attribute.

    Shape: ``whatsapp-cli-<host>``, where ``<host>`` is the macOS Computer
    Name when available (user-friendly, matches what System Settings shows)
    and falls back to the hostname elsewhere. A non-generic name makes it
    possible to distinguish multiple CLI installs on different machines
    paired to the same WhatsApp account.
    """
    host = ""
    if sys.platform == "darwin":
        try:
            r = subprocess.run(
                ["scutil", "--get", "ComputerName"],
                capture_output=True, text=True, timeout=2,
            )
            if r.returncode == 0:
                host = r.stdout.strip()
        except Exception:
            pass
    if not host:
        host = socket.gethostname()
    host = host.removesuffix(".local")
    return f"whatsapp-cli-{host}" if host else "whatsapp-cli"

# Fallback WA Web version if we can't fetch a fresh one. Sync periodically
# with whatsmeow's ``waVersion`` in store/clientpayload.go; a stale tuple
# causes a 500 ``stream:error`` once pairing reaches the phone.
WA_VERSION_FALLBACK = (2, 3000, 1037989485)
_CLIENT_REVISION_RE = re.compile(rb'"client_revision":(\d+)')
_cached_version: tuple[int, int, int] | None = None


def get_wa_version() -> tuple[int, int, int]:
    """Best-effort fetch of the current WA Web version.

    Scrapes ``web.whatsapp.com`` once per process for ``client_revision``;
    if anything goes wrong (network, HTML layout change, throttling) we fall
    back to the hardcoded tuple. Stale versions produce server error 500
    mid-pairing, so prefer dynamic.
    """
    global _cached_version
    if _cached_version is not None:
        return _cached_version
    try:
        req = urllib.request.Request(
            "https://web.whatsapp.com/",
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                    "Version/17.0 Safari/605.1.15"
                )
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            html = resp.read()
        match = _CLIENT_REVISION_RE.search(html)
        if match:
            rev = int(match.group(1))
            _cached_version = (2, 3000, rev)
            log.info("fetched live WA Web version: 2.3000.%d", rev)
            return _cached_version
    except Exception as e:
        log.warning("failed to fetch live WA version, using fallback: %s", e)
    _cached_version = WA_VERSION_FALLBACK
    return _cached_version

# Curve25519 key-type tag (from libsignal DjbType).
ECC_DJB_TYPE = 0x05


def _device_props_bytes() -> bytes:
    props = waCompanionReg_pb2.DeviceProps()
    # `os` is what the phone displays for this companion in
    # Settings → Linked Devices. Including the host makes the entry
    # identifiable when more than one machine is paired.
    props.os = device_label()
    props.version.primary = 0
    props.version.secondary = 1
    props.version.tertiary = 0
    props.platformType = waCompanionReg_pb2.DeviceProps.UNKNOWN
    props.requireFullSync = False
    # History sync hints — match whatsmeow's defaults so we don't stand out.
    hs = props.historySyncConfig
    hs.storageQuotaMb = 10240
    hs.inlineInitialPayloadInE2EeMsg = True
    hs.supportCallLogHistory = False
    hs.supportBotUserAgentChatHistory = True
    hs.supportCagReactionsAndPolls = True
    hs.supportBizHostedMsg = True
    hs.supportRecentSyncChunkMessageCountTuning = True
    hs.supportHostedGroupMsg = True
    hs.supportFbidBotChatHistory = True
    hs.supportMessageAssociation = True
    hs.supportGroupHistory = True
    hs.thumbnailSyncDaysLimit = 60
    hs.supportManusHistory = True
    hs.supportHatchHistory = True
    # On-demand history sync gate — without this, the phone silently
    # refuses peerDataOperationRequestMessage(HISTORY_SYNC_ON_DEMAND).
    # whatsmeow / Baileys both leave this undefined, which is why
    # community-built clients see "phone never replies"; web.whatsapp.com
    # sets it `true`, which is why it works.
    hs.onDemandReady = True
    hs.completeOnDemandReady = True
    return props.SerializeToString()


def _base_payload() -> waWa6_pb2.ClientPayload:
    v = get_wa_version()
    p = waWa6_pb2.ClientPayload()
    ua = p.userAgent
    ua.platform = waWa6_pb2.ClientPayload.UserAgent.WEB
    ua.releaseChannel = waWa6_pb2.ClientPayload.UserAgent.RELEASE
    ua.appVersion.primary = v[0]
    ua.appVersion.secondary = v[1]
    ua.appVersion.tertiary = v[2]
    ua.mcc = "000"
    ua.mnc = "000"
    ua.osVersion = "0.1"
    ua.manufacturer = ""
    ua.device = "Desktop"
    ua.osBuildNumber = "0.1"
    ua.localeLanguageIso6391 = "en"
    ua.localeCountryIso31661Alpha2 = "US"

    p.webInfo.webSubPlatform = waWa6_pb2.ClientPayload.WebInfo.WEB_BROWSER
    p.connectType = waWa6_pb2.ClientPayload.WIFI_UNKNOWN
    p.connectReason = waWa6_pb2.ClientPayload.USER_ACTIVATED
    return p


def build_registration_payload(device: Device) -> bytes:
    """ClientPayload for a not-yet-paired device. Includes DevicePairingData."""
    p = _base_payload()
    d = p.devicePairingData
    d.eRegid = struct.pack(">I", device.registration_id)
    d.eKeytype = bytes([ECC_DJB_TYPE])
    d.eIdent = device.identity_key.pub
    # ESkeyID is a 24-bit BE integer (drop the high byte of a u32).
    d.eSkeyID = struct.pack(">I", device.signed_pre_key.key_id)[1:]
    d.eSkeyVal = device.signed_pre_key.pub
    d.eSkeySig = device.signed_pre_key.signature
    v = get_wa_version()
    d.buildHash = hashlib.md5(f"{v[0]}.{v[1]}.{v[2]}".encode()).digest()
    d.deviceProps = _device_props_bytes()
    p.passive = False
    p.pull = False
    return p.SerializeToString()


def build_login_payload(device: Device) -> bytes:
    """ClientPayload for reconnecting a paired device. Identifies by JID."""
    if not device.is_paired():
        raise RuntimeError("device not paired — use build_registration_payload")
    from wa.wabinary.jid import JID

    p = _base_payload()
    jid = JID.parse(device.jid)
    # Username is the phone-number portion parsed as u64.
    p.username = int(jid.user) if jid.user else 0
    p.device = jid.device
    p.passive = True
    p.pull = True
    p.lidDbMigrated = True
    p.lc = 1
    return p.SerializeToString()
