"""Sandbox documents: validation, trust, merging, unattended defaults and protection."""

import asyncio
import json
from pathlib import Path
from typing import cast

import pytest
from claude_agent_sdk import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from dromon import cli, exits
from dromon.daemon import check_sandboxes, protected_dirs
from dromon.driver import Driver, Strategy
from dromon.engine import ClaudeCodeLine, permission_gate
from dromon.lineconfig import (
    DEFAULT_CONFIG,
    ConfigError,
    LineConfig,
    config_from_document,
    decode_config,
    encode_config,
)
from dromon.paths import project_paths
from dromon.projectconfig import ConfigFileError, parse
from dromon.sandbox import (
    SandboxConfig,
    SandboxError,
    blocks_unsandboxed,
    effective,
    merge,
    parse_sandbox,
    sandbox_from_settings,
)
from dromon.scheduler import LineSpec, engine_factory

ROOT = Path("/work/project")
STATE = ROOT / ".dromon"
RUN = Path("/tmp/dr-run")  # noqa: S108
PROTECTED = (STATE, RUN)

FULL: dict[str, object] = {
    "enabled": True,
    "failIfUnavailable": False,
    "autoAllowBashIfSandboxed": True,
    "excludedCommands": ["docker", "git"],
    "enableWeakerNestedSandbox": False,
    "enableWeakerNetworkIsolation": False,
    "ignoreViolations": {"*": ["/etc/hosts"]},
    "filesystem": {
        "allowWrite": ["~/.kube", "/tmp/build"],  # noqa: S108
        "denyWrite": ["./secret"],
        "denyRead": ["~/.ssh"],
        "allowRead": ["."],
    },
    "network": {
        "allowedDomains": ["github.com", "*.npmjs.org"],
        "deniedDomains": ["evil.example"],
        "strictAllowlist": False,
        "allowUnixSockets": ["/var/run/docker.sock"],
        "allowLocalBinding": True,
        "allowMachLookup": ["com.apple.coresimulator.*"],
        "httpProxyPort": 8080,
        "socksProxyPort": 1080,
    },
    "credentials": {
        "files": [{"path": "~/.aws/credentials", "mode": "deny"}],
        "envVars": [{"name": "GITHUB_TOKEN", "mode": "deny"}],
    },
}


def sandbox(document: object, *, operator: bool = False) -> SandboxConfig:
    return parse_sandbox(document, operator=operator)


# Validation.


def test_every_supported_key_validates_and_round_trips() -> None:
    parsed = sandbox(FULL)
    assert parsed.document()["network"] == {
        **cast(dict[str, object], FULL["network"]),
        "allowedDomains": ["*.npmjs.org", "github.com"],
    }
    assert sandbox(parsed.document()) == parsed
    assert parsed.enabled


def test_arrays_are_sorted_and_deduplicated_so_equal_documents_compare_equal() -> None:
    one = sandbox({"excludedCommands": ["git", "docker", "git"]})
    other = sandbox({"excludedCommands": ["docker", "git"]})
    assert one == other
    assert one.document() == {"excludedCommands": ["docker", "git"]}


BAD: list[tuple[str, object, str]] = [
    ("not a table", ["enabled"], "sandbox must be a table"),
    ("unknown key", {"enable": True}, "sandbox.enable is not a known key"),
    ("unknown nested key", {"network": {"domains": []}}, "sandbox.network.domains"),
    ("boolean as text", {"enabled": "yes"}, "sandbox.enabled must be a boolean"),
    ("integer as boolean", {"enabled": 1}, "must be a boolean"),
    ("bad domain", {"network": {"allowedDomains": ["a b"]}}, "not a valid domain"),
    ("scheme in domain", {"network": {"allowedDomains": ["https://x.io"]}}, "domain"),
    ("port zero", {"network": {"httpProxyPort": 0}}, "port number"),
    ("port as text", {"network": {"socksProxyPort": "80"}}, "port number"),
    ("empty path", {"filesystem": {"denyRead": [""]}}, "text of 1 to"),
    ("nul in path", {"filesystem": {"denyRead": ["a\x00b"]}}, "NUL"),
    ("path list as text", {"filesystem": {"denyRead": "~/.ssh"}}, "must be an array"),
    ("too many paths", {"filesystem": {"denyRead": ["/x"] * 257}}, "more than 256"),
    (
        "masked credential",
        {"credentials": {"files": [{"path": "~/.npmrc", "mode": "mask"}]}},
        "Masking is not yet supported",
    ),
    (
        "credential with extra field",
        {
            "credentials": {
                "envVars": [{"name": "X", "mode": "deny", "injectHosts": []}]
            }
        },
        "exactly name and mode",
    ),
    ("violations as list", {"ignoreViolations": ["*"]}, "table of command pattern"),
    ("managed key", {"bwrapPath": "/usr/bin/bwrap"}, "only from managed settings"),
    (
        "managed nested key",
        {"network": {"allowManagedDomainsOnly": True}},
        "applies only to managed settings",
    ),
    ("custom binary", {"ripgrep": {"command": "rg"}}, "names a binary to run"),
    ("aws pairs", {"credentials": {"awsPairs": []}}, "re-signing"),
]


@pytest.mark.parametrize(
    ("document", "message"), [case[1:] for case in BAD], ids=[case[0] for case in BAD]
)
def test_a_malformed_or_unsupported_document_is_refused(
    document: object, message: str
) -> None:
    for operator in (False, True):
        with pytest.raises(SandboxError, match=message):
            sandbox(document, operator=operator)


OPERATOR_ONLY = [
    {"allowNestedUserNamespaces": True},
    {"allowAppleEvents": True},
    {"filesystem": {"disabled": True}},
    {"network": {"allowAllUnixSockets": True}},
    {"network": {"tlsTerminate": True}},
]


@pytest.mark.parametrize("document", OPERATOR_ONLY)
def test_boundary_removing_keys_need_operator_authority(
    document: dict[str, object],
) -> None:
    with pytest.raises(SandboxError, match="only through the client"):
        sandbox(document)
    assert sandbox(document, operator=True).document() == document


def test_a_settings_file_contributes_only_its_sandbox_key() -> None:
    settings = {"permissions": {"allow": ["Bash(ls)"]}, "sandbox": {"enabled": True}}
    assert sandbox_from_settings(settings) == sandbox({"enabled": True})
    assert sandbox_from_settings({"enabled": True}) == sandbox({"enabled": True})
    with pytest.raises(SandboxError):
        sandbox_from_settings({"sandbox": {"permissions": {}}})


# Merging, as Claude Code merges settings scopes.


def test_layers_merge_scalars_by_specificity_and_unite_arrays() -> None:
    project = sandbox(
        {
            "enabled": True,
            "filesystem": {"denyRead": ["~/.ssh"]},
            "network": {"allowedDomains": ["github.com"]},
        }
    )
    line = sandbox(
        {
            "enabled": False,
            "filesystem": {"denyRead": ["~/.aws"]},
            "network": {"allowLocalBinding": True},
        }
    )
    merged = merge([project, None, line])
    assert merged is not None
    assert merged.document() == {
        "enabled": False,
        "filesystem": {"denyRead": ["~/.aws", "~/.ssh"]},
        "network": {"allowedDomains": ["github.com"], "allowLocalBinding": True},
    }
    assert merge([None, None]) is None
    assert merge([line]) == line


def test_no_layer_removes_another_layers_deny_entry() -> None:
    project = sandbox({"credentials": {"envVars": [{"name": "A", "mode": "deny"}]}})
    line = sandbox({"credentials": {"envVars": []}})
    merged = merge([project, line])
    assert merged is not None
    assert merged.document()["credentials"] == {
        "envVars": [{"mode": "deny", "name": "A"}]
    }


# The effective settings handed to the engine.


def test_nothing_set_means_nothing_is_passed() -> None:
    assert effective(None, ROOT, PROTECTED) is None


def test_an_enabled_sandbox_gets_unattended_defaults_and_protection() -> None:
    settings = effective(sandbox({"enabled": True}), ROOT, PROTECTED)
    assert settings == {
        "enabled": True,
        "failIfUnavailable": True,
        "allowUnsandboxedCommands": False,
        "network": {"strictAllowlist": True},
        "filesystem": {
            "denyRead": sorted(str(p) for p in PROTECTED),
            "denyWrite": sorted(str(p) for p in PROTECTED),
        },
    }


def test_explicit_choices_override_the_unattended_defaults() -> None:
    chosen = {
        "enabled": True,
        "failIfUnavailable": False,
        "allowUnsandboxedCommands": True,
        "network": {"strictAllowlist": False},
    }
    settings = effective(sandbox(chosen), ROOT, PROTECTED)
    assert settings is not None
    assert settings["failIfUnavailable"] is False
    assert settings["allowUnsandboxedCommands"] is True
    assert settings["network"] == {"strictAllowlist": False}


def test_relative_paths_resolve_against_the_project_root() -> None:
    settings = effective(sandbox(FULL), ROOT, PROTECTED)
    assert settings is not None
    filesystem = cast(dict[str, list[str]], settings["filesystem"])
    assert filesystem["denyWrite"] == sorted(
        [str(ROOT / "secret"), *(str(p) for p in PROTECTED)]
    )
    assert filesystem["allowRead"] == [str(ROOT)]
    assert filesystem["allowWrite"] == ["/tmp/build", "~/.kube"]  # noqa: S108


def test_a_disabled_sandbox_is_passed_without_defaults_or_protection() -> None:
    settings = effective(
        sandbox({"enabled": False, "filesystem": {"denyRead": ["x"]}}), ROOT, PROTECTED
    )
    assert settings == {"enabled": False, "filesystem": {"denyRead": [str(ROOT / "x")]}}


@pytest.mark.parametrize(
    "document",
    [
        {"enabled": True, "filesystem": {"allowRead": [".dromon"]}},
        {"enabled": True, "filesystem": {"allowWrite": [".dromon/dromon.toml"]}},
        {"enabled": True, "filesystem": {"allowRead": ["/tmp/dr-run"]}},  # noqa: S108
        {"network": {"allowUnixSockets": ["/tmp/dr-run/dromond.sock"]}},  # noqa: S108
    ],
)
def test_no_layer_can_reopen_a_protected_directory(
    document: dict[str, object],
) -> None:
    with pytest.raises(SandboxError, match="Dromon protects"):
        effective(sandbox(document), ROOT, PROTECTED)


def test_a_home_relative_path_into_a_protected_directory_is_refused(
    tmp_path: Path,
) -> None:
    home = Path.home().resolve()
    protected = (home / "work" / ".dromon",)
    document = {"enabled": True, "filesystem": {"allowRead": ["~/work/.dromon"]}}
    with pytest.raises(SandboxError, match="Dromon protects"):
        effective(sandbox(document), tmp_path, protected)


def test_a_symbolic_link_does_not_disguise_a_protected_directory(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real-state"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    document = {"enabled": True, "filesystem": {"allowRead": [str(link)]}}
    with pytest.raises(SandboxError, match="Dromon protects"):
        effective(sandbox(document), tmp_path, (real,))


def test_a_path_that_only_shares_a_prefix_is_not_protected() -> None:
    document = {"enabled": True, "filesystem": {"allowRead": [".dromon-notes"]}}
    settings = effective(sandbox(document), ROOT, PROTECTED)
    assert settings is not None


# Unsandboxed retries.


ON = {"enabled": True, "allowUnsandboxedCommands": False}
ESCAPE = {"command": "curl x", "dangerouslyDisableSandbox": True}


@pytest.mark.parametrize(
    ("settings", "tool", "tool_input", "blocked"),
    [
        (ON, "Bash", ESCAPE, True),
        (ON, "PowerShell", ESCAPE, True),
        (ON, "Monitor", ESCAPE, True),
        (ON, "Bash", {"command": "ls"}, False),
        (ON, "Bash", {"command": "ls", "dangerouslyDisableSandbox": "true"}, False),
        (ON, "Read", ESCAPE, False),
        ({"enabled": True, "allowUnsandboxedCommands": True}, "Bash", ESCAPE, False),
        ({"enabled": True}, "Bash", ESCAPE, True),
        ({"enabled": False}, "Bash", ESCAPE, False),
        (None, "Bash", ESCAPE, False),
    ],
)
def test_an_unsandboxed_retry_is_blocked_only_when_the_sandbox_forbids_it(
    settings: dict[str, object] | None,
    tool: str,
    tool_input: dict[str, object],
    blocked: bool,
) -> None:
    assert blocks_unsandboxed(settings, tool, tool_input) is blocked


def test_the_gate_refuses_an_unsandboxed_retry_even_for_an_allowed_tool() -> None:
    config = LineConfig(builtin_tools=("Bash",))
    settings = effective(sandbox({"enabled": True}), ROOT, PROTECTED)
    gate = permission_gate(config, frozenset(), settings)
    context = ToolPermissionContext()
    refused = asyncio.run(gate("Bash", dict(ESCAPE), context))
    allowed = asyncio.run(gate("Bash", {"command": "ls"}, context))
    assert isinstance(refused, PermissionResultDeny)
    assert "unsandboxed" in refused.message
    assert isinstance(allowed, PermissionResultAllow)


# Engine, line configuration and factory.


def driver() -> Driver:
    return Driver.for_model("claude-haiku-5-5", Strategy.SUBSCRIPTION)


def test_the_engine_hands_the_sandbox_to_claude_code() -> None:
    settings = effective(sandbox({"enabled": True}), ROOT, PROTECTED)
    target = ClaudeCodeLine(driver(), ROOT, sandbox=settings)
    assert target.options().sandbox == settings
    assert ClaudeCodeLine(driver(), ROOT).options().sandbox is None


def test_a_line_configuration_carries_its_sandbox_through_the_log() -> None:
    config = config_from_document({"builtin_tools": ["Bash"], "sandbox": FULL})
    assert config.sandbox == sandbox(FULL)
    assert decode_config(encode_config(config)) == config
    assert "sandbox" not in json.loads(encode_config(DEFAULT_CONFIG))
    with pytest.raises(ConfigError, match="sandbox.enable is not a known key"):
        config_from_document({"sandbox": {"enable": True}})


def test_the_factory_merges_the_project_sandbox_under_each_line() -> None:
    project = sandbox({"enabled": True, "network": {"allowedDomains": ["pypi.org"]}})
    own = sandbox({"network": {"allowedDomains": ["github.com"]}})
    build = engine_factory(str(ROOT), project_sandbox=project, protected=PROTECTED)
    spec = LineSpec(
        "alpha",
        "claude-haiku-5-5",
        Strategy.SUBSCRIPTION,
        LineConfig(sandbox=own),
    )
    engine = build(spec, [])
    assert isinstance(engine, ClaudeCodeLine)
    assert engine.sandbox == effective(merge([project, own]), ROOT, PROTECTED)
    network = cast(dict[str, object], (engine.sandbox or {})["network"])
    assert network["allowedDomains"] == ["github.com", "pypi.org"]


# The project file.


PROJECT = """\
version = 1

[sandbox]
enabled = true

[sandbox.network]
allowedDomains = ["pypi.org"]

[profiles.worker]
model = "claude-haiku-5-5"
builtin_tools = ["Bash"]

[profiles.worker.sandbox.filesystem]
denyRead = ["~/.ssh"]

[lines.alpha]
profile = "worker"

[lines.alpha.sandbox.filesystem]
denyRead = ["~/.aws"]

[lines.beta]
model = "claude-haiku-5-5"
"""


def test_the_project_file_holds_project_profile_and_line_sandboxes() -> None:
    config = parse(PROJECT)
    assert config.sandbox == sandbox(
        {"enabled": True, "network": {"allowedDomains": ["pypi.org"]}}
    )
    alpha = config.lines["alpha"].config.sandbox
    assert alpha == sandbox({"filesystem": {"denyRead": ["~/.aws", "~/.ssh"]}})
    assert config.lines["beta"].config.sandbox is None


@pytest.mark.parametrize(
    ("text", "message", "line"),
    [
        (
            "version = 1\n[sandbox]\nallowAppleEvents = true\n",
            "sandbox: sandbox.allowAppleEvents may be set only through the client",
            2,
        ),
        (
            "version = 1\n[lines.alpha]\nmodel = 'claude-haiku-5-5'\n"
            "[lines.alpha.sandbox.network]\nallowedDomain = []\n",
            "lines.alpha.sandbox: sandbox.network.allowedDomain is not a known key",
            4,
        ),
        (
            "version = 1\n[profiles.p]\n[profiles.p.sandbox]\nenabled = 'on'\n"
            "[lines.alpha]\nprofile = 'p'\nmodel = 'claude-haiku-5-5'\n",
            "profiles.p.sandbox: sandbox.enabled must be a boolean",
            3,
        ),
    ],
)
def test_a_bad_sandbox_in_the_file_is_refused_with_its_location(
    text: str, message: str, line: int
) -> None:
    with pytest.raises(ConfigFileError) as raised:
        parse(text)
    assert message in str(raised.value)
    assert raised.value.line == line


def test_the_daemon_refuses_a_file_that_reopens_a_protected_directory(
    tmp_path: Path, short_runtime: Path
) -> None:
    paths = project_paths(tmp_path, short_runtime)
    reopening = parse(
        "version = 1\n[sandbox]\nenabled = true\n"
        "[sandbox.filesystem]\nallowRead = ['.dromon']\n"
    )
    with pytest.raises(SandboxError, match="Dromon protects"):
        check_sandboxes(reopening, paths)
    check_sandboxes(parse(PROJECT), paths)
    assert protected_dirs(paths) == (
        paths.state_dir.resolve(),
        paths.runtime_dir.resolve(),
    )


def test_dromond_exits_on_an_unsafe_sandbox(
    tmp_path: Path, short_runtime: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from dromon import daemon

    state = tmp_path / ".dromon"
    state.mkdir(mode=0o700)
    (state / "dromon.toml").write_text(
        "version = 1\n[sandbox]\nenabled = true\n"
        "[sandbox.filesystem]\nallowWrite = ['.dromon']\n"
    )
    argv = ["--project", str(tmp_path), "--runtime-dir", str(short_runtime)]
    assert daemon.main(argv) == exits.FAILURE
    assert "Dromon protects" in capsys.readouterr().err


# The client.


def test_the_client_reads_a_sandbox_file_with_operator_authority(
    tmp_path: Path,
) -> None:
    file = tmp_path / "settings.json"
    file.write_text(json.dumps({"sandbox": {"allowAppleEvents": True}}))
    arguments = cli.build_parser().parse_args(
        ["configure", "alpha", "--sandbox-config", str(file)]
    )
    document = cli._config_document(arguments)  # pyright: ignore[reportPrivateUsage]
    assert document is not None
    assert document["sandbox"] == {"allowAppleEvents": True}


def test_the_client_refuses_a_bad_sandbox_file(
    tmp_path: Path, short_runtime: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    file = tmp_path / "sandbox.json"
    file.write_text(json.dumps({"enabeld": True}))
    argv = [
        "--project",
        str(tmp_path),
        "--runtime-dir",
        str(short_runtime),
        "configure",
        "alpha",
        "--sandbox-config",
        str(file),
    ]
    assert cli.main(argv) == exits.FAILURE
    assert "sandbox.enabeld is not a known key" in capsys.readouterr().err


def test_dromon_config_reports_each_lines_sandbox(
    tmp_path: Path, short_runtime: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / ".dromon"
    state.mkdir(mode=0o700)
    (state / "dromon.toml").write_text(PROJECT)
    argv = ["--project", str(tmp_path), "--runtime-dir", str(short_runtime), "config"]
    assert cli.main(argv) == exits.OK
    out = capsys.readouterr().out
    assert "project: sandboxed" in out
    assert "line alpha: claude-haiku-5-5 subscription, allowlist, Bash" in out
    assert out.count(", sandboxed") == 2


# One real turn, on the operator's subscription.


@pytest.mark.live
def test_a_live_sandboxed_line_writes_in_the_project_but_not_at_home(
    tmp_path: Path,
) -> None:
    import os
    import secrets

    if os.environ.get("DROMON_LIVE_TESTS") != "1":
        pytest.skip("set DROMON_LIVE_TESTS=1 to call the engine on the subscription")
    project = tmp_path.resolve()
    inside = project / "inside.txt"
    outside = Path.home() / f"dromon-sandbox-probe-{secrets.token_hex(4)}"
    settings = effective(sandbox({"enabled": True}), project, (project / ".dromon",))
    target = ClaudeCodeLine(
        driver(), project, config=LineConfig(builtin_tools=("Bash",)), sandbox=settings
    )
    prompt = (
        "Run exactly these two Bash commands, one after the other, each as its own"
        f" Bash call, and then reply with the word done.\n1. touch {inside}\n"
        f"2. touch {outside}\nDo not retry a command that fails."
    )
    try:
        report = asyncio.run(target.run_turn(prompt))
        assert report.tool_uses.count("Bash") >= 2, "both commands must be attempted"
        assert inside.exists(), "the positive control did not run"
        assert not outside.exists(), "the sandbox let a command write at home"
    finally:
        outside.unlink(missing_ok=True)
