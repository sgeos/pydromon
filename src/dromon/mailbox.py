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

from collections.abc import Collection
from dataclasses import dataclass
from typing import Final

from dromon.model import Address, HarnessAddress, Message, RemoteAddress, State

MAX_PAGE: Final = 50
"""Upper bound on the messages one listing returns, so a large mailbox is paged."""


@dataclass(frozen=True, slots=True)
class MailSummary:
    new: int
    unread: int
    unanswered: int
    collapsed: int = 0
    """The owner's own unread notes, such as fired wakes and task-end notes, that an
    earlier turn was shown and that ``unread`` therefore leaves out."""

    def sentence(self) -> str:
        """The summary in the form a wake prompt opens with."""
        plural = "" if self.new == 1 else "s"
        text = (
            f"{self.new} new message{plural}. {self.unread} unread."
            f" {self.unanswered} unanswered."
        )
        if self.collapsed:
            own = "note" if self.collapsed == 1 else "notes"
            verb = "is" if self.collapsed == 1 else "are"
            text += (
                f" {self.collapsed} {own} of your own, such as fired wakes, shown to"
                f" an earlier turn {verb} left out of unread."
            )
        return text


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


def summary(
    state: State,
    owner: Address,
    new: int,
    *,
    fresh: Collection[str] | None = None,
) -> MailSummary:
    """The counts for ``owner``'s mailbox after ``new`` messages arrived. A reply to a
    question Dromon asked on the owner's behalf when it held the owner, such as an
    operator's ``resume``, a note from the owner to itself, a fired wake or a
    task-end note, and a note from the harness, which nothing can reply to, are not
    counted unanswered, since none asks a reply of the owner.

    ``fresh``, given by a wake prompt, holds the identifiers that prompt delivers.
    The owner's own unread notes outside it were delivered to an earlier turn, which
    was shown them and dealt with them or chose not to, so they are counted as
    ``collapsed`` and left out of ``unread``. **Decided by core on 2026-10-28, for
    review.** A wake that fires after the work it was set for is done otherwise
    swells ``unread`` for good, since a line acknowledges such a note without
    opening it. Mail from anyone else is never collapsed."""
    held = mailbox(state, owner)
    replied = replied_ids(state, owner)
    collapsed = (
        0
        if fresh is None
        else sum(
            1
            for message_id, message in held
            if not message.read and message.sender == owner and message_id not in fresh
        )
    )
    return MailSummary(
        new=new,
        unread=sum(1 for _, message in held if not message.read) - collapsed,
        unanswered=sum(
            1
            for message_id, message in held
            if not is_answered(message_id, message, replied)
            and message.reply_to not in state.hold_questions
            and message.sender != owner
            and not isinstance(message.sender, HarnessAddress)
        ),
        collapsed=collapsed,
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
    delivery: str
    """``waiting``, ``delivered`` or ``read`` for local mail. Mail to a peer is
    ``waiting for peer``, ``delivered to peer``, ``returned`` with its code, or
    ``unconfirmed``, since this daemon never learns whether a remote party read it."""

    def line(self) -> str:
        return (
            f"{self.message_id} to {self.recipient}, {self.kind}, {self.delivery},"
            f" {self.outcome}"
        )


def delivery(message: Message) -> str:
    """How far ``message`` went, as ``SentStatus.delivery`` gives it."""
    if not isinstance(message.recipient, RemoteAddress):
        return (
            "read" if message.read else "delivered" if message.delivered else "waiting"
        )
    if message.returned is not None:
        return f"returned {message.returned.value}"
    if message.unconfirmed:
        return "unconfirmed"
    return "delivered to peer" if message.delivered else "waiting for peer"


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
            delivery(message),
        )
        if not unanswered_only or status.outcome == "unanswered":
            statuses.append(status)
    return statuses[offset : offset + limit], len(statuses)
