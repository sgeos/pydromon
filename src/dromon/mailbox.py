"""Mailboxes: what a party has received, and the views that let it look through them.

Every party, line or operator, has a mailbox holding the messages delivered to it, in
the order sent. Within it a message is unread until its recipient opens it, and
unanswered until the recipient sends a message replying to it. Neither state obliges
anything. A recipient may read, answer, defer or ignore any message, and doing nothing
is always valid.

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


def answered_ids(state: State, owner: Address) -> set[str]:
    """The messages ``owner`` has sent a reply to."""
    return {
        message.reply_to
        for message in state.messages.values()
        if message.sender == owner and message.reply_to is not None
    }


def summary(state: State, owner: Address, new: int) -> MailSummary:
    """The counts for ``owner``'s mailbox after ``new`` messages arrived."""
    held = mailbox(state, owner)
    answered = answered_ids(state, owner)
    return MailSummary(
        new=new,
        unread=sum(1 for _, message in held if not message.read),
        unanswered=sum(1 for message_id, _ in held if message_id not in answered),
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


def header(message_id: str, message: Message, answered: set[str]) -> str:
    """One line describing a message without its body."""
    parts = [f"{message_id} from {message.sender}", message.kind.value]
    if message.reply_to is not None:
        parts.append(f"replying to {message.reply_to}")
    parts.append("read" if message.read else "unread")
    if message_id in answered:
        parts.append("answered")
    return ", ".join(parts)
