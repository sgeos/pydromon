"""The project's disclosure list, and the check that keeps its terms out of messages.

A project may name a file of terms that must not leave the machine through Dromon, such
as private context or code names. ``[daemon] disclosure_terms`` in the project file
names it, relative to the project root. The daemon reads it at start and at each reload.
Each line is one term, matched as a fixed string without regard to case, and blank lines
and lines whose first non-blank character is ``#`` are ignored, as in
``scripts/check.sh``. A message, question or scheduled wake note that a line or an
external agent sends through Dromon's tools is refused when its text holds a term.
``docs/protocol/PROJECT_CONFIG.md`` is normative.

Secrecy. A term is never logged, returned or stored. A refusal says only that the text
holds a term from the list, and errors about the file name the file, never its content.

Limits. Matching is by substring after Unicode case folding, so a term written another
way, split across messages, or encoded, passes. The check is a guard against accidents,
not against a line that tries to evade it. Operator messages are not checked, since the
operator holds authority.

Porting note. Case folding uses Python's ``str.casefold``, which follows the Unicode
full case folding. A port must use the same folding to match the same texts.
"""

from collections.abc import Iterable
from pathlib import Path
from typing import Final

MAX_TERMS_FILE_BYTES: Final = 1024 * 1024
"""The largest disclosure list read, so a mistaken path cannot exhaust memory."""

REFUSAL: Final = "the text contains a term from the project's disclosure list"
"""The whole of what a refusal says, so that it never reveals which term matched."""


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


def matching_lines(terms: Iterable[str], text: str) -> int:
    """How many lines of ``text`` hold any of ``terms``, which are case folded. The
    count says how much of a text to look at and never which term matched. Since
    ``load_terms`` reads each term from one line, no term holds a line break, so the
    count is 0 exactly when ``discloses`` finds nothing."""
    listed = tuple(terms)
    return sum(
        1
        for line in text.casefold().splitlines()
        if any(term in line for term in listed)
    )
