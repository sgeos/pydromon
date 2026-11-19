"""The display name a Claude Code session Dromon starts is given, so an operator can
tell the sessions on the machine apart.

**Round 19, the operator's direction.** Claude Code names a session from its working
directory unless it is given ``--name``, so every line in one worktree shows under the
same kind of name. Dromon passes ``--name`` through the Agent SDK's ``extra_args`` on
every start and resume, built from the project file's ``[daemon] session_name``
template. ``docs/protocol/PROJECT_CONFIG.md`` is normative.

The template is literal characters and fields in braces. The default is
``DEFAULT_TEMPLATE``, which gives names such as ``PDr00-pydromon-review-luna-Ha55lo``.

- ``{vv}``, the installed pydromon's major version followed by its minor version,
  each in decimal, so 0.0.0 gives ``00`` and 1.2.3 gives ``12``.
- ``{project}``, the last step of the project root, the name the fleet index gives the
  project.
- ``{role}``, the role the line is a seat of, or the line's identifier when it is a
  seat of none.
- ``{variant-}``, empty, or the variant followed by ``-``. The variant is the part of
  the line identifier beyond the role, as ``variant_of`` says, with digits appended
  when the name would equal another live session's, as ``unique_name`` says.
- ``{model}``, the model's code and version, ``Op55`` for ``claude-opus-5-5``.
- ``{effort}``, the effort's code, ``hi`` for high, or empty when no effort is known.

Every name is checked against ``NAME_PATTERN``, so it is one argument, never begins with
``-`` and is at most ``MAX_NAME_CHARS`` characters, and against the disclosure list. A
name that fails either is not given, and the session keeps Claude Code's own name.

**Porting note.** Plain string work and a bounded search, with no dynamic features.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from dromon import disclosure

DEFAULT_TEMPLATE: Final = "PDr{vv}-{project}-{role}-{variant-}{model}{effort}"
"""The operator's pattern of round 19."""

FIELDS: Final = frozenset({"vv", "project", "role", "variant-", "model", "effort"})
"""The fields a template may name, each at most once."""

MAX_TEMPLATE_CHARS: Final = 128
MAX_NAME_CHARS: Final = 96
"""The longest name given. **Assumption.** No limit of Claude Code's is known to
Dromon, so the bound is Dromon's own, chosen to keep a name readable in a list of
sessions. A line or role identifier may be 64 characters, so a name can exceed it. A
longer name is not given, which fails toward Claude Code's own name."""

LITERAL_PATTERN: Final = re.compile(r"[A-Za-z0-9._-]*")
"""The characters a template's literal text may hold, which are safe in one argument."""

NAME_PATTERN: Final = re.compile(
    rf"[A-Za-z0-9][A-Za-z0-9._-]{{0,{MAX_NAME_CHARS - 1}}}"
)
"""A name that is one argument, begins with no ``-``, and is bounded."""

MODEL_CODES: Final[Mapping[str, str]] = {
    "opus": "Op",
    "haiku": "Ha",
    "fable": "Fa",
    "sonnet": "Sn",
}
EFFORT_CODES: Final[Mapping[str, str]] = {
    "low": "lo",
    "medium": "me",
    "high": "hi",
    "xhigh": "xh",
    "max": "mx",
}

_MODEL = re.compile(r"claude-([a-z]+)-([0-9]{1,2})(?:-([0-9]{1,2}))?")
_VERSION = re.compile(r"([0-9]+)\.([0-9]+)(?:\.[0-9].*)?")
_FIELD = re.compile(r"\{([^{}]*)\}")


class SessionNameError(ValueError):
    """A template that cannot give a safe name."""


@dataclass(frozen=True, slots=True)
class Template:
    """A checked template, as its literal text and fields in order."""

    text: str
    parts: tuple[tuple[bool, str], ...]
    """Each part, ``(True, field)`` for a field and ``(False, text)`` for literal."""


def parse_template(text: str) -> Template:
    """``text`` as a ``Template``, or ``SessionNameError``.

    A template is at most ``MAX_TEMPLATE_CHARS`` characters of literal text from
    ``LITERAL_PATTERN`` and fields from ``FIELDS``, each at most once. It must hold
    ``{variant-}``, since that is where the digits that tell two equal names apart
    go, and must not begin with ``-``."""
    if not text or len(text) > MAX_TEMPLATE_CHARS:
        raise SessionNameError(
            f"must be a template of 1 to {MAX_TEMPLATE_CHARS} characters"
        )
    if text.startswith("-"):
        raise SessionNameError("must not begin with -, so a name is never an option")
    parts: list[tuple[bool, str]] = []
    seen: set[str] = set()
    start = 0
    for found in _FIELD.finditer(text):
        parts.append((False, text[start : found.start()]))
        name = found.group(1)
        if name not in FIELDS:
            raise SessionNameError(
                f"names an unknown field {{{name}}}. Known fields are"
                f" {', '.join('{' + field + '}' for field in sorted(FIELDS))}"
            )
        if name in seen:
            raise SessionNameError(f"names the field {{{name}}} twice")
        seen.add(name)
        parts.append((True, name))
        start = found.end()
    parts.append((False, text[start:]))
    for is_field, part in parts:
        if not is_field and LITERAL_PATTERN.fullmatch(part) is None:
            raise SessionNameError(
                "holds a character other than a letter, a digit, '.', '_' or '-'"
                " outside a field"
            )
    if "variant-" not in seen:
        raise SessionNameError(
            "must hold {variant-}, where digits tell two equal names apart"
        )
    return Template(text, tuple((f, p) for f, p in parts if f or p))


DEFAULT: Final = parse_template(DEFAULT_TEMPLATE)


def version_code(version: str) -> str | None:
    """The major version followed by the minor version, or ``None`` for a version
    that does not begin with two numbers. 0.0.0 gives ``00``.

    **Residual.** Once a major or minor version reaches 10, the code is longer than
    two digits and two versions can give one code, such as 1.12 and 11.2."""
    found = _VERSION.fullmatch(version)
    return None if found is None else found.group(1) + found.group(2)


def model_code(model: str) -> str | None:
    """The code and version of a Claude model, ``Op55`` for ``claude-opus-5-5``, or
    ``None`` for a model with no code."""
    found = _MODEL.fullmatch(model)
    if found is None or found.group(1) not in MODEL_CODES:
        return None
    return MODEL_CODES[found.group(1)] + found.group(2) + (found.group(3) or "")


def effort_code(effort: str | None) -> str:
    """The code of ``effort``, or empty when it is unknown."""
    return "" if effort is None else EFFORT_CODES.get(effort, "")


def variant_of(line_id: str, role: str | None) -> str:
    """The part of ``line_id`` that is not ``role``, or empty for a line that is the
    role's own or a seat of none.

    A seat ``review-luna`` of role ``review`` gives ``luna``, and so does
    ``luna-review``. A seat whose identifier holds no ``role-`` prefix and no ``-role``
    suffix gives its whole identifier, since no part of it is the role's."""
    if role is None or line_id == role:
        return ""
    if line_id.startswith(role + "-"):
        return line_id[len(role) + 1 :]
    if line_id.endswith("-" + role):
        return line_id[: -len(role) - 1]
    return line_id


@dataclass(frozen=True, slots=True)
class Named:
    """The name, or ``None`` with the reason no name is given."""

    name: str | None
    reason: str = ""


def render(template: Template, values: Mapping[str, str]) -> str:
    """``template`` with each field replaced by its value in ``values``."""
    return "".join(
        values[part] if is_field else part for is_field, part in template.parts
    )


def session_name(
    template: Template,
    *,
    version: str,
    project: str,
    line_id: str,
    role: str | None,
    model: str,
    effort: str | None,
    taken: Iterable[str],
    terms: Iterable[str],
) -> Named:
    """The name of a session of ``line_id``, unique among ``taken``, the names of the
    other live sessions Dromon started, or no name with the reason.

    When the name equals one taken, digits are appended to the variant, the fewest
    that make it unique, trying 1, 2 and on in order. A role's own line with no variant
    then gets the digits alone as its variant. There are at most ``len(taken)`` names
    to avoid, so the search ends within ``len(taken) + 1`` tries.

    The reason never holds a disclosure term or the name refused for one."""
    vv = version_code(version)
    if vv is None:
        return Named(None, f"the installed version {version!r} has no major and minor")
    code = model_code(model)
    if code is None:
        return Named(None, f"the model {model!r} has no session name code")
    variant = variant_of(line_id, role)
    values = {
        "vv": vv,
        "project": project,
        "role": line_id if role is None else role,
        "model": code,
        "effort": effort_code(effort),
    }
    others = frozenset(taken)
    listed = tuple(terms)
    name = ""
    # The tries give distinct names, since only the variant differs, so at most
    # len(others) of them are taken and the loop ends at a free one.
    for suffix in range(len(others) + 1):
        tried = variant + ("" if suffix == 0 else str(suffix))
        name = render(template, {**values, "variant-": tried + "-" if tried else ""})
        if name not in others:
            break
    if NAME_PATTERN.fullmatch(name) is None:
        return Named(
            None,
            "the name would not be one argument of at most"
            f" {MAX_NAME_CHARS} letters, digits, '.', '_' and '-'",
        )
    if disclosure.discloses(listed, (name,)):
        return Named(None, "the name holds a term from the project's disclosure list")
    return Named(name)
