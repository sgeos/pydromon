"""Forwarding operator messages through relays, including the command relay."""

import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from dromon.eventlog import EventLog, replay
from dromon.model import (
    Event,
    LineAddress,
    LineCreated,
    MessageKind,
    MessageSent,
    OperatorAddress,
    OperatorRegistered,
)
from dromon.relay import (
    NOTICE_VERSION,
    CommandRelay,
    Notice,
    RelayError,
    encode_notice,
    forward_pending,
    pending_notices,
)

ALPHA = LineAddress("alpha")
ANA = OperatorAddress("ana")
BEN = OperatorAddress("ben")
CLEO = OperatorAddress("cleo")
QUESTION = MessageKind.QUESTION

SETUP: tuple[Event, ...] = (
    LineCreated("alpha", "claude-opus-5-5", "subscription"),
    OperatorRegistered("ana"),
    OperatorRegistered("ben"),
    OperatorRegistered("cleo"),
    MessageSent("q1", ALPHA, ANA, QUESTION, "For Ana?", ("yes", "no"), blocking=True),
    MessageSent("q2", ALPHA, BEN, QUESTION, "For Ben?"),
    MessageSent("q3", ALPHA, CLEO, QUESTION, "For Cleo?"),
    MessageSent("n1", ANA, ALPHA, MessageKind.NOTE, "not for an operator"),
)


class Recorder:
    """A relay that keeps what it accepts, or refuses everything."""

    def __init__(self, *, refuse: bool = False) -> None:
        self.notices: list[Notice] = []
        self.refuse = refuse

    def forward(self, notice: Notice) -> None:
        if self.refuse:
            raise RelayError("channel unavailable")
        self.notices.append(notice)


@pytest.fixture
def log(tmp_path: Path) -> Iterator[EventLog]:
    with EventLog(tmp_path / "events.sqlite") as opened:
        for event in SETUP:
            opened.append(event)
        yield opened


def test_only_undelivered_operator_messages_are_pending(log: EventLog) -> None:
    pending = pending_notices(log.state)
    assert [n.message_id for n in pending] == ["q1", "q2", "q3"]
    first = pending[0]
    assert (first.sender, first.operator_id, first.choices) == (
        "alpha",
        "ana",
        ("yes", "no"),
    )
    assert first.blocking


def test_each_operator_is_routed_to_their_own_relay(log: EventLog) -> None:
    ana, ben = Recorder(), Recorder()
    report = forward_pending(log, {"ana": ana, "ben": ben})
    assert [n.message_id for n in ana.notices] == ["q1"]
    assert [n.message_id for n in ben.notices] == ["q2"]
    assert report.delivered == ("q1", "q2")
    assert report.unrouted == ("q3",)
    assert report.failed == ()


def test_delivery_is_recorded_durably_and_not_repeated(
    log: EventLog, tmp_path: Path
) -> None:
    ana = Recorder()
    forward_pending(log, {"ana": ana})
    assert replay(tmp_path / "events.sqlite").messages["q1"].delivered
    second = forward_pending(log, {"ana": ana})
    assert second.delivered == ()
    assert len(ana.notices) == 1


def test_a_failing_relay_leaves_its_notice_pending_and_others_proceed(
    log: EventLog,
) -> None:
    report = forward_pending(log, {"ana": Recorder(refuse=True), "ben": Recorder()})
    assert report.failed == (("q1", "channel unavailable"),)
    assert report.delivered == ("q2",)
    assert not log.state.messages["q1"].delivered


def test_a_defect_in_a_relay_propagates(log: EventLog) -> None:
    class Broken:
        def forward(self, notice: Notice) -> None:
            raise KeyError(notice.message_id)

    with pytest.raises(KeyError):
        forward_pending(log, {"ana": Broken()})
    assert not log.state.messages["q1"].delivered


def test_the_notice_encoding_is_canonical_and_versioned() -> None:
    notice = Notice("q", "alpha", "ana", QUESTION, "✓", ("a",), True, None)
    text = encode_notice(notice)
    document = json.loads(text)
    assert document["v"] == NOTICE_VERSION
    assert list(document) == sorted(document)
    assert "✓" in text
    assert document["reply_to"] is None


# The command relay, run as a real subprocess. Each script is passed with -c, so no
# file is executed from the test directory.

ECHO_TO_FILE = (
    "import sys, pathlib; pathlib.Path(sys.argv[1]).write_text(sys.stdin.read())"
)


def test_the_command_relay_passes_the_notice_on_standard_input(
    log: EventLog, tmp_path: Path
) -> None:
    out = tmp_path / "notice.json"
    relay = CommandRelay([sys.executable, "-I", "-c", ECHO_TO_FILE, str(out)])
    report = forward_pending(log, {"ana": relay})
    assert report.delivered == ("q1",)
    received = json.loads(out.read_text(encoding="utf-8"))
    assert received["message_id"] == "q1"
    assert received["operator_id"] == "ana"
    assert received["body"] == "For Ana?"


def test_a_nonzero_exit_is_a_relay_error_with_detail() -> None:
    relay = CommandRelay(
        [sys.executable, "-I", "-c", "import sys; sys.exit('no route')"]
    )
    notice = Notice("q", "alpha", "ana", QUESTION, "?", (), False, None)
    with pytest.raises(RelayError, match="status 1: no route"):
        relay.forward(notice)


def test_a_slow_command_times_out() -> None:
    relay = CommandRelay(
        [sys.executable, "-I", "-c", "import time; time.sleep(5)"], timeout_seconds=0.2
    )
    notice = Notice("q", "alpha", "ana", QUESTION, "?", (), False, None)
    with pytest.raises(RelayError, match="timed out"):
        relay.forward(notice)


def test_a_missing_command_is_a_relay_error(tmp_path: Path) -> None:
    relay = CommandRelay([str(tmp_path / "no-such-command")])
    notice = Notice("q", "alpha", "ana", QUESTION, "?", (), False, None)
    with pytest.raises(RelayError, match="could not start"):
        relay.forward(notice)


def test_notice_text_cannot_change_what_runs(tmp_path: Path) -> None:
    out = tmp_path / "notice.json"
    marker = tmp_path / "injected"
    relay = CommandRelay([sys.executable, "-I", "-c", ECHO_TO_FILE, str(out)])
    body = f"; touch {marker} $(touch {marker}) `touch {marker}`"
    relay.forward(Notice("q", "alpha", "ana", QUESTION, body, (), False, None))
    assert json.loads(out.read_text(encoding="utf-8"))["body"] == body
    assert not marker.exists()


@pytest.mark.parametrize(
    ("argv", "timeout"),
    [([], 1.0), ([""], 1.0), (["x"], 0.0), (["x"], -1.0), (["x"], 3601.0)],
)
def test_invalid_command_relay_configuration_is_refused(
    argv: list[str], timeout: float
) -> None:
    with pytest.raises(ValueError, match="argv|timeout"):
        CommandRelay(argv, timeout_seconds=timeout)
