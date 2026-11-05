"""Task of 2026-11-05, item 3. A declared run command may name parameters a line
supplies, each matched against the operator's pattern and rules the operator cannot
widen. ``docs/architecture/RUNS.md`` gives the trust argument."""

import asyncio
import os
import subprocess
import sys
import time
from collections.abc import Generator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import runparams, runs, spawn
from dromon.driver import Strategy
from dromon.eventlog import EventLog
from dromon.projectconfig import ConfigFileError, parse
from dromon.runparams import ParameterRefused, RunParameter
from dromon.scheduler import START_RUN_TOOL_NAME, LineEngine, LineSpec, Scheduler

HAIKU = "claude-haiku-5-5"
AT = "2026-10-31T09:00:00Z"
WAIT_SECONDS = 20
TEST_PATH = RunParameter("test", r"tests/[a-z0-9_]+\.py", "one test file")
COMMAND = ("uv", "run", "pytest", "{test}", "-q")


# The rules for a declaration.


@pytest.mark.parametrize(
    ("pattern", "reason"),
    [
        ("", "1 to 256 bytes"),
        ("a" * 257, "1 to 256 bytes"),
        ("(?=x)x", "(?"),
        ("(?P<n>a)", "(?"),
        (r"(a)\1", "backreference"),
        ("a*+", "possessive"),
        ("a{2}+", "possessive"),
        ("[", "not valid"),
        ("([a-z]+)+", "repeat a group"),
        (r"(\w+\s?)*", "repeat a group"),
        ("(ab){2,}", "repeat a group"),
        ("x(a|b)+y", "repeat a group"),
        ("((a))+", "repeat a group"),
    ],
)
def test_a_pattern_that_is_empty_unportable_or_invalid_is_refused(
    pattern: str, reason: str
) -> None:
    with pytest.raises(ParameterRefused, match=reason.replace("(", r"\(")):
        runparams.check_pattern(pattern)


@pytest.mark.parametrize(
    "pattern",
    [
        r"tests/[a-z0-9_]+\.py",
        r"tests/(unit|integration)/[a-z_]+\.py",
        r"[a-z]+(::[A-Za-z_]+)?",
        r"\)+",
        "[)]+",
        "[]a)]+",
        "[^])]+",
        r"[\])]+",
    ],
)
def test_a_pattern_that_repeats_no_group_is_accepted(pattern: str) -> None:
    """Round 15 review, P1. An escaped parenthesis, or one in a character class, is
    no group, and a group that is not repeated, or only made optional, is
    accepted."""
    assert not runparams.repeats_group(pattern)
    runparams.check_pattern(pattern)


def test_the_group_scan_ends_on_a_trailing_escape() -> None:
    assert not runparams.repeats_group("a\\")
    assert runparams.repeats_group("[x](a)+")
    assert not runparams.repeats_group("[(a)+]")


def test_placeholders_are_whole_arguments_after_the_program() -> None:
    argv = ("prog", "{a}", "x{b}", "{c}y", "{d}", "{Upper}")
    assert runparams.placeholders(argv) == ("a", "d")


@pytest.mark.parametrize(
    ("argv", "declared", "reason"),
    [
        (("{test}", "x"), {"test": TEST_PATH}, "program may not"),
        (("prog", "{test}", "{test}"), {"test": TEST_PATH}, "appear once"),
        (("prog", "{test}"), {}, "no parameter declares"),
        (("prog", "x"), {"test": TEST_PATH}, "no placeholder for"),
    ],
)
def test_placeholders_and_parameters_must_agree(
    argv: tuple[str, ...], declared: dict[str, RunParameter], reason: str
) -> None:
    with pytest.raises(ParameterRefused, match=reason):
        runparams.check_declared(argv, declared)


def test_a_command_declares_at_most_eight_parameters() -> None:
    names = [f"p{n}" for n in range(runparams.MAX_PARAMETERS + 1)]
    argv = ("prog", *(f"{{{name}}}" for name in names))
    declared = {name: RunParameter(name, "x") for name in names}
    with pytest.raises(ParameterRefused, match="at most 8"):
        runparams.check_declared(argv, declared)


# The rules for a value, which the pattern cannot widen.


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (7, "must be text"),
        ("", "1 to 256 bytes"),
        ("t" * 257, "1 to 256 bytes"),
        ("tests/a.py\n", "printable"),
        ("tests/a\x00.py", "printable"),
        ("-k", "begin with -"),
        ("--rootdir=/", "begin with -"),
        ("tests/../secret.py", "a .. step"),
        ("tests/a.py extra", "does not match"),
        ("src/a.py", "does not match"),
    ],
)
def test_a_value_is_refused_by_the_fixed_rules_or_its_pattern(
    value: object, reason: str
) -> None:
    with pytest.raises(ParameterRefused, match=reason):
        substitute(("prog", "{test}"), {"test": TEST_PATH}, {"test": value})


def substitute(
    argv: Sequence[str],
    declared: dict[str, RunParameter],
    given: dict[str, object] | None,
    seconds: float = runparams.MATCH_SECONDS,
) -> tuple[str, ...]:
    """Prepare and match as ``Scheduler.start_run`` does."""
    return asyncio.run(
        runparams.match(runparams.prepare(argv, declared, given), seconds)
    )


def test_a_dash_is_refused_even_where_the_pattern_allows_it() -> None:
    wide = RunParameter("any", ".+")
    with pytest.raises(ParameterRefused, match="begin with -"):
        runparams.check_fixed(wide, "-x")
    assert runparams.check_fixed(wide, "x-") == "x-"


@pytest.mark.parametrize(
    "value",
    [
        "tests/../../etc/passwd",
        "..",
        "../x",
        "a/..",
        "a/../b",
        "/etc/passwd",
        "/",
    ],
)
def test_a_path_out_is_refused_whatever_the_pattern(value: str) -> None:
    """Round 15 review, P2. The audit's ``tests/[A-Za-z0-9_./]+`` accepted
    ``tests/../../etc/passwd``. A leading / or a .. step is refused before any
    pattern is matched."""
    wide = RunParameter("test", r"[A-Za-z0-9_./]+")
    with pytest.raises(ParameterRefused, match=r"begin with / or hold a \.\. step"):
        runparams.check_fixed(wide, value)


@pytest.mark.parametrize("value", ["a..b", "tests/x..py", "...", "a/.../b", "./a"])
def test_dots_that_are_no_step_up_are_accepted(value: str) -> None:
    wide = RunParameter("test", r"[A-Za-z0-9_./]+")
    assert runparams.check_fixed(wide, value) == value


def test_a_backtracking_match_is_refused_at_its_deadline_off_the_loop() -> None:
    """Round 15 review, P1. A pattern no group repeats can still backtrack
    exponentially, as a run of optional items does. The child that matches it is
    killed at the deadline, the value is refused, and the loop runs meanwhile."""
    count = 40
    slow = RunParameter("value", "a?" * count + "a" * count)
    assert not runparams.repeats_group(slow.pattern)
    prepared = runparams.prepare(
        ("prog", "{value}"), {"value": slow}, {"value": "a" * count}
    )
    ticks: list[float] = []

    async def scenario() -> str:
        async def tick() -> None:
            for _ in range(1000):
                ticks.append(time.monotonic())
                await asyncio.sleep(0.01)

        ticking = asyncio.create_task(tick())
        try:
            await runparams.match(prepared, 0.5)
        except ParameterRefused as error:
            return str(error)
        finally:
            ticking.cancel()
        return "matched"

    started = time.monotonic()
    refusal = asyncio.run(scenario())
    assert "took longer than 0.5 seconds" in refusal
    assert time.monotonic() - started < 10
    # The loop ran while the child matched, so the ticks span the wait.
    assert len(ticks) > 10


SLOW_MATCHER = "import time; time.sleep(30)"
"""A matcher that answers far past any deadline, so a test can tell a killed child
from one left to run or waited out."""


def spawned_children(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[subprocess.Popen[bytes], float]]:
    """Record each child ``match`` starts through ``spawn``, with the monotonic time
    it started."""
    started: list[tuple[subprocess.Popen[bytes], float]] = []
    real = cast(Any, spawn.start)

    def recording(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        child = cast(subprocess.Popen[bytes], real(*args, **kwargs))
        started.append((child, time.monotonic()))
        return child

    monkeypatch.setattr(runparams.spawn, "start", recording)
    return started


def assert_killed(child: subprocess.Popen[bytes]) -> None:
    """The child was killed and reaped, so it no longer exists."""
    assert child.returncode == -9, "the child was not killed"
    with pytest.raises(ProcessLookupError):
        os.kill(child.pid, 0)


def test_the_child_is_killed_at_the_default_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 15 re-review. The audit found that removing the kill, or raising
    ``MATCH_SECONDS`` to 200 seconds, survived the tests, which passed their own
    deadline. This one uses the default deadline that ``Scheduler.start_run`` uses,
    and a child that would sleep 30 seconds, so a child waited out or left running
    fails it. The time is taken from the child's start, so a slow start under load
    does not count against the deadline."""
    monkeypatch.setattr(runparams, "MATCHER", SLOW_MATCHER)
    started = spawned_children(monkeypatch)
    prepared = runparams.prepare(
        ("prog", "{test}"), {"test": TEST_PATH}, {"test": "tests/a.py"}
    )
    with pytest.raises(ParameterRefused, match="took longer than 2 seconds"):
        asyncio.run(runparams.match(prepared))
    ended = time.monotonic()
    [(child, began)] = started
    assert runparams.MATCH_SECONDS <= ended - began < 10
    assert_killed(child)


def test_the_child_is_killed_when_the_start_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A start whose call is cancelled while the child matches, as when the line's
    turn ends, kills and reaps the child before the cancellation goes on."""
    monkeypatch.setattr(runparams, "MATCHER", SLOW_MATCHER)
    started = spawned_children(monkeypatch)
    prepared = runparams.prepare(
        ("prog", "{test}"), {"test": TEST_PATH}, {"test": "tests/a.py"}
    )

    async def scenario() -> None:
        task = asyncio.create_task(runparams.match(prepared))
        for _ in range(1000):
            if started:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(asyncio.wait_for(scenario(), 20))
    [(child, _)] = started
    assert_killed(child)


def test_a_child_that_fails_refuses_the_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """A child that exits with an error, or prints anything but one answer for each
    value, refuses the start, so a broken matcher never lets a value through."""
    prepared = runparams.prepare(
        ("prog", "{test}"), {"test": TEST_PATH}, {"test": "tests/a.py"}
    )
    for program in (
        "import sys; sys.exit(3)",
        "print('[true, true]')",
        "print('[1]')",
        "print('not json')",
        "print('x' * 5000)",
    ):
        monkeypatch.setattr(runparams, "MATCHER", program)
        with pytest.raises(ParameterRefused, match="could not be matched"):
            asyncio.run(runparams.match(prepared))
    monkeypatch.setattr(runparams, "MATCHER", "print('[false]')")
    with pytest.raises(ParameterRefused, match="does not match its pattern"):
        asyncio.run(runparams.match(prepared))


def test_a_start_that_fails_or_stalls_refuses_and_goes_through_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The matching child is started by ``spawn.start``, so its start is bounded and
    counted, and a start that fails or stalls refuses the values and never lets one
    through. Merge of the ops work, which started it with asyncio."""
    prepared = runparams.prepare(
        ("prog", "{test}"), {"test": TEST_PATH}, {"test": "tests/a.py"}
    )
    real = cast(Any, spawn.start)
    for failure in (OSError("no exec"), spawn.StartStalled("stalled")):

        def failing(
            *args: object, error: Exception = failure, **kwargs: object
        ) -> object:
            raise error

        monkeypatch.setattr(runparams.spawn, "start", failing)
        with pytest.raises(ParameterRefused, match="could not be matched"):
            asyncio.run(runparams.match(prepared))
    calls: list[object] = []

    def counting(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        calls.append(args)
        return cast(subprocess.Popen[bytes], real(*args, **kwargs))

    monkeypatch.setattr(runparams.spawn, "start", counting)
    assert asyncio.run(runparams.match(prepared)) == ("prog", "tests/a.py")
    assert len(calls) == 1


def test_a_command_without_values_starts_no_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def never(*args: object, **kwargs: object) -> object:
        raise AssertionError("a child was started")

    monkeypatch.setattr(runparams.spawn, "start", never)
    assert substitute(("make",), {}, None) == ("make",)


def test_values_replace_their_placeholders_as_whole_arguments() -> None:
    argv = ("uv", "run", "pytest", "{test}", "x{test}")
    declared = {"test": TEST_PATH}
    # x{test} is not a placeholder, so it is left as the operator wrote it.
    assert substitute(argv[:4], declared, {"test": "tests/test_x.py"}) == (
        "uv",
        "run",
        "pytest",
        "tests/test_x.py",
    )
    assert runparams.placeholders(argv) == ("test",)


@pytest.mark.parametrize(
    ("given", "reason"),
    [
        ({}, "needs arguments test"),
        (None, "needs arguments test"),
        ({"test": "tests/a.py", "other": "x"}, "no parameter other"),
    ],
)
def test_a_missing_or_extra_value_is_refused(
    given: dict[str, object] | None, reason: str
) -> None:
    with pytest.raises(ParameterRefused, match=reason):
        runparams.prepare(COMMAND, {"test": TEST_PATH}, given)


def test_a_command_without_parameters_takes_no_arguments() -> None:
    assert substitute(("make",), {}, None) == ("make",)
    assert substitute(("make",), {}, {}) == ("make",)
    with pytest.raises(ParameterRefused, match="takes no arguments"):
        runparams.prepare(("make",), {}, {"x": "y"})


def test_describe_names_each_pattern() -> None:
    assert runparams.describe({"test": TEST_PATH}) == (
        r"test matching tests/[a-z0-9_]+\.py, one test file"
    )
    assert runparams.describe({}) == ""


# The project file.

PROJECT = """version = 1
[runs.commands]
one_test = ["uv", "run", "pytest", "{test}", "-q"]
gate = ["./scripts/check.sh"]
[runs.parameters.one_test.test]
pattern = 'tests/[a-z0-9_]+\\.py'
description = "one test file"
"""


def test_the_project_file_declares_parameters() -> None:
    config = parse(PROJECT)
    assert config.run_parameters == {"one_test": {"test": TEST_PATH}}
    assert config.run_commands["one_test"] == COMMAND


@pytest.mark.parametrize(
    ("body", "where"),
    [
        (
            '[runs.commands]\na = ["x", "{p}"]\n',
            "runs.parameters.a",
        ),
        (
            '[runs.commands]\na = ["x"]\n[runs.parameters.b.p]\npattern = "x"\n',
            "runs.parameters.b",
        ),
        (
            '[runs.commands]\na = ["x", "{p}"]\n'
            '[runs.parameters.a.p]\npattern = "(?=x)"\n',
            "runs.parameters.a.p.pattern",
        ),
        (
            '[runs.commands]\na = ["x", "{p}"]\n[runs.parameters.a.p]\nflag = 1\n',
            "runs.parameters.a.p.flag",
        ),
        (
            '[runs.commands]\na = ["x", "{P}"]\n[runs.parameters.a.P]\npattern = "x"\n',
            "runs.parameters.a.P",
        ),
        (
            '[runs.commands]\na = ["x", "{p}"]\n[runs.parameters.a.p]\npattern = 3\n',
            "runs.parameters.a.p.pattern",
        ),
        (
            '[runs.commands]\na = ["x", "{p}"]\n[runs.parameters.a.p]\n'
            'pattern = "x"\ndescription = "a\\u0007"\n',
            "runs.parameters.a.p.description",
        ),
    ],
)
def test_the_project_file_refuses_a_bad_declaration(body: str, where: str) -> None:
    with pytest.raises(ConfigFileError, match=where.replace(".", r"\.")):
        parse(f"version = 1\n{body}")


def test_a_commit_command_may_declare_parameters_too() -> None:
    config = parse(
        "version = 1\n"
        '[runs.commit_commands]\nreview = ["./gate.sh", "{test}"]\n'
        '[runs.commit_refs]\npatterns = ["refs/remotes/origin/main"]\n'
        "[runs.parameters.review.test]\npattern = 'tests/[a-z_]+\\.py'\n"
    )
    assert set(config.run_parameters) == {"review"}


# A run with an argument, end to end.


class Clock:
    def __call__(self) -> datetime:
        return datetime(2026, 10, 31, 9, 0, 0, tzinfo=UTC)


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
    raise AssertionError(f"no engine may start in this test, but {spec.line_id} did")


def stand_in_start(pid: int) -> str | None:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return None
    return AT


@pytest.fixture
def project(tmp_path: Path) -> Generator[Path]:
    (tmp_path / "reports").mkdir()
    yield tmp_path


ECHO = (sys.executable, "-c", "import sys; print(repr(sys.argv[1:]))", "{test}")


def scheduler_on(log: EventLog, root: Path) -> Scheduler:
    found = Scheduler(
        log,
        no_engine,
        hold_wakes=True,
        wall_clock=Clock(),
        root=root,
        report_dir=root / "reports",
        run_start_of=stand_in_start,
        run_occupied=lambda _session: False,
    )
    found.set_runs(
        {"one": ECHO, "plain": ("true",)}, parameters={"one": {"test": TEST_PATH}}
    )
    found.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
    return found


def test_the_scheduler_refuses_a_placeholder_without_a_pattern(project: Path) -> None:
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        with pytest.raises(ParameterRefused, match="no parameter declares"):
            scheduler.set_runs({"one": ECHO})


def test_a_line_runs_one_test_by_its_argument(project: Path) -> None:
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        tools = {tool.name: tool for tool in scheduler.own_tools("alpha")}
        start = tools[START_RUN_TOOL_NAME].handler

        async def call(args: dict[str, object]) -> tuple[str, bool]:
            result = await start(args)
            return str(result["content"][0]["text"]), result.get("is_error") is True

        refused: list[dict[str, object]] = [
            {"command": "one", "previous": "none"},
            {"command": "one", "previous": "none", "arguments": {"test": "-x"}},
            {"command": "one", "previous": "none", "arguments": {"test": "src/a.py"}},
            {"command": "one", "previous": "none", "arguments": ["tests/a.py"]},
            {"command": "plain", "previous": "none", "arguments": {"x": "y"}},
        ]
        for args in refused:
            text, error = asyncio.run(call(args))
            assert error, (args, text)
        assert log.state.runs == {}
        text, error = asyncio.run(
            call(
                {
                    "command": "one",
                    "previous": "none",
                    "arguments": {"test": "tests/test_x.py"},
                }
            )
        )
        assert not error, text
        (run_id,) = log.state.runs
        started = log.state.runs[run_id].started
        assert started.argv == (*ECHO[:3], "tests/test_x.py")
        children = scheduler._run_children  # pyright: ignore[reportPrivateUsage]
        child = children.get(run_id)
        if child is not None:
            child.wait(timeout=WAIT_SECONDS)
        output = (project / started.output_file).read_text()
        assert output == "['tests/test_x.py']\n"
        assert "test matching tests/[a-z0-9_]+\\.py" in scheduler.run_status("alpha")


def test_a_refused_argument_spends_no_run(project: Path) -> None:
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        with pytest.raises(runs.RunRefused, match="does not match"):
            asyncio.run(
                scheduler.start_run("alpha", "one", "none", None, {"test": "x.py"})
            )
        assert log.state.runs == {}


def test_a_start_whose_match_outlasts_the_deadline_is_refused_and_its_child_killed(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 15 re-review. The scheduler matches under the default deadline, so a
    start that passed a longer one would be refused late or not at all."""
    monkeypatch.setattr(runparams, "MATCHER", SLOW_MATCHER)
    started = spawned_children(monkeypatch)
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        with pytest.raises(runs.RunRefused, match="took longer than 2 seconds"):
            asyncio.run(
                scheduler.start_run(
                    "alpha", "one", "none", None, {"test": "tests/a.py"}
                )
            )
        ended = time.monotonic()
        assert log.state.runs == {}
    [(child, began)] = started
    assert ended - began < 10
    assert_killed(child)
