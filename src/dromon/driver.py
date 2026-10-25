"""Per-model drivers, Keleusma stream processors run through ``pykeleusma``.

A driver holds the behaviour specific to one model: its settings, the model it accepts,
and its reaction to each engine event, such as interrupting a turn that exceeds the
tool budget. The host performs every action a driver yields, so no Python callable runs
inside the bounded path and the driver's worst-case time covers its whole decision.

Driver interface 1. The entry point is
``loop main(input: (Event, State)) -> (Action, State)``, where an event and an action
are ``(Text, Text, Word)`` and the state is ``(Word, Word)``. The host owns the state
between cycles, because ``pykeleusma`` does not yet support data segments. Each event
is one cycle. The host resumes with the event and receives the action, then resumes
once more to complete the cycle, which Keleusma reports as a reset.

Trust. Drivers ship with Dromon and are trusted code, but every yielded action is still
checked, so a driver defect surfaces as ``DriverError`` and never as a silent misstep.
"""

from dataclasses import dataclass
from enum import StrEnum
from importlib import resources
from typing import Final, cast

import keleusma

INTERFACE_VERSION: Final = 1
MAX_SETTINGS: Final = 32
"""Upper bound on the settings a driver may declare, so reading them terminates."""

DRIVERS: Final[dict[str, str]] = {"claude-opus-5-5": "claude_opus_5_5"}
"""The driver resource for each supported model."""

type Triple = tuple[str, str, int]
type DriverState = tuple[int, int]

_IDLE: Final[tuple[Triple, DriverState]] = (("idle", "", 0), (0, 0))
"""The value that completes a cycle. The driver ignores it."""


class DriverError(Exception):
    """A driver could not be loaded, failed at run time, or yielded an invalid value."""


class ActionTag(StrEnum):
    CONTINUE = "continue"
    INTERRUPT = "interrupt"
    END_TURN = "end_turn"
    ACCEPT = "accept"
    REFUSE = "refuse"


@dataclass(frozen=True, slots=True)
class Event:
    tag: str
    text: str = ""
    number: int = 0


@dataclass(frozen=True, slots=True)
class Action:
    tag: ActionTag
    text: str
    number: int


@dataclass(frozen=True, slots=True)
class DriverSettings:
    model: str
    effort: str
    max_turns: int
    tool_budget: int
    context_window: int


def _items(value: object, size: int, what: str) -> tuple[object, ...]:
    """Return ``value`` as a tuple of ``size`` items, or raise ``DriverError``."""
    if not isinstance(value, tuple):
        raise DriverError(f"the driver yielded a malformed {what}: {value!r}")
    items = cast(tuple[object, ...], value)
    if len(items) != size:
        raise DriverError(f"the driver yielded a malformed {what}: {value!r}")
    return items


def _triple(value: object, what: str) -> Triple:
    tag, text, number = _items(value, 3, what)
    if not isinstance(tag, str) or not isinstance(text, str) or type(number) is not int:
        raise DriverError(f"the driver yielded a malformed {what}: {value!r}")
    return tag, text, number


def _state(value: object) -> DriverState:
    first, second = _items(value, 2, "state")
    if type(first) is not int or type(second) is not int:
        raise DriverError(f"the driver yielded a malformed state: {value!r}")
    return first, second


class Driver:
    """One running driver. Not thread-safe, and each session line owns its own."""

    def __init__(self, source: str, *, state: DriverState = (0, 0)) -> None:
        try:
            self._vm = keleusma.VM(keleusma.compile(source))
        except (keleusma.SourceError, keleusma.VerifyError) as error:
            raise DriverError(f"the driver does not compile: {error}") from error
        wcet = self._vm.wcet_per_iteration
        if wcet is None:
            raise DriverError("the driver's entry point is not a loop main")
        self._wcet = wcet
        self._state = state
        self._started = False
        self.settings = self._read_settings()

    @classmethod
    def for_model(cls, model: str, *, state: DriverState = (0, 0)) -> "Driver":
        """Load the packaged driver for ``model``."""
        name = DRIVERS.get(model)
        if name is None:
            raise DriverError(f"no driver for model {model!r}")
        source = (
            resources.files("dromon.drivers")
            .joinpath(f"{name}.kel")
            .read_text(encoding="utf-8")
        )
        return cls(source, state=state)

    @property
    def state(self) -> DriverState:
        """The state to persist, so a restarted line resumes where it left off."""
        return self._state

    @property
    def wcet_per_iteration(self) -> int:
        """The worst-case time of one decision, in Keleusma's nominal cost units."""
        return self._wcet

    def _cycle(self, event: Event) -> Triple:
        payload = ((event.tag, event.text, event.number), self._state)
        try:
            if self._started:
                yielded = self._vm.resume(payload)
            else:
                yielded = self._vm.call(payload)
                self._started = True
            if not isinstance(yielded, keleusma.Yielded):
                raise DriverError(f"the driver did not yield: {yielded!r}")
            completed = self._vm.resume(_IDLE)
        except keleusma.VMError as error:
            raise DriverError(f"the driver failed: {error}") from error
        if not isinstance(completed, keleusma.Reset):
            raise DriverError(f"the driver did not complete its cycle: {completed!r}")
        step = _items(yielded.value, 2, "step")
        triple = _triple(step[0], "action")
        self._state = _state(step[1])
        return triple

    def step(self, event: Event) -> Action:
        """Feed one engine event to the driver and return the action to perform."""
        tag, text, number = self._cycle(event)
        try:
            action_tag = ActionTag(tag)
        except ValueError as error:
            raise DriverError(
                f"the driver yielded an unknown action {tag!r}"
            ) from error
        return Action(action_tag, text, number)

    def _read_settings(self) -> DriverSettings:
        found: dict[str, Triple] = {}
        for index in range(MAX_SETTINGS):
            triple = self._cycle(Event("settings", "", index))
            if triple[0] == "end":
                break
            if triple[0] in found:
                raise DriverError(f"the driver repeats the setting {triple[0]!r}")
            found[triple[0]] = triple
        else:
            raise DriverError(f"the driver declares more than {MAX_SETTINGS} settings")
        required = {
            "interface",
            "model",
            "effort",
            "max_turns",
            "tool_budget",
            "context_window",
        }
        missing = required - set(found)
        if missing:
            raise DriverError(f"the driver omits the settings {sorted(missing)}")
        interface = found["interface"][2]
        if interface != INTERFACE_VERSION:
            raise DriverError(
                f"the driver implements interface {interface}, not {INTERFACE_VERSION}"
            )
        settings = DriverSettings(
            model=found["model"][1],
            effort=found["effort"][1],
            max_turns=found["max_turns"][2],
            tool_budget=found["tool_budget"][2],
            context_window=found["context_window"][2],
        )
        if settings.max_turns < 1 or settings.tool_budget < 0 or not settings.model:
            raise DriverError(f"the driver declares invalid settings: {settings}")
        return settings
