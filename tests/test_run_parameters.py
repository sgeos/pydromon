"""Task of 2026-11-05, item 3. A declared run command may name parameters a line
supplies, each matched against the operator's pattern and rules the operator cannot
widen. ``docs/architecture/RUNS.md`` gives the trust argument."""

import asyncio
import os
import sys
from collections.abc import Generator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import runparams, runs
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
    ],
)
def test_a_pattern_that_is_empty_unportable_or_invalid_is_refused(
    pattern: str, reason: str
) -> None:
    with pytest.raises(ParameterRefused, match=reason.replace("(", r"\(")):
        runparams.check_pattern(pattern)


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
        ("tests/../secret.py", "does not match"),
        ("tests/a.py extra", "does not match"),
        ("src/a.py", "does not match"),
    ],
)
def test_a_value_is_refused_by_the_fixed_rules_or_its_pattern(
    value: object, reason: str
) -> None:
    with pytest.raises(ParameterRefused, match=reason):
        runparams.check_value(TEST_PATH, value)


def test_a_dash_is_refused_even_where_the_pattern_allows_it() -> None:
    wide = RunParameter("any", ".+")
    with pytest.raises(ParameterRefused, match="begin with -"):
        runparams.check_value(wide, "-x")
    assert runparams.check_value(wide, "x-") == "x-"


def test_values_replace_their_placeholders_as_whole_arguments() -> None:
    argv = ("uv", "run", "pytest", "{test}", "x{test}")
    declared = {"test": TEST_PATH}
    # x{test} is not a placeholder, so it is left as the operator wrote it.
    assert runparams.substitute(argv[:4], declared, {"test": "tests/test_x.py"}) == (
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
        runparams.substitute(COMMAND, {"test": TEST_PATH}, given)


def test_a_command_without_parameters_takes_no_arguments() -> None:
    assert runparams.substitute(("make",), {}, None) == ("make",)
    assert runparams.substitute(("make",), {}, {}) == ("make",)
    with pytest.raises(ParameterRefused, match="takes no arguments"):
        runparams.substitute(("make",), {}, {"x": "y"})


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
