"""Wire-format decoding for the home.nest.com protobuf API.

Every response from grpc-web.production.nest.com is a ``google.rpc.StreamBody``
(sent base64-encoded; this module works on the decoded bytes). Each entry in
``StreamBody.message`` is one serialized response message: an Observe stream
carries many, a BatchUpdateState call carries one. ``StreamBody.status`` reports
errors and ``StreamBody.noop`` frames are keepalives.

Trait payloads arrive as ``google.protobuf.Any`` with type URLs of the form
``type.nestlabs.com/<fully.qualified.TraitName>``. Resolving them needs every
generated module imported, which ``load_trait_types`` does. That import is slow
and blocking, so in Home Assistant run it in an executor, once, before decoding.
"""

from __future__ import annotations

import importlib
import pkgutil

from google.protobuf import any_pb2, descriptor_pool, message, message_factory

from . import proto_gen
from .proto_gen.google.rpc import stream_body_pb2
from .proto_gen.nestlabs.gateway import v1_pb2, v2_pb2

TYPE_URL_PREFIX = "type.nestlabs.com/"

# The vendored schema's top-level ObserveResponse/BatchUpdateStateResponse are
# StreamBody-shaped wrappers (field 1 = repeated message). The nested types are
# the actual per-frame response messages.
ObserveFrame = v2_pb2.ObserveResponse.ObserveResponse
UpdateStateFrame = v1_pb2.BatchUpdateStateResponse.TraitOperationStateResponse

_trait_types_loaded = False


class StreamError(Exception):
    """The server ended a response stream with a non-OK google.rpc.Status."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"gRPC status {code}: {message}")
        self.code = code
        self.message = message


def parse_stream_body(data: bytes) -> list[bytes]:
    """Split a decoded StreamBody into its response messages.

    Raises StreamError if the body carries a non-OK status.
    """
    body = stream_body_pb2.StreamBody()
    body.ParseFromString(data)
    if body.HasField("status") and body.status.code != 0:
        raise StreamError(body.status.code, body.status.message)
    return list(body.message)


def parse_observe(data: bytes) -> list[ObserveFrame]:
    """Parse a decoded Observe response body into its frames."""
    return [ObserveFrame.FromString(m) for m in parse_stream_body(data)]


def parse_update_state(data: bytes) -> list[UpdateStateFrame]:
    """Parse a decoded BatchUpdateState response body."""
    return [UpdateStateFrame.FromString(m) for m in parse_stream_body(data)]


def load_trait_types() -> int:
    """Import every generated module so all trait types can be resolved.

    Blocking (imports ~60 modules). Idempotent. Returns the module count.
    """
    global _trait_types_loaded
    count = 0
    for info in pkgutil.walk_packages(proto_gen.__path__, proto_gen.__name__ + "."):
        importlib.import_module(info.name)
        count += 1
    _trait_types_loaded = True
    return count


def trait_type_name(any_msg: any_pb2.Any) -> str:
    """Return the fully qualified message name an Any refers to."""
    return any_msg.type_url.rsplit("/", 1)[-1]


def unpack(any_msg: any_pb2.Any) -> message.Message | None:
    """Decode a trait payload, or return None if its type is not in the schema."""
    if not _trait_types_loaded:
        raise RuntimeError("call load_trait_types() before unpack()")
    try:
        desc = descriptor_pool.Default().FindMessageTypeByName(trait_type_name(any_msg))
    except KeyError:
        return None
    msg = message_factory.GetMessageClass(desc)()
    msg.ParseFromString(any_msg.value)
    return msg


def pack(msg: message.Message) -> any_pb2.Any:
    """Wrap a trait message in an Any with the nestlabs type URL."""
    any_msg = any_pb2.Any()
    any_msg.Pack(msg, type_url_prefix=TYPE_URL_PREFIX)
    return any_msg
