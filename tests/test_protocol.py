"""The client protocol's encoding and its strict decoding of untrusted lines."""

import json

import pytest

from dromon.protocol import (
    MAX_LINE_BYTES,
    PROTOCOL_VERSION,
    ErrorCode,
    Operation,
    ProtocolError,
    Request,
    Response,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
    request_id_of,
)


def line(document: object) -> bytes:
    return (json.dumps(document) + "\n").encode()


@pytest.mark.parametrize("operation", list(Operation))
def test_every_request_round_trips(operation: Operation) -> None:
    request = Request("r-1", operation)
    assert decode_request(encode_request(request)) == request


def test_a_request_is_one_canonical_line() -> None:
    encoded = encode_request(Request("r", Operation.PING))
    assert encoded == b'{"id":"r","op":"ping","v":1}\n'


def test_responses_round_trip() -> None:
    ok = Response("r", True, result={"pid": 7, "nested": {"a": [1]}})
    failed = Response("r", False, error_code=ErrorCode.BAD_REQUEST, error_message="no")
    assert decode_response(encode_response(ok)) == ok
    assert decode_response(encode_response(failed)) == failed


BAD_REQUESTS: list[tuple[str, bytes, ErrorCode]] = [
    ("not JSON", b"{\n", ErrorCode.BAD_REQUEST),
    ("not UTF-8", b"\xff\n", ErrorCode.BAD_REQUEST),
    ("an array", b"[]\n", ErrorCode.BAD_REQUEST),
    ("no version", line({"id": "r", "op": "ping"}), ErrorCode.UNSUPPORTED_VERSION),
    (
        "a future version",
        line({"id": "r", "op": "ping", "v": PROTOCOL_VERSION + 1}),
        ErrorCode.UNSUPPORTED_VERSION,
    ),
    (
        "a boolean version",
        line({"id": "r", "op": "ping", "v": True}),
        ErrorCode.UNSUPPORTED_VERSION,
    ),
    ("a missing id", line({"op": "ping", "v": 1}), ErrorCode.BAD_REQUEST),
    (
        "a malformed id",
        line({"id": "a b", "op": "ping", "v": 1}),
        ErrorCode.BAD_REQUEST,
    ),
    (
        "an extra key",
        line({"id": "r", "op": "ping", "v": 1, "x": 0}),
        ErrorCode.BAD_REQUEST,
    ),
    (
        "an unknown operation",
        line({"id": "r", "op": "reboot", "v": 1}),
        ErrorCode.UNKNOWN_OPERATION,
    ),
    (
        "an operation that is not text",
        line({"id": "r", "op": 1, "v": 1}),
        ErrorCode.BAD_REQUEST,
    ),
    ("a float", b'{"id":"r","op":"ping","v":1.0}\n', ErrorCode.BAD_REQUEST),
    ("NaN", b'{"id":"r","op":"ping","v":NaN}\n', ErrorCode.BAD_REQUEST),
    (
        "a duplicate key",
        b'{"id":"r","id":"s","op":"ping","v":1}\n',
        ErrorCode.BAD_REQUEST,
    ),
    ("an oversized line", b" " * MAX_LINE_BYTES + b"\n", ErrorCode.TOO_LONG),
]


@pytest.mark.parametrize(
    ("payload", "code"),
    [case[1:] for case in BAD_REQUESTS],
    ids=[case[0] for case in BAD_REQUESTS],
)
def test_a_bad_request_is_refused_with_its_code(
    payload: bytes, code: ErrorCode
) -> None:
    with pytest.raises(ProtocolError) as raised:
        decode_request(payload)
    assert raised.value.code is code


def test_the_id_of_a_bad_request_is_recovered_when_readable() -> None:
    assert request_id_of(line({"id": "r-9", "op": "reboot", "v": 1})) == "r-9"
    assert request_id_of(b"{\n") == ""
    assert request_id_of(line({"id": "a b"})) == ""


BAD_RESPONSES: list[tuple[str, bytes]] = [
    ("ok that is not a boolean", line({"id": "r", "ok": 1, "result": {}, "v": 1})),
    (
        "a result that is not an object",
        line({"id": "r", "ok": True, "result": [], "v": 1}),
    ),
    ("a success with an error", line({"id": "r", "ok": True, "error": {}, "v": 1})),
    ("an error without fields", line({"id": "r", "ok": False, "error": {}, "v": 1})),
    (
        "an unknown error code",
        line({"id": "r", "ok": False, "error": {"code": "x", "message": ""}, "v": 1}),
    ),
    ("an id that is not text", line({"id": 1, "ok": True, "result": {}, "v": 1})),
]


@pytest.mark.parametrize(
    "payload",
    [case[1] for case in BAD_RESPONSES],
    ids=[case[0] for case in BAD_RESPONSES],
)
def test_a_malformed_response_is_refused(payload: bytes) -> None:
    with pytest.raises(ProtocolError):
        decode_response(payload)


def test_an_oversized_response_cannot_be_encoded() -> None:
    with pytest.raises(ProtocolError) as raised:
        encode_response(Response("r", True, result={"x": "y" * MAX_LINE_BYTES}))
    assert raised.value.code is ErrorCode.TOO_LONG
