"""Per-line tool configuration: parsing, strict rejection and the policy's decisions."""

import json

import pytest

from dromon.lineconfig import (
    DEFAULT_CONFIG,
    ConfigError,
    LineConfig,
    ToolPolicy,
    config_from_document,
    decode_config,
    encode_config,
    parse_servers,
)

OWN = frozenset({"mcp__dromon__send_message"})

FULL = {
    "builtin_tools": ["Read", "Edit", "Read"],
    "mcp_servers": {
        "github": {"command": "gh-mcp", "args": ["--stdio"], "env": {"TOKEN": "t"}},
        "docs": {
            "type": "http",
            "url": "https://docs.example/mcp",
            "headers": {"A": "b"},
        },
        "local": {"type": "sse", "url": "http://127.0.0.1:9000/sse"},
    },
    "tool_policy": "allowlist",
    "allowed_tools": ["mcp__github__*", "mcp__docs__search"],
    "inherit_account_mcp": False,
}


def test_a_full_configuration_parses_and_round_trips() -> None:
    config = config_from_document(FULL)
    assert config.builtin_tools == ("Edit", "Read")
    assert config.server_names() == {"github", "docs", "local"}
    assert decode_config(encode_config(config)) == config


def test_the_encoding_is_canonical_and_versioned() -> None:
    text = encode_config(config_from_document(FULL))
    document = json.loads(text)
    assert document["v"] == 1
    assert text == json.dumps(
        document, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def test_the_default_allows_only_dromon_tools() -> None:
    assert DEFAULT_CONFIG.tool_policy is ToolPolicy.ALLOWLIST
    assert DEFAULT_CONFIG.allows("mcp__dromon__send_message", OWN)
    assert not DEFAULT_CONFIG.allows("Bash", OWN)
    assert not DEFAULT_CONFIG.allows("mcp__claude_ai_Gmail__send_message", OWN)


def test_an_allowlist_allows_listed_tools_patterns_and_builtins_only() -> None:
    config = config_from_document(FULL)
    assert config.allows("Read", OWN)
    assert config.allows("mcp__github__create_issue", OWN)
    assert config.allows("mcp__docs__search", OWN)
    assert not config.allows("mcp__docs__delete", OWN)
    assert not config.allows("mcp__githubx__anything", OWN)
    assert not config.allows("Bash", OWN)


def test_allow_all_allows_every_tool() -> None:
    config = LineConfig(tool_policy=ToolPolicy.ALLOW_ALL)
    assert config.allows("Bash", OWN)
    assert config.allows("mcp__anything__at_all", OWN)


def test_a_claude_code_mcp_file_is_accepted() -> None:
    servers = parse_servers({"mcpServers": {"fs": {"command": "fs-mcp"}}})
    assert [name for name, _ in servers] == ["fs"]
    assert servers[0][1].sdk_config() == {"type": "stdio", "command": "fs-mcp"}


BAD: list[tuple[str, object, str]] = [
    ("an unknown key", {"tools": []}, "unknown configuration keys"),
    ("an unknown policy", {"tool_policy": "trust_me"}, "unknown tool_policy"),
    ("a lowercase built-in", {"builtin_tools": ["read"]}, "malformed"),
    ("a loose pattern", {"allowed_tools": ["mcp__*"]}, "neither"),
    ("a mid-name wildcard", {"allowed_tools": ["mcp__a__b*"]}, "neither"),
    ("a reserved server", {"mcp_servers": {"dromon": {"command": "x"}}}, "reserved"),
    (
        "a bad server name",
        {"mcp_servers": {"Bad Name": {"command": "x"}}},
        "identifier",
    ),
    ("a server without a command", {"mcp_servers": {"a": {}}}, "command"),
    (
        "an unknown transport",
        {"mcp_servers": {"a": {"type": "ws", "url": "x"}}},
        "type",
    ),
    (
        "plain http off the local host",
        {"mcp_servers": {"a": {"type": "http", "url": "http://example.com"}}},
        "https",
    ),
    (
        "an extra server key",
        {"mcp_servers": {"a": {"command": "x", "cwd": "/"}}},
        "only",
    ),
    ("a non-boolean inherit", {"inherit_account_mcp": 1}, "boolean"),
    (
        "args that are not text",
        {"mcp_servers": {"a": {"command": "x", "args": [1]}}},
        "args",
    ),
]


@pytest.mark.parametrize(
    ("document", "reason"), [case[1:] for case in BAD], ids=[case[0] for case in BAD]
)
def test_a_malformed_configuration_is_refused(document: object, reason: str) -> None:
    with pytest.raises(ConfigError, match=reason):
        config_from_document(document)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        '{"v": 2}',
        '{"builtin_tools": []}',
        '{"v": 1, "v": 1}',
        '{"v": 1.0}',
    ],
)
def test_a_malformed_stored_configuration_is_refused(text: str) -> None:
    with pytest.raises(ConfigError):
        decode_config(text)
