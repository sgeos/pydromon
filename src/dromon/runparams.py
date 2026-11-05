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
**Round 15 review, P2.** Nor may it begin with ``/`` or hold a ``..`` step between
slashes, so a value never names a path outside the working directory by itself,
however wide the pattern. A program may still resolve a relative path through a link
in the worktree, which the line could plant, so a command that must not leave the
worktree must refuse links itself.

Porting note. Patterns use Python's ``re`` syntax, matched with ``fullmatch``. To keep
them portable to a Rust port, whose ``regex`` crate has neither lookaround nor
backreferences, a pattern with ``(?``, a backslash before a digit, or a possessive
quantifier is refused.

**Round 15 review, P1.** Python's ``re`` backtracks, and the audit measured
``([a-z]+)+`` holding the daemon's loop for 59 seconds on a value of 30 characters.
Two guards answer it. A pattern that repeats a group with ``*``, ``+`` or ``{`` is
refused when it is declared, which removes the usual exponential forms and is what a
port to a linear engine would accept. That does not remove them all, since a run of
optional items such as ``a?a?a?aaa`` or of alternatives such as ``(a|a)(a|a)`` still
backtracks exponentially, and four unbounded quantifiers in a row took 0.7 seconds on
256 characters. So the values are matched by a child process under a deadline of
``MATCH_SECONDS``, and a match that does not end in time is refused and its child
killed. ``re`` holds the interpreter lock while it matches, so a worker thread would
not free the loop, and a process does. **Porting note.** A port with a linear engine
needs neither the child nor the deadline, but must keep the refusal so the same
declarations load.
"""

import asyncio
import json
import re
import subprocess
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, cast

from dromon import spawn

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

MATCH_SECONDS: Final = 2.0
"""How long the child that matches a start's values may take. **Round 15 review,
P1.** A pattern without a repeated group matched 256 characters in under a second in
the audit's worst cases but one, so a deadline of two seconds refuses only a pattern
that backtracks, and bounds how long a start waits."""

MAX_MATCH_OUTPUT_BYTES: Final = 4096
"""The most the matching child may print, which bounds what is read from it."""

MATCHER: Final = (
    "import json, re, sys\n"
    "pairs = json.loads(sys.stdin.read())\n"
    "print(json.dumps([re.fullmatch(p, v) is not None for p, v in pairs]))\n"
)
"""The child's program, which reads pattern and value pairs and prints whether each
matched. It is run with ``-I``, so no environment variable or path of the daemon's
changes what it imports."""


class ParameterRefused(ValueError):
    """A declaration or a value that is refused, with the reason."""


@dataclass(frozen=True, slots=True)
class RunParameter:
    """One parameter a command declares."""

    name: str
    pattern: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class Prepared:
    """A command with its values in place, and the values still to be matched
    against their patterns, as name, pattern and value."""

    argv: tuple[str, ...]
    checks: tuple[tuple[str, str, str], ...]


def repeats_group(pattern: str) -> bool:
    """Whether ``pattern`` follows a group's closing parenthesis with ``*``, ``+``
    or ``{``. Escapes and character classes are skipped, so ``\\)+`` and ``[)]+``
    are not groups. A ``]`` first in a class, or after ``^``, is literal. Each step
    advances at least one character, so the loop ends within ``len(pattern)``
    steps."""
    index = 0
    length = len(pattern)
    in_class = False
    class_body = 0
    while index < length:
        char = pattern[index]
        if char == "\\":
            index += 2
            continue
        if in_class:
            if char == "]" and index > class_body:
                in_class = False
        elif char == "[":
            in_class = True
            class_body = index + 1
            if class_body < length and pattern[class_body] == "^":
                class_body += 1
        elif char == ")" and index + 1 < length and pattern[index + 1] in "*+{":
            return True
        index += 1
    return False


def check_pattern(pattern: str) -> re.Pattern[str]:
    """``pattern`` compiled, or ``ParameterRefused`` when it is empty, too long,
    unportable, repeats a group or is not a valid pattern. It is compiled and never
    matched here, which takes time linear in its length."""
    if not pattern or len(pattern.encode()) > MAX_PATTERN_BYTES:
        raise ParameterRefused(
            f"a pattern must be text of 1 to {MAX_PATTERN_BYTES} bytes"
        )
    if UNPORTABLE.search(pattern) is not None:
        raise ParameterRefused(
            "a pattern may not use (?, a backreference or a possessive quantifier,"
            " which a port could not match the same way"
        )
    if repeats_group(pattern):
        raise ParameterRefused(
            "a pattern may not repeat a group with *, + or {, which can take time"
            " exponential in a value's length to match. Repeat a character class"
            " instead, such as [a-z_/]+"
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


def check_fixed(parameter: RunParameter, value: object) -> str:
    """``value`` when it passes the rules the pattern cannot widen, or
    ``ParameterRefused``. The pattern is matched later, by ``match``."""
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
    if value.startswith("/") or ".." in value.split("/"):
        raise ParameterRefused(
            f"{name} may not begin with / or hold a .. step, so it never names a"
            " path outside the working directory"
        )
    return value


def prepare(
    argv: Sequence[str],
    parameters: Mapping[str, RunParameter],
    given: Mapping[str, object] | None,
) -> Prepared:
    """``argv`` with each placeholder replaced by the value ``given`` names for it,
    and the values still to match, or ``ParameterRefused`` for a value that is
    missing, extra or refused by the fixed rules. A command without parameters takes
    none. Nothing here matches a pattern, so it takes time linear in its input."""
    supplied = dict(given or {})
    if not parameters:
        if supplied:
            raise ParameterRefused("the command takes no arguments")
        return Prepared(tuple(argv), ())
    extra = sorted(set(supplied) - set(parameters))
    if extra:
        raise ParameterRefused(f"the command has no parameter {', '.join(extra)}")
    missing = sorted(set(parameters) - set(supplied))
    if missing:
        raise ParameterRefused(f"the command needs arguments {', '.join(missing)}")
    values = {
        name: check_fixed(parameter, supplied[name])
        for name, parameter in sorted(parameters.items())
    }
    result = [argv[0]]
    for argument in argv[1:]:
        found = PLACEHOLDER.fullmatch(argument)
        result.append(argument if found is None else values[found.group(1)])
    checks = tuple(
        (name, check_pattern(parameter.pattern).pattern, values[name])
        for name, parameter in sorted(parameters.items())
    )
    return Prepared(tuple(result), checks)


async def match(prepared: Prepared, seconds: float = MATCH_SECONDS) -> tuple[str, ...]:
    """The prepared command when every value matches its pattern, or
    ``ParameterRefused`` naming the first that does not, or when the child that
    matches them does not end within ``seconds``, or fails. The child is killed
    and reaped on every path out, so none outlives the call. A command without
    parameters starts no child."""
    if not prepared.checks:
        return prepared.argv
    payload = json.dumps(
        [[pattern, value] for _, pattern, value in prepared.checks]
    ).encode()
    matching = _Matching()
    # Review of the merge of the ops work. ``asyncio.create_subprocess_exec`` runs
    # ``Popen`` on the event loop's own thread, where a start that stalls in the
    # exec-pipe read would stop the whole daemon. The child's whole life, its start by
    # ``spawn.start``, which bounds it, its match, its kill and its reap, runs on one
    # worker thread, and the loop stays free meanwhile.
    work = asyncio.ensure_future(
        asyncio.to_thread(_run_matcher, payload, seconds, matching)
    )
    try:
        output, code, timed_out = await asyncio.shield(work)
    except asyncio.CancelledError:
        # Fix of 2026-11-05. A cancellation that arrived after the child started but
        # before the worker handed it back left it running with its pipes open, as
        # core's full gate showed. The child is now killed wherever it is, and the
        # cancellation goes on only once the worker has reaped it and closed its pipes.
        matching.abandon()
        await _settle(work, matching)
        raise
    except (OSError, TimeoutError) as error:
        # A start that failed or stalled refuses, as a child that fails does, so a
        # broken start never lets a value through.
        raise ParameterRefused(
            "the values could not be matched against their patterns"
        ) from error
    if timed_out:
        raise ParameterRefused(
            f"the values took longer than {seconds:g} seconds to match their"
            " patterns, so they are refused. A pattern with many optional items or"
            " alternatives in a row can take time exponential in a value's length"
        )
    _check_answers(prepared, output, code)
    return prepared.argv


@dataclass(slots=True)
class _Matching:
    """The matching child, shared by the worker that owns it and a cancelled call
    that asks it to be killed. Both read and write it under ``lock``, so a kill asked
    for before the start ends is seen by the worker once it does."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    child: subprocess.Popen[bytes] | None = None
    abandoned: bool = False

    def abandon(self) -> None:
        """Kill the child now if it has started, or have the worker kill it as soon
        as it starts. ``Popen.kill`` does nothing to a child already reaped."""
        with self.lock:
            self.abandoned = True
            child = self.child
        if child is not None:
            child.kill()


_Outcome = tuple[bytes, int | None, bool]
"""What the matching child printed, its exit code, and whether it outran its
deadline."""


def _run_matcher(payload: bytes, seconds: float, matching: _Matching) -> _Outcome:
    """Start the matching child, give it ``payload`` and return what it printed, its
    exit code and whether it outran ``seconds``. On every path out the child is
    killed if it still runs, its pipes are closed and it is reaped, by
    ``Popen.__exit__``, so none outlives this call. A start that fails or stalls
    raises, and ``spawn`` reaps a start that ends late."""
    child = spawn.start(
        [sys.executable, "-I", "-c", MATCHER],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    with matching.lock:
        matching.child = child
        abandoned = matching.abandoned
    with child:
        try:
            if abandoned:
                child.kill()
            output, _ = child.communicate(payload, timeout=seconds)
        except subprocess.TimeoutExpired:
            return b"", None, True
        finally:
            # The kill ends a match that outran its deadline or was abandoned, so
            # the wait in ``__exit__`` returns at once.
            child.kill()
    return output, child.returncode, False


async def _settle(work: asyncio.Task[_Outcome], matching: _Matching) -> None:
    """Wait for the worker of a cancelled match to end, killing the child again on
    each further cancellation, and drop its result, which no caller reads. The
    worker ends within the start bound and the match deadline, so the loop ends."""
    while not work.done():
        try:
            await asyncio.wait({work})
        except asyncio.CancelledError:
            matching.abandon()
    if not work.cancelled():
        work.exception()


def _check_answers(prepared: Prepared, output: bytes, code: int | None) -> None:
    """Return when the child's ``output`` says every value matched, or raise
    ``ParameterRefused`` naming the first that did not, or when the child failed or
    printed anything but one answer for each value, so a broken child refuses every
    start."""
    results: object = None
    if code == 0 and len(output) <= MAX_MATCH_OUTPUT_BYTES:
        try:
            results = json.loads(output)
        except ValueError:
            results = None
    if not isinstance(results, list):
        raise ParameterRefused("the values could not be matched against their patterns")
    answers = cast(list[object], results)
    if len(answers) != len(prepared.checks) or any(
        type(answer) is not bool for answer in answers
    ):
        raise ParameterRefused("the values could not be matched against their patterns")
    for (name, pattern, _), answer in zip(prepared.checks, answers, strict=True):
        if answer is not True:
            raise ParameterRefused(f"{name} does not match its pattern {pattern}")


def describe(parameters: Mapping[str, RunParameter]) -> str:
    """The parameters as ``run_status`` lists them, such as ``test matching
    tests/[a-z_]+\\.py``, or nothing for a command without any."""
    return "; ".join(
        f"{name} matching {parameter.pattern}"
        + (f", {parameter.description}" if parameter.description else "")
        for name, parameter in sorted(parameters.items())
    )
