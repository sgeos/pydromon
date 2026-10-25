"""The Opus 5.5 driver run through pykeleusma, and the host's checks on any driver."""

from importlib import resources

import pytest

from dromon.driver import (
    INTERFACE_VERSION,
    MAX_SETTINGS,
    ActionTag,
    Driver,
    DriverError,
    Event,
)

MODEL = "claude-opus-5-5"


def packaged_source() -> str:
    path = resources.files("dromon.drivers").joinpath("claude_opus_5_5.kel")
    return path.read_text(encoding="utf-8")


def with_budget(budget: int) -> Driver:
    source = packaged_source().replace(
        "fn tool_budget() -> Word { 40 }", f"fn tool_budget() -> Word {{ {budget} }}"
    )
    assert f"{{ {budget} }}" in source
    return Driver(source)


def test_the_packaged_driver_declares_its_model_and_settings() -> None:
    driver = Driver.for_model(MODEL)
    settings = driver.settings
    assert settings.model == MODEL
    assert settings.effort == "high"
    assert settings.max_turns >= 1
    assert settings.tool_budget == 40
    assert settings.context_window == 1_000_000
    assert driver.wcet_per_iteration > 0


def test_the_driver_accepts_its_own_model_and_refuses_another() -> None:
    driver = Driver.for_model(MODEL)
    accepted = driver.step(Event("init", MODEL))
    refused = driver.step(Event("init", "claude-sonnet-5-5"))
    assert (accepted.tag, accepted.text) == (ActionTag.ACCEPT, MODEL)
    assert (refused.tag, refused.text) == (ActionTag.REFUSE, MODEL)


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
    driver.step(Event("turn_start"))
    driver.step(Event("tool_use", "Read"))
    end = driver.step(Event("result", "success", 3))
    assert (end.tag, end.text, end.number) == (ActionTag.END_TURN, "success", 3)
    assert driver.state == (1, 1)
    driver.step(Event("turn_start"))
    assert driver.state == (0, 1)
    assert driver.step(Event("tool_use", "Read")).tag is ActionTag.CONTINUE


def test_state_carries_across_a_restart() -> None:
    first = with_budget(1)
    first.step(Event("turn_start"))
    first.step(Event("tool_use", "Read"))
    restarted = Driver(packaged_source().replace("{ 40 }", "{ 1 }"), state=first.state)
    assert restarted.step(Event("tool_use", "Bash")).tag is ActionTag.INTERRUPT


def test_an_unknown_event_continues_and_changes_nothing() -> None:
    driver = Driver.for_model(MODEL)
    before = driver.state
    action = driver.step(Event("something_new", "x", 9))
    assert action.tag is ActionTag.CONTINUE
    assert driver.state == before


def test_many_decisions_run_on_one_driver() -> None:
    driver = Driver.for_model(MODEL)
    for turn in range(200):
        driver.step(Event("turn_start"))
        driver.step(Event("text", "", 5))
        driver.step(Event("result", "success", 1))
        assert driver.state == (0, turn + 1)


def test_an_unknown_model_has_no_driver() -> None:
    with pytest.raises(DriverError, match="no driver"):
        Driver.for_model("claude-sonnet-5-5")


# The host's checks on a defective driver.

STEP = "((Text, Text, Word), (Word, Word))"


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
        (driver_source(GOOD_SETTINGS.replace('"", 2)', '"", 0)')), "invalid settings"),
        (
            driver_source(GOOD_SETTINGS + ' 6 => ("model", "n", 0),'),
            "repeats the setting",
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
