"""Parameters of a declared run command that a line supplies, each matched against a
pattern the operator wrote. **Task of 2026-11-05, item 3.**
``docs/architecture/RUNS.md`` is normative, and its section on parameters gives the
trust argument.

A command declares a parameter by an argument that is exactly ``{NAME}``, and the
project file gives its pattern under ``[runs.parameters.COMMAND.NAME]``. A line names
every parameter of the command it starts and no other, and each value replaces its
placeholder as one whole argument. Nothing is joined to other text, split, expanded or
passed to a shell, so a value can only ever be the one argument the operator placed.

Each value must also pass rules the operator cannot widen. It is text of 1 to
``MAX_VALUE_BYTES`` bytes, holds only printable characters, and does not begin with
``-``, so it can never be read as an option by the program, whatever the pattern says.

Porting note. Patterns use Python's ``re`` syntax, matched with ``fullmatch``. To keep
them portable to a Rust port, whose ``regex`` crate has neither lookaround nor
backreferences, a pattern with ``(?``, a backslash before a digit, or a possessive
quantifier is refused. **Residual.** A pattern with nested quantifiers can take time
exponential in a value's length to match. The value is bounded, and the pattern is the
operator's, which this module trusts as it trusts the command.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

MAX_PARAMETERS: Final = 8
"""The most parameters one command declares."""

MAX_PATTERN_BYTES: Final = 256
"""The longest pattern, in bytes."""

MAX_DESCRIPTION_BYTES: Final = 256
"""The longest description of a parameter, in bytes."""

MAX_VALUE_BYTES: Final = 256
"""The longest value a line may give, in bytes, which also bounds a match's time."""

NAME_PATTERN: Final = re.compile(r"[a-z][a-z0-9_]{0,31}")
"""A parameter's name."""

PLACEHOLDER: Final = re.compile(r"\{([a-z][a-z0-9_]{0,31})\}")
"""An argument that is exactly a placeholder, read with ``fullmatch``."""

UNPORTABLE: Final = re.compile(r"\(\?|\\[0-9]|[*+?}]\+")
"""Constructs a pattern may not use, as the porting note says."""


class ParameterRefused(ValueError):
    """A declaration or a value that is refused, with the reason."""


@dataclass(frozen=True, slots=True)
class RunParameter:
    """One parameter a command declares."""

    name: str
    pattern: str
    description: str = ""


def check_pattern(pattern: str) -> re.Pattern[str]:
    """``pattern`` compiled, or ``ParameterRefused`` when it is empty, too long,
    unportable or not a valid pattern."""
    if not pattern or len(pattern.encode()) > MAX_PATTERN_BYTES:
        raise ParameterRefused(
            f"a pattern must be text of 1 to {MAX_PATTERN_BYTES} bytes"
        )
    if UNPORTABLE.search(pattern) is not None:
        raise ParameterRefused(
            "a pattern may not use (?, a backreference or a possessive quantifier,"
            " which a port could not match the same way"
        )
    try:
        return re.compile(pattern)
    except re.error as error:
        raise ParameterRefused(f"the pattern is not valid: {error}") from error


def placeholders(argv: Sequence[str]) -> tuple[str, ...]:
    """The names of the arguments of ``argv`` that are exactly a placeholder, in
    order. The program, ``argv[0]``, is never one, so a line never names what runs."""
    return tuple(
        found.group(1)
        for argument in argv[1:]
        if (found := PLACEHOLDER.fullmatch(argument)) is not None
    )


def check_declared(argv: Sequence[str], parameters: Mapping[str, RunParameter]) -> None:
    """Refuse a command whose placeholders and declared parameters differ, or that
    names one placeholder twice or its program as one. Each placeholder must be
    declared, so a literal ``{name}`` is never left to chance, and each declaration
    used, so none is a dead pattern."""
    if argv and PLACEHOLDER.fullmatch(argv[0]) is not None:
        raise ParameterRefused("the program may not be a placeholder")
    names = placeholders(argv)
    if len(set(names)) != len(names):
        raise ParameterRefused("a placeholder may appear once")
    if len(parameters) > MAX_PARAMETERS:
        raise ParameterRefused(f"a command declares at most {MAX_PARAMETERS}")
    missing = sorted(set(names) - set(parameters))
    if missing:
        raise ParameterRefused(
            f"the command holds placeholders {', '.join(missing)}, which no"
            " parameter declares"
        )
    unused = sorted(set(parameters) - set(names))
    if unused:
        raise ParameterRefused(
            f"declares {', '.join(unused)}, which the command has no placeholder for"
        )


def check_value(parameter: RunParameter, value: object) -> str:
    """``value`` when it may replace ``parameter``'s placeholder, or
    ``ParameterRefused``. The rules that do not depend on the pattern come first."""
    name = parameter.name
    if not isinstance(value, str):
        raise ParameterRefused(f"{name} must be text")
    if not value or len(value.encode()) > MAX_VALUE_BYTES:
        raise ParameterRefused(f"{name} must be text of 1 to {MAX_VALUE_BYTES} bytes")
    if not value.isprintable():
        raise ParameterRefused(f"{name} may hold only printable characters")
    if value.startswith("-"):
        raise ParameterRefused(
            f"{name} may not begin with -, so it is never read as an option"
        )
    if check_pattern(parameter.pattern).fullmatch(value) is None:
        raise ParameterRefused(f"{name} does not match its pattern {parameter.pattern}")
    return value


def substitute(
    argv: Sequence[str],
    parameters: Mapping[str, RunParameter],
    given: Mapping[str, object] | None,
) -> tuple[str, ...]:
    """``argv`` with each placeholder replaced by the value ``given`` names for it,
    or ``ParameterRefused`` for a value that is missing, extra or refused. A
    command without parameters takes none."""
    supplied = dict(given or {})
    if not parameters:
        if supplied:
            raise ParameterRefused("the command takes no arguments")
        return tuple(argv)
    extra = sorted(set(supplied) - set(parameters))
    if extra:
        raise ParameterRefused(f"the command has no parameter {', '.join(extra)}")
    missing = sorted(set(parameters) - set(supplied))
    if missing:
        raise ParameterRefused(f"the command needs arguments {', '.join(missing)}")
    values = {
        name: check_value(parameter, supplied[name])
        for name, parameter in sorted(parameters.items())
    }
    result = [argv[0]]
    for argument in argv[1:]:
        found = PLACEHOLDER.fullmatch(argument)
        result.append(argument if found is None else values[found.group(1)])
    return tuple(result)


def describe(parameters: Mapping[str, RunParameter]) -> str:
    """The parameters as ``run_status`` lists them, such as ``test matching
    tests/[a-z_]+\\.py``, or nothing for a command without any."""
    return "; ".join(
        f"{name} matching {parameter.pattern}"
        + (f", {parameter.description}" if parameter.description else "")
        for name, parameter in sorted(parameters.items())
    )
