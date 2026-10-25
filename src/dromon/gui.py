"""The graphical front end of ``dromon attach``, a Tk window attached to one line.

It shows the same view as the curses client, through ``dromon.attach``, and polls the
daemon on a fixed interval. Tk ships with Python, so the window needs no dependency.
Each window is an independent client, so an operator may open one per line.

The window is a thin shell. Its behaviour lives in ``dromon.attach`` and is tested
there, because a Tk window needs a display that automated tests may not have.
"""

import tkinter as tk
from tkinter import scrolledtext
from typing import Final

from dromon.attach import Attachment, Quit, parse_input, render
from dromon.client import DaemonNotRunning
from dromon.protocol import ProtocolError

COLUMNS: Final = 100
DEFAULT_POLL_MILLISECONDS: Final = 1000


def run(
    attachment: Attachment, poll_milliseconds: int = DEFAULT_POLL_MILLISECONDS
) -> int:
    """Open the window and run it until it is closed. Returns an exit status."""
    root = tk.Tk()
    root.title(f"dromon {attachment.line_id}")
    transcript = scrolledtext.ScrolledText(root, width=COLUMNS, height=30, wrap="none")
    transcript.configure(state="disabled")
    transcript.pack(fill="both", expand=True)
    status = tk.StringVar(
        value=f"attached to {attachment.line_id} as @{attachment.operator}. /help"
    )
    tk.Label(root, textvariable=status, anchor="w").pack(fill="x")
    entry = tk.Entry(root)
    entry.pack(fill="x")
    entry.focus_set()

    def refresh() -> None:
        try:
            lines = render(attachment.fetch(), COLUMNS)
        except DaemonNotRunning:
            status.set("the daemon is not running")
        except (ProtocolError, TimeoutError, OSError) as error:
            status.set(f"error: {error}")
        else:
            transcript.configure(state="normal")
            transcript.delete("1.0", "end")
            transcript.insert("end", "\n".join(lines))
            transcript.see("end")
            transcript.configure(state="disabled")
        root.after(poll_milliseconds, refresh)

    def submit(_event: object) -> None:
        command = parse_input(entry.get())
        entry.delete(0, "end")
        if command is None:
            return
        if isinstance(command, Quit):
            root.destroy()
            return
        status.set(attachment.execute(command))

    entry.bind("<Return>", submit)
    root.after(0, refresh)
    root.mainloop()
    return 0
