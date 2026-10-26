"""Settings an operator changes from a client, and how they layer."""

import pytest

from dromon.limits import DEFAULT_LIMITS, Limits
from dromon.lineconfig import ConfigError, LineConfig, ToolPolicy
from dromon.overrides import (
    MAX_LIST,
    SPECS,
    Kind,
    Layer,
    OverrideError,
    Scope,
    apply_config,
    apply_limits,
    changed,
    check,
    decimal,
    decode,
    encode,
    pick,
    spec_of,
)


def test_every_key_is_unique_within_its_scope() -> None:
    pairs = [(spec.scope, spec.key) for spec in SPECS]
    assert len(pairs) == len(set(pairs))


@pytest.mark.parametrize(
    ("scope", "key", "value", "expected"),
    [
        (Scope.LINE, "inherit_account_mcp", True, True),
        (Scope.LINE, "limits.max_autonomous_turns", 5, 5),
        (Scope.LINE, "limits.max_cost_usd", "2.5", "2.5"),
        (Scope.LINE, "effort", "high", "high"),
        (Scope.LINE, "builtin_tools", ["Read", "Grep"], ("Read", "Grep")),
        (Scope.PROJECT, "daemon.operator", "ana", "ana"),
    ],
)
def test_values_of_the_right_kind_are_accepted(
    scope: Scope, key: str, value: object, expected: object
) -> None:
    assert check(spec_of(scope, key), value) == expected


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("inherit_account_mcp", "yes"),
        ("limits.max_autonomous_turns", True),
        ("limits.max_autonomous_turns", -1),
        ("limits.max_cost_usd", 2.5),
        ("limits.max_cost_usd", "1e3"),
        ("effort", "extreme"),
        ("model", ""),
        ("builtin_tools", "Read"),
        ("builtin_tools", ["Read", ""]),
        ("builtin_tools", ["Read"] * (MAX_LIST + 1)),
    ],
)
def test_values_of_the_wrong_kind_are_refused(key: str, value: object) -> None:
    with pytest.raises(OverrideError):
        check(spec_of(Scope.LINE, key), value)


def test_keys_outside_a_scope_are_refused() -> None:
    with pytest.raises(OverrideError, match="not a project setting"):
        spec_of(Scope.PROJECT, "effort")
    with pytest.raises(OverrideError, match="not a line setting"):
        spec_of(Scope.LINE, "daemon.operator")
    assert spec_of(Scope.LINE, "tool_policy").kind is Kind.CHOICE


def test_documents_round_trip_and_skip_what_no_longer_applies() -> None:
    document = {"effort": "low", "builtin_tools": ("Read",), "limits.max_cost_usd": "3"}
    text = encode(document)
    assert text == encode(dict(reversed(list(document.items()))))
    assert decode(Scope.LINE, text) == (document, [])
    kept, skipped = decode(Scope.LINE, '{"effort":"low","colour":"red","effort2":1}')
    assert kept == {"effort": "low"} and len(skipped) == 2
    assert decode(Scope.LINE, None) == ({}, [])
    assert decode(Scope.LINE, "[1]")[0] == {}
    with pytest.raises(OverrideError, match="too large"):
        encode({"instructions": tuple("x" * 1024 for _ in range(70))})


def test_configuration_overrides_are_checked_as_the_project_files_are() -> None:
    base = LineConfig(builtin_tools=("Read",), effort="low")
    assert apply_config(base, {}) is base
    found = apply_config(base, {"effort": "high", "tool_policy": "allow_all"})
    assert found.effort == "high" and found.tool_policy is ToolPolicy.ALLOW_ALL
    assert found.builtin_tools == ("Read",)
    assert apply_config(base, {"limits.max_cost_usd": "1"}) is base
    with pytest.raises(ConfigError, match="malformed"):
        apply_config(base, {"builtin_tools": ("not a tool",)})


def test_limit_overrides_replace_only_the_keys_they_name() -> None:
    found = apply_limits(DEFAULT_LIMITS, {"limits.max_cost_usd": "7.5"})
    assert found == Limits(DEFAULT_LIMITS.max_autonomous_turns, 7.5, 0.0)
    with pytest.raises(ValueError, match="max_autonomous_turns"):
        apply_limits(DEFAULT_LIMITS, {"limits.max_autonomous_turns": 0})


def test_decimals_are_written_without_trailing_zeros() -> None:
    assert [decimal(x) for x in (2.0, 2.5, 0.0, 0.125)] == ["2", "2.5", "0", "0.125"]


def test_the_first_layer_with_a_key_wins() -> None:
    layers = [Layer("high", {"a": 1}), Layer("low", {"a": 2, "b": 3})]
    assert pick("a", layers) == (1, "high")
    assert pick("b", layers) == (3, "low")
    assert pick("c", layers) is None
    assert changed({"a": 1}, "a", None) == {}
    assert changed({}, "a", 2) == {"a": 2}
