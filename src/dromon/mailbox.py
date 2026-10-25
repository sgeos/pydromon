"""Mailboxes: what a party has received, and the views that let it look through them.

Every party, line or operator, has a mailbox holding the messages delivered to it, in
the order sent. Within it a message is unread until its recipient opens it, and
unanswered until the recipient replies to it or acknowledges it. An acknowledgement
marks a message dealt with without a reply. Neither state obliges anything. A recipient
may read, reply, acknowledge, defer or ignore any message, and doing nothing is always
valid.

Every party also sees the mail it has sent, with each message's fate on the
recipient's side, so a sender expecting a reply can tell a reply from a mere
acknowledgement.

These are pure functions of the state, so a client, a line's tools and the wake prompt
all see the same mailbox.
"""

from dataclasses import dataclass
from typing import Final

from dromon.model import Address, Message, State

MAX_PAGE: Final = 50
"""Upper bound on the messages one listing returns, so a large mailbox is paged."""


@dataclass(frozen=True, slots=True)
class MailSummary:
    new: int
    unread: int
    unanswered: int

    def sentence(self) -> str:
        """The summary in the form a wake prompt opens with."""
        plural = "" if self.new == 1 else "s"
        return (
            f"{self.new} new message{plural}. {self.unread} unread."
            f" {self.unanswered} unanswered."
        )


def mailbox(state: State, owner: Address) -> list[tuple[str, Message]]:
    """Every message delivered to ``owner``, oldest first."""
    return [
        (message_id, message)
        for message_id, message in state.messages.items()
        if message.recipient == owner and message.delivered
    ]


def replied_ids(state: State, owner: Address) -> set[str]:
    """The messages ``owner`` has sent a reply to."""
    return {
        message.reply_to
        for message in state.messages.values()
        if message.sender == owner and message.reply_to is not None
    }


def is_answered(message_id: str, message: Message, replied: set[str]) -> bool:
    """Whether the recipient replied to the message or acknowledged it."""
    return message_id in replied or message.acknowledged


def summary(state: State, owner: Address, new: int) -> MailSummary:
    """The counts for ``owner``'s mailbox after ``new`` messages arrived."""
    held = mailbox(state, owner)
    replied = replied_ids(state, owner)
    return MailSummary(
        new=new,
        unread=sum(1 for _, message in held if not message.read),
        unanswered=sum(
            1
            for message_id, message in held
            if not is_answered(message_id, message, replied)
        ),
    )


def listing(
    state: State,
    owner: Address,
    *,
    unread_only: bool = False,
    sender: Address | None = None,
    offset: int = 0,
    limit: int = MAX_PAGE,
) -> tuple[list[tuple[str, Message]], int]:
    """A page of ``owner``'s mailbox, oldest first, and the number of matches in all."""
    if offset < 0 or not 1 <= limit <= MAX_PAGE:
        raise ValueError(f"offset must be at least 0 and limit in [1, {MAX_PAGE}]")
    matches = [
        (message_id, message)
        for message_id, message in mailbox(state, owner)
        if (not unread_only or not message.read)
        and (sender is None or message.sender == sender)
    ]
    return matches[offset : offset + limit], len(matches)


def outcome(message_id: str, message: Message, replied: set[str]) -> str:
    """``replied``, ``acknowledged`` or ``unanswered``, from the recipient's side."""
    if message_id in replied:
        return "replied"
    if message.acknowledged:
        return "acknowledged"
    return "unanswered"


def header(message_id: str, message: Message, replied: set[str]) -> str:
    """One line describing a received message without its body."""
    parts = [f"{message_id} from {message.sender}", message.kind.value]
    if message.reply_to is not None:
        parts.append(f"replying to {message.reply_to}")
    parts.append("read" if message.read else "unread")
    answered = outcome(message_id, message, replied)
    if answered != "unanswered":
        parts.append(answered)
    return ", ".join(parts)


@dataclass(frozen=True, slots=True)
class SentStatus:
    """A sent message's fate on the recipient's side."""

    message_id: str
    recipient: str
    kind: str
    delivered: bool
    read: bool
    outcome: str
    """``replied``, ``acknowledged`` or ``unanswered``."""

    def line(self) -> str:
        delivery = "read" if self.read else "delivered" if self.delivered else "waiting"
        return (
            f"{self.message_id} to {self.recipient}, {self.kind}, {delivery},"
            f" {self.outcome}"
        )


def sent(
    state: State,
    owner: Address,
    *,
    unanswered_only: bool = False,
    offset: int = 0,
    limit: int = MAX_PAGE,
) -> tuple[list[SentStatus], int]:
    """A page of the mail ``owner`` has sent, oldest first, and the number in all."""
    if offset < 0 or not 1 <= limit <= MAX_PAGE:
        raise ValueError(f"offset must be at least 0 and limit in [1, {MAX_PAGE}]")
    statuses: list[SentStatus] = []
    for message_id, message in state.messages.items():
        if message.sender != owner:
            continue
        replied = replied_ids(state, message.recipient)
        status = SentStatus(
            message_id,
            str(message.recipient),
            message.kind.value,
            message.delivered,
            message.read,
            outcome(message_id, message, replied),
        )
        if not unanswered_only or status.outcome == "unanswered":
            statuses.append(status)
    return statuses[offset : offset + limit], len(statuses)
