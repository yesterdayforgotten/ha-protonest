"""StreamBodyReader: incremental parsing must not depend on chunk boundaries."""

import pytest

from pynestproto import codec
from pynestproto.proto_gen.google.rpc import stream_body_pb2
from pynestproto.transport import StreamBodyReader


def read_all(data: bytes, chunk: int) -> tuple[list[bytes], int, bool]:
    noops = 0

    def on_noop():
        nonlocal noops
        noops += 1

    reader = StreamBodyReader(on_noop)
    out = []
    for i in range(0, len(data), chunk):
        out += reader.feed(data[i : i + chunk])
    return out, noops, reader.ended


@pytest.mark.parametrize("chunk", [1, 7, 4096, 10**9])
def test_capture_parses_identically_at_any_chunking(capture, chunk):
    data = capture("nest_proto")
    messages, noops, ended = read_all(data, chunk)
    assert messages == codec.parse_stream_body(data)
    assert len(messages) == 16
    assert noops == 8
    assert not ended  # snapshot was cut mid-stream, no closing status


def test_ok_status_marks_end():
    body = stream_body_pb2.StreamBody(message=[b"abc"])
    body.status.code = 0
    body.status.SetInParent()
    messages, _, ended = read_all(body.SerializeToString(), 1)
    assert messages == [b"abc"] and ended


def test_error_status_raises_after_earlier_messages():
    body = stream_body_pb2.StreamBody(message=[b"abc"])
    body.status.code = 16
    body.status.message = "UNAUTHENTICATED"
    reader = StreamBodyReader()
    data = body.SerializeToString()
    with pytest.raises(codec.StreamError) as err:
        reader.feed(data)
    assert err.value.code == 16


def test_partial_message_is_held_until_complete():
    data = stream_body_pb2.StreamBody(message=[b"x" * 300]).SerializeToString()
    reader = StreamBodyReader()
    assert reader.feed(data[:-1]) == []
    assert reader.feed(data[-1:]) == [b"x" * 300]
