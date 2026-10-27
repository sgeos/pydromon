"""Engine settings: validation, merging, unattended rules, protection, references and
their path from the project file and the client to the engine."""

import asyncio
import json
import os
from pathlib import Path
from typing import cast

import pytest

from dromon import cli, exits
from dromon.driver import Driver, Strategy
from dromon.engine import ClaudeCodeLine, EngineRefused, flag_settings
from dromon.lineconfig import (
    DEFAULT_CONFIG,
    ConfigError,
    LineConfig,
    UnsetVariable,
    config_from_document,
    decode_config,
    encode_config,
    expand,
    parse_servers,
    references,
)
from dromon.projectconfig import ConfigFileError, parse
from dromon.scheduler import LineSpec, engine_factory
from dromon.settings import (
    SettingsConfig,
    SettingsError,
    effective,
    merge,
    parse_settings,
    protected_rules,
    reserved_variable,
    split_settings_file,
)

ROOT = Path("/work/project")
STATE = ROOT / ".dromon"
RUN = Path("/run/dromon")
PROTECTED = (STATE, RUN)

HOOK = {"type": "command", "command": "interline hook --line alpha", "timeout": 10}

FULL: dict[str, object] = {
    "permissions": {
        "allow": ["Bash(git status)", "WebFetch(domain:github.com)", "WebSearch"],
        "ask": ["Bash(git push *)"],
        "deny": ["Read(~/.ssh/**)", "mcp__mail__send"],
    },
    "hooks": {
        "SessionStart": [{"hooks": [HOOK]}],
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "x"}]}
        ],
    },
    "env": {"SCCACHE_DIR": "/cache", "EMPTY": ""},
    "attribution": {"commit": "", "pr": ""},
}


def settings(document: object) -> SettingsConfig:
    return parse_settings(document)


# Validation.


def test_every_supported_key_validates_and_round_trips() -> None:
    parsed = settings(FULL)
    assert parsed.document() == FULL
    assert settings(parsed.document()) == parsed


def test_rules_are_sorted_and_deduplicated_so_equal_documents_compare_equal() -> None:
    first = settings({"permissions": {"deny": ["Bash(rm *)", "Edit", "Bash(rm *)"]}})
    second = settings({"permissions": {"deny": ["Edit", "Bash(rm *)"]}})
    assert first == second
    assert first.document() == {"permissions": {"deny": ["Bash(rm *)", "Edit"]}}


@pytest.mark.parametrize(
    "rule",
    [
        "Bash",
        "Bash(npm run test *)",
        "WebFetch(domain:*.example.com)",
        "Read(//etc/**)",
        "Edit(./src/**)",
        "mcp__github",
        "mcp__github__*",
        "mcp__github__get_issue",
        "Agent(Explore)",
    ],
)
def test_the_rule_shapes_the_harnesses_use_are_accepted(rule: str) -> None:
    assert settings({"permissions": {"allow": [rule]}}).document() == {
        "permissions": {"allow": [rule]}
    }


BAD: list[tuple[object, str, str]] = [
    ([], "settings must be a table", ""),
    ({"theme": "dark"}, "settings.theme is not a known key", "theme"),
    (
        {"permissions": {"defaultMode": "auto"}},
        "is not a known key",
        "permissions.defaultMode",
    ),
    ({"permissions": {"allow": "Bash"}}, "must be an array", "permissions.allow"),
    (
        {"permissions": {"allow": ["bash"]}},
        "is not Tool or Tool(specifier)",
        "permissions.allow",
    ),
    ({"permissions": {"deny": ["Bash(rm"]}}, "is not Tool or", "permissions.deny"),
    ({"permissions": {"deny": ["Bash()"]}}, "is not Tool or", "permissions.deny"),
    ({"permissions": {"deny": ["Bash(a\nb)"]}}, "is not Tool or", "permissions.deny"),
    ({"permissions": {"deny": [""]}}, "must be text", "permissions.deny"),
    ({"hooks": {"OnBoot": []}}, "is not a known hook event", "hooks.OnBoot"),
    ({"hooks": {"Stop": {}}}, "must be an array", "hooks.Stop"),
    ({"hooks": {"Stop": [{"hooks": []}]}}, "must not be empty", "hooks.Stop"),
    ({"hooks": {"Stop": [{}]}}, "must be an array", "hooks.Stop"),
    (
        {"hooks": {"Stop": [{"hooks": [HOOK], "if": "x"}]}},
        "is not a known key",
        "hooks.Stop.if",
    ),
    (
        {"hooks": {"Stop": [{"hooks": [{"type": "http", "url": "x"}]}]}},
        "is not a known key",
        "hooks.Stop.url",
    ),
    (
        {"hooks": {"Stop": [{"hooks": [{"type": "prompt"}]}]}},
        "must be command",
        "hooks.Stop",
    ),
    (
        {"hooks": {"Stop": [{"hooks": [{"type": "command"}]}]}},
        "command must be text",
        "hooks.Stop",
    ),
    (
        {"hooks": {"Stop": [{"hooks": [{**HOOK, "timeout": 0}]}]}},
        "timeout must be",
        "hooks.Stop",
    ),
    (
        {"hooks": {"Stop": [{"hooks": [{**HOOK, "timeout": 86401}]}]}},
        "timeout must be",
        "hooks.Stop",
    ),
    (
        {"hooks": {"Stop": [{"hooks": [{**HOOK, "timeout": True}]}]}},
        "timeout must be",
        "hooks.Stop",
    ),
    (
        {"hooks": {"Stop": [{"hooks": [{**HOOK, "timeout": 1.5}]}]}},
        "timeout must be",
        "hooks.Stop",
    ),
    ({"env": {"1X": "a"}}, "is not a variable name", "env.1X"),
    ({"env": {"PATH": 3}}, "must be text", "env.PATH"),
    ({"env": {"NUL": "a\x00b"}}, "NUL", "env.NUL"),
    ({"attribution": {"commits": ""}}, "is not a known key", "attribution.commits"),
    ({"attribution": {"pr": None}}, "must be text", "attribution.pr"),
    ({"permissions": {"allow": ["Bash"] * 257}}, "more than 256", "permissions.allow"),
]


@pytest.mark.parametrize(("document", "message", "key"), BAD)
def test_a_malformed_document_is_refused_with_the_offending_key(
    document: object, message: str, key: str
) -> None:
    with pytest.raises(SettingsError, match=None) as raised:
        settings(document)
    assert message in str(raised.value)
    assert raised.value.key == key


@pytest.mark.parametrize(
    "name",
    [
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_PROFILE",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CONFIG_DIR",
        "AWS_BEARER_TOKEN_BEDROCK",
    ],
)
def test_env_may_not_change_the_engines_credential_or_provider(name: str) -> None:
    assert reserved_variable(name)
    with pytest.raises(SettingsError, match="credential or provider"):
        settings({"env": {name: "x"}})


def test_ordinary_variables_are_not_reserved() -> None:
    for name in ("PATH", "SCCACHE_DIR", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "GH_TOKEN"):
        assert not reserved_variable(name)


def test_engine_credential_variables_cover_every_variable_the_engine_refuses() -> None:
    from dromon.engine import NON_SUBSCRIPTION_ENVIRONMENT

    assert all(reserved_variable(name) for name in NON_SUBSCRIPTION_ENVIRONMENT)


def test_a_settings_file_splits_into_settings_and_its_sandbox() -> None:
    found, sandbox = split_settings_file(
        {"$schema": "https://json.schemastore.org/x", "env": {"A": "1"}, "sandbox": {}}
    )
    assert found == settings({"env": {"A": "1"}})
    assert sandbox == {}
    assert split_settings_file({"sandbox": {"enabled": True}}) == (
        None,
        {"enabled": True},
    )
    with pytest.raises(SettingsError, match="settings.statusLine is not a known key"):
        split_settings_file({"statusLine": {}})


def test_the_summary_counts_without_showing_values() -> None:
    summary = settings(FULL).summary()
    assert summary == "6 permission rules, 2 hooks, 2 env, attribution"
    assert "/cache" not in summary


# Layers.


def test_layers_unite_rules_append_hooks_and_replace_values() -> None:
    project = settings(
        {
            "permissions": {"deny": ["Read(~/.ssh/**)"]},
            "hooks": {"SessionStart": [{"hooks": [HOOK]}]},
            "env": {"A": "project", "B": "project"},
            "attribution": {"commit": "x", "pr": "y"},
        }
    )
    other = {"type": "command", "command": "other"}
    line = settings(
        {
            "permissions": {"deny": ["Read(~/.aws/**)"], "allow": ["Bash(ls)"]},
            "hooks": {
                "SessionStart": [{"hooks": [other]}, {"hooks": [HOOK]}],
                "Stop": [{"hooks": [other]}],
            },
            "env": {"B": "line"},
            "attribution": {"pr": ""},
        }
    )
    merged = merge([project, None, line])
    assert merged is not None
    assert merged.document() == {
        "permissions": {
            "allow": ["Bash(ls)"],
            "deny": ["Read(~/.aws/**)", "Read(~/.ssh/**)"],
        },
        "hooks": {
            "SessionStart": [{"hooks": [HOOK]}, {"hooks": [other]}],
            "Stop": [{"hooks": [other]}],
        },
        "env": {"A": "project", "B": "line"},
        "attribution": {"commit": "x", "pr": ""},
    }
    assert merge([None, None]) is None
    assert merge([line]) == line


def test_no_layer_removes_another_layers_deny_rule() -> None:
    profile = settings({"permissions": {"deny": ["Bash(curl *)"]}})
    line = settings({"permissions": {"deny": [], "allow": ["Bash(curl *)"]}})
    merged = merge([profile, line])
    assert merged is not None
    permissions = cast(dict[str, list[str]], merged.document()["permissions"])
    assert permissions["deny"] == ["Bash(curl *)"]


# What reaches the engine.


def test_ask_rules_are_denied_and_protected_directories_are_closed() -> None:
    handed = effective(settings(FULL), PROTECTED)
    assert handed is not None
    permissions = cast(dict[str, list[str]], handed["permissions"])
    assert permissions["allow"] == [
        "Bash(git status)",
        "WebFetch(domain:github.com)",
        "WebSearch",
    ]
    assert "ask" not in permissions
    assert "Bash(git push *)" in permissions["deny"]
    assert {"Read(~/.ssh/**)", "mcp__mail__send"} <= set(permissions["deny"])
    assert set(protected_rules(PROTECTED)) <= set(permissions["deny"])
    assert handed["env"] == FULL["env"]
    assert handed["hooks"] == FULL["hooks"]


def test_protected_rules_name_each_directory_as_an_absolute_rule_path() -> None:
    rules = protected_rules(PROTECTED)
    for directory in PROTECTED:
        for tool in ("Read", "Edit"):
            assert f"{tool}(/{directory}/**)" in rules
    assert all(rule.split("(", 1)[1].startswith("//") for rule in rules)


def test_a_symbolic_link_does_not_disguise_a_protected_directory(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    rules = protected_rules((link,))
    assert f"Read(/{link}/**)" in rules
    assert f"Read(/{os.path.realpath(link)}/**)" in rules


def test_a_relative_protected_directory_is_refused() -> None:
    with pytest.raises(SettingsError, match="not absolute"):
        protected_rules((Path("relative"),))


def test_with_nothing_set_only_the_protection_is_handed_over() -> None:
    assert effective(None, ()) is None
    assert effective(None, PROTECTED) == {
        "permissions": {"deny": protected_rules(PROTECTED)}
    }
    assert effective(settings({"env": {"A": "1"}}), ()) == {"env": {"A": "1"}}


# Line configuration and references.


def test_a_line_configuration_carries_settings_and_effort_through_the_log() -> None:
    config = config_from_document({"settings": FULL, "effort": "high"})
    assert config.settings == settings(FULL)
    assert config.effort == "high"
    assert decode_config(encode_config(config)) == config
    stored = json.loads(encode_config(DEFAULT_CONFIG))
    assert "settings" not in stored and "effort" not in stored
    with pytest.raises(ConfigError, match="settings.thme is not a known key"):
        config_from_document({"settings": {"thme": 1}})
    with pytest.raises(ConfigError, match="effort must be one of"):
        config_from_document({"effort": "extreme"})


def test_references_are_found_and_expanded_with_defaults() -> None:
    text = "Bearer ${TOKEN} on ${HOST:-localhost} ${EMPTY:-}"
    assert references(text) == ("TOKEN", "HOST", "EMPTY")
    assert expand(text, {"TOKEN": "t", "HOST": "h"}) == "Bearer t on h "
    assert expand(text, {"TOKEN": "t"}) == "Bearer t on localhost "
    assert references("no references, $HOME stays") == ()
    with pytest.raises(UnsetVariable, match="does not set TOKEN"):
        expand(text, {})


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("${", "malformed variable reference"),
        ("${1X}", "malformed variable reference"),
        ("${A B}", "malformed variable reference"),
        ("ok ${A} then ${", "malformed variable reference"),
        ("${ANTHROPIC_API_KEY}", "may not refer to the variable ANTHROPIC_API_KEY"),
        ("${CLAUDE_CODE_OAUTH_TOKEN:-x}", "may not refer to the variable"),
    ],
)
def test_a_bad_reference_is_refused_when_the_server_is_configured(
    text: str, message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_servers(
            {"s": {"type": "http", "url": "https://x", "headers": {"A": text}}}
        )


def test_a_server_stores_references_and_expands_them_for_the_engine() -> None:
    servers = parse_servers(
        {
            "mail": {
                "type": "http",
                "url": "https://${MAIL_HOST:-mail.example}/mcp",
                "headers": {"Authorization": "Bearer ${MAIL_TOKEN}"},
            },
            "tool": {
                "command": "${HOME}/bin/tool",
                "args": ["--key", "${KEY}"],
                "env": {"K": "${KEY}"},
            },
        }
    )
    config = LineConfig(mcp_servers=servers)
    assert "${MAIL_TOKEN}" in encode_config(config)
    environment = {"MAIL_TOKEN": "secret", "HOME": "/home/a", "KEY": "k"}
    mail, tool = (server.sdk_config(environment) for _, server in servers)
    assert mail == {
        "type": "http",
        "url": "https://mail.example/mcp",
        "headers": {"Authorization": "Bearer secret"},
    }
    assert tool == {
        "type": "stdio",
        "command": "/home/a/bin/tool",
        "args": ["--key", "k"],
        "env": {"K": "k"},
    }


def test_an_expanded_url_must_still_meet_the_transport_rule() -> None:
    servers = parse_servers({"s": {"type": "http", "url": "${URL}"}})
    with pytest.raises(ConfigError, match="must use https"):
        servers[0][1].sdk_config({"URL": "http://evil.example"})
    assert (
        servers[0][1].sdk_config({"URL": "https://ok.example"})["url"]
        == "https://ok.example"
    )


# Engine and factory.


def driver() -> Driver:
    return Driver.for_model("claude-haiku-5-5", Strategy.SUBSCRIPTION)


def test_the_engine_hands_settings_to_claude_code_as_json() -> None:
    handed = effective(settings(FULL), PROTECTED)
    target = ClaudeCodeLine(driver(), ROOT, settings=handed)
    text = target.options().settings
    assert text is not None
    assert json.loads(text) == flag_settings(handed)
    assert ClaudeCodeLine(driver(), ROOT).options().settings == json.dumps(
        flag_settings(None)
    )


def test_a_line_effort_replaces_the_drivers() -> None:
    declared = driver().settings.effort
    assert ClaudeCodeLine(driver(), ROOT).options().effort == declared
    chosen = "low" if declared != "low" else "high"
    config = LineConfig(effort=chosen)
    assert ClaudeCodeLine(driver(), ROOT, config=config).options().effort == chosen


def test_the_engine_expands_references_from_its_environment() -> None:
    servers = parse_servers(
        {"s": {"type": "http", "url": "https://x", "headers": {"A": "${T}"}}}
    )
    config = LineConfig(mcp_servers=servers)
    target = ClaudeCodeLine(driver(), ROOT, config=config, environment={"T": "v"})
    assert target.options().mcp_servers == {
        "s": {"type": "http", "url": "https://x", "headers": {"A": "v"}}
    }
    unset = ClaudeCodeLine(driver(), ROOT, config=config, environment={})
    with pytest.raises(EngineRefused, match="the MCP server s cannot start"):
        unset.options()


def test_the_factory_merges_project_settings_under_each_line() -> None:
    project = settings({"permissions": {"deny": ["Bash(curl *)"]}, "env": {"A": "p"}})
    own = settings({"permissions": {"ask": ["Bash(git push *)"]}, "env": {"A": "l"}})
    build = engine_factory(str(ROOT), project_settings=project, protected=PROTECTED)
    spec = LineSpec(
        "alpha", "claude-haiku-5-5", Strategy.SUBSCRIPTION, LineConfig(settings=own)
    )
    engine = build(spec, [])
    assert isinstance(engine, ClaudeCodeLine)
    assert engine.settings == effective(merge([project, own]), PROTECTED)
    handed = engine.settings or {}
    deny = cast(dict[str, list[str]], handed["permissions"])["deny"]
    assert {"Bash(curl *)", "Bash(git push *)"} <= set(deny)
    assert handed["env"] == {"A": "l"}
    bare = build(LineSpec("beta", "claude-haiku-5-5", Strategy.SUBSCRIPTION), [])
    assert isinstance(bare, ClaudeCodeLine)
    assert bare.settings == effective(project, PROTECTED)


# The project file.


PROJECT = """\
version = 1

[settings.permissions]
deny = ["Read(~/.ssh/**)"]

[settings.env]
SCCACHE_DIR = "/cache"

[profiles.worker]
model = "claude-haiku-5-5"
builtin_tools = ["Bash"]
effort = "high"

[profiles.worker.settings.permissions]
allow = ["Bash(cargo *)"]

[[profiles.worker.settings.hooks.SessionStart]]
hooks = [{ type = "command", command = "interline hook", timeout = 10 }]

[lines.alpha]
profile = "worker"

[lines.alpha.settings.permissions]
ask = ["Bash(git push *)"]

[lines.alpha.settings.attribution]
commit = ""

[lines.beta]
profile = "worker"
effort = "low"

[lines.gamma]
model = "claude-haiku-5-5"
"""


def test_the_project_file_holds_project_profile_and_line_settings() -> None:
    config = parse(PROJECT)
    assert config.settings == settings(
        {"permissions": {"deny": ["Read(~/.ssh/**)"]}, "env": {"SCCACHE_DIR": "/cache"}}
    )
    alpha = config.lines["alpha"].config
    assert alpha.settings == settings(
        {
            "permissions": {"allow": ["Bash(cargo *)"], "ask": ["Bash(git push *)"]},
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "interline hook",
                                "timeout": 10,
                            }
                        ]
                    }
                ]
            },
            "attribution": {"commit": ""},
        }
    )
    assert alpha.effort == "high"
    assert config.lines["beta"].config.effort == "low"
    assert config.lines["gamma"].config.settings is None
    assert config.lines["gamma"].config.effort is None


@pytest.mark.parametrize(
    ("text", "message", "line"),
    [
        (
            "version = 1\n[settings]\ntheme = 'dark'\n",
            "settings: settings.theme is not a known key",
            2,
        ),
        (
            "version = 1\n[lines.alpha]\nmodel = 'claude-haiku-5-5'\n"
            "[lines.alpha.settings.env]\nANTHROPIC_API_KEY = 'x'\n",
            "lines.alpha.settings: settings.env may not set ANTHROPIC_API_KEY",
            4,
        ),
        (
            "version = 1\n[profiles.p]\n[profiles.p.settings.permissions]\n"
            "allow = 'Bash'\n"
            "[lines.alpha]\nprofile = 'p'\nmodel = 'claude-haiku-5-5'\n",
            "profiles.p.settings: settings.permissions.allow must be an array",
            3,
        ),
        (
            "version = 1\n[lines.alpha]\nmodel = 'claude-haiku-5-5'\neffort = 'huge'\n",
            "lines.alpha: effort must be one of",
            2,
        ),
    ],
)
def test_bad_settings_in_the_file_are_refused_with_their_location(
    text: str, message: str, line: int
) -> None:
    with pytest.raises(ConfigFileError) as raised:
        parse(text)
    assert message in str(raised.value)
    assert raised.value.line == line


# The client.


def test_the_client_reads_a_claude_code_settings_file(tmp_path: Path) -> None:
    file = tmp_path / "settings.local.json"
    file.write_text(json.dumps({**FULL, "sandbox": {"enabled": True}}))
    arguments = cli.build_parser().parse_args(
        ["configure", "alpha", "--settings-config", str(file), "--effort", "max"]
    )
    document = cli._config_document(arguments)  # pyright: ignore[reportPrivateUsage]
    assert document is not None
    assert document["settings"] == settings(FULL).document()
    assert document["sandbox"] == {"enabled": True}
    assert document["effort"] == "max"
    assert config_from_document(document).effort == "max"


def test_the_client_refuses_a_sandbox_given_twice(tmp_path: Path) -> None:
    file = tmp_path / "settings.json"
    file.write_text(json.dumps({"sandbox": {"enabled": True}}))
    arguments = cli.build_parser().parse_args(
        [
            "configure",
            "alpha",
            "--settings-config",
            str(file),
            "--sandbox-config",
            str(file),
        ]
    )
    with pytest.raises(ValueError, match="so does --sandbox-config"):
        cli._config_document(arguments)  # pyright: ignore[reportPrivateUsage]


def test_the_client_refuses_an_unsupported_settings_key(
    tmp_path: Path, short_runtime: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    file = tmp_path / "settings.json"
    file.write_text(json.dumps({"theme": "dark"}))
    argv = [
        "--project",
        str(tmp_path),
        "--runtime-dir",
        str(short_runtime),
        "configure",
        "alpha",
        "--settings-config",
        str(file),
    ]
    assert cli.main(argv) == exits.FAILURE
    assert "settings.theme is not a known key" in capsys.readouterr().err


def test_dromon_config_reports_each_lines_settings_without_values(
    tmp_path: Path, short_runtime: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / ".dromon"
    state.mkdir(mode=0o700)
    (state / "dromon.toml").write_text(PROJECT)
    argv = ["--project", str(tmp_path), "--runtime-dir", str(short_runtime), "config"]
    assert cli.main(argv) == exits.OK
    out = capsys.readouterr().out
    assert "project: no sandbox settings, 1 permission rules, 0 hooks, 1 env" in out
    assert "3 permission rules, 1 hooks, 1 env, attribution, effort high" in out
    assert "effort low" in out
    assert "/cache" not in out


# Real turns, on the operator's subscription.


def _live() -> None:
    if os.environ.get("DROMON_LIVE_TESTS") != "1":
        pytest.skip("set DROMON_LIVE_TESTS=1 to call the engine on the subscription")


@pytest.mark.live
def test_a_live_line_runs_its_hook_with_its_env(tmp_path: Path) -> None:
    _live()
    project = tmp_path.resolve()
    marker = project / "hook.txt"
    hook = {
        "type": "command",
        "command": f"printf '%s' \"$DROMON_PROBE\" > {marker};"
        " echo 'The code word is PELICAN.'",
        "timeout": 10,
    }
    handed = effective(
        settings(
            {
                "env": {"DROMON_PROBE": "heron"},
                "hooks": {"UserPromptSubmit": [{"hooks": [hook]}]},
            }
        ),
        (project / ".dromon",),
    )
    target = ClaudeCodeLine(driver(), project, settings=handed)
    report = asyncio.run(
        target.run_turn(
            "Use no tool. Reply with the code word you were given and nothing else."
        )
    )
    assert marker.read_text() == "heron", "the hook did not run with the line's env"
    assert "PELICAN" in report.text


@pytest.mark.live
def test_a_live_ask_rule_denies_the_call_before_the_gate(tmp_path: Path) -> None:
    _live()
    project = tmp_path.resolve()
    created = project / "probe.txt"
    allowed = project / "allowed.txt"
    handed = effective(
        settings({"permissions": {"ask": ["Bash(touch *probe.txt)"]}}),
        (project / ".dromon",),
    )
    target = ClaudeCodeLine(
        driver(), project, config=LineConfig(builtin_tools=("Bash",)), settings=handed
    )
    prompt = (
        "Run exactly these two Bash commands, each as its own Bash call, and then"
        f" reply with the word done.\n1. touch {allowed}\n2. touch {created}\n"
        "Do not retry a command that fails."
    )
    report = asyncio.run(target.run_turn(prompt))
    assert report.tool_uses.count("Bash") >= 2, "both commands must be attempted"
    assert allowed.exists(), "the positive control did not run"
    assert not created.exists(), "the ask rule did not deny the call"
