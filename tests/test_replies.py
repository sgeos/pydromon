"""The replies query, ``replies`` and ``dromon mail --replies-to``.

Round 9, item 5, and its review, F11, F12 and F13. The query gives the bodies of the
replies to one message and marks none read, delivers the operator's mail as ``mail``
does, and fits one protocol line whatever its entries hold."""

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import cli
from dromon import daemon as daemon_module
from dromon.daemon import REPLIES_BUDGET_BYTES, Daemon
from dromon.eventlog import EventLog
from dromon.model import (
    ID_PATTERN,
    LineAddress,
    LineCreated,
    MessageDelivered,
    MessageKind,
    MessageRead,
    MessageReceived,
    MessageSent,
    OperatorAddress,
    OperatorRegistered,
    PeerDeclared,
    RemoteAddress,
)
from dromon.paths import ProjectPaths, project_paths
from dromon.protocol import MAX_LINE_BYTES, Operation, Response, encode_response
from dromon.scheduler import LineEngine, LineSpec, Scheduler

HAIKU = "claude-haiku-5-5"
ANA = OperatorAddress("ana")
ALPHA = LineAddress("alpha")


def replies_log(path: Path) -> None:
    """A log in which @ana asked alpha something, alpha replied twice, @ana read the
    first reply, and alpha sent @ana one note that replies to nothing."""
    with EventLog(path) as log:
        log.append(LineCreated("alpha", HAIKU, "subscription"))
        log.append(OperatorRegistered("ana"))
        log.append(MessageSent("m-q", ANA, ALPHA, MessageKind.NOTE, "status?"))
        log.append(MessageDelivered("m-q"))
        for message_id, body in (("m-1", "first"), ("m-2", "second\nline")):
            log.append(
                MessageSent(
                    message_id, ALPHA, ANA, MessageKind.NOTE, body, reply_to="m-q"
                )
            )
        log.append(MessageDelivered("m-1"))
        log.append(MessageRead("m-1"))
        log.append(MessageSent("m-3", ALPHA, ANA, MessageKind.NOTE, "aside"))


def held_daemon(paths: ProjectPaths, log: EventLog) -> Daemon:
    def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
        raise AssertionError(spec.line_id)

    return Daemon(
        paths, log, "2026-10-31T00:00:00Z", Scheduler(log, no_engine, hold_wakes=True)
    )


def entries(response: Response) -> list[dict[str, Any]]:
    assert response.result is not None
    return cast(list[dict[str, Any]], response.result["messages"])


def ask(daemon: Daemon, op: str, args: dict[str, object]) -> Response:
    line = json.dumps({"id": "r", "op": op, "v": 1, "args": args}).encode()
    return daemon.respond(line + b"\n")


@pytest.fixture
def paths(tmp_path: Path, short_runtime: Path) -> ProjectPaths:
    found = project_paths(tmp_path, short_runtime)
    found.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    return found


def test_replies_gives_the_bodies_and_marks_nothing_read(paths: ProjectPaths) -> None:
    replies_log(paths.event_log)
    with EventLog(paths.event_log) as log:
        daemon = held_daemon(paths, log)
        assert log.state.messages["m-2"].delivered is False
        response = ask(daemon, "replies", {"operator": "ana", "message_id": "m-q"})
        assert response.ok and response.result is not None
        result = response.result
        assert (result["total"], result["replies"], result["unread"]) == (2, 2, 1)
        assert result["left_out"] == 0
        messages = entries(response)
        assert [(m["message_id"], m["body"], m["read"]) for m in messages] == [
            ("m-1", "first", True),
            ("m-2", "second\nline", False),
        ]
        assert log.state.messages["m-2"].read is False
        # Round 9 review, F11. Listing delivers, as mail and collect do, which
        # changes the log but is not reading. The aside is delivered as well.
        assert log.state.messages["m-2"].delivered is True
        assert log.state.messages["m-3"].delivered is True
        assert log.state.messages["m-3"].read is False
        again = ask(
            daemon,
            "replies",
            {"operator": "ana", "message_id": "m-q", "unread_only": True},
        )
        assert again.result is not None
        assert [m["message_id"] for m in entries(again)] == ["m-2"]
        assert again.result["unread"] == 1


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        ({"operator": "ana"}, "with operator and message_id"),
        ({"operator": "ana", "message_id": "m-q", "x": 1}, "replies takes"),
        ({"operator": "ana", "message_id": "m-none"}, "no message m-none"),
        ({"operator": "ana", "message_id": "m-q", "limit": 0}, "limit in"),
        ({"operator": "ana", "message_id": "m-q", "offset": -1}, "offset must"),
        ({"operator": "ana", "message_id": "m-q", "unread_only": 1}, "boolean"),
    ],
)
def test_replies_refuses_malformed_arguments(
    paths: ProjectPaths, args: dict[str, object], reason: str
) -> None:
    """The unknown identifier tells whether one exists, accepted in review as F12,
    since identifiers are random and the socket is the operator's own."""
    replies_log(paths.event_log)
    with EventLog(paths.event_log) as log:
        response = ask(held_daemon(paths, log), "replies", args)
        assert not response.ok
        assert reason in (response.error_message or "")
        assert log.state.messages["m-2"].read is False


def test_replies_withholds_bodies_past_its_budget(paths: ProjectPaths) -> None:
    replies_log(paths.event_log)
    large = "\x01" * (REPLIES_BUDGET_BYTES // 6 + 1)
    with EventLog(paths.event_log) as log:
        log.append(
            MessageSent("m-4", ALPHA, ANA, MessageKind.NOTE, large, reply_to="m-q")
        )
        response = ask(
            held_daemon(paths, log), "replies", {"operator": "ana", "message_id": "m-q"}
        )
        assert response.result is not None
        last = entries(response)[-1]
        assert (last["message_id"], last["body"]) == ("m-4", None)
        assert last["body_bytes"] == len(large.encode("utf-8"))


def longest(prefix: str) -> str:
    """The longest identifier that begins with ``prefix``."""
    width = 64
    found = (prefix + "x" * width)[:width]
    assert ID_PATTERN.fullmatch(found)
    return found


def test_a_full_page_of_the_longest_replies_fits_one_protocol_line(
    paths: ProjectPaths,
) -> None:
    """Round 9 review, F13, at the worst case. Every identifier is as long as the
    log allows, every sender is a remote address of two such identifiers, the page
    is full, and every body escapes sixfold and would fill the budget alone. The
    whole response, encoded as the daemon writes it, fits the line limit. The
    question went to the remote party, since a reply comes from whom it asked."""
    operator = longest("o")
    question = longest("q")
    peer = longest("p")
    sender = RemoteAddress(peer, OperatorAddress(longest("a")))
    body = "\x01" * (REPLIES_BUDGET_BYTES // 12)
    page = 50
    with EventLog(paths.event_log) as log:
        log.append(OperatorRegistered(operator))
        log.append(LineCreated("alpha", HAIKU, "subscription"))
        log.append(PeerDeclared(peer, "unix", "{}"))
        log.append(
            MessageSent(
                question, OperatorAddress(operator), sender, MessageKind.NOTE, "?"
            )
        )
        for index in range(page):
            log.append(
                MessageReceived(
                    longest(f"r{index:02d}"),
                    sender,
                    OperatorAddress(operator),
                    body,
                    question,
                    longest(f"g{index:02d}"),
                    "2026-10-31T00:00:00Z",
                )
            )
        response = ask(
            held_daemon(paths, log),
            "replies",
            {"operator": operator, "message_id": question, "limit": page},
        )
        assert response.ok and response.result is not None
        shown = entries(response)
        assert len(shown) + cast(int, response.result["left_out"]) == page
        assert any(entry["body"] is not None for entry in shown)
        assert any(entry["body"] is None for entry in shown)
        response = Response(longest("i")[:64], ok=True, result=response.result)
        assert len(encode_response(response)) <= MAX_LINE_BYTES


def test_entries_whose_fields_do_not_fit_are_left_out_in_order(
    paths: ProjectPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 9 review, F13. Fields count against the budget, so with a budget that
    holds one entry's fields and not two, the second and every later entry are left
    out and counted, for a later page to give."""
    replies_log(paths.event_log)
    with EventLog(paths.event_log) as log:
        log.append(MessageSent("m-4", ALPHA, ANA, MessageKind.NOTE, "", reply_to="m-q"))
        daemon = held_daemon(paths, log)
        full = ask(daemon, "replies", {"operator": "ana", "message_id": "m-q"})
        first = entries(full)[0]
        fields = len(json.dumps(first, separators=(",", ":")).encode())
        monkeypatch.setattr(daemon_module, "REPLIES_BUDGET_BYTES", fields + 20)
        cut = ask(daemon, "replies", {"operator": "ana", "message_id": "m-q"})
        assert cut.result is not None
        assert [m["message_id"] for m in entries(cut)] == ["m-1"]
        assert entries(cut)[0]["body"] == "first"
        assert cut.result["left_out"] == 2
        later = ask(
            daemon, "replies", {"operator": "ana", "message_id": "m-q", "offset": 1}
        )
        assert later.result is not None
        assert [m["message_id"] for m in entries(later)] == ["m-2"]
        assert later.result["left_out"] == 1


def mail_arguments(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "operator": "ana",
        "unread": False,
        "sender": None,
        "offset": 0,
        "limit": 20,
        "replies_to": "m-q",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_dromon_mail_replies_to_prints_bodies_through_the_replies_request(
    tmp_path: Path,
    short_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asked: list[tuple[Operation, dict[str, object]]] = []
    left_out = [0]

    def fake_call(
        paths: object, name: str, operation: Operation, args: dict[str, object]
    ) -> dict[str, object]:
        asked.append((operation, args))
        return {
            "message_id": "m-q",
            "total": 2,
            "replies": 2,
            "unread": 1,
            "left_out": left_out[0],
            "messages": [
                {
                    "message_id": "m-1",
                    "sender": "alpha",
                    "kind": "note",
                    "read": True,
                    "outcome": "unanswered",
                    "body": "first\nsecond",
                    "body_bytes": 12,
                },
                {
                    "message_id": "m-2",
                    "sender": "alpha",
                    "kind": "note",
                    "read": False,
                    "outcome": "unanswered",
                    "body": None,
                    "body_bytes": 99_999,
                },
            ],
        }

    monkeypatch.setattr(cli, "_call", fake_call)
    paths = project_paths(tmp_path, short_runtime)
    assert cli._mail(paths, mail_arguments()) == 0  # pyright: ignore[reportPrivateUsage]
    assert asked == [
        (
            Operation.REPLIES,
            {
                "operator": "ana",
                "message_id": "m-q",
                "unread_only": False,
                "offset": 0,
                "limit": 20,
            },
        )
    ]
    out = capsys.readouterr().out
    assert out.startswith("2 matching of 2 replies to m-q, 1 unread\n")
    assert "m-1 from alpha, note, read\n  first\n  second\n" in out
    assert "dromon read m-2 opens it and marks it read" in out
    assert "did not fit" not in out
    left_out[0] = 3
    assert cli._mail(paths, mail_arguments(offset=4)) == 0  # pyright: ignore[reportPrivateUsage]
    assert "3 more did not fit this answer. --offset 6 gives them." in (
        capsys.readouterr().out
    )
    assert cli._mail(paths, mail_arguments(sender="alpha")) == 2  # pyright: ignore[reportPrivateUsage]
    assert "does not combine" in capsys.readouterr().err


def test_the_parser_takes_replies_to() -> None:
    parsed = cli.build_parser().parse_args(["mail", "--replies-to", "m-q", "--unread"])
    assert (parsed.replies_to, parsed.unread) == ("m-q", True)
