"""Per-model drivers, Keleusma stream processors run through ``pykeleusma``.

A driver holds the behaviour specific to one model: its settings, the model it accepts,
and its reaction to each engine event, such as interrupting a turn that exceeds the
tool budget. The host performs every action a driver yields, so no Python callable runs
inside the bounded path and the driver's worst-case time covers its whole decision.

Driver interface 3. The entry point is
``loop main(input: (Event, State)) -> (Action, State)``, where an event and an action
are ``(Text, Text, Word)`` and the state is ``(Word, Word, Word)``. The host owns the
state between cycles, so the event log can record it and a restarted line can resume
it. Each event is one cycle. The host resumes with the event and
receives the action, then resumes once more to complete the cycle, which Keleusma
reports as a reset.

Strategies. Each driver is concrete, with one model and one credential strategy fixed
in its code: subscription only, API only, or flexible. A subscription draws on a plan's
limits while API usage is charged per token, so the strategies differ in what they
accept and how they run the model. The engine reports the credential actually in use
with a ``credential`` event, and the driver accepts or refuses it.

Engines. Each driver names the engine it is written for, one of ``ENGINES``.
A model has a packaged driver once its drivers are written, but a line can be created
on it only once Dromon has an adapter for its engine, so a driver can be written and
tested ahead of its adapter without a line ever running it on the wrong engine.

Trust. Drivers ship with Dromon and are trusted code, but every yielded action is still
checked, so a driver defect surfaces as ``DriverError`` and never as a silent misstep.
"""

from dataclasses import dataclass
from enum import StrEnum
from importlib import resources
from typing import Final, cast

import keleusma

INTERFACE_VERSION: Final = 3
MAX_SETTINGS: Final = 32
"""Upper bound on the settings a driver may declare, so reading them terminates."""

ENGINE_OF: Final[dict[str, str]] = {
    "claude-opus-5-5": "claude-code",
    "claude-haiku-5-5": "claude-code",
    "claude-fable-5-1": "claude-code",
    "gpt-6-luna": "codex",
    "grok-4.7": "grok-build",
    "pi:openai/gpt-6-luna": "pi",
    "cursor-agent:gpt-5.6-luna-medium": "cursor-agent",
    "antigravity:gemini-3.8-flash-high": "antigravity",
}
"""Each model with a packaged driver for every strategy, and the engine it runs on.

Naming. The first engines each run their own vendor's models, so a model is named by
the vendor's identifier. Pi, Cursor Agent and Antigravity run other vendors' models,
and Cursor Agent's ``claude-opus-5-5-high`` or Pi's ``openai/gpt-6-luna`` would read
as, or come to collide with, the models above, which run on another engine with other
credentials and costs. Their models are therefore named ``ENGINE:ID``, the engine and
the harness's own identifier, so the engine can be read from the name and a name
never stands for two engines. No harness identifier seen in their catalogues on
2026-10-26 contains a colon, so the first colon divides the two parts."""

ENGINES: Final = (
    "claude-code",
    "codex",
    "grok-build",
    "pi",
    "cursor-agent",
    "antigravity",
)
"""The engines a driver may name."""

ADAPTED_ENGINES: Final = frozenset(
    {"claude-code", "codex", "grok-build", "pi", "cursor-agent", "antigravity"}
)
"""The engines Dromon has an adapter for."""

DOCUMENTLESS_ENGINES: Final = frozenset(
    {"codex", "grok-build", "pi", "cursor-agent", "antigravity"}
)
"""Engines that take no sandbox or settings document, which only Claude Code honours,
so a project's own sandbox or settings must not reach their lines."""

MODELS: Final = tuple(
    model for model, engine in ENGINE_OF.items() if engine in ADAPTED_ENGINES
)
"""Models a line can be created on, those whose engine has an adapter."""

type Triple = tuple[str, str, int]
type DriverState = tuple[int, int, int]

INITIAL_STATE: Final[DriverState] = (0, 0, 0)

_IDLE: Final[tuple[Triple, DriverState]] = (("idle", "", 0), INITIAL_STATE)
"""The value that completes a cycle. The driver ignores it."""


class DriverError(Exception):
    """A driver could not be loaded, failed at run time, or yielded an invalid value."""


class Credential(StrEnum):
    """How the engine is actually paid for in a turn."""

    SUBSCRIPTION = "subscription"
    API = "api"
    UNVERIFIED = "unverified"
    """An engine that names no credential source, so that Dromon cannot tell a plan
    from a key. No packaged driver accepts it, so such a turn is refused."""


class Strategy(StrEnum):
    """Which credentials a driver accepts, fixed in the driver's code."""

    SUBSCRIPTION = "subscription"
    API = "api"
    FLEXIBLE = "flexible"


REPORTED_NAMES: Final[dict[str, str]] = {
    "cursor-agent:gpt-5.6-luna-medium": "GPT-5.6 Luna 1M",
}
"""How an engine names a model in its ``init`` event, where that is not the harness
identifier. Cursor Agent reports the model's display name, as its listing shows it."""


def harness_id(model: str) -> str:
    """The identifier a harness takes for ``model``, the part after the colon of an
    engine-qualified name, or the name itself."""
    _, colon, identifier = model.partition(":")
    return identifier if colon else model


def reported_model(model: str) -> str:
    """The name the model's engine reports for it, which its drivers accept."""
    return REPORTED_NAMES.get(model, harness_id(model))


def driver_resource(model: str, strategy: Strategy) -> str:
    """The packaged file name of the driver for ``model`` and ``strategy``. The colon
    and slash of an engine-qualified name become double underscores, which no vendor
    identifier contains."""
    stem = model.replace(":", "__").replace("/", "__")
    return f"{stem.replace('-', '_').replace('.', '_')}_{strategy.value}.kel"


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


TOOL_RESULT: Final = "tool_result"
"""The event an adapter feeds once a tool the turn used has finished and its result
was delivered to the model. Added on 2026-10-27 for the safe point of priority mail,
as ``dromon.priority`` describes. The packaged drivers answer it with ``continue``,
as they answer every tag they do not name."""

MESSAGE_END: Final = "message_end"
"""The event an adapter feeds at the end of an assistant message, with the message's
count of tool uses as its number, where the engine reports a whole message. Added
with ``TOOL_RESULT``, and answered with ``continue`` in the same way."""


@dataclass(frozen=True, slots=True)
class Action:
    tag: ActionTag
    text: str
    number: int


@dataclass(frozen=True, slots=True)
class DriverSettings:
    engine: str
    strategy: Strategy
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
    first, second, third = _items(value, 3, "state")
    if type(first) is not int or type(second) is not int or type(third) is not int:
        raise DriverError(f"the driver yielded a malformed state: {value!r}")
    return first, second, third


class Driver:
    """One running driver. Not thread-safe, and each session line owns its own."""

    def __init__(self, source: str, *, state: DriverState = INITIAL_STATE) -> None:
        try:
            self._vm: keleusma.VM | None = keleusma.VM(keleusma.compile(source))
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
    def for_model(
        cls,
        model: str,
        strategy: Strategy = Strategy.SUBSCRIPTION,
        *,
        state: DriverState = INITIAL_STATE,
    ) -> "Driver":
        """Load the packaged driver for ``model`` and ``strategy``, whether or not its
        engine has an adapter yet."""
        if model not in ENGINE_OF:
            raise DriverError(f"no driver for model {model!r}")
        resource = resources.files("dromon.drivers").joinpath(
            driver_resource(model, strategy)
        )
        driver = cls(resource.read_text(encoding="utf-8"), state=state)
        found = driver.settings
        expected = (ENGINE_OF[model], model, strategy)
        if (found.engine, found.model, found.strategy) != expected:
            raise DriverError(
                f"the packaged driver for {model} {strategy} declares"
                f" {found.model} {found.strategy} on {found.engine}"
            )
        return driver

    @property
    def state(self) -> DriverState:
        """The state to persist, so a restarted line resumes where it left off."""
        return self._state

    @property
    def closed(self) -> bool:
        """Whether this driver's thread-affine virtual machine has been released."""
        return self._vm is None

    @property
    def wcet_per_iteration(self) -> int:
        """The worst-case time of one decision, in Keleusma's nominal cost units."""
        return self._wcet

    def _cycle(self, event: Event) -> Triple:
        payload = ((event.tag, event.text, event.number), self._state)
        vm = self._vm
        if vm is None:
            raise DriverError("the driver is closed")
        try:
            if self._started:
                yielded = vm.resume(payload)
            else:
                yielded = vm.call(payload)
                self._started = True
            if not isinstance(yielded, keleusma.Yielded):
                raise DriverError(f"the driver did not yield: {yielded!r}")
            completed = vm.resume(_IDLE)
        except keleusma.VMError as error:
            raise DriverError(f"the driver failed: {error}") from error
        if not isinstance(completed, keleusma.Reset):
            raise DriverError(f"the driver did not complete its cycle: {completed!r}")
        step = _items(yielded.value, 2, "step")
        triple = _triple(step[0], "action")
        self._state = _state(step[1])
        return triple

    def close(self) -> None:
        """Release the thread-affine VM on the thread that owns this driver.

        A line engine may be part of a callback cycle with its scheduler. Explicitly
        dropping the VM when that engine is retired keeps Python's cycle collector
        from finalizing it later on an unrelated thread.
        """
        vm, self._vm = self._vm, None
        self._started = False
        del vm

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
            "engine",
            "strategy",
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
        declared = found["strategy"][1]
        try:
            strategy = Strategy(declared)
        except ValueError as error:
            raise DriverError(
                f"the driver declares an unknown strategy {declared!r}"
            ) from error
        engine = found["engine"][1]
        if engine not in ENGINES:
            raise DriverError(f"the driver names an unknown engine {engine!r}")
        settings = DriverSettings(
            engine=engine,
            strategy=strategy,
            model=found["model"][1],
            effort=found["effort"][1],
            max_turns=found["max_turns"][2],
            tool_budget=found["tool_budget"][2],
            context_window=found["context_window"][2],
        )
        if settings.max_turns < 1 or settings.tool_budget < 0 or not settings.model:
            raise DriverError(f"the driver declares invalid settings: {settings}")
        return settings
