"""Forwarding of messages addressed to operators, through pluggable relays.

An operator may be reached through any channel, such as email, a chat service or a
client's question queue. A relay is the adapter for one channel. The daemon routes each
operator to a relay, forwards every undelivered message addressed to that operator, and
records delivery in the event log only after the relay accepts the message.

Delivery is at least once. If the process stops after a relay accepts a message but
before the log records its delivery, the next pass forwards it again. Every notice
carries its message identifier, so a channel or a reader can recognize a repeat.

Trust boundaries.

- Notice bodies are untrusted text from session lines. A relay must pass them to its
  channel as data and never interpret them as commands or markup beyond what the
  channel itself displays.
- Routes and relay configuration are operator configuration, trusted like the daemon's
  own settings. ``CommandRelay`` runs only the argument list it is configured with and
  never passes text through a shell.
- Relays are outbound only for now. Every adapter must eventually support a response
  path, so an operator can reply through the same channel. A reply is attributable to
  an operator only after that path authenticates them. ``docs/protocol/RELAY.md``
  states the required properties, and their design is technical debt.
"""

import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from dromon.eventlog import EventLog
from dromon.engine_environment import BASE_ENVIRONMENT, allowlisted
from dromon.model import (
    MessageDelivered,
    MessageKind,
    OperatorAddress,
    State,
)

NOTICE_VERSION: Final = 1


@dataclass(frozen=True, slots=True)
class Notice:
    """One message for one operator, as a relay receives it."""

    message_id: str
    sender: str
    operator_id: str
    kind: MessageKind
    body: str
    choices: tuple[str, ...]
    reply_to: str | None


class RelayError(Exception):
    """A relay could not hand a notice to its channel. The notice stays pending."""


class Relay(Protocol):
    """The adapter for one channel to operators."""

    def forward(self, notice: Notice) -> None:
        """Hand ``notice`` to the channel. Returning means the channel accepted it.

        Raise ``RelayError`` for a failure that should leave the notice pending. Any
        other exception is a defect in the relay and propagates to the caller.
        """
        ...


@dataclass(frozen=True, slots=True)
class ForwardReport:
    delivered: tuple[str, ...]
    failed: tuple[tuple[str, str], ...]
    """Each failed message identifier with the relay's reason."""
    unrouted: tuple[str, ...]
    """Messages whose operator has no relay. They stay pending."""


def pending_notices(state: State) -> list[Notice]:
    """Every undelivered message addressed to an operator, in the order sent."""
    notices: list[Notice] = []
    for message_id, message in state.messages.items():
        recipient = message.recipient
        if message.delivered or not isinstance(recipient, OperatorAddress):
            continue
        notices.append(
            Notice(
                message_id,
                str(message.sender),
                recipient.operator_id,
                message.kind,
                message.body,
                message.choices,
                message.reply_to,
            )
        )
    return notices


def forward_pending(log: EventLog, routes: Mapping[str, Relay]) -> ForwardReport:
    """Forward each pending operator message through its operator's relay.

    One failing relay does not stop the others. Delivery is appended to ``log`` after
    each acceptance, so a later failure cannot undo an earlier record.
    """
    delivered: list[str] = []
    failed: list[tuple[str, str]] = []
    unrouted: list[str] = []
    for notice in pending_notices(log.state):
        relay = routes.get(notice.operator_id)
        if relay is None:
            unrouted.append(notice.message_id)
            continue
        try:
            relay.forward(notice)
        except RelayError as error:
            failed.append((notice.message_id, str(error)))
            continue
        log.append(MessageDelivered(notice.message_id))
        delivered.append(notice.message_id)
    return ForwardReport(tuple(delivered), tuple(failed), tuple(unrouted))


def encode_notice(notice: Notice) -> str:
    """Return ``notice`` as canonical JSON, the input every command relay receives."""
    document = {
        "body": notice.body,
        "choices": list(notice.choices),
        "kind": notice.kind.value,
        "message_id": notice.message_id,
        "operator_id": notice.operator_id,
        "reply_to": notice.reply_to,
        "sender": notice.sender,
        "v": NOTICE_VERSION,
    }
    return json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


class CommandRelay:
    """A relay that runs a configured command once per notice.

    The notice arrives on standard input as the JSON of ``encode_notice``, followed by
    a newline. Exit status 0 means the channel accepted it. Any other status, a timeout
    or a failure to start the command is a ``RelayError``. This lets a short script
    send email, post to a chat service or write to a file without Dromon knowing the
    channel.

    Porting note. A port needs only process spawning with an argument vector, standard
    input and a timeout, which every target platform has.
    """

    def __init__(
        self,
        argv: Sequence[str],
        timeout_seconds: float = 30.0,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if not argv or not all(argv):
            raise ValueError("argv must be a non-empty sequence of non-empty strings")
        if not 0 < timeout_seconds <= 3600:
            raise ValueError("timeout_seconds must be in (0, 3600]")
        self._argv = tuple(argv)
        self._timeout = timeout_seconds
        self._environment = os.environ if environment is None else environment

    def forward(self, notice: Notice) -> None:
        payload = encode_notice(notice) + "\n"
        try:
            # The argument vector is operator configuration, and no shell is involved,
            # so notice text cannot change what runs.
            completed = subprocess.run(  # noqa: S603
                self._argv,
                input=payload.encode("utf-8"),
                capture_output=True,
                env=allowlisted(self._environment, BASE_ENVIRONMENT),
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise RelayError(
                f"the relay command timed out after {self._timeout}s"
            ) from error
        except OSError as error:
            raise RelayError(f"the relay command could not start: {error}") from error
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()[:200]
            raise RelayError(
                f"the relay command exited with status {completed.returncode}: {detail}"
            )
