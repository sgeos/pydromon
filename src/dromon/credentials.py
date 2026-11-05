"""Credential-shaped values masked in the text Dromon records or relays.

**Round 15, item 1.** Another project saw a temporary credential from a hosting service
printed in tool output. ``redact`` masks only command lines. This module masks the
free text that enters the event log, which every view, status, export and backup of the
log shows: mail bodies, questions, notes, wake notes, briefs, run and task summaries,
and the reasons the daemon records. The one place is ``EventLog.append``, through
``scrub_event``, so a text is masked before it is stored and before the state applies
it, and every view built from the state is masked by construction.

What is masked, in a text, each by pattern.
- Values with a known prefix and length, such as GitHub, GitLab, Slack, Stripe,
  SendGrid, npm, PyPI, Hugging Face, DigitalOcean, Google and Anthropic or OpenAI keys,
  Amazon access key identifiers and the session token an Amazon temporary credential
  carries, JSON web tokens, and the body of a private key block.
- The credential after ``Bearer`` or ``Basic``, and the user and password of a URL,
  or of ``user:password@host`` with no scheme. A password with ``@`` is masked whole.
  One with ``/`` is masked in a URL and not without a scheme, where it cannot be told
  from a path.
- The value of an upper case ``NAME=value`` pair whose name says token, secret,
  password, credential, access key, private key, API key or session, as an environment
  dump prints it.
- The value of a JSON member whose name says the same, as a service's reply prints it.
- The literal value of any environment variable a project grants a line by name in its
  ``secrets`` list, wherever the value appears, when it has at least
  ``MIN_SECRET_VALUE`` characters.

What is not covered. **Never masked here** is the engine's own context, which Dromon
does not hold, so a line that reads a credential in its own tool output still holds it,
and may write it into a file or a mail, where only this module's patterns stand between
it and the log. The raw output file of a supervised run, the files a line writes, the
launcher's report file and the engine's own transcript are not scrubbed, because
Dromon does not author them and the log holds only their digest. A credential with no
known prefix, no ``NAME=value`` shape and no listed name is recorded as it is. A secret
shorter than ``MIN_SECRET_VALUE`` is not matched by value, since a short value would
mask ordinary words. A value split across two texts is not matched.

False positives. The patterns need a prefix and a length, or an upper case name, so
a commit name, a digest, a UUID and prose do not match. A text that really holds such a
value as an example is masked too, which fails safe.

Failure modes. **Review of 03ccbf3.** A secret source that raises fails closed. An
event that has a covered text field is refused with ``SecretsUnavailable``, which is an
``EventRejected``, and nothing is stored, because storing it masked by pattern alone
would record a literal value the patterns do not know, with nothing to say so. The
source is asked only for an event that has a covered field, so every other event is
stored as before. The source reads a dictionary and the environment, so it fails only
when a reload changes the dictionary as it is read, and the next append succeeds.
Masking changes the stored text and never the request digest of a keyed call, which is
taken over the arguments as given, so a retry is unaffected.

Porting note. The patterns are plain regular expressions, which a port carries across
unchanged, and ``scrub_event`` is a table of event classes and their text fields.
"""

import dataclasses
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Final, cast

from dromon import model
from dromon.model import Event
from dromon.redact import MASK

MIN_SECRET_VALUE: Final = 8
"""The shortest literal secret value matched by value."""

MAX_SECRET_VALUES: Final = 256
"""The most literal values matched, so the work for a text is bounded."""

_NAME_WORDS: Final = (
    r"(?:TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|ACCESS_KEY|PRIVATE_KEY|API_KEY"
    r"|SESSION)"
)

_NAME_WORDS_LOWER: Final = (
    r"(?:token|secret|password|passwd|credential|access[_-]?key|private[_-]?key"
    r"|api[_-]?key|session)"
)

_JSON_WORDS: Final = (
    r"(?:token|secret|password|passwd|credential|apikey|api_key|accesskey|access_key"
    r"|sessiontoken|session_token|private_key|privatekey)"
)

_PREFIXED: Final = (
    r"gh[pousr]_[A-Za-z0-9]{36,}"
    r"|github_pat_[A-Za-z0-9_]{22,}"
    r"|glpat-[A-Za-z0-9_-]{20,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|sk-ant-[A-Za-z0-9_-]{20,}"
    r"|sk-(?:proj-)?[A-Za-z0-9_-]{32,}"
    r"|[rs]k_live_[A-Za-z0-9]{16,}"
    r"|SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"
    r"|npm_[A-Za-z0-9]{36}"
    r"|pypi-AgEIcHlwaS5vcmc[A-Za-z0-9_-]{40,}"
    r"|hf_[A-Za-z0-9]{30,}"
    r"|dop_v1_[a-f0-9]{64}"
    r"|AIza[0-9A-Za-z_-]{35}"
    r"|ya29\.[0-9A-Za-z_-]{20,}"
    r"|(?:AKIA|ASIA)[0-9A-Z]{16}"
    r"|IQoJb3JpZ2lu[A-Za-z0-9+/=_-]{40,}"
    r"|FwoGZXIvYXdz[A-Za-z0-9+/=_-]{40,}"
    r"|eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
)

_PATTERNS: Final = (
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
            r".*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
            re.DOTALL,
        ),
        MASK,
    ),
    (re.compile(rf"(?<![A-Za-z0-9_-])(?:{_PREFIXED})(?![A-Za-z0-9])"), MASK),
    (re.compile(r"(?i)\b(bearer|basic)(\s+)[A-Za-z0-9._~+/=-]{8,}"), rf"\1\2{MASK}"),
    (
        # The user and password of a URL. The password runs to the last ``@`` of the
        # word, so one that holds ``/`` or ``@`` is masked whole, at the price of also
        # masking what lies between the host and a later ``@`` of the same word. A
        # password that begins with up to five digits and a ``/`` reads as a port and
        # a path, as in ``host:8080/pkg@1.0``, and is not matched. **Review of
        # 03ccbf3.**
        re.compile(
            r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@:]+:(?!\d{1,5}/)\S+@"
            r"(?!sha\d+:)(?=[A-Za-z0-9\[])"
        ),
        rf"\1{MASK}@",
    ),
    (
        # A user and password before a host with no scheme, as a connection string
        # or a remote shorthand prints it. An image reference with a digest is kept.
        # The password may hold ``@`` and runs to the last one of the word. One that
        # holds ``/`` is not matched, since ``host:dir/file@name`` is ordinary text.
        # **Review of 03ccbf3.**
        re.compile(
            r"(?<![\w/:@.*-])[^\s/@:'\"<>*]{1,64}:[^\s/:'\"<>*]{3,128}"
            r"@(?!sha\d+:)(?=[A-Za-z0-9])"
        ),
        f"{MASK}@",
    ),
    (
        # The same names in lower case or mixed case, such as ``github_token=...`` or
        # ``--api-key=...``, which the upper case pattern below does not reach.
        re.compile(
            rf"(?i)(?<![A-Za-z0-9_])([a-z0-9_.-]*{_NAME_WORDS_LOWER}[a-z0-9_.-]*=)"
            r"[^\s'\"]{8,}"
        ),
        rf"\1{MASK}",
    ),
    (
        re.compile(rf"\b([A-Z][A-Z0-9_]*{_NAME_WORDS}[A-Z0-9_]*=)[^\s'\"]{{8,}}"),
        rf"\1{MASK}",
    ),
    (
        re.compile(
            rf'(?i)("[A-Za-z0-9_]*{_JSON_WORDS}[A-Za-z0-9_]*"\s*:\s*")[^"\s]{{12,}}(")'
        ),
        rf"\1{MASK}\2",
    ),
)


class SecretsUnavailable(model.EventRejected):
    """The literal secret values could not be read, so an event with text was not
    stored. Retrying later may succeed. The message never names a value."""


def has_text(event: Event) -> bool:
    """Whether ``scrub_event`` looks at any field of ``event``."""
    return type(event) in COVERED


type SecretSource = Callable[[], Iterable[str]]
"""Where the literal secret values come from, asked at each scrub."""


def scrub_text(text: str, secrets: Iterable[str] = ()) -> str:
    """``text`` with every credential-shaped value masked, and every literal value of
    ``secrets`` of at least ``MIN_SECRET_VALUE`` characters. Idempotent."""
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    values = sorted(
        {value for value in secrets if len(value) >= MIN_SECRET_VALUE},
        key=len,
        reverse=True,
    )
    for value in values[:MAX_SECRET_VALUES]:
        text = text.replace(value, MASK)
    return text


def environment_values(
    granted: Mapping[str, Sequence[str]], environment: Mapping[str, str]
) -> tuple[str, ...]:
    """The values, in ``environment``, of every name any line is granted in
    ``granted``, the project's ``secrets`` lists. A name the environment lacks is
    skipped, and an empty value is skipped as too short to match. The tuple is cut to
    ``MAX_SECRET_VALUES``."""
    found: set[str] = set()
    for names in granted.values():
        for name in names:
            value = environment.get(name)
            if value:
                found.add(value)
    return tuple(sorted(found))[:MAX_SECRET_VALUES]


COVERED: Final[dict[type, tuple[str, ...]]] = {
    model.MessageSent: ("body", "choices"),
    model.MessageReceived: ("body",),
    model.MessageAcknowledged: ("reason",),
    model.WakeScheduled: ("note",),
    model.TaskNoted: ("body",),
    model.TaskRecorded: ("command",),
    model.RestartNoted: ("body",),
    model.StopUnverified: ("note",),
    model.WalkLimited: ("note",),
    model.ProcessesLeft: ("commands",),
    model.RoleNoted: ("body",),
    model.BriefSet: ("task", "decisions", "artifacts", "next_action"),
    model.BudgetStated: ("note",),
    model.LeaseAcquired: ("note",),
    model.RunEnded: ("note",),
    model.CommitRunRefused: ("reason",),
    model.RunStarted: ("argv",),
}
"""The events with free text, and the fields of each that are scrubbed. **Round 15,
review of 3344ca6.** ``RunStarted.argv`` is added. It is the project file's argument
vector for a run, and the run is launched from the project file and never from the
event, so masking the recorded copy changes only what a view shows.

Fields with text that are deliberately **not** scrubbed.
- ``LineConfigured.config``, ``LineOverridden.overrides`` and
  ``ProjectOverridden.overrides`` are canonical JSON that the daemon decodes again to
  build a line's engine, so a mask inside them would change the configuration the line
  runs under. The values a configuration may carry as credentials, the environment and
  headers of an MCP server, are taken out into the sealed store before the event is
  written, as ``dromon.sealed`` says. A credential written into another place, such as
  the instructions, is recorded as written, and the operator is told so.
- ``PeerDeclared.policy`` is written by the federation module and never holds a token.
- ``RunStarted.command``, ``CommitRunRefused.command`` and ``LeaseReleased.name`` are
  names the project file declares, checked as identifiers, not free text.
"""


def scrub_event(event: Event, secrets: Iterable[str] = ()) -> Event:
    """``event`` with the text fields ``COVERED`` names scrubbed, or ``event`` itself
    when nothing changed or its class has none. A field is a text, a tuple of texts or
    ``None``."""
    fields = COVERED.get(type(event))
    if fields is None:
        return event
    held = tuple(secrets)
    changes: dict[str, object] = {}
    for name in fields:
        value: object = getattr(event, name)
        if isinstance(value, str):
            found = scrub_text(value, held)
            if found != value:
                changes[name] = found
        elif isinstance(value, tuple):
            elements = cast("tuple[object, ...]", value)
            items = tuple(
                scrub_text(item, held) if isinstance(item, str) else item
                for item in elements
            )
            if items != elements:
                changes[name] = items
    if not changes:
        return event
    try:
        return dataclasses.replace(event, **changes)  # pyright: ignore[reportArgumentType]
    except model.InvalidField as error:
        # Review of 3344ca6. Two choices that differed only by a credential are equal
        # once masked, and an event that repeats a choice is refused, not stored. That
        # is right, since an answer names a choice by its text and two equal choices
        # cannot be told apart, and storing one of them would drop a choice silently.
        # The refusal says why, without echoing a value.
        raise model.InvalidField(
            f"after credential-shaped values were masked, {error}. Do not put a"
            " credential in this text"
        ) from error
