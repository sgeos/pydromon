"""Round 14, items 2 to 4: the report directory's configuration, a file checked for
disclosure in parts, and a budget that says unknown."""

import random

import pytest

from dromon.disclosure import (
    LineTooLong,
    matching_lines,
    matching_lines_in_parts,
    split_parts,
)
from dromon.lineconfig import (
    LineConfig,
    ConfigError,
    config_from_document,
    encode_config,
)
from dromon.model import BudgetSource, BudgetStated
from dromon.scheduler import (
    UNKNOWN,
    UNKNOWN_COST_RULE,
    UNKNOWN_TURN_COST_USD,
    _budget_entry,  # pyright: ignore[reportPrivateUsage]
    _budget_view,  # pyright: ignore[reportPrivateUsage]
)

# The report directory in a line's configuration.


def test_a_report_directory_round_trips_and_is_omitted_when_unset() -> None:
    config = config_from_document({"report_directory": "tmp/reports"})
    assert config.report_directory == "tmp/reports"
    assert '"report_directory":"tmp/reports"' in encode_config(config)
    assert "report_directory" not in encode_config(LineConfig())
    assert LineConfig().report_directory is None
    deeper = config_from_document({"report_directory": "tmp/reports/review"})
    assert deeper.report_directory == "tmp/reports/review"


@pytest.mark.parametrize(
    ("value", "why"),
    [
        ("/" + "tmp/reports", "relative path"),
        ("tmp//reports", "relative path"),
        ("tmp/./reports", "relative path"),
        ("tmp/reports/", "relative path"),
        ("tmp/../x", "relative path"),
        ("../tmp/reports", "relative path"),
        ("tmp", "directory under tmp/"),
        ("reports", "directory under tmp/"),
        ("src/reports", "directory under tmp/"),
        ("tmp/lines", "any of lines, worktrees, runs, probes"),
        ("tmp/lines/alpha", "any of lines, worktrees, runs, probes"),
        ("tmp/Lines", "any of lines"),
        ("tmp/LINES/alpha", "any of lines"),
        ("tmp/lInEs", "any of lines"),
        ("tmp/worktrees", "any of lines"),
        ("tmp/worktrees/core", "any of lines"),
        ("tmp/Worktrees/x", "any of lines"),
        ("tmp/runs", "any of lines"),
        ("tmp/RUNS/x", "any of lines"),
        ("tmp/probes", "any of lines"),
        ("tmp/\uff4c\uff49\uff4e\uff45\uff53", "any of lines"),
        ("tmp/.git", "starts with ."),
        ("tmp/reports/.hidden", "starts with ."),
        ("tmp/reports.", "ends with ."),
        ("tmp/reports ", "ends with ."),
        ("Tmp/reports", "directory under tmp/"),
        ("tmp\\reports", "backslash"),
        ("tmp/re\x00ports", "NUL"),
        ("tmp/a/b/c/d/e/f/g/h", "more than 8 components"),
        ("", "1 to 4096"),
        (3, "1 to 4096"),
        (["tmp/reports"], "1 to 4096"),
    ],
)
def test_a_report_directory_outside_the_rule_is_refused(
    value: object, why: str
) -> None:
    with pytest.raises(ConfigError, match=why):
        config_from_document({"report_directory": value})


def test_the_most_components_are_allowed() -> None:
    assert config_from_document({"report_directory": "tmp/a/b/c/d/e/f/g"})


# Disclosure in parts.

TERMS = ("codename",)


def whole(data: bytes) -> int:
    return matching_lines(TERMS, data.decode("utf-8", "replace"))


def test_parts_end_after_a_newline_and_cover_the_data_in_order() -> None:
    data = b"aa\nbbbb\ncc\n\ndddddd\neee"
    for size in (7, 8, 9, 12):
        parts = split_parts(data, size)
        assert b"".join(parts) == data
        assert all(len(part) <= size for part in parts)
        assert all(part.endswith(b"\n") for part in parts[:-1])
    assert split_parts(b"", 4) == []
    assert split_parts(b"abc", 3) == [b"abc"]


def test_a_line_longer_than_a_part_is_refused() -> None:
    with pytest.raises(LineTooLong):
        split_parts(b"x" * 10 + b"\nshort\n", 8)
    with pytest.raises(LineTooLong):
        split_parts(b"x" * 9, 8)
    assert split_parts(b"x" * 8, 8) == [b"x" * 8]
    assert split_parts(b"x" * 7 + b"\n", 8) == [b"x" * 7 + b"\n"]
    with pytest.raises(ValueError, match="at least one byte"):
        split_parts(b"x", 0)


def test_the_sum_over_parts_equals_the_count_for_the_whole() -> None:
    generator = random.Random(14)  # noqa: S311, a test, not a secret
    words = [b"alpha", b"CODENAME", b"beta", b"gamma code", b"name", b"\xff\xfe"]
    separators = [b"\n", b"\r\n", b" ", b"\n\n", b"\r", b"\x0b", b"\xe2\x80\xa8"]
    for _ in range(200):
        data = b"".join(
            generator.choice(words) + generator.choice(separators)
            for _ in range(generator.randrange(0, 60))
        )
        for size in (16, 32, 100, 4096):
            try:
                summed = matching_lines_in_parts(TERMS, data, size)
            except LineTooLong:
                continue
            assert summed == whole(data), (data, size)


def test_a_term_split_by_a_cut_is_still_found_because_cuts_follow_newlines() -> None:
    line = b"x" * 5 + b"codename" + b"y" * 5 + b"\n"
    data = line * 9
    assert matching_lines_in_parts(TERMS, data, len(line) * 2 + 3) == 9


def test_accepted_line_digests_apply_across_parts() -> None:
    from hashlib import sha256

    data = b"codename one\nnothing\ncodename two\n"
    digest = sha256(b"codename one").hexdigest()
    assert matching_lines_in_parts(TERMS, data, 16, [digest]) == 1
    assert matching_lines_in_parts(TERMS, data, 16) == 2


# A budget that says unknown.


def test_an_unknown_budget_is_a_named_entry_and_never_none() -> None:
    entry = _budget_entry("claude-code", None)
    assert entry["status"] == UNKNOWN
    assert entry["provider"] == "claude-code"
    assert all(value is not None for value in entry.values())
    assert all(
        entry[key] == UNKNOWN
        for key in ("remaining_percent", "resets_at", "source", "stated_at")
    )
    assert "moves no role" in str(entry["note"])
    assert _budget_entry(None, None)["provider"] == UNKNOWN


def test_a_stated_budget_with_a_missing_field_says_unknown_for_it() -> None:
    stated = BudgetStated(
        "claude-code",
        None,
        "2026-11-02T00:00:00Z",
        BudgetSource.OPERATOR,
        "set by hand",
        "2026-11-01T00:00:00Z",
    )
    view = _budget_view(stated)
    assert view["status"] == "stated"
    assert view["remaining_percent"] == UNKNOWN
    assert view["resets_at"] == "2026-11-02T00:00:00Z"
    assert all(value is not None for value in view.values())
    percent = BudgetStated(
        "claude-code",
        40,
        None,
        BudgetSource.OPERATOR,
        "",
        "2026-11-01T00:00:00Z",
    )
    assert _budget_view(percent)["remaining_percent"] == 40
    assert _budget_view(percent)["resets_at"] == UNKNOWN


def test_a_turn_of_unknown_cost_counts_as_zero_and_says_so() -> None:
    assert UNKNOWN_TURN_COST_USD == 0.0
    assert "counts as 0 toward the limit" in UNKNOWN_COST_RULE


def test_a_project_line_may_declare_a_report_directory() -> None:
    from dromon.projectconfig import ConfigFileError, parse

    project = parse(
        'version = 1\n[lines.review]\nmodel = "claude-haiku-5-5"\n'
        'report_directory = "tmp/reports"\n'
    )
    assert project.lines["review"].config.report_directory == "tmp/reports"
    with pytest.raises(ConfigFileError, match="report_directory"):
        parse(
            'version = 1\n[lines.review]\nmodel = "claude-haiku-5-5"\n'
            'report_directory = "src"\n'
        )


# Item 1. allow_compact in the settings view, with its source.


def line_entry(model: str, config: LineConfig, key: str) -> dict[str, object]:
    from dromon.configview import LineFacts, Overrides, line_entries, project_layers
    from dromon.limits import DEFAULT_LIMITS
    from dromon.projectconfig import ProjectConfig

    facts = LineFacts(
        "a",
        model,
        model,
        (model,),
        "subscription",
        None,
        config,
        {},
        DEFAULT_LIMITS,
        False,
        1,
        False,
    )
    layers = project_layers(ProjectConfig(), Overrides(), {}, False)
    return next(e for e in line_entries(facts, layers) if e["key"] == key)


def test_the_settings_view_shows_allow_compact_with_its_source() -> None:
    off = line_entry("claude-haiku-5-5", LineConfig(), "allow_compact")
    assert off["source"] == "default"
    assert "no compact_session" in str(off["value"])
    assert off["editable"] is False
    on = line_entry("claude-haiku-5-5", LineConfig(allow_compact=True), "allow_compact")
    assert on["source"] == "line configuration"
    assert "has compact_session" in str(on["value"])
    codex = line_entry("gpt-6-luna", LineConfig(allow_compact=True), "allow_compact")
    assert "has compact_session" in str(codex["value"])


def test_the_settings_view_shows_the_thresholds_with_their_sources() -> None:
    plain = line_entry("claude-haiku-5-5", LineConfig(), "context_warn_tokens")
    assert (plain["value"], plain["source"]) == ("150000", "default")
    set_ = line_entry(
        "claude-haiku-5-5",
        LineConfig(allow_compact=True, context_warn_tokens=90_000),
        "context_warn_tokens",
    )
    assert (set_["value"], set_["source"]) == ("90000", "line configuration")
    auto = line_entry(
        "claude-haiku-5-5",
        LineConfig(allow_compact=True, auto_compact_tokens=80_000),
        "auto_compact_tokens",
    )
    assert (auto["value"], auto["source"]) == ("80000", "line configuration")
    none = line_entry("claude-haiku-5-5", LineConfig(), "auto_compact_tokens")
    assert none["source"] == "default" and "warning is the threshold" in str(
        none["value"]
    )


def test_the_view_and_the_scheduler_agree_on_which_engines_compact() -> None:
    from dromon.codex import ENGINE_NAME as CODEX
    from dromon.configview import COMPACTING_ENGINES
    from dromon.engine import ENGINE_NAME as CLAUDE

    assert set(COMPACTING_ENGINES) == {CLAUDE, CODEX}
