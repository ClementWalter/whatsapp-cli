"""WebSocket framing for WhatsApp's binary protocol.

Wire format over ``wss://web.whatsapp.com/ws/chat`` (binary frames only):

- First client→server frame is prefixed by a one-time 4-byte connection
  header: ``b'WA' || 0x06 || DictVersion`` (where DictVersion is the
  wabinary token-table version).
- Every WA frame (in either direction) is then encoded as a 3-byte
  big-endian length followed by that many payload bytes.
- A single WebSocket binary message may contain multiple WA frames back
  to back, or a WA frame may span multiple WebSocket messages; the
  parser must buffer across boundaries.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

from aiohttp import ClientSession, WSMsgType

from wa.wabinary.tokens import DICT_VERSION

WA_URL = "wss://web.whatsapp.com/ws/chat"
WA_ORIGIN = "https://web.whatsapp.com"
WA_HEADER: bytes = bytes([ord("W"), ord("A"), 6, DICT_VERSION])
FRAME_LEN_BYTES = 3
FRAME_MAX_SIZE = 1 << 24


class FrameParser:
    """Streaming parser: feed bytes in, get completed frames out.

    Stateful. Call :py:meth:`feed` with any chunk of bytes; returns the list
    of complete frame payloads that can be extracted from the buffer.
    Partial frames are retained for subsequent feeds.
    """

    __slots__ = ("_buf",)

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self._buf.extend(data)
        frames: list[bytes] = []
        while True:
            if len(self._buf) < FRAME_LEN_BYTES:
                break
            # 3-byte big-endian length prefix.
            length = (self._buf[0] << 16) | (self._buf[1] << 8) | self._buf[2]
            if length > FRAME_MAX_SIZE:
                raise ValueError(f"frame too large: {length}")
            total = FRAME_LEN_BYTES + length
            if len(self._buf) < total:
                break
            frame = bytes(self._buf[FRAME_LEN_BYTES:total])
            del self._buf[:total]
            frames.append(frame)
        return frames


def encode_frame(payload: bytes) -> bytes:
    """Serialize a single frame: 3-byte BE length + payload."""
    length = len(payload)
    if length >= FRAME_MAX_SIZE:
        raise ValueError(f"frame too large: {length}")
    return bytes([(length >> 16) & 0xFF, (length >> 8) & 0xFF, length & 0xFF]) + payload


class FrameSocket:
    """Async WebSocket connection that sends/receives WA frames.

    Lifecycle:
        async with FrameSocket() as fs:
            await fs.connect()
            await fs.send(frame_bytes)  # first send includes WA_HEADER
            async for frame in fs.frames():
                ...
    """

    def __init__(self, url: str = WA_URL, origin: str = WA_ORIGIN) -> None:
        self._url = url
        self._origin = origin
        self._session: ClientSession | None = None
        self._ws = None
        self._parser = FrameParser()
        self._header_sent = False
        self._incoming: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._reader_task: asyncio.Task | None = None
        self._on_disconnect: Callable[[], None] | None = None

    async def __aenter__(self) -> "FrameSocket":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def connect(self) -> None:
        """Open the WebSocket and start the reader pump."""
        if self._ws is not None:
            raise RuntimeError("already connected")
        self._session = ClientSession()
        self._ws = await self._session.ws_connect(
            self._url,
            headers={"Origin": self._origin},
            max_msg_size=FRAME_MAX_SIZE,
        )
        self._reader_task = asyncio.create_task(self._read_pump())

    async def _read_pump(self) -> None:
        assert self._ws is not None
        try:
            async for msg in self._ws:
                if msg.type == WSMsgType.BINARY:
                    for frame in self._parser.feed(msg.data):
                        await self._incoming.put(frame)
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                    break
        finally:
            # Sentinel so consumers of frames() can exit cleanly.
            await self._incoming.put(None)
            if self._on_disconnect is not None:
                self._on_disconnect()

    async def send(self, payload: bytes) -> None:
        """Send one frame. Prepends the WA connection header on first send."""
        assert self._ws is not None
        buf = encode_frame(payload)
        if not self._header_sent:
            buf = WA_HEADER + buf
            self._header_sent = True
        await self._ws.send_bytes(buf)

    async def recv(self, timeout: float | None = None) -> bytes:
        """Await the next complete frame. Raises TimeoutError on expiry."""
        frame = await asyncio.wait_for(self._incoming.get(), timeout)
        if frame is None:
            raise ConnectionError("socket closed")
        return frame

    async def frames(self) -> AsyncIterator[bytes]:
        while True:
            frame = await self._incoming.get()
            if frame is None:
                return
            yield frame

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        if self._session is not None:
            await self._session.close()
            self._session = None
        if self._reader_task is not None:
            try:
                await asyncio.wait_for(self._reader_task, timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            self._reader_task = None
