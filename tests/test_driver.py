"""The packaged Keleusma drivers run through pykeleusma, and the host's checks."""

import re
from importlib import resources

import pytest

from dromon.driver import (
    INITIAL_STATE,
    INTERFACE_VERSION,
    ADAPTED_ENGINES,
    ENGINE_OF,
    ENGINES,
    MAX_SETTINGS,
    MODELS,
    ActionTag,
    Driver,
    DriverError,
    Event,
    Strategy,
    driver_resource,
    reported_model,
)

OPUS = "claude-opus-5-5"
HAIKU = "claude-haiku-5-5"
LUNA = "gpt-6-luna"
EVERY_DRIVER = [(model, strategy) for model in ENGINE_OF for strategy in Strategy]


def packaged_source(
    model: str = OPUS, strategy: Strategy = Strategy.SUBSCRIPTION
) -> str:
    path = resources.files("dromon.drivers").joinpath(driver_resource(model, strategy))
    return path.read_text(encoding="utf-8")


def budget_source(budget: int) -> str:
    """The packaged Opus driver with its tool budget replaced, whatever it was."""
    source = re.sub(
        r"fn tool_budget\(\) -> Word \{ \d+ \}",
        f"fn tool_budget() -> Word {{ {budget} }}",
        packaged_source(),
    )
    assert f"fn tool_budget() -> Word {{ {budget} }}" in source
    return source


def with_budget(budget: int) -> Driver:
    return Driver(budget_source(budget))


@pytest.mark.parametrize(("model", "strategy"), EVERY_DRIVER)
def test_every_packaged_driver_declares_its_own_model_and_strategy(
    model: str, strategy: Strategy
) -> None:
    driver = Driver.for_model(model, strategy)
    assert (driver.settings.model, driver.settings.strategy) == (model, strategy)
    assert driver.settings.engine == ENGINE_OF[model]
    assert driver.settings.max_turns >= 1
    assert driver.wcet_per_iteration > 0
    assert driver.state == INITIAL_STATE


ACCEPTS = {
    Strategy.SUBSCRIPTION: {"subscription"},
    Strategy.API: {"api"},
    Strategy.FLEXIBLE: {"subscription", "api"},
}


@pytest.mark.parametrize(("model", "strategy"), EVERY_DRIVER)
@pytest.mark.parametrize("credential", ["subscription", "api", "bedrock", ""])
def test_each_strategy_accepts_exactly_its_credentials(
    model: str, strategy: Strategy, credential: str
) -> None:
    driver = Driver.for_model(model, strategy)
    verdict = driver.step(Event("credential", credential))
    if credential in ACCEPTS[strategy]:
        code = {"subscription": 1, "api": 2}[credential]
        assert (verdict.tag, verdict.text, verdict.number) == (
            ActionTag.ACCEPT,
            credential,
            code,
        )
        assert driver.state[2] == code
    else:
        assert (verdict.tag, verdict.text) == (ActionTag.REFUSE, strategy.value)
        assert driver.state == INITIAL_STATE


@pytest.mark.parametrize("model", ENGINE_OF)
def test_metered_strategies_run_shorter_than_the_subscription(model: str) -> None:
    subscription = Driver.for_model(model, Strategy.SUBSCRIPTION).settings
    for metered in (Strategy.API, Strategy.FLEXIBLE):
        settings = Driver.for_model(model, metered).settings
        assert settings.max_turns <= subscription.max_turns
        assert settings.tool_budget <= subscription.tool_budget
    assert (
        Driver.for_model(model, Strategy.API).settings.max_turns
        < subscription.max_turns
    )


def test_haiku_is_the_low_effort_testing_model() -> None:
    for strategy in Strategy:
        assert Driver.for_model(HAIKU, strategy).settings.effort == "low"
    assert Driver.for_model(OPUS, Strategy.SUBSCRIPTION).settings.effort == "high"


def test_luna_runs_on_codex_with_the_catalogues_default_effort_and_window() -> None:
    # Codex's model catalogue, fetched on 2026-10-25, gives GPT-6-Luna a default
    # effort of medium and a context window of 272,000 tokens.
    subscription = Driver.for_model(LUNA, Strategy.SUBSCRIPTION).settings
    assert (subscription.engine, subscription.effort) == ("codex", "medium")
    for strategy in Strategy:
        assert Driver.for_model(LUNA, strategy).settings.context_window == 272000
    for metered in (Strategy.API, Strategy.FLEXIBLE):
        assert Driver.for_model(LUNA, metered).settings.effort == "low"


def test_only_models_on_an_adapted_engine_can_carry_a_line() -> None:
    assert set(MODELS) == {m for m, e in ENGINE_OF.items() if e in ADAPTED_ENGINES}
    assert LUNA in MODELS
    assert set(ENGINE_OF.values()) <= set(ENGINES)
    assert set(ENGINES) >= ADAPTED_ENGINES


@pytest.mark.parametrize(("model", "strategy"), EVERY_DRIVER)
def test_each_driver_accepts_its_model_and_refuses_another(
    model: str, strategy: Strategy
) -> None:
    driver = Driver.for_model(model, strategy)
    other = HAIKU if model == OPUS else OPUS
    assert driver.step(Event("init", reported_model(model))).tag is ActionTag.ACCEPT
    refused = driver.step(Event("init", other))
    assert (refused.tag, refused.text) == (ActionTag.REFUSE, model)


def test_tool_uses_are_counted_and_the_budget_interrupts() -> None:
    driver = with_budget(2)
    driver.step(Event("turn_start"))
    first = driver.step(Event("tool_use", "Read"))
    second = driver.step(Event("tool_use", "Bash"))
    third = driver.step(Event("tool_use", "Edit"))
    assert (first.tag, first.text, first.number) == (ActionTag.CONTINUE, "Read", 1)
    assert second.tag is ActionTag.CONTINUE
    assert (third.tag, third.text, third.number) == (
        ActionTag.INTERRUPT,
        "tool_budget",
        3,
    )


def test_a_new_turn_resets_the_tool_count_and_a_result_counts_the_turn() -> None:
    driver = with_budget(1)
    driver.step(Event("credential", "subscription"))
    driver.step(Event("turn_start"))
    driver.step(Event("tool_use", "Read"))
    end = driver.step(Event("result", "success", 3))
    assert (end.tag, end.text, end.number) == (ActionTag.END_TURN, "success", 3)
    assert driver.state == (1, 1, 1)
    driver.step(Event("turn_start"))
    assert driver.state == (0, 1, 1)
    assert driver.step(Event("tool_use", "Read")).tag is ActionTag.CONTINUE


def test_state_carries_across_a_restart() -> None:
    first = with_budget(1)
    first.step(Event("turn_start"))
    first.step(Event("tool_use", "Read"))
    restarted = Driver(budget_source(1), state=first.state)
    assert restarted.step(Event("tool_use", "Bash")).tag is ActionTag.INTERRUPT


def test_an_unknown_event_continues_and_changes_nothing() -> None:
    driver = Driver.for_model(OPUS)
    before = driver.state
    action = driver.step(Event("something_new", "x", 9))
    assert action.tag is ActionTag.CONTINUE
    assert driver.state == before


def test_many_decisions_run_on_one_driver() -> None:
    driver = Driver.for_model(HAIKU)
    for turn in range(200):
        driver.step(Event("turn_start"))
        driver.step(Event("text", "", 5))
        driver.step(Event("result", "success", 1))
        assert driver.state == (0, turn + 1, 0)


def test_an_unknown_model_has_no_driver() -> None:
    with pytest.raises(DriverError, match="no driver"):
        Driver.for_model("claude-sonnet-5-5")


# The host's checks on a defective driver.

STEP = "((Text, Text, Word), (Word, Word, Word))"


def driver_source(settings: str, action: str = '("continue", "", 0)') -> str:
    return (
        "fn setting(index: Word) -> (Text, Text, Word) { match index { "
        + settings
        + ' _ => ("end", "", 0), } }\n'
        f"loop main(input: {STEP}) -> {STEP} {{ "
        "let event = input.0; let state = input.1; "
        'let step = if event.0 == "settings" { (setting(event.2), state) } '
        f"else {{ ({action}, state) }}; "
        'let _ = yield step; (("idle", "", 0), state) }'
    )


GOOD_SETTINGS = (
    f'0 => ("interface", "", {INTERFACE_VERSION}), 1 => ("model", "m", 0), '
    '7 => ("engine", "codex", 0), '
    '6 => ("strategy", "flexible", 0), '
    '2 => ("effort", "high", 0), 3 => ("max_turns", "", 2), '
    '4 => ("tool_budget", "", 1), 5 => ("context_window", "", 10),'
)


def test_a_minimal_valid_driver_loads() -> None:
    assert Driver(driver_source(GOOD_SETTINGS)).settings.model == "m"


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        ("loop main(", "does not compile"),
        ("fn main() -> Word { 1 }", "not a loop main"),
        (driver_source(GOOD_SETTINGS.replace('1 => ("model", "m", 0), ', "")), "omits"),
        (
            driver_source(
                GOOD_SETTINGS.replace(f'"", {INTERFACE_VERSION})', '"", 99)')
            ),
            "interface 99",
        ),
        (
            driver_source(
                GOOD_SETTINGS.replace('("max_turns", "", 2)', '("max_turns", "", 0)')
            ),
            "invalid settings",
        ),
        (
            driver_source(GOOD_SETTINGS + ' 8 => ("model", "n", 0),'),
            "repeats the setting",
        ),
        (
            driver_source(GOOD_SETTINGS.replace('"flexible"', '"metered"')),
            "unknown strategy 'metered'",
        ),
        (
            driver_source(GOOD_SETTINGS.replace('"codex"', '"gemini-cli"')),
            "unknown engine 'gemini-cli'",
        ),
        (
            driver_source(GOOD_SETTINGS.replace('7 => ("engine", "codex", 0), ', "")),
            "omits the settings \\['engine'\\]",
        ),
    ],
)
def test_a_defective_driver_is_refused_at_load(source: str, reason: str) -> None:
    with pytest.raises(DriverError, match=reason):
        Driver(source)


def test_a_driver_with_endless_settings_is_refused() -> None:
    endless = driver_source("").replace(
        '_ => ("end", "", 0)', '_ => ("model", "m", index)'
    )
    with pytest.raises(DriverError, match="repeats|more than"):
        Driver(endless)
    assert MAX_SETTINGS > 6


def test_an_unknown_action_is_refused() -> None:
    driver = Driver(driver_source(GOOD_SETTINGS, '("launch", "", 0)'))
    with pytest.raises(DriverError, match="unknown action 'launch'"):
        driver.step(Event("turn_start"))


DEVELOPMENT_TOOL_BUDGET = 100
DEVELOPMENT_TURNS = 50


@pytest.mark.parametrize("model", list(ENGINE_OF))
def test_subscription_drivers_allow_a_development_turn(model: str) -> None:
    # A line that develops software reads, edits and runs a gate many times in one
    # turn, so a subscription driver's budgets are sized for that. The per-line cost
    # and autonomous-turn limits are the operator's safety net. Drivers that may bill
    # per token keep their small budgets, which the strategy tests above order.
    settings = Driver.for_model(model, Strategy.SUBSCRIPTION).settings
    assert settings.tool_budget >= DEVELOPMENT_TOOL_BUDGET
    assert settings.max_turns >= DEVELOPMENT_TURNS


def test_fable_follows_the_opus_drivers_on_claude_code() -> None:
    # No live turn has run on Claude Fable 5.1, so its settings copy Opus 5.5's,
    # which are the development-scale ones on a subscription and small otherwise.
    assert ENGINE_OF["claude-fable-5-1"] == "claude-code"
    for strategy in Strategy:
        fable = Driver.for_model("claude-fable-5-1", strategy).settings
        opus = Driver.for_model(OPUS, strategy).settings
        assert fable.model == "claude-fable-5-1"
        assert (
            fable.engine,
            fable.strategy,
            fable.effort,
            fable.max_turns,
            fable.tool_budget,
            fable.context_window,
        ) == (
            opus.engine,
            opus.strategy,
            opus.effort,
            opus.max_turns,
            opus.tool_budget,
            opus.context_window,
        )


@pytest.mark.parametrize("model", ["gpt-6.1-sol", "gpt-6.1-terra"])
def test_sol_and_terra_follow_the_luna_drivers_with_the_catalogues_window(
    model: str,
) -> None:
    # Codex's model catalogue, fetched on 2026-10-30, gives GPT-6.1-Sol and GPT-6.1
    # Terra a context window of 500,000 tokens. No turn on either has run through
    # Dromon, so every other setting copies GPT-6-Luna's for the same strategy.
    assert ENGINE_OF[model] == "codex"
    for strategy in Strategy:
        settings = Driver.for_model(model, strategy).settings
        luna = Driver.for_model(LUNA, strategy).settings
        assert (settings.model, settings.context_window) == (model, 500000)
        assert (
            settings.engine,
            settings.strategy,
            settings.effort,
            settings.max_turns,
            settings.tool_budget,
        ) == (
            luna.engine,
            luna.strategy,
            luna.effort,
            luna.max_turns,
            luna.tool_budget,
        )
