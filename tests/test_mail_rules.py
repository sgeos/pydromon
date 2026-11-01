"""The unanswered count and the operator alias.

**Round 14 task of 2026-11-01.** The count only grew, since
every report and every reply counted until its recipient answered it. A reply or a
correction naming a message closes it, a note that asks nothing is not counted, and
a line lists its unanswered mail with how long each has waited and closes one with a
recorded reason. ``@operator`` names the project's default operator."""

import asyncio
from dataclasses import replace
from collections.abc import Generator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import scheduler as scheduler_module
from dromon.codec import decode, encode
from dromon.driver import Strategy
from dromon.engine import TurnReport
from dromon.eventlog import EventLog
from dromon.mailbox import (
    asks_reply,
    counts_unanswered,
    listing,
    replied_ids,
    sent,
    summary,
)
from dromon.model import (
    MAX_CLOSE_REASON_BYTES,
    Event,
    EventRejected,
    InvalidField,
    LineAddress,
    LineCreated,
    Message,
    MessageAcknowledged,
    MessageDelivered,
    MessageKind,
    MessageSent,
    OperatorAddress,
    OperatorRegistered,
    State,
)
from dromon.scheduler import LineSpec, Scheduler

HAIKU = "claude-haiku-5-5"
ANA = OperatorAddress("ana")
ALPHA = LineAddress("alpha")
BETA = LineAddress("beta")
START = datetime(2026, 11, 1, 9, 0, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def text(self) -> str:
        return self.now.isoformat(timespec="microseconds").replace("+00:00", "Z")

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


class Idle:
    def __init__(self, spec: LineSpec) -> None:
        self.session_id = spec.session_id

    async def run_turn(self, prompt: str) -> TurnReport:
        del prompt
        return TurnReport("success", 1, "ok", (), None, None, "session-1", None, 0.0)


class World:
    def __init__(self, path: Path, root: Path, default_operator: str = "ana") -> None:
        self.path = path
        self.root = root
        self.default_operator = default_operator
        self.clock = Clock()
        self.log = EventLog(path, self.clock.text)
        self.scheduler = self.start()
        self.scheduler.ensure_operator("ana")
        self.scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        self.scheduler.create_line("beta", HAIKU, Strategy.SUBSCRIPTION)

    def start(self) -> Scheduler:
        def build(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> Idle:
            del tools
            return Idle(spec)

        return Scheduler(
            self.log,
            build,
            tick_seconds=0,
            wall_clock=self.clock,
            default_operator=self.default_operator,
            root=self.root,
            protected=(self.root / ".dromon",),
        )

    def restart(self) -> None:
        self.log.close()
        self.log = EventLog(self.path, self.clock.text)
        self.scheduler = self.start()

    def tool(self, name: str, owner: str = "alpha") -> SdkMcpTool[Any]:
        return next(t for t in self.scheduler.own_tools(owner) if t.name == name)

    def call(self, name: str, args: dict[str, object], owner: str = "alpha") -> str:
        tool = self.tool(name, owner)

        async def invoke() -> dict[str, Any]:
            return await tool.handler(args)

        result = asyncio.run(invoke())
        text = cast(str, result["content"][0]["text"])
        if result.get("is_error") is True:
            raise AssertionError(text)
        return text

    def refused(self, name: str, args: dict[str, object], owner: str = "alpha") -> str:
        tool = self.tool(name, owner)

        async def invoke() -> dict[str, Any]:
            return await tool.handler(args)

        result = asyncio.run(invoke())
        assert result.get("is_error") is True, result
        return cast(str, result["content"][0]["text"])

    def deliver(self, message_id: str) -> str:
        self.log.append(MessageDelivered(message_id))
        return message_id

    def unanswered(self, owner: LineAddress | OperatorAddress = ALPHA) -> int:
        return summary(self.log.state, owner, 0).unanswered


@pytest.fixture
def world(tmp_path: Path) -> Generator[World]:
    root = tmp_path.resolve() / "project"
    (root / ".dromon").mkdir(parents=True)
    target = World(tmp_path / "events.sqlite", root)
    yield target
    target.log.close()


# What closes a message.


def test_a_reply_from_the_recipient_closes_the_message(world: World) -> None:
    task = world.deliver(world.scheduler.send(ANA, ALPHA, "do the task"))
    assert world.unanswered() == 1
    world.scheduler.send(ALPHA, ANA, "done", task)
    assert world.unanswered() == 0
    assert world.log.state.messages[task].replied


def test_a_correction_closes_the_note_it_corrects_and_is_left_to_answer(
    world: World,
) -> None:
    first = world.deliver(world.scheduler.send(ANA, ALPHA, "do the task"))
    fixed = world.deliver(
        world.scheduler.send(ANA, ALPHA, "do the task on main", first)
    )
    state = world.log.state
    assert state.messages[first].corrected_by == fixed
    assert not state.messages[first].replied
    assert world.unanswered() == 1
    page, _ = listing(state, ALPHA, unanswered_only=True)
    assert [message_id for message_id, _ in page] == [fixed]


def test_a_correction_asks_what_the_note_it_corrects_asked(world: World) -> None:
    quiet = world.deliver(
        world.scheduler.send(ANA, ALPHA, "for the record", asks_reply=False)
    )
    world.deliver(world.scheduler.send(ANA, ALPHA, "for the record, fixed", quiet))
    assert world.unanswered() == 0
    asking = world.deliver(world.scheduler.send(ANA, ALPHA, "report on this"))
    told = world.deliver(
        world.scheduler.send(ANA, ALPHA, "no reply needed", asking, asks_reply=False)
    )
    # Round 14 review, M1. A correction never lowers the ask it stands in for.
    assert world.unanswered() == 1
    assert world.log.state.messages[told].asks_reply is True


def test_a_correction_may_raise_an_ask(world: World) -> None:
    quiet = world.deliver(
        world.scheduler.send(ANA, ALPHA, "for the record", asks_reply=False)
    )
    raised = world.deliver(
        world.scheduler.send(ANA, ALPHA, "now answer this", quiet, asks_reply=True)
    )
    assert world.unanswered() == 1
    page, _ = listing(world.log.state, ALPHA, unanswered_only=True)
    assert [message_id for message_id, _ in page] == [raised]


# Round 14 review, M1. A line cannot hide its own ask from an operator.


def test_a_lines_note_to_an_operator_asks_whatever_it_says(world: World) -> None:
    world.deliver(world.scheduler.send(ALPHA, ANA, "ship or hold?", asks_reply=False))
    assert world.unanswered(ANA) == 1
    report = world.deliver(world.scheduler.send(BETA, ANA, "done"))
    world.deliver(world.scheduler.send(ANA, BETA, "thanks", report))
    world.deliver(
        world.scheduler.send(BETA, ANA, "also done", report, asks_reply=False)
    )
    # A reply from the operator closes the report, and beta's correction of its
    # report keeps the report's ask whatever it says.
    assert world.unanswered(ANA) == 2


def test_a_correction_to_an_operator_cannot_lower_the_ask(world: World) -> None:
    ask = world.deliver(
        world.scheduler.send(ALPHA, ANA, "ship or hold?", asks_reply=True)
    )
    assert world.unanswered(ANA) == 1
    fixed = world.deliver(
        world.scheduler.send(ALPHA, ANA, "never mind", ask, asks_reply=False)
    )
    state = world.log.state
    assert state.messages[ask].corrected_by == fixed
    assert world.unanswered(ANA) == 1
    page, _ = listing(state, ANA, unanswered_only=True)
    assert [message_id for message_id, _ in page] == [fixed]


def test_a_reply_to_an_operator_still_asks_nothing_by_default(world: World) -> None:
    task = world.deliver(world.scheduler.send(ANA, ALPHA, "do the task"))
    world.deliver(world.scheduler.send(ALPHA, ANA, "done", task, asks_reply=False))
    world.deliver(world.scheduler.send(ALPHA, ANA, "done too", task))
    assert world.unanswered(ANA) == 0


@pytest.mark.parametrize(
    ("recipient", "given", "reply_to", "asks"),
    [
        (ALPHA, False, None, False),
        (ALPHA, None, None, True),
        (ALPHA, None, "m-0", False),
        (ANA, False, None, True),
        (ANA, False, "m-0", False),
        (ANA, True, "m-0", True),
        (ANA, None, None, True),
    ],
)
def test_the_rule_honours_false_only_for_a_line(
    recipient: LineAddress | OperatorAddress,
    given: bool | None,
    reply_to: str | None,
    asks: bool,
) -> None:
    message = Message(BETA, recipient, MessageKind.NOTE, "x", (), reply_to)
    assert asks_reply(replace(message, asks_reply=given)) is asks


def test_a_line_may_correct_its_own_note(world: World) -> None:
    report = world.deliver(world.scheduler.send(ALPHA, ANA, "the gate passed"))
    text = world.call(
        "send_message",
        {"to": "@ana", "body": "the gate passed with 10 skipped", "reply_to": report},
    )
    assert text.startswith("sent as ")
    assert world.log.state.messages[report].corrected_by == text.removeprefix(
        "sent as "
    )
    (status,), _ = sent(world.log.state, ALPHA, limit=1)
    assert status.outcome == "corrected"


def test_a_reply_or_correction_from_a_third_party_is_refused(world: World) -> None:
    note = world.scheduler.send(ANA, ALPHA, "for alpha")
    with pytest.raises(EventRejected, match="or correct it from @ana to alpha"):
        world.scheduler.send(BETA, ANA, "not mine to answer", note)
    with pytest.raises(EventRejected, match="must go from alpha"):
        world.scheduler.send(ANA, BETA, "a different recipient", note)


def test_only_a_note_corrects_a_note(world: World) -> None:
    question = world.scheduler.ask("alpha", ANA, "which branch?")
    with pytest.raises(EventRejected, match="only a note corrects a note"):
        world.scheduler.send(ALPHA, ANA, "which branch, main?", question)
    note = world.scheduler.send(ALPHA, ANA, "a note")
    with pytest.raises(EventRejected):
        world.log.append(
            MessageSent("q-x", ALPHA, ANA, MessageKind.QUESTION, "?", (), note)
        )


# What asks a reply.


def test_a_note_that_asks_nothing_is_not_counted(world: World) -> None:
    world.deliver(world.scheduler.send(ANA, ALPHA, "just so", asks_reply=False))
    assert world.unanswered() == 0
    world.deliver(world.scheduler.send(ANA, ALPHA, "answer me", asks_reply=True))
    assert world.unanswered() == 1


def test_a_reply_asks_nothing_unless_its_sender_says_so(world: World) -> None:
    report = world.deliver(world.scheduler.send(ALPHA, ANA, "done"))
    world.deliver(world.scheduler.send(ANA, ALPHA, "thanks", report))
    assert world.unanswered() == 0
    second = world.deliver(world.scheduler.send(ALPHA, ANA, "done again"))
    world.deliver(
        world.scheduler.send(ANA, ALPHA, "now do this", second, asks_reply=True)
    )
    assert world.unanswered() == 1


def test_questions_and_issues_always_ask_and_never_say_otherwise() -> None:
    question = MessageSent("q-1", ALPHA, ANA, MessageKind.QUESTION, "?")
    issue = MessageSent("i-1", ALPHA, ANA, MessageKind.ISSUE, "a refusal misleads")
    for event in (question, issue):
        with pytest.raises(InvalidField, match="only a note says"):
            MessageSent(
                event.message_id,
                event.sender,
                event.recipient,
                event.kind,
                event.body,
                asks_reply=False,
            )
    state = State()
    for event in (
        OperatorRegistered("ana"),
        LineCreated("alpha", HAIKU, "subscription"),
        question,
        issue,
    ):
        state.apply(event)
    assert asks_reply(state.messages["q-1"])
    assert asks_reply(state.messages["i-1"])


def test_asks_reply_must_be_a_boolean() -> None:
    with pytest.raises(InvalidField, match="not a boolean"):
        MessageSent(
            "m-1",
            ANA,
            ALPHA,
            MessageKind.NOTE,
            "x",
            asks_reply=cast(bool, 1),
        )


def test_the_send_tool_checks_asks_reply(world: World) -> None:
    text = world.refused(
        "send_message", {"to": "@ana", "body": "x", "asks_reply": "yes"}
    )
    assert text == "asks_reply must be true or false"
    text = world.refused(
        "send_message",
        {"to": "@ana", "body": "x", "asks_reply": False, "issue": True},
    )
    assert "takes no asks_reply" in text
    world.deliver(
        world.call(
            "send_message", {"to": "@ana", "body": "fyi", "asks_reply": False}
        ).removeprefix("sent as ")
    )
    # Round 14 review, M1. To an operator, false is not honoured.
    assert world.unanswered(ANA) == 1


# Listing and closing.


def test_the_unanswered_listing_is_exactly_what_the_count_counts(
    world: World,
) -> None:
    asked = world.deliver(world.scheduler.send(ANA, ALPHA, "do this"))
    world.clock.advance(3_725)
    world.deliver(world.scheduler.send(ANA, ALPHA, "fyi", asks_reply=False))
    later = world.deliver(world.scheduler.send(BETA, ALPHA, "and this"))
    world.clock.advance(65)
    text = world.call("list_mail", {"unanswered_only": True})
    lines = text.splitlines()
    assert lines[0].startswith("2 of 2 matching messages")
    assert "2 unanswered" in lines[0]
    assert lines[1].startswith(asked) and lines[1].endswith("unanswered for 1h 3m")
    assert lines[2].startswith(later) and lines[2].endswith("unanswered for 1m 5s")


def test_unanswered_and_own_notes_together_are_refused(world: World) -> None:
    text = world.refused("list_mail", {"unanswered_only": True, "own_notes_only": True})
    assert "select different messages" in text
    assert world.refused("list_mail", {"unanswered_only": 1}) == (
        "unanswered_only must be true or false"
    )


def test_a_message_closed_with_a_reason_leaves_the_count_and_keeps_it(
    world: World,
) -> None:
    task = world.deliver(world.scheduler.send(ANA, ALPHA, "do this"))
    world.call(
        "acknowledge_mail",
        {"message_id": task, "reason": "answered by the report m-1"},
    )
    assert world.unanswered() == 0
    message = world.log.state.messages[task]
    assert message.acknowledged
    assert message.closed_reason == "answered by the report m-1"
    text = world.call("list_mail", {})
    assert "closed because answered by the report m-1" in text
    (status,), _ = sent(world.log.state, ANA, limit=1)
    assert status.outcome == "acknowledged"
    assert "answered by the report" not in status.line()


def test_a_reason_that_is_empty_too_long_or_late_is_refused(world: World) -> None:
    task = world.deliver(world.scheduler.send(ANA, ALPHA, "do this"))
    assert "empty" in world.refused(
        "acknowledge_mail", {"message_id": task, "reason": "  "}
    )
    long = "x" * (MAX_CLOSE_REASON_BYTES + 1)
    assert "reason" in world.refused(
        "acknowledge_mail", {"message_id": task, "reason": long}
    )
    assert world.refused("acknowledge_mail", {"message_id": task, "reason": 7}) == (
        "reason must be text"
    )
    assert not world.log.state.messages[task].acknowledged
    world.call("acknowledge_mail", {"message_id": task})
    assert "its reason is kept" in world.refused(
        "acknowledge_mail", {"message_id": task, "reason": "too late"}
    )
    world.call("acknowledge_mail", {"message_id": task})


def test_a_reason_holding_a_disclosed_term_is_refused_and_records_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = world.deliver(world.scheduler.send(ANA, ALPHA, "do this"))

    def refuse(line_id: str, *fields: tuple[str, str]) -> str | None:
        del line_id
        return f"refused {fields[0][0]}"

    monkeypatch.setattr(world.scheduler, "refuse_disclosed", refuse)
    text = world.refused(
        "acknowledge_mail", {"message_id": task, "reason": "a listed term"}
    )
    assert text == "refused reason"
    assert not world.log.state.messages[task].acknowledged


# The times and the flags, against a scan.


def test_the_replied_flag_agrees_with_a_scan_of_the_replies(
    scenario: tuple[Event, ...],
) -> None:
    state = State()
    for event in scenario:
        state.apply(event)
        for message_id, message in state.messages.items():
            assert message.replied == (
                message_id in replied_ids(state, message.recipient)
            )


def test_the_times_are_kept_for_exactly_the_unanswered_mail(
    scenario: tuple[Event, ...], tmp_path: Path
) -> None:
    log = EventLog(tmp_path / "events.sqlite", lambda: "2026-11-01T09:00:00.000000Z")
    try:
        for event in scenario:
            log.append(event)
            state = log.state
            counted = {
                message_id
                for message_id, message in state.messages.items()
                if counts_unanswered(state, message)
            }
            assert set(log.times.unanswered_recorded) == counted
    finally:
        log.close()


def test_the_ages_survive_a_restart(world: World) -> None:
    task = world.deliver(world.scheduler.send(ANA, ALPHA, "do this"))
    before = world.log.times.unanswered_since(task)
    world.clock.advance(30)
    world.restart()
    assert before is not None
    assert world.log.times.unanswered_since(task) == before


def test_the_new_fields_round_trip_and_are_left_out_when_absent() -> None:
    plain = MessageSent("m-1", ANA, ALPHA, MessageKind.NOTE, "x")
    asking = MessageSent("m-2", ANA, ALPHA, MessageKind.NOTE, "x", asks_reply=False)
    closed = MessageAcknowledged("m-1", "answered elsewhere")
    for event in (plain, asking, closed, MessageAcknowledged("m-1")):
        assert decode(encode(event)) == event
    assert "asks_reply" not in encode(plain)
    assert "reason" not in encode(MessageAcknowledged("m-1"))


def test_age_words_name_the_two_largest_units() -> None:
    words = scheduler_module._age_words  # pyright: ignore[reportPrivateUsage]
    assert [words(s) for s in (0, 59, 60, 3_599, 3_600, 86_399, 86_400, 90_061)] == [
        "0s",
        "59s",
        "1m 0s",
        "59m 59s",
        "1h 0m",
        "23h 59m",
        "1d 0h",
        "1d 1h",
    ]
    with pytest.raises(ValueError, match="never negative"):
        words(-1)


# The operator alias.


def operator_world(tmp_path: Path, default: str, *names: str) -> World:
    root = tmp_path.resolve() / "project"
    (root / ".dromon").mkdir(parents=True)
    target = World(tmp_path / "events.sqlite", root, default)
    for name in names:
        target.scheduler.ensure_operator(name)
    return target


def test_at_operator_reaches_the_default_operator(tmp_path: Path) -> None:
    world = operator_world(tmp_path, "ana")
    try:
        text = world.call("send_message", {"to": "@operator", "body": "a report"})
        message = world.log.state.messages[text.removeprefix("sent as ")]
        assert message.recipient == ANA
        question = world.call("ask_operator", {"question": "go?", "to": "@operator"})
        assert any(
            message.recipient == ANA and message.kind is MessageKind.QUESTION
            for message in world.log.state.messages.values()
        ), question
    finally:
        world.log.close()


def test_an_operator_named_operator_is_meant_by_its_own_name(tmp_path: Path) -> None:
    world = operator_world(tmp_path, "ana", "operator")
    try:
        text = world.call("send_message", {"to": "@operator", "body": "a report"})
        message = world.log.state.messages[text.removeprefix("sent as ")]
        assert message.recipient == OperatorAddress("operator")
    finally:
        world.log.close()


def test_at_operator_without_a_registered_default_names_the_operators(
    tmp_path: Path,
) -> None:
    world = operator_world(tmp_path, "zoe", "ben")
    try:
        text = world.refused("send_message", {"to": "@operator", "body": "x"})
        assert text == (
            "no operator is configured for @operator to name; the operators are"
            " @ana, @ben"
        )
        text = world.refused("ask_operator", {"question": "?", "to": "@operator"})
        assert "the operators are @ana, @ben" in text
    finally:
        world.log.close()


def test_an_unknown_operator_names_the_operators(world: World) -> None:
    text = world.refused("send_message", {"to": "@nobody", "body": "x"})
    assert text == "no operator nobody; the operators are @ana"
    assert not any(message.body == "x" for message in world.log.state.messages.values())


def test_no_operator_at_all_says_there_are_none(tmp_path: Path) -> None:
    root = tmp_path.resolve() / "project"
    (root / ".dromon").mkdir(parents=True)
    log = EventLog(tmp_path / "events.sqlite", Clock().text)
    try:
        scheduler = Scheduler(log, lambda spec, tools: Idle(spec), tick_seconds=0)
        scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        with pytest.raises(EventRejected, match="there are none"):
            scheduler.send(ALPHA, OperatorAddress("operator"), "x")
    finally:
        log.close()
