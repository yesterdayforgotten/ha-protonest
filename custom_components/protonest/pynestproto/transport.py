"""HTTP transport for grpc-web.production.nest.com.

This is not standard gRPC or grpc-web. Requests are bare serialized protobuf
(no length prefix) POSTed with ``Content-Type: application/x-protobuf``; the
response is a ``google.rpc.StreamBody``. We ask for it in binary
(``X-Accept-Content-Transfer-Encoding: binary``); the web app asks for base64
only because browser XHR reads responses as text.

An Observe response is a never-ending StreamBody. ``StreamBodyReader`` parses
it incrementally, yielding each complete ``message`` as its bytes arrive.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Callable

import aiohttp
from google.protobuf import message as pb_message
from google.rpc import status_pb2

from .auth import USER_AGENT, NestAuth
from .codec import StreamError

_LOGGER = logging.getLogger(__name__)

GRPC_URL = "https://grpc-web.production.nest.com"
OBSERVE_PATH = "/nestlabs.gateway.v2.GatewayService/Observe"
BATCH_UPDATE_STATE_PATH = "/nestlabs.gateway.v1.TraitBatchApi/BatchUpdateState"

# google.rpc.Code values meaning "your token is bad".
_AUTH_CODES = {7, 16}  # PERMISSION_DENIED, UNAUTHENTICATED

_UNARY_TIMEOUT = aiohttp.ClientTimeout(total=30)

# Observed on the live Observe stream (2026-10-03):
# - the server sends a noop keepalive every 600 s, counted from stream start;
# - it ends every stream 60 min after it opened, with status 4
#   (DEADLINE_EXCEEDED): a normal end, reconnect. The limit is fixed, not tied
#   to the token: a stream opened with 40 min of token left ran past the
#   token's expiry (2026-10-04). The token is only checked at connect.
STREAM_MAX_AGE = 60 * 60
KEEPALIVE_INTERVAL = 600
# Must comfortably exceed the keepalive interval: at exactly 600 s the read
# timeout races the keepalive and drops healthy streams on quiet nights.
OBSERVE_READ_TIMEOUT = 1.5 * KEEPALIVE_INTERVAL


class TransportError(Exception):
    """The request failed at the HTTP level."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class AuthExpired(TransportError):
    """The server rejected our token. Invalidate it and retry."""


class StreamBodyReader:
    """Incremental parser for a binary google.rpc.StreamBody.

    StreamBody is ``repeated bytes message = 1; Status status = 2;
    repeated bytes noop = 15;``. Each top-level field is length-delimited, so
    a field can be emitted as soon as all its bytes have arrived.
    """

    def __init__(self, on_noop: Callable[[], None] | None = None) -> None:
        self._buf = bytearray()
        self._on_noop = on_noop
        self.ended = False  # a status field arrived (end of stream)

    def feed(self, data: bytes) -> list[bytes]:
        """Add bytes; return the complete messages now available.

        Raises StreamError if the stream ends with a non-OK status.
        """
        self._buf += data
        out: list[bytes] = []
        while True:
            parsed = self._next_field()
            if parsed is None:
                return out
            field, payload = parsed
            if field == 1:
                out.append(payload)
            elif field == 2:
                self.ended = True
                status = status_pb2.Status.FromString(payload)
                if status.code != 0:
                    raise StreamError(status.code, status.message)
            elif field == 15 and self._on_noop:
                self._on_noop()

    def _next_field(self) -> tuple[int, bytes] | None:
        buf = self._buf
        tag = _read_varint(buf, 0)
        if tag is None:
            return None
        key, pos = tag
        field, wire_type = key >> 3, key & 7
        if wire_type == 2:
            length = _read_varint(buf, pos)
            if length is None:
                return None
            size, pos = length
            end = pos + size
        elif wire_type == 0:
            varint = _read_varint(buf, pos)
            if varint is None:
                return None
            end = varint[1]
            pos = end
        elif wire_type == 1:
            end = pos + 8
        elif wire_type == 5:
            end = pos + 4
        else:
            raise TransportError(f"unexpected wire type {wire_type} in StreamBody")
        if len(buf) < end:
            return None
        payload = bytes(buf[pos:end])
        del buf[:end]
        return field, payload


def _read_varint(buf: bytearray, pos: int) -> tuple[int, int] | None:
    """Read a varint at pos; return (value, next_pos), or None if incomplete."""
    value = shift = 0
    while pos < len(buf):
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
        if shift > 63:
            raise TransportError("malformed varint in StreamBody")
    return None


class NestTransport:
    """Sends protobuf requests to the Nest gateway."""

    def __init__(
        self, session: aiohttp.ClientSession, auth: NestAuth, base_url: str = GRPC_URL
    ) -> None:
        self._session = session
        self._auth = auth
        self._base_url = base_url

    async def _headers(self) -> dict[str, str]:
        token = await self._auth.async_get_token()
        return {
            "Authorization": token.authorization,
            "Content-Type": "application/x-protobuf",
            "X-Accept-Content-Transfer-Encoding": "binary",
            "X-Accept-Response-Streaming": "true",
            "request-id": str(uuid.uuid4()),
            "User-Agent": USER_AGENT,
            "Origin": "https://home.nest.com",
            "Referer": "https://home.nest.com/",
        }

    async def _check(self, resp: aiohttp.ClientResponse, path: str) -> None:
        if resp.status in (401, 403):
            self._auth.invalidate()
            raise AuthExpired(f"{path}: HTTP {resp.status}", resp.status)
        if resp.status != 200:
            body = (await resp.text(errors="replace"))[:200]
            raise TransportError(f"{path}: HTTP {resp.status}: {body}", resp.status)

    async def async_call(self, path: str, request: pb_message.Message) -> list[bytes]:
        """Unary call. Returns the response messages (usually one).

        Retries once with a fresh token if the first one is rejected.
        """
        for attempt in (1, 2):
            try:
                return await self._async_call_once(path, request)
            except AuthExpired:
                if attempt == 2:
                    raise
                _LOGGER.debug("%s: token rejected, retrying with a fresh one", path)
        raise AssertionError("unreachable")

    async def _async_call_once(self, path: str, request: pb_message.Message) -> list[bytes]:
        try:
            async with self._session.post(
                self._base_url + path,
                data=request.SerializeToString(),
                headers=await self._headers(),
                timeout=_UNARY_TIMEOUT,
            ) as resp:
                await self._check(resp, path)
                reader = StreamBodyReader()
                messages = reader.feed(await resp.read())
        except StreamError as err:
            if err.code in _AUTH_CODES:
                self._auth.invalidate()
                raise AuthExpired(f"{path}: {err}") from err
            raise
        except aiohttp.ClientError as err:
            raise TransportError(f"{path}: {err}") from err
        return messages

    async def async_stream(
        self,
        path: str,
        request: pb_message.Message,
        *,
        read_timeout: float = OBSERVE_READ_TIMEOUT,
        on_noop: Callable[[], None] | None = None,
    ) -> AsyncIterator[bytes]:
        """Server-streaming call. Yields response messages as they arrive.

        Ends normally when the server closes the stream; callers reconnect.
        Raises AuthExpired if the token was rejected (it is already
        invalidated, so reconnecting fetches a new one).
        """
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=read_timeout)
        reader = StreamBodyReader(on_noop)
        try:
            async with self._session.post(
                self._base_url + path,
                data=request.SerializeToString(),
                headers=await self._headers(),
                timeout=timeout,
            ) as resp:
                await self._check(resp, path)
                async for chunk in resp.content.iter_any():
                    for msg in reader.feed(chunk):
                        yield msg
        except StreamError as err:
            if err.code in _AUTH_CODES:
                self._auth.invalidate()
                raise AuthExpired(f"{path}: {err}") from err
            raise
        except aiohttp.ClientError as err:
            raise TransportError(f"{path}: {err}") from err
        except TimeoutError as err:
            raise TransportError(f"{path}: no data for {read_timeout}s") from err
