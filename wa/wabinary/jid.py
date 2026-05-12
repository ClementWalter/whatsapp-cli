"""JID — WhatsApp/XMPP-style identifiers.

Several server variants exist. The encoder uses the structural form to pick
the right tag byte (JID_PAIR for plain user@server, AD_JID for
agent.device-tagged variants, FB_JID / INTEROP_JID for the cross-product
Messenger path).
"""

from __future__ import annotations

from dataclasses import dataclass

# Server constants from whatsmeow/types/jid.go.
DEFAULT_USER_SERVER = "s.whatsapp.net"
HIDDEN_USER_SERVER = "lid"
GROUP_SERVER = "g.us"
BROADCAST_SERVER = "broadcast"
NEWSLETTER_SERVER = "newsletter"
MESSENGER_SERVER = "msgr"
INTEROP_SERVER = "interop"
HOSTED_SERVER = "hosted"
HOSTED_LID_SERVER = "hosted.lid"
SERVER_JID = "s.whatsapp.net"


@dataclass(frozen=True)
class JID:
    user: str = ""
    server: str = DEFAULT_USER_SERVER
    agent: int = 0
    device: int = 0
    integrator: int = 0

    @classmethod
    def parse(cls, s: str) -> "JID":
        """Parse a canonical JID string.

        Accepts: "user@server", "user.agent:device@server", "user:device@server".
        """
        if "@" not in s:
            return cls(user="", server=s)
        user_part, server = s.split("@", 1)
        agent = 0
        device = 0
        if ":" in user_part:
            user_part, dev_str = user_part.split(":", 1)
            device = int(dev_str)
        if "." in user_part and server != DEFAULT_USER_SERVER:
            # ad-form only when the server expects it; be permissive and split
            u, a = user_part.rsplit(".", 1)
            try:
                agent = int(a)
                user_part = u
            except ValueError:
                pass
        return cls(user=user_part, server=server, agent=agent, device=device)

    def is_ad(self) -> bool:
        """True if this JID needs the AD_JID (agent.device) wire encoding."""
        return (
            (self.server in (DEFAULT_USER_SERVER, HIDDEN_USER_SERVER) and self.device > 0)
            or self.server in (HOSTED_SERVER, HOSTED_LID_SERVER)
        )

    def actual_agent(self) -> int:
        """The agent byte written in AD-form. LID server implicitly has agent=1."""
        if self.agent:
            return self.agent
        if self.server == HIDDEN_USER_SERVER:
            return 1
        return 0

    def __str__(self) -> str:
        if not self.user:
            return self.server
        base = self.user
        if self.agent:
            base = f"{base}.{self.agent}"
        if self.device:
            base = f"{base}:{self.device}"
        return f"{base}@{self.server}"

    def ad_string(self) -> str:
        """Always-AD-form: ``user.agent:device@server``, including zeros.

        Mirrors whatsmeow's ``JID.ADString()``. Used to compute the
        ``phash`` participant-list hash on outgoing messages — both ends
        must agree on the canonical string form bit-for-bit. Uses
        ``actual_agent()`` (1 for LID, 0 for PN) so a JID dataclass with
        a defaulted ``agent=0`` field hashes the same way the wire form
        is interpreted by the server.
        """
        return f"{self.user}.{self.actual_agent()}:{self.device}@{self.server}"
