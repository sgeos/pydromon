"""Settings in the client core: commands, typed values, completion and the panel."""

from dataclasses import replace

import pytest

from dromon.ui import editor as edit
from dromon.ui.commands import (
    ClearSession,
    Choices,
    CreateLine,
    Invalid,
    ReloadProject,
    Set,
    SetSetting,
    ShowSettings,
    parse_input,
    suggest,
)
from dromon.ui.data import SettingEntry, settings_of, shown
from dromon.ui.keys import Char, Named
from dromon.ui.model import (
    Done,
    Effect,
    Event,
    FetchSettings,
    Model,
    Overlay,
    Pressed,
    Request,
    SettingsLoaded,
)
from dromon.ui.render import frame, text_of
from dromon.ui.runtime import notice_of, request_of
from dromon.ui.settings import Problem, find, prefill, rows, value_of
from dromon.ui.settings import request_of as settings_request
from dromon.ui.update import initial, update
from dromon.protocol import Operation

RESULT: dict[str, object] = {
    "line_id": "alpha",
    "models": ["claude-opus-5-5", "claude-haiku-5-5", "gpt-6-luna"],
    "line": [
        {
            "key": "model",
            "kind": "choice",
            "summary": "the model",
            "choices": ["claude-opus-5-5", "claude-haiku-5-5"],
            "value": "claude-haiku-5-5",
            "source": "created",
            "editable": True,
        },
        {
            "key": "strategy",
            "kind": "info",
            "summary": "fixed at creation",
            "choices": [],
            "value": "subscription",
            "source": "",
            "editable": False,
        },
        {
            "key": "effort",
            "kind": "choice",
            "summary": "the reasoning effort",
            "choices": ["low", "medium", "high"],
            "value": "medium",
            "source": "driver",
            "editable": True,
        },
        {
            "key": "builtin_tools",
            "kind": "list",
            "summary": "built-in tools",
            "choices": [],
            "value": ["Read", "Grep"],
            "source": "line configuration",
            "editable": True,
        },
        {
            "key": "claude_code_prompt",
            "kind": "bool",
            "summary": "the system prompt",
            "choices": [],
            "value": False,
            "source": "line configuration",
            "editable": True,
        },
        {
            "key": "limits.max_autonomous_turns",
            "kind": "int",
            "summary": "turns",
            "choices": [],
            "value": 12,
            "source": "project, default",
            "editable": True,
        },
    ],
    "project": [
        {
            "key": "limits.max_cost_usd",
            "kind": "decimal",
            "summary": "cost",
            "choices": [],
            "value": "2",
            "source": "default",
            "editable": True,
        },
        {
            "key": "daemon.operator",
            "kind": "text",
            "summary": "who hears",
            "choices": [],
            "value": "operator",
            "source": "default",
            "editable": True,
        },
    ],
}
SETTINGS = settings_of(RESULT)


def entry(key: str) -> SettingEntry:
    found = find(SETTINGS, key)
    assert found is not None
    return found[1]


def test_a_settings_result_reads_into_typed_values() -> None:
    assert SETTINGS.line_id == "alpha" and "gpt-6-luna" in SETTINGS.models
    assert entry("builtin_tools").shown == "Read, Grep"
    assert entry("claude_code_prompt").shown == "off"
    assert not entry("strategy").editable
    assert entry("project.daemon.operator").kind == "text"
    assert find(SETTINGS, "project.effort") is None
    assert settings_of({"line": [{"kind": "int"}, 4]}).line == ()
    samples: list[object] = [[], None, True, 3]
    assert [shown(v) for v in samples] == ["none", "", "on", "3"]


@pytest.mark.parametrize(
    ("text", "command"),
    [
        ("/config", ShowSettings()),
        ("/model", ShowSettings("model")),
        ("/effort", ShowSettings("effort")),
        ("/model claude-opus-5-5", Set("model", "claude-opus-5-5")),
        ("/effort high", Set("effort", "high")),
        ("/set builtin_tools Read, Grep", Set("builtin_tools", "Read, Grep")),
        ("/set builtin_tools", Set("builtin_tools", "")),
        ("/set project.limits.max_cost_usd 3", Set("project.limits.max_cost_usd", "3")),
        ("/unset effort", Set("effort", None)),
        ("/clear", ClearSession()),
        ("/reload", ReloadProject()),
        ("/create beta gpt-6-luna", CreateLine("beta", "gpt-6-luna", "subscription")),
        ("/create beta gpt-6-luna api", CreateLine("beta", "gpt-6-luna", "api")),
        ("/set", Invalid("usage: /set KEY VALUE")),
        ("/unset", Invalid("usage: /unset KEY")),
        ("/unset a b", Invalid("usage: /unset KEY")),
        ("/model a b", Invalid("usage: /model [MODEL]")),
        ("/create beta", Invalid("usage: /create LINE MODEL [STRATEGY]")),
        ("/clear now", Invalid("usage: /clear")),
    ],
)
def test_settings_commands_parse(text: str, command: object) -> None:
    assert parse_input(text) == command


@pytest.mark.parametrize(
    ("key", "text", "value"),
    [
        ("claude_code_prompt", "on", True),
        ("claude_code_prompt", "No", False),
        ("limits.max_autonomous_turns", "20", 20),
        ("project.limits.max_cost_usd", "2.5", "2.5"),
        ("builtin_tools", "Read, Grep Bash", ("Read", "Grep", "Bash")),
        ("builtin_tools", "none", ()),
        ("builtin_tools", "", ()),
        ("effort", "high", "high"),
        ("project.daemon.operator", " ben ", "ben"),
    ],
)
def test_typed_text_becomes_a_value_of_the_settings_kind(
    key: str, text: str, value: object
) -> None:
    assert value_of(entry(key), text) == value


@pytest.mark.parametrize(
    ("key", "text"),
    [
        ("claude_code_prompt", "maybe"),
        ("limits.max_autonomous_turns", "-3"),
        ("limits.max_autonomous_turns", "12345678"),
        ("project.limits.max_cost_usd", "1e3"),
        ("effort", "extreme"),
        ("project.daemon.operator", ""),
        ("strategy", "api"),
    ],
)
def test_text_that_is_no_value_is_a_problem(key: str, text: str) -> None:
    assert isinstance(value_of(entry(key), text), Problem)


def test_a_set_checks_against_the_settings_before_any_request() -> None:
    assert settings_request(SETTINGS, Set("effort", "low")) == SetSetting(
        False, "effort", "low"
    )
    assert settings_request(SETTINGS, Set("project.limits.max_cost_usd", None)) == (
        SetSetting(True, "limits.max_cost_usd", None)
    )
    assert "no setting" in str(settings_request(SETTINGS, Set("colour", "red")))
    assert "cannot be changed" in str(settings_request(SETTINGS, Set("strategy", "x")))
    assert "takes one of" in str(settings_request(SETTINGS, Set("effort", "x")))
    assert "not arrived" in str(settings_request(None, Set("effort", "low")))


def test_rows_and_prefill_follow_the_panel() -> None:
    found = rows(SETTINGS)
    assert [key for _, key in [(p, e.key) for p, e in found]][:2] == [
        "model",
        "strategy",
    ]
    assert found[-1][0] is True
    assert prefill(False, entry("builtin_tools")) == "/set builtin_tools Read,Grep "
    assert prefill(True, entry("project.limits.max_cost_usd")) == (
        "/set project.limits.max_cost_usd 2 "
    )


def model_with_settings(**changes: object) -> Model:
    model = replace(initial("alpha", "ana", 1.0), width=80, height=24)
    model, _ = update(model, SettingsLoaded("alpha", SETTINGS))
    return replace(model, **changes)  # pyright: ignore[reportArgumentType]


def run(model: Model, *events: Event) -> tuple[Model, list[Effect]]:
    effects: list[Effect] = []
    for event in events:
        model, produced = update(model, event)
        effects.extend(produced)
    return model, effects


def keys(text: str) -> list[Event]:
    return [Pressed(Char(character)) for character in text]


def test_settings_load_only_for_the_attached_line() -> None:
    model = replace(initial("alpha", "ana", 1.0))
    assert update(model, SettingsLoaded("beta", SETTINGS)) == (model, ())
    assert model_with_settings().settings == SETTINGS


def test_a_set_command_becomes_a_request_and_a_bad_one_a_notice() -> None:
    model, effects = run(
        model_with_settings(), *keys("/effort high"), Pressed(Named.ESCAPE)
    )
    model, effects = run(model, Pressed(Named.ENTER))
    assert effects == [Request(SetSetting(False, "effort", "high"), "alpha", "ana")]
    model, effects = run(model, *keys("/effort extreme"), Pressed(Named.ESCAPE))
    model, effects = run(model, Pressed(Named.ENTER))
    assert effects == [] and model.alarm and "takes one of" in model.notice
    bare = replace(initial("alpha", "ana", 1.0))
    bare, effects = run(
        bare, *keys("/effort low"), Pressed(Named.ESCAPE), Pressed(Named.ENTER)
    )
    assert effects == [FetchSettings("alpha")] and bare.alarm


def test_a_completed_request_fetches_the_settings_again() -> None:
    _, effects = run(model_with_settings(), Done("set effort"))
    assert FetchSettings("alpha") in effects


def test_config_opens_the_panel_and_enter_prefills_the_edit() -> None:
    model, effects = run(model_with_settings(), *keys("/model"), Pressed(Named.ESCAPE))
    model, effects = run(model, Pressed(Named.ENTER))
    assert model.overlay is Overlay.SETTINGS and effects == [FetchSettings("alpha")]
    assert rows(SETTINGS)[model.setting_row][1].key == "model"
    model, _ = run(model, Pressed(Named.DOWN))
    model, _ = run(model, Pressed(Named.ENTER))
    assert model.overlay is Overlay.SETTINGS and "cannot be changed" in model.notice
    model, _ = run(model, Pressed(Named.DOWN), Pressed(Named.ENTER))
    assert model.overlay is Overlay.NONE
    assert model.editor == edit.Editor("/set effort medium ", 19)
    model, _ = run(model, Pressed(Named.CTRL_U), *keys("/config"))
    model, _ = run(model, Pressed(Named.ESCAPE), Pressed(Named.ENTER))
    model, _ = run(model, *[Pressed(Named.PAGE_DOWN)] * 3, Pressed(Named.DOWN))
    assert model.setting_row == len(rows(SETTINGS)) - 1
    model, _ = run(
        model, Pressed(Named.PAGE_UP), Pressed(Named.UP), Pressed(Named.ESCAPE)
    )
    assert model.overlay is Overlay.NONE and model.setting_row == 0


def test_the_panel_draws_both_scopes_and_keeps_the_row_in_view() -> None:
    model = model_with_settings(overlay=Overlay.SETTINGS, setting_row=7)
    text = "\n".join(text_of(row) for row in frame(model).rows)
    assert "Line alpha" in text and "Project" in text
    assert "builtin_tools" in text and "Read, Grep  [line configuration]" in text
    assert "project.daemon.operator" in text
    short = model_with_settings(overlay=Overlay.SETTINGS, setting_row=7, height=8)
    drawn = frame(short)
    assert len(drawn.rows) == 8
    assert any("daemon.operator" in text_of(row) for row in drawn.rows)
    waiting = replace(initial("alpha", "ana", 1.0), overlay=Overlay.SETTINGS)
    assert "waiting" in "\n".join(text_of(row) for row in frame(waiting).rows)


def test_settings_keys_values_and_models_complete() -> None:
    model, _ = run(model_with_settings(), *keys("/set cla"))
    assert [s.text for s in model.suggestions] == ["/set claude_code_prompt "]
    model, _ = run(model_with_settings(), *keys("/set claude_code_prompt o"))
    assert [s.label for s in model.suggestions] == ["on", "off"]
    model, _ = run(model_with_settings(), *keys("/set project.l"))
    assert [s.text for s in model.suggestions] == ["/set project.limits.max_cost_usd "]
    model, _ = run(model_with_settings(), *keys("/model claude-o"))
    assert [s.text for s in model.suggestions] == ["/model claude-opus-5-5"]
    model, _ = run(model_with_settings(), *keys("/create beta gpt"))
    assert [s.text for s in model.suggestions] == ["/create beta gpt-6-luna "]
    model, _ = run(model_with_settings(), *keys("/create beta gpt-6-luna a"))
    assert [s.text for s in model.suggestions] == ["/create beta gpt-6-luna api"]
    assert suggest("/create beta gpt-6-luna api x", Choices()) == ()
    model, _ = run(model_with_settings(), *keys("/effort h"))
    assert [s.label for s in model.suggestions] == ["high"]


@pytest.mark.parametrize(
    ("command", "operation", "args"),
    [
        (
            SetSetting(False, "builtin_tools", ("Read",)),
            Operation.SET_SETTING,
            {
                "scope": "line",
                "key": "builtin_tools",
                "line_id": "alpha",
                "value": ["Read"],
            },
        ),
        (
            SetSetting(True, "wake.every_ticks", None),
            Operation.SET_SETTING,
            {"scope": "project", "key": "wake.every_ticks"},
        ),
        (ClearSession(), Operation.CLEAR_SESSION, {"line_id": "alpha"}),
        (
            CreateLine("beta", "gpt-6-luna", "subscription"),
            Operation.CREATE_LINE,
            {"line_id": "beta", "model": "gpt-6-luna", "strategy": "subscription"},
        ),
        (ReloadProject(), Operation.RELOAD_PROJECT, {}),
    ],
)
def test_settings_requests_map_onto_operations(
    command: SetSetting | ClearSession | CreateLine | ReloadProject,
    operation: Operation,
    args: dict[str, object],
) -> None:
    found, sent, _ = request_of(command, "alpha", "ana")
    assert (found, sent) == (operation, args)


def test_setting_notices_say_where_the_value_comes_from() -> None:
    command = SetSetting(True, "limits.max_cost_usd", "9")
    shadowed = {"value": "3", "source": "command line", "shadowed": True}
    assert notice_of(command, "alpha", "", shadowed) == (
        "project.limits.max_cost_usd is recorded, but the command line value 3 applies"
    )
    landed = {"value": "9", "source": "override", "shadowed": False}
    assert "is now 9" in notice_of(command, "alpha", "", landed)
    unset = SetSetting(False, "effort", None)
    back = {"value": "medium", "source": "driver", "shadowed": False}
    assert (
        notice_of(unset, "alpha", "", back)
        == "effort returns to medium from the driver"
    )
    assert notice_of(ClearSession(), "alpha", "fresh", {}) == "fresh"
