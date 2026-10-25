"""Canonical encoding of events and strict decoding of untrusted payloads."""

import json
from dataclasses import fields

import pytest

from dromon.codec import (
    FORMAT_VERSION,
    KIND_FIELDS,
    MAX_PAYLOAD_BYTES,
    DecodeError,
    decode,
    encode,
)
from dromon.model import (
    MAX_CHOICE_BYTES,
    MAX_CHOICES,
    MAX_TEXT_BYTES,
    Event,
    LineCreated,
    QuestionAnswered,
    QuestionAsked,
)


def wire(kind: object, data: object, version: object = FORMAT_VERSION) -> str:
    return json.dumps({"v": version, "kind": kind, "data": data})


def test_every_event_round_trips(scenario: tuple[Event, ...]) -> None:
    for event in scenario:
        assert decode(encode(event)) == event


def test_the_field_table_matches_each_event(scenario: tuple[Event, ...]) -> None:
    for event in scenario:
        document = json.loads(encode(event))
        declared = tuple(f.name for f in fields(event))
        assert KIND_FIELDS[document["kind"]] == declared
        assert set(document["data"]) == set(declared)


def test_the_field_table_names_exactly_the_scenario_kinds(
    scenario: tuple[Event, ...],
) -> None:
    kinds = {json.loads(encode(event))["kind"] for event in scenario}
    assert kinds == set(KIND_FIELDS)


def test_encoding_is_canonical(scenario: tuple[Event, ...]) -> None:
    for event in scenario:
        text = encode(event)
        canonical = json.dumps(
            json.loads(text), ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        assert text == canonical
        assert encode(decode(text)) == text


def test_an_encoding_is_pinned_so_ports_can_compare_bytes() -> None:
    expected = (
        '{"data":{"engine":"claude-opus-5-5","line_id":"alpha"},'
        '"kind":"line_created","v":1}'
    )
    assert encode(LineCreated("alpha", "claude-opus-5-5")) == expected


def test_string_escapes_are_pinned_so_ports_can_compare_bytes() -> None:
    text = "".join(chr(c) for c in range(0x20)) + '"\\/\x7f '
    escaped = (
        r"\u0000\u0001\u0002\u0003\u0004\u0005\u0006\u0007\b\t\n\u000b\f\r"
        r"\u000e\u000f\u0010\u0011\u0012\u0013\u0014\u0015\u0016\u0017"
        r"\u0018\u0019\u001a\u001b\u001c\u001d\u001e\u001f"
        '\\"\\\\/\x7f '
    )
    event = QuestionAnswered("q", text)
    expected = f'{{"data":{{"answer":"{escaped}","question_id":"q"}},'
    assert encode(event).startswith(expected)
    assert decode(encode(event)) == event


def test_non_ascii_text_is_not_escaped() -> None:
    event = QuestionAsked("q", "a", "✓", (), blocking=False)
    assert "✓" in encode(event)


def test_the_largest_valid_event_fits_the_payload_bound() -> None:
    # Control characters need a six-byte escape, the worst case for JSON expansion.
    worst = "\x01"
    event = QuestionAsked(
        "q" * 64,
        "a" * 64,
        worst * MAX_TEXT_BYTES,
        tuple(f"{n:02}" + worst * (MAX_CHOICE_BYTES - 2) for n in range(MAX_CHOICES)),
        blocking=True,
    )
    assert decode(encode(event)) == event


VALID_DATA = {"line_id": "alpha", "engine": "e"}

MALFORMED: list[tuple[str, str, str]] = [
    ("not JSON", "{", "not valid JSON"),
    ("an array", "[]", "not an object"),
    ("a float", wire("line_created", VALID_DATA, 1.0), "non-integer"),
    ("NaN", '{"v": NaN, "kind": "line_created", "data": {}}', "non-integer"),
    ("Infinity", '{"v": Infinity, "kind": "x", "data": {}}', "non-integer"),
    ("a boolean version", wire("line_created", VALID_DATA, True), "version"),
    ("a future version", wire("line_created", VALID_DATA, 2), "version"),
    ("a string version", wire("line_created", VALID_DATA, "1"), "version"),
    ("an unknown kind", wire("line_deleted", VALID_DATA), "unknown event kind"),
    ("a kind that is not text", wire(7, VALID_DATA), "unknown event kind"),
    (
        "an extra envelope key",
        json.dumps({"v": 1, "kind": "line_created", "data": VALID_DATA, "x": 0}),
        "keys",
    ),
    ("a missing envelope key", json.dumps({"v": 1, "kind": "line_created"}), "keys"),
    (
        "a duplicate key",
        '{"v": 1, "kind": "line_created", "kind": "line_created", "data": {}}',
        "duplicate",
    ),
    ("data that is not an object", wire("line_created", [], 1), "not an object"),
    ("an extra field", wire("line_created", {**VALID_DATA, "x": "y"}), "keys"),
    ("a missing field", wire("line_created", {"line_id": "alpha"}), "keys"),
    (
        "an invalid identifier",
        wire("line_created", {**VALID_DATA, "line_id": "A"}),
        "identifier",
    ),
    (
        "a numeric identifier",
        wire("line_created", {**VALID_DATA, "line_id": 1}),
        "not a string",
    ),
    (
        "an unknown line state",
        wire("line_state_changed", {"line_id": "a", "state": "paused"}),
        "no value",
    ),
    (
        "an integer for a boolean",
        wire(
            "question_asked",
            {
                "question_id": "q",
                "line_id": "a",
                "text": "?",
                "choices": [],
                "blocking": 1,
            },
        ),
        "boolean",
    ),
    (
        "choices that are not an array",
        wire(
            "question_asked",
            {
                "question_id": "q",
                "line_id": "a",
                "text": "?",
                "choices": "yes",
                "blocking": True,
            },
        ),
        "not an array",
    ),
    (
        "a choice that is not text",
        wire(
            "question_asked",
            {
                "question_id": "q",
                "line_id": "a",
                "text": "?",
                "choices": [1],
                "blocking": True,
            },
        ),
        "not a string",
    ),
    (
        "a lone surrogate escape",
        wire("question_answered", {"question_id": "q", "answer": "\ud800"}),
        "Unicode",
    ),
    ("deep nesting", "[" * 100_000 + "]" * 100_000, "nests too deeply|not an object"),
]


@pytest.mark.parametrize(
    ("payload", "reason"),
    [case[1:] for case in MALFORMED],
    ids=[case[0] for case in MALFORMED],
)
def test_a_malformed_payload_is_refused(payload: str, reason: str) -> None:
    with pytest.raises(DecodeError, match=reason):
        decode(payload)


def test_an_oversized_payload_is_refused_before_parsing() -> None:
    padding = " " * MAX_PAYLOAD_BYTES
    with pytest.raises(DecodeError, match="over"):
        decode(wire("line_created", VALID_DATA) + padding)


def test_a_payload_with_a_lone_surrogate_is_refused() -> None:
    with pytest.raises(DecodeError, match="Unicode"):
        decode("\ud800")
