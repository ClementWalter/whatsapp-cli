"""WebSocket transport and frame framing for WhatsApp.

Layered:
- FrameParser: pure byte-buffer state machine (testable without network)
- FrameSocket: aiohttp WebSocket wrapper around FrameParser, sends/receives
  length-prefixed frames with WA's one-time connection header.
"""

from .framesocket import (
    WA_HEADER,
    WA_ORIGIN,
    WA_URL,
    FrameParser,
    FrameSocket,
)

__all__ = ["FrameParser", "FrameSocket", "WA_URL", "WA_ORIGIN", "WA_HEADER"]
