"""The display name of each Claude Code session Dromon starts. **Round 19, the
operator's direction.** ``dromon.sessionname`` gives the rule, and
``docs/protocol/PROJECT_CONFIG.md`` and ``docs/protocol/LINE_CONFIG.md`` are
normative."""

from collections.abc import Generator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import ClaudeAgentOptions, SdkMcpTool
from claude_agent_sdk._internal.transport.subprocess_cli import (  # pyright: ignore[reportPrivateImportUsage]
    SubprocessCLITransport,
)

from dromon import __version__
from dromon.codex import CodexLine
from dromon.configview import Overrides, project_entries, project_layers
from dromon.driver import Strategy
from dromon.engine import ClaudeCodeLine, EngineRefused, TurnReport
from dromon.eventlog import EventLog
from dromon.lineconfig import DEFAULT_CONFIG, LineConfig
from dromon.model import RoleDeclared, SeatBound
from dromon.projectconfig import ConfigFileError, parse
from dromon.scheduler import LineEngine, LineSpec, Scheduler, engine_factory
from dromon.sessionname import (
    DEFAULT,
    DEFAULT_TEMPLATE,
    EFFORT_CODES,
    MAX_NAME_CHARS,
    MAX_TEMPLATE_CHARS,
    MODEL_CODES,
    Named,
    SessionNameError,
    Template,
    model_code,
    parse_template,
    session_name,
    variant_of,
    version_code,
)

HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"
LUNA = "gpt-6-luna"


def named(
    line_id: str,
    role: str | None,
    *,
    model: str = SONNET,
    effort: str | None = "high",
    version: str = "0.0.0",
    project: str = "pydromon",
    taken: Sequence[str] = (),
    terms: Sequence[str] = (),
    template: Template = DEFAULT,
) -> Named:
    return session_name(
        template,
        version=version,
        project=project,
        line_id=line_id,
        role=role,
        model=model,
        effort=effort,
        taken=taken,
        terms=terms,
    )


# The operator's examples and each rule.


def test_the_operators_examples_are_given() -> None:
    assert named("runtime", "runtime").name == "PDr00-pydromon-runtime-Sn55hi"
    luna = named("review-luna", "review", model=HAIKU, effort="low")
    assert luna.name == "PDr00-pydromon-review-luna-Ha55lo"


@pytest.mark.parametrize(
    ("version", "code"),
    [("0.0.0", "00"), ("1.2.3", "12"), ("0.4.0.dev1", "04"), ("0.10.1", "010")],
)
def test_the_version_is_the_major_then_the_minor(version: str, code: str) -> None:
    assert version_code(version) == code
    assert named("core", "core", version=version).name == (
        f"PDr{code}-pydromon-core-Sn55hi"
    )


@pytest.mark.parametrize("version", ["", "1", "x.y", "1.x.0"])
def test_a_version_without_a_major_and_minor_gives_no_name(version: str) -> None:
    assert version_code(version) is None
    result = named("core", "core", version=version)
    assert result.name is None and "version" in result.reason


def test_the_installed_version_gives_a_code() -> None:
    assert version_code(__version__) is not None


def test_the_project_is_the_given_name() -> None:
    assert named("core", "core", project="other").name == "PDr00-other-core-Sn55hi"


def test_a_line_that_is_a_seat_of_no_role_is_named_by_its_identifier() -> None:
    assert named("scout", None).name == "PDr00-pydromon-scout-Sn55hi"


@pytest.mark.parametrize(
    ("line_id", "role", "variant"),
    [
        ("review", "review", ""),
        ("review-luna", "review", "luna"),
        ("luna-review", "review", "luna"),
        ("engines-opus", "engines", "opus"),
        ("auditor", "review", "auditor"),
        ("reviewer", "review", "reviewer"),
        ("scout", None, ""),
    ],
)
def test_the_variant_is_what_the_line_adds_to_the_role(
    line_id: str, role: str | None, variant: str
) -> None:
    assert variant_of(line_id, role) == variant
    expected = f"PDr00-pydromon-{role or line_id}-" + (f"{variant}-" if variant else "")
    assert named(line_id, role).name == expected + "Sn55hi"


@pytest.mark.parametrize(
    ("family", "code"), sorted(MODEL_CODES.items()), ids=sorted(MODEL_CODES)
)
def test_each_model_family_has_its_code(family: str, code: str) -> None:
    assert model_code(f"claude-{family}-5-5") == f"{code}55"
    assert model_code(f"claude-{family}-5-1") == f"{code}51"
    assert model_code(f"claude-{family}-6") == f"{code}6"


def test_the_four_codes_are_the_operators() -> None:
    assert dict(MODEL_CODES) == {
        "opus": "Op",
        "haiku": "Ha",
        "fable": "Fa",
        "sonnet": "Sn",
    }


@pytest.mark.parametrize(
    "model", [LUNA, "claude-mythos-5-5", "claude-opus", "opus-5-5", "claude-opus-5-5x"]
)
def test_a_model_with_no_code_gives_no_name(model: str) -> None:
    assert model_code(model) is None
    result = named("core", "core", model=model)
    assert result.name is None and "no session name code" in result.reason


@pytest.mark.parametrize(
    ("effort", "code"), sorted(EFFORT_CODES.items()), ids=sorted(EFFORT_CODES)
)
def test_each_effort_has_its_code(effort: str, code: str) -> None:
    assert (
        named("core", "core", effort=effort).name == f"PDr00-pydromon-core-Sn55{code}"
    )


def test_the_five_effort_codes_are_the_operators() -> None:
    assert dict(EFFORT_CODES) == {
        "low": "lo",
        "medium": "me",
        "high": "hi",
        "xhigh": "xh",
        "max": "mx",
    }


@pytest.mark.parametrize("effort", [None, "extreme"])
def test_an_unknown_effort_is_left_out(effort: str | None) -> None:
    assert named("core", "core", effort=effort).name == "PDr00-pydromon-core-Sn55"


# The collision suffix.


def test_a_name_another_live_session_holds_takes_the_fewest_digits() -> None:
    first = named("review-luna", "review").name
    assert first is not None
    second = named("review-luna", "review", taken=[first]).name
    assert second == first.replace("-luna-", "-luna1-")
    third = named("review-luna", "review", taken=[first, second or ""]).name
    assert third == first.replace("-luna-", "-luna2-")


def test_a_roles_own_line_takes_the_digits_alone_as_its_variant() -> None:
    first = named("core", "core").name
    assert first == "PDr00-pydromon-core-Sn55hi"
    assert named("core", "core", taken=[first]).name == "PDr00-pydromon-core-1-Sn55hi"


def test_the_digits_are_tried_in_order() -> None:
    base = "PDr00-pydromon-review-luna{}-Sn55hi"
    taken = [base.format("")] + [base.format(n) for n in range(1, 10)]
    assert named("review-luna", "review", taken=taken).name == base.format(10)
    gap = [base.format(""), base.format(1), base.format(3)]
    assert named("review-luna", "review", taken=gap).name == base.format(2)


def test_a_taken_name_of_another_shape_needs_no_digits() -> None:
    assert named("core", "core", taken=["PDr00-pydromon-core-Ha55hi"]).name == (
        "PDr00-pydromon-core-Sn55hi"
    )


# The refusals.


def test_a_name_over_the_bound_is_not_given() -> None:
    fits = "p" * (MAX_NAME_CHARS - len("PDr00--core-Sn55hi"))
    assert named("core", "core", project=fits).name is not None
    result = named("core", "core", project=fits + "p")
    assert result.name is None and str(MAX_NAME_CHARS) in result.reason


@pytest.mark.parametrize("project", ["my project", "a;b", "a$(b)", "é", "a/b"])
def test_a_name_that_is_not_one_safe_argument_is_not_given(project: str) -> None:
    result = named("core", "core", project=project)
    assert result.name is None and "one argument" in result.reason


def test_a_name_that_would_begin_with_a_dash_is_not_given() -> None:
    template = parse_template("{project}-{variant-}{model}")
    result = named("core", "core", project="-x", template=template)
    assert result.name is None


def test_a_name_holding_a_disclosure_term_is_not_given_nor_its_term_said() -> None:
    term = "pydromon-runtime"
    result = named("runtime", "runtime", terms=[term.casefold()])
    assert result.name is None
    assert term not in result.reason.casefold()
    assert "pydromon" not in result.reason and "runtime" not in result.reason
    assert named("runtime", "runtime", terms=["unrelated"]).name is not None


def test_a_disclosure_term_is_matched_regardless_of_case() -> None:
    assert named("runtime", "runtime", terms=["pdr00"]).name is None


# The template.


def test_the_default_template_is_the_operators_pattern() -> None:
    assert DEFAULT.text == DEFAULT_TEMPLATE
    assert parse_template(DEFAULT_TEMPLATE) == DEFAULT


def test_a_custom_template_is_rendered() -> None:
    template = parse_template("dr.{role}_{variant-}{model}")
    assert named("review-luna", "review", template=template).name == (
        "dr.review_luna-Sn55"
    )


@pytest.mark.parametrize(
    ("text", "said"),
    [
        ("", "1 to"),
        ("x" * (MAX_TEMPLATE_CHARS + 1), "1 to"),
        ("-{variant-}", "begin with -"),
        ("{role}-{model}", "{variant-}"),
        ("{variant-}{name}", "unknown field {name}"),
        ("{variant-}{role}{role}", "twice"),
        ("{variant-} {role}", "a letter, a digit"),
        ("{variant-};rm", "a letter, a digit"),
        ("{variant-}{", "a letter, a digit"),
        ("{variant-}}", "a letter, a digit"),
        ("{{variant-}}", "a letter, a digit"),
    ],
)
def test_a_template_that_cannot_give_a_safe_name_is_refused(
    text: str, said: str
) -> None:
    with pytest.raises(SessionNameError) as raised:
        parse_template(text)
    assert said in str(raised.value)


# The project file.


def project(daemon: str = "") -> str:
    return f"version = 1\n[daemon]\n{daemon}\n"


def test_the_project_file_gives_the_default_template_when_silent() -> None:
    assert parse(project()).session_name == DEFAULT


def test_the_project_file_may_turn_naming_off() -> None:
    assert parse(project("session_name = false")).session_name is None


def test_the_project_file_may_give_its_own_template() -> None:
    config = parse(project('session_name = "x-{role}-{variant-}{model}"'))
    assert config.session_name == parse_template("x-{role}-{variant-}{model}")


@pytest.mark.parametrize(
    "value", ["true", "3", '"{role}"', '"{variant-} x"', '["{variant-}"]']
)
def test_the_project_file_refuses_a_bad_template_at_load(value: str) -> None:
    with pytest.raises(ConfigFileError) as raised:
        parse(project(f"session_name = {value}"))
    assert "session_name" in str(raised.value)


# The engine.


def factory_built(
    root: Path,
    model: str = HAIKU,
    config: LineConfig | None = None,
    asked: list[tuple[str, str | None]] | None = None,
) -> LineEngine:
    calls = [] if asked is None else asked

    def name(model: str, effort: str | None) -> str | None:
        calls.append((model, effort))
        return "PDr00-test-core-Ha55hi"

    spec = LineSpec(
        "alpha",
        model,
        Strategy.SUBSCRIPTION,
        config or LineConfig(),
        session_name=name,
    )
    return engine_factory(str(root))(spec, [])


def test_a_claude_line_is_asked_its_name_with_its_model_and_effort(
    tmp_path: Path,
) -> None:
    asked: list[tuple[str, str | None]] = []
    engine = factory_built(tmp_path, asked=asked)
    assert isinstance(engine, ClaudeCodeLine)
    assert asked == [(engine.driver.settings.model, engine.driver.settings.effort)]
    assert engine.session_name == "PDr00-test-core-Ha55hi"


def test_a_configured_effort_is_the_one_named(tmp_path: Path) -> None:
    asked: list[tuple[str, str | None]] = []
    engine = factory_built(tmp_path, config=LineConfig(effort="low"), asked=asked)
    assert isinstance(engine, ClaudeCodeLine)
    assert asked == [(HAIKU, "low")]
    assert engine.options().effort == "low"


def test_a_codex_line_is_not_named(tmp_path: Path) -> None:
    asked: list[tuple[str, str | None]] = []
    engine = factory_built(tmp_path, model=LUNA, asked=asked)
    assert isinstance(engine, CodexLine) and asked == []


def test_the_name_reaches_claude_code_as_one_argument(tmp_path: Path) -> None:
    engine = factory_built(tmp_path)
    assert isinstance(engine, ClaudeCodeLine)
    options = engine.options()
    assert options.extra_args == {"name": "PDr00-test-core-Ha55hi"}
    command = command_of(options)
    at = command.index("--name")
    assert command[at + 1] == "PDr00-test-core-Ha55hi"


def test_a_resumed_or_kept_session_is_given_the_name_too(tmp_path: Path) -> None:
    for config in (LineConfig(), LineConfig(keep_session=True)):
        engine = factory_built(tmp_path, config=config)
        assert isinstance(engine, ClaudeCodeLine)
        engine.session_id = "resumed-session"
        options = engine.options()
        assert options.resume == "resumed-session"
        assert options.extra_args == {"name": "PDr00-test-core-Ha55hi"}


def test_a_line_given_no_name_passes_no_name(tmp_path: Path) -> None:
    engine = engine_factory(str(tmp_path))(
        LineSpec("alpha", HAIKU, Strategy.SUBSCRIPTION), []
    )
    assert isinstance(engine, ClaudeCodeLine)
    assert engine.session_name is None
    assert engine.options().extra_args == {}
    assert "--name" not in command_of(engine.options())


def command_of(options: ClaudeAgentOptions) -> list[str]:
    """The command the pinned Agent SDK starts Claude Code with."""
    transport = SubprocessCLITransport(prompt="", options=options)
    return transport._build_command()  # pyright: ignore[reportPrivateUsage]


# The scheduler.


class Fake:
    session_id: str | None = None

    async def run_turn(self, prompt: str) -> TurnReport:
        raise AssertionError("no turn runs here")


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path.resolve() / "pydromon"
        self.root.mkdir()
        self.log = EventLog(tmp_path / "events.sqlite")
        self.names: dict[str, str | None] = {}
        self.scheduler = Scheduler(
            self.log, self.build, tick_seconds=0, default_operator="ana", root=self.root
        )
        self.scheduler.set_session_name(DEFAULT)
        for line_id in ("review", "review-luna", "luna-review", "scout"):
            self.scheduler.create_line(line_id, HAIKU, Strategy.SUBSCRIPTION)
            self.scheduler.configure_line(line_id, replace(DEFAULT_CONFIG))
        for event in (
            RoleDeclared("review", ".", 1800),
            SeatBound("review", "review", True),
            SeatBound("review-luna", "review", True),
            SeatBound("luna-review", "review", True),
        ):
            self.log.append(event)

    def build(self, spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
        self.names[spec.line_id] = spec.session_name(HAIKU, "low")
        return cast(LineEngine, Fake())


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
    raise AssertionError(f"no engine may start here, but {spec.line_id} did")


@pytest.fixture
def rig(tmp_path: Path) -> Generator[Rig]:
    made = Rig(tmp_path)
    yield made
    made.log.close()


def expected(variant: str) -> str:
    code = version_code(__version__)
    middle = f"{variant}-" if variant else ""
    return f"PDr{code}-pydromon-review-{middle}Ha55lo"


def test_the_scheduler_names_each_line_by_its_seat(rig: Rig) -> None:
    rig.scheduler.engine_for("review")
    rig.scheduler.engine_for("review-luna")
    rig.scheduler.engine_for("scout")
    assert rig.names["review"] == expected("")
    assert rig.names["review-luna"] == expected("luna")
    assert rig.names["scout"] == expected("").replace("review", "scout")


def test_two_live_lines_never_share_a_name(rig: Rig) -> None:
    rig.scheduler.engine_for("review-luna")
    rig.scheduler.engine_for("luna-review")
    assert rig.names["review-luna"] == expected("luna")
    assert rig.names["luna-review"] == expected("luna1")


def test_a_dropped_engine_frees_its_name(rig: Rig) -> None:
    rig.scheduler.engine_for("review-luna")
    rig.scheduler._drop_engine("review-luna")  # pyright: ignore[reportPrivateUsage]
    rig.scheduler.engine_for("luna-review")
    assert rig.names["luna-review"] == expected("luna")


def test_a_build_refused_after_naming_frees_its_name(rig: Rig) -> None:
    def refused(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
        rig.build(spec, tools)
        raise EngineRefused("refused after naming")

    rig.scheduler._engine_factory = refused  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(EngineRefused):
        rig.scheduler.engine_for("review-luna")
    assert rig.names["review-luna"] == expected("luna")
    rig.scheduler._engine_factory = rig.build  # pyright: ignore[reportPrivateUsage]
    rig.scheduler.engine_for("luna-review")
    assert rig.names["luna-review"] == expected("luna")


def test_a_rebuilt_engine_keeps_its_name(rig: Rig) -> None:
    rig.scheduler.engine_for("review-luna")
    rig.scheduler._drop_engine("review-luna")  # pyright: ignore[reportPrivateUsage]
    rig.scheduler.engine_for("review-luna")
    assert rig.names["review-luna"] == expected("luna")


def test_naming_turned_off_gives_no_name(rig: Rig) -> None:
    rig.scheduler.set_session_name(None)
    rig.scheduler.engine_for("review")
    assert rig.names["review"] is None


def test_a_disclosed_name_is_warned_of_without_its_term(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    term = "ha55lo"
    rig.scheduler.set_disclosure_terms([term])
    rig.scheduler.engine_for("review-luna")
    assert rig.names["review-luna"] is None
    warned = capsys.readouterr().err
    assert "keeps Claude Code's own session name" in warned
    assert "disclosure list" in warned
    assert term not in warned.casefold() and expected("luna") not in warned


def test_a_scheduler_without_a_project_gives_no_name(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.sqlite")
    scheduler = Scheduler(log, no_engine, tick_seconds=0, default_operator="ana")
    scheduler.set_session_name(DEFAULT)
    try:
        scheduler.create_line("core", HAIKU, Strategy.SUBSCRIPTION)
        assert scheduler.name_session("core", HAIKU, "low") is None
    finally:
        log.close()


@pytest.mark.parametrize(
    ("daemon", "shown"),
    [
        ("", DEFAULT_TEMPLATE),
        ("session_name = false", "off"),
        ('session_name = "x-{variant-}{model}"', "x-{variant-}{model}"),
    ],
)
def test_the_settings_view_shows_the_template(daemon: str, shown: str) -> None:
    config = parse(project(daemon))
    layers = project_layers(config, Overrides(), {}, True)
    entries = project_entries(layers, config, "dromon.toml", True)
    entry = next(e for e in entries if e["key"] == "daemon.session_name")
    assert entry["value"] == shown
