"""The keep-session ceiling, its configuration, and the variables a line may not set.

**The operator's decision A of 2026-10-26**, and rows 4 and 12 of the T3 Code review.
"""

from typing import Any

import pytest

from dromon import ceiling as ceiling_module
from dromon.agents import LINE_TOKEN_IDLE_SECONDS, AgentError, LineTokens
from dromon.ceiling import (
    CHOICES,
    HARD_REASON,
    IDLE_REASON,
    MAX_REASON,
    NO_OPERATOR_REASON,
    Ceiling,
    Choice,
    parse_choice,
    is_question,
    question_text,
)
from dromon.launcher import StopPolicy
from dromon.lineconfig import (
    DEFAULT_KEEP_HARD_SECONDS,
    MAX_KEEP_HARD_SECONDS,
    AtMax,
    ConfigError,
    LineConfig,
    config_from_document,
    decode_config,
    encode_config,
)
from dromon.projectconfig import parse
from dromon.settings import SettingsError, parse_settings, reserved_variable

LAST_END = 1000.0


def config(at_max: AtMax, **values: Any) -> LineConfig:  # noqa: ANN401
    return LineConfig(
        keep_session=True,
        keep_session_idle_seconds=60,
        keep_session_max_seconds=600,
        keep_session_hard_seconds=3600,
        keep_session_at_max=at_max,
        **values,
    )


class Operator:
    """Answers a ceiling's ask with question identifiers, and records withdrawals."""

    def __init__(self, reachable: bool = True) -> None:
        self.reachable = reachable
        self.asked: list[int] = []
        self.withdrawn: list[str] = []

    def ask(self, tasks: int) -> str | None:
        self.asked.append(tasks)
        return f"q{len(self.asked)}" if self.reachable else None

    def settle(self, question_id: str) -> None:
        self.withdrawn.append(question_id)


def watched(at_max: AtMax, operator: Operator | None = None) -> Ceiling:
    target = operator or Operator()
    return Ceiling(config(at_max), target.ask, target.settle)


def test_without_background_work_the_idle_deadline_applies() -> None:
    for at_max in AtMax:
        found = watched(at_max)
        assert found.deadline(LAST_END, busy=False) == LAST_END + 60
        assert found.due(LAST_END, 0, LAST_END + 59) is None
        assert found.due(LAST_END, 0, LAST_END + 60) == IDLE_REASON


def test_close_closes_at_the_ceiling() -> None:
    found = watched(AtMax.CLOSE)
    assert found.deadline(LAST_END, busy=True) == LAST_END + 600
    assert found.due(LAST_END, 2, LAST_END + 599) is None
    assert found.due(LAST_END, 2, LAST_END + 600) == MAX_REASON


def test_keep_keeps_while_work_runs_up_to_the_hard_bound() -> None:
    found = watched(AtMax.KEEP)
    assert found.deadline(LAST_END, busy=True) == LAST_END + 3600
    assert found.due(LAST_END, 1, LAST_END + 3599) is None
    assert found.due(LAST_END, 1, LAST_END + 3600) == HARD_REASON


def test_ask_asks_once_and_keeps_until_the_hard_bound() -> None:
    operator = Operator()
    found = watched(AtMax.ASK, operator)
    assert found.due(LAST_END, 3, LAST_END + 600) is None
    assert operator.asked == [3] and found.question == "q1"
    assert found.deadline(LAST_END, busy=True) == LAST_END + 3600
    assert found.due(LAST_END, 3, LAST_END + 1200) is None
    assert operator.asked == [3]
    assert found.due(LAST_END, 3, LAST_END + 3600) == HARD_REASON


def test_ask_without_an_operator_closes_as_close_does() -> None:
    found = watched(AtMax.ASK, Operator(reachable=False))
    assert found.due(LAST_END, 1, LAST_END + 600) == NO_OPERATOR_REASON


def test_extend_moves_the_ceiling_and_asks_again() -> None:
    operator = Operator()
    found = watched(AtMax.ASK, operator)
    found.due(LAST_END, 1, LAST_END + 600)
    assert found.decide("q1", " Extend ", LAST_END + 700) is Choice.EXTEND
    assert found.question is None
    assert found.deadline(LAST_END, busy=True) == LAST_END + 1300
    assert found.due(LAST_END, 1, LAST_END + 1300) is None
    assert operator.asked == [1, 1] and found.question == "q2"


def test_an_extension_never_passes_the_hard_bound() -> None:
    found = watched(AtMax.ASK)
    found.due(LAST_END, 1, LAST_END + 600)
    found.decide("q1", "extend", LAST_END + 3500)
    assert found.deadline(LAST_END, busy=True) == LAST_END + 3600


def test_let_it_finish_keeps_it_while_work_runs_across_turns() -> None:
    operator = Operator()
    found = watched(AtMax.ASK, operator)
    found.due(LAST_END, 1, LAST_END + 600)
    assert found.decide("q1", "let it finish", LAST_END + 601) is Choice.FINISH
    found.turn_ended()
    assert found.deadline(LAST_END + 1000, busy=True) == LAST_END + 1000 + 3600
    assert operator.asked == [1]


def test_stop_is_returned_for_the_engine_to_carry_out() -> None:
    found = watched(AtMax.ASK)
    found.due(LAST_END, 1, LAST_END + 600)
    assert found.decide("q1", "STOP", LAST_END + 601) is Choice.STOP
    assert found.question is None


def test_an_answer_that_chooses_nothing_or_to_another_question_decides_nothing() -> (
    None
):
    found = watched(AtMax.ASK)
    found.due(LAST_END, 1, LAST_END + 600)
    assert found.decide("q9", "extend", LAST_END + 601) is None
    assert found.decide("q1", "yes, keep it", LAST_END + 601) is None
    assert found.question == "q1"
    assert found.deadline(LAST_END, busy=True) == LAST_END + 3600


def test_a_turn_or_a_close_withdraws_an_open_question() -> None:
    operator = Operator()
    found = watched(AtMax.ASK, operator)
    found.due(LAST_END, 1, LAST_END + 600)
    found.decide("q1", "extend", LAST_END + 601)
    found.due(LAST_END, 1, LAST_END + 1201)
    found.turn_ended()
    assert operator.withdrawn == ["q2"] and found.question is None
    assert found.soft is None
    found.withdraw()
    assert operator.withdrawn == ["q2"]


def test_choices_are_read_whatever_their_case_and_spacing() -> None:
    assert CHOICES == ("extend", "stop", "let it finish")
    assert parse_choice("  Let\tIT   finish\n") is Choice.FINISH
    for other in ("", "finish", "extend it", "stopp"):
        assert parse_choice(other) is None


def test_the_question_names_the_bounds_and_the_choices() -> None:
    text = question_text("alpha", 2, config(AtMax.ASK))
    for part in ("alpha", "600", "3600", "2 background", *CHOICES):
        assert part in text
    assert "end its background work now" in text


def test_under_leave_background_the_question_does_not_promise_an_end() -> None:
    """Item 7 of the live check of 2026-10-26. A close under that policy leaves the
    work running, so stop is not offered as its end."""
    text = question_text(
        "alpha", 2, config(AtMax.ASK, stop_policy=StopPolicy.LEAVE_BACKGROUND)
    )
    assert "end its background work" not in text
    assert "leaves its background work running" in text
    assert is_question(text, CHOICES)


def test_the_default_engine_hooks_ask_nobody() -> None:
    assert ceiling_module.no_operator(3) is None
    found = Ceiling(config(AtMax.ASK))
    assert found.due(LAST_END, 1, LAST_END + 600) == NO_OPERATOR_REASON
    found.question = "q"
    found.withdraw()
    assert found.question is None


# The configuration.


def test_the_new_keys_round_trip_and_are_omitted_at_their_defaults() -> None:
    chosen = config_from_document(
        {
            "keep_session": True,
            "keep_session_at_max": "ask",
            "keep_session_hard_seconds": 28800,
            "stop_policy": "leave_background",
        }
    )
    assert chosen.keep_session_at_max is AtMax.ASK
    assert chosen.keep_session_hard_seconds == 28800
    assert chosen.stop_policy is StopPolicy.LEAVE_BACKGROUND
    assert decode_config(encode_config(chosen)) == chosen
    stored = encode_config(LineConfig())
    for key in ("keep_session_at_max", "keep_session_hard_seconds", "stop_policy"):
        assert key not in stored
    assert LineConfig().keep_session_at_max is AtMax.CLOSE
    assert LineConfig().keep_session_hard_seconds == DEFAULT_KEEP_HARD_SECONDS
    assert LineConfig().stop_policy is StopPolicy.KILL_ALL


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({"keep_session_at_max": "later"}, "keep_session_at_max must be one of"),
        ({"keep_session_at_max": "ASK"}, "keep_session_at_max must be one of"),
        ({"keep_session_at_max": 1}, "keep_session_at_max must be one of"),
        ({"stop_policy": "kill"}, "stop_policy must be one of"),
        ({"stop_policy": None}, "stop_policy must be one of"),
        ({"keep_session_hard_seconds": 59}, "keep_session_hard_seconds must be"),
        (
            {"keep_session_hard_seconds": MAX_KEEP_HARD_SECONDS + 1},
            "keep_session_hard_seconds must be",
        ),
        ({"keep_session_hard_seconds": "600"}, "keep_session_hard_seconds must be"),
        ({"keep_session_hard_seconds": True}, "keep_session_hard_seconds must be"),
        (
            {"keep_session_max_seconds": 7200, "keep_session_hard_seconds": 3600},
            "must not be less than keep_session_max_seconds",
        ),
    ],
)
def test_the_new_keys_are_checked_strictly(
    document: dict[str, object], message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        config_from_document(document)


def test_the_project_file_sets_the_new_keys_for_a_line_and_a_profile() -> None:
    project = parse(
        "version = 1\n"
        "[profiles.kept]\nmodel = 'claude-haiku-5-5'\nkeep_session = true\n"
        "keep_session_at_max = 'keep'\n"
        "[lines.a]\nprofile = 'kept'\nstop_policy = 'leave_background'\n"
        "keep_session_hard_seconds = 43200\n"
    )
    line = project.lines["a"]
    assert line.config.keep_session_at_max is AtMax.KEEP
    assert line.config.stop_policy is StopPolicy.LEAVE_BACKGROUND
    assert line.config.keep_session_hard_seconds == 43200


# Variables a line may not set.


def test_a_line_may_not_set_the_pinned_tool_search() -> None:
    """Row 4 of the T3 Code review. Any value is refused, the pinned one too, so
    that the line's settings never decide it."""
    for value in ("auto", "auto:10", "false", "true"):
        with pytest.raises(SettingsError, match="pins") as refused:
            parse_settings({"env": {"ENABLE_TOOL_SEARCH": value}})
        assert refused.value.key == "env.ENABLE_TOOL_SEARCH"
    assert reserved_variable("ENABLE_TOOL_SEARCH")


def test_a_line_may_not_set_a_variable_dromon_sets() -> None:
    for name in ("DROMON_STOP_POLICY", "DROMON_LAUNCH_TARGET", "DROMON_ANYTHING"):
        with pytest.raises(SettingsError, match="Dromon sets itself"):
            parse_settings({"env": {name: "x"}})
        assert reserved_variable(name)
    assert not reserved_variable("DROMONISH")
    assert parse_settings({"env": {"TOOL_SEARCH": "auto"}}) is not None


def test_a_server_may_not_refer_to_a_pinned_or_dromon_variable() -> None:
    for name in ("ENABLE_TOOL_SEARCH", "DROMON_STOP_POLICY"):
        with pytest.raises(ConfigError, match=name):
            config_from_document(
                {
                    "mcp_servers": {
                        "s": {"type": "stdio", "command": "x", "args": [f"${{{name}}}"]}
                    }
                }
            )


# Line tokens, row 12.


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_a_line_token_is_kept_for_a_build_and_renewed_for_each_session() -> None:
    tokens = LineTokens(Clock())
    first = tokens.issue("alpha")
    assert tokens.issue("alpha") == first
    second = tokens.renew("alpha")
    assert second != first
    with pytest.raises(AgentError, match="needs its token"):
        tokens.check("alpha", first)
    assert tokens.check("alpha", second) == "alpha"


def test_a_revoked_line_token_no_longer_works() -> None:
    tokens = LineTokens(Clock())
    token = tokens.renew("alpha")
    tokens.revoke("alpha")
    tokens.revoke("alpha")
    with pytest.raises(AgentError, match="needs its token"):
        tokens.check("alpha", token)
    assert tokens.issue("alpha") != token


def test_a_line_token_does_not_expire_while_its_turn_runs() -> None:
    """Review F15. A long turn's late call is not refused."""
    clock = Clock()
    tokens = LineTokens(clock)
    token = tokens.renew("alpha")
    clock.now += 10 * LINE_TOKEN_IDLE_SECONDS
    assert tokens.check("alpha", token) == "alpha"
    clock.now += 10 * LINE_TOKEN_IDLE_SECONDS
    assert tokens.check("alpha", token) == "alpha"


def test_a_kept_line_token_expires_unused_between_turns() -> None:
    """A token kept for a process that outlived its turn expires unused, and each
    use keeps it."""
    clock = Clock()
    tokens = LineTokens(clock)
    token = tokens.renew("alpha")
    tokens.finish("alpha")
    clock.now += LINE_TOKEN_IDLE_SECONDS
    assert tokens.check("alpha", token) == "alpha"
    clock.now += LINE_TOKEN_IDLE_SECONDS
    assert tokens.check("alpha", token) == "alpha"
    clock.now += LINE_TOKEN_IDLE_SECONDS + 1
    with pytest.raises(AgentError, match="expired"):
        tokens.check("alpha", token)
    with pytest.raises(AgentError, match="needs its token"):
        tokens.check("alpha", token)
    assert tokens.begin("alpha") is None
    clock.now += LINE_TOKEN_IDLE_SECONDS + 1
    assert tokens.issue("alpha") != token


def test_a_kept_token_is_kept_for_the_next_turn_on_the_same_process() -> None:
    clock = Clock()
    tokens = LineTokens(clock)
    token = tokens.renew("alpha")
    tokens.finish("alpha")
    clock.now += LINE_TOKEN_IDLE_SECONDS - 1
    assert tokens.begin("alpha") == token
    clock.now += 10 * LINE_TOKEN_IDLE_SECONDS
    assert tokens.check("alpha", token) == "alpha"
    assert tokens.begin("beta") is None


def test_a_line_token_is_refused_and_revoked_once_its_process_ends() -> None:
    """Review F14. The token belongs to the engine's process, not to the turn."""
    tokens = LineTokens(Clock())
    running = [True]
    token = tokens.renew("alpha")
    tokens.finish("alpha")
    tokens.bind("alpha", lambda: running[0])
    assert tokens.check("alpha", token) == "alpha"
    running[0] = False
    with pytest.raises(AgentError, match="process has ended"):
        tokens.check("alpha", token)
    with pytest.raises(AgentError, match="needs its token"):
        tokens.check("alpha", token)


def test_a_liveness_check_that_fails_refuses_the_token() -> None:
    tokens = LineTokens(Clock())
    token = tokens.renew("alpha")

    def broken() -> bool:
        raise OSError("gone")

    tokens.bind("alpha", broken)
    with pytest.raises(AgentError, match="process has ended"):
        tokens.check("alpha", token)


def test_line_tokens_are_checked_as_before() -> None:
    tokens = LineTokens()
    with pytest.raises(AgentError, match="not an identifier"):
        tokens.renew("Bad Name")
    with pytest.raises(AgentError, match="must be text"):
        tokens.check("alpha", None)
