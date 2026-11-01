"""The project's disclosure list, and the check that keeps its terms out of messages.

A project may name a file of terms that must not leave the machine through Dromon, such
as private context or code names. ``[daemon] disclosure_terms`` in the project file
names it, relative to the project root. The daemon reads it at start and at each reload.
Each line is one term, matched as a fixed string without regard to case, and blank lines
and lines whose first non-blank character is ``#`` are ignored, as in
``scripts/check.sh``. A message, question or scheduled wake note that a line or an
external agent sends through Dromon's tools is refused when its text holds a term.
``docs/protocol/PROJECT_CONFIG.md`` is normative.

Secrecy. A term is never logged, returned or stored. A refusal names the matching
field without its value. File errors name the file, never its content.

Limits. Matching is by substring after Unicode case folding, so a term written another
way, split across messages, or encoded, passes. The check is a guard against accidents,
not against a line that tries to evade it. Operator messages are not checked, since the
operator holds authority.

Porting note. Case folding uses Python's ``str.casefold``, which follows the Unicode
full case folding. A port must use the same folding to match the same texts.
"""

from collections.abc import Iterable
from hashlib import sha256
from pathlib import Path
from typing import Final, cast

MAX_TERMS_FILE_BYTES: Final = 1024 * 1024
"""The largest disclosure list read, so a mistaken path cannot exhaust memory."""


REFUSAL: Final = "the text contains a term from the project's disclosure list"
"""What a refusal says when it cannot name the field, so it never reveals which term
or which field matched."""


def field_refusal(fields: Iterable[str]) -> str:
    """Name the call fields holding a listed term, without revealing the term."""
    names = tuple(dict.fromkeys(fields))
    if not names:
        raise ValueError("a disclosure refusal must name at least one field")
    joined = names[0] if len(names) == 1 else " and ".join(names)
    noun, verb = ("field", "contains") if len(names) == 1 else ("fields", "contain")
    return f"the {joined} {noun} {verb} a term from the project's disclosure list"


def refusal(matched: Iterable[str], offered: int) -> str:
    """Name a field only when the call offered text in that one field.

    Round 4 review F1 bounds each counted refusal to one bit. Calls offering
    several fields receive the generic refusal even if only one field matches.
    """
    names = tuple(dict.fromkeys(matched))
    if not names:
        raise ValueError("a disclosure refusal must name at least one field")
    if offered == 1 and len(names) == 1:
        return field_refusal(names)
    return REFUSAL


class DisclosureError(ValueError):
    """The disclosure list cannot be read. The message names the file only."""


def load_terms(path: Path) -> tuple[str, ...]:
    """The terms in the list at ``path``, case folded, or ``DisclosureError``."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_TERMS_FILE_BYTES + 1)
    except OSError as error:
        reason = error.strerror or type(error).__name__
        raise DisclosureError(
            f"the disclosure list {path} cannot be read: {reason}"
        ) from error
    if len(raw) > MAX_TERMS_FILE_BYTES:
        raise DisclosureError(
            f"the disclosure list {path} is larger than {MAX_TERMS_FILE_BYTES} bytes"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DisclosureError(f"the disclosure list {path} is not UTF-8") from error
    terms: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        terms.add(stripped.casefold())
    return tuple(sorted(terms))


def discloses(terms: Iterable[str], texts: Iterable[str]) -> bool:
    """Whether any of ``texts`` holds any of ``terms``, which are case folded."""
    folded = [text.casefold() for text in texts]
    return any(term in text for term in terms for text in folded)


def accepted_entries(raw: object) -> dict[str, tuple[str, ...]]:
    """Validate operator supplied paths and digests without reading their contents."""
    if not isinstance(raw, dict) or len(cast(dict[object, object], raw)) > 256:
        raise DisclosureError(
            "disclosure_accepted must be a table of at most 256 paths"
        )
    result: dict[str, tuple[str, ...]] = {}
    for path, hashes in cast(dict[object, object], raw).items():
        if (
            not isinstance(path, str)
            or not path
            or Path(path).is_absolute()
            or ".." in Path(path).parts
            or "\\" in path
            or "\x00" in path
        ):
            raise DisclosureError(
                "disclosure_accepted needs project relative file paths"
            )
        if (
            not isinstance(hashes, list)
            or not hashes
            or len(cast(list[object], hashes)) > 256
            or any(
                not isinstance(digest, str)
                or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)
                for digest in cast(list[object], hashes)
            )
        ):
            raise DisclosureError(
                "disclosure_accepted needs lists of lowercase SHA-256 digests"
            )
        result[path] = tuple(cast(list[str], hashes))
    return result


def matching_lines(
    terms: Iterable[str], text: str, accepted: Iterable[str] = ()
) -> int:
    """Count matching lines, exempting exact operator accepted line digests.

    No file or line locations are returned. An exemption applies only when the
    caller supplied the digests for the checked file, never for arbitrary text.
    """
    listed = tuple(terms)
    exemptions = frozenset(accepted)
    return sum(
        1
        for line in text.splitlines()
        if sha256(line.removesuffix("\r").encode("utf-8")).hexdigest() not in exemptions
        and any(term in line.casefold() for term in listed)
    )
