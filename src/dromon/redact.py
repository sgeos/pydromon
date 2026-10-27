"""Command lines and task descriptions as Dromon records them, with secrets masked.

**Review N9 and N10 of the stop policy's re-review.** A process an engine left running,
and a background task's command or description, are recorded in the event log, which
persists, and shown in a line's status. A command line can carry a secret, as
``curl -H "Authorization: Bearer ..."`` or ``git clone https://user:pass@host/...``
does. So the log and the status record only the executable's name and an argument
summary with the forms below masked, and the whole command stays only in the
launcher's report file, which its owner alone can read.

Masked, each case-insensitive.
- The credential after an ``Authorization`` scheme, ``Bearer`` or ``Basic``.
- The user and password in a URL, as ``scheme://user:pass@``.
- The value of an option whose name holds ``password``, ``passwd``, ``pwd``,
  ``token``, ``key``, ``secret``, ``credential`` or ``auth``, given after ``=`` or as
  the next argument, and of a ``name=value`` pair whose name holds one of them, such
  as ``API_KEY=...`` or ``?token=...``.
- The value after a header name that holds one of them or ``cookie``, as
  ``X-Api-Key: ...``.

**Limits, stated plainly.** Masking is by pattern, so a secret in another form, such
as a bare positional argument, is recorded as it is. It also masks more than secrets,
such as a ``--keyboard`` option's value, which fails safe. It is not the disclosure
scan, which applies only where a project names terms. Porting note, the patterns are
plain regular expressions, which a port carries across unchanged.
"""

import re
from typing import Final

MASK: Final = "***"

MAX_SUMMARY_CHARACTERS: Final = 200
"""The longest argument summary recorded, after masking."""

SECRET_WORDS: Final = (
    "password",
    "passwd",
    "pwd",
    "token",
    "key",
    "secret",
    "credential",
    "auth",
)
"""What a name holds to have its value masked."""

_SECRET_NAME: Final = rf"[\w.-]*(?:{'|'.join(SECRET_WORDS)})[\w.-]*"

_PATTERNS: Final = (
    # The credential after an authorization scheme, before any rule below can take
    # the scheme's name for the value and leave the credential.
    (re.compile(r"(?i)\b(bearer|basic)(\s+)[^\s'\"]+"), rf"\1\2{MASK}"),
    (
        re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@"),
        rf"\1{MASK}@",
    ),
    (re.compile(rf"(?i)(\b{_SECRET_NAME}=)[^\s&'\"]+"), rf"\1{MASK}"),
    (re.compile(rf"(?i)(--?{_SECRET_NAME})(\s+)(?!-)[^\s'\"]+"), rf"\1\2{MASK}"),
    (
        re.compile(rf"(?i)\b((?:{_SECRET_NAME}|[\w-]*cookie[\w-]*):\s*)[^\s'\"]+"),
        rf"\1{MASK}",
    ),
)


def mask(text: str) -> str:
    """``text`` with every form the module names masked."""
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def summary(command: str) -> str:
    """What the log and a line's status record of a command line: the executable's
    name without its directory, and the masked arguments, cut to
    ``MAX_SUMMARY_CHARACTERS``. The whole line is masked before it is split, so a
    secret in the first word, such as ``TOKEN=... curl``, is masked too."""
    masked = mask(command.strip())
    first, _, rest = masked.partition(" ")
    name = first if "=" in first else first.rsplit("/", 1)[-1]
    cut = f"{name} {rest.strip()}".strip()
    return cut[:MAX_SUMMARY_CHARACTERS]
