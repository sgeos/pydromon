"""Recovery event contracts and wire compatibility for stream failures."""

import pytest

from dromon.codec import decode, encode
from dromon.model import FailureCause, InvalidField, LineTurnEnded
from dromon.model import (
    EventRejected,
    LineAddress,
    LineCreated,
    MessageDelivered,
    MessageKind,
    MessageSent,
    OperatorAddress,
    OperatorRegistered,
    State,
)


def test_recoverable_turn_end_and_retry_ids_round_trip() -> None:
    event = LineTurnEnded(
        "alpha",
        None,
        True,
        True,
        FailureCause.REFUSED,
        "EngineStreamTooLong",
        "record exceeded its bound",
        True,
        ("m-first", "m-next"),
    )
    assert decode(encode(event)) == event


@pytest.mark.parametrize("recoverable", [False, True])
def test_recoverable_requires_a_failed_turn(recoverable: bool) -> None:
    with pytest.raises(InvalidField):
        LineTurnEnded("alpha", None, recoverable=recoverable)


def test_retry_requires_recoverable_and_unique_identifiers() -> None:
    with pytest.raises(InvalidField):
        LineTurnEnded("alpha", None, retry_messages=("m-first",))
    with pytest.raises(InvalidField):
        LineTurnEnded(
            "alpha",
            None,
            failed=True,
            recoverable=True,
            retry_messages=("m-first", "m-first"),
        )


@pytest.mark.parametrize("delivered", [False, True])
def test_retry_refuses_undelivered_mail_or_another_recipient(delivered: bool) -> None:
    state = State()
    for event in (
        LineCreated("alpha", "claude-opus-5-5", "subscription"),
        LineCreated("beta", "claude-opus-5-5", "subscription"),
        OperatorRegistered("ana"),
        MessageSent(
            "m-first",
            OperatorAddress("ana"),
            LineAddress("beta" if delivered else "alpha"),
            MessageKind.NOTE,
            "test",
        ),
    ):
        state.check(event)
        state.apply(event)
    if delivered:
        state.apply(MessageDelivered("m-first"))
    event = LineTurnEnded(
        "alpha", None, True, True, recoverable=True, retry_messages=("m-first",)
    )
    with pytest.raises(EventRejected):
        state.check(event)
