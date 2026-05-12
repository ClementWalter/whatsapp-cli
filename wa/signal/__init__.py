"""Signal Double Ratchet integration (via ``signal-protocol`` PyO3 bindings).

Wraps Signal Foundation's libsignal so we can decrypt WhatsApp's
``<enc type="pkmsg|msg|skmsg">`` payloads. We hand the crypto off to
libsignal and just translate between our on-disk Device / JID world and
libsignal's IdentityKeyPair / ProtocolAddress / SessionRecord types.
"""

from .session import SignalSession

__all__ = ["SignalSession"]
