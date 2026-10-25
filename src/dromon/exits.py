"""Exit statuses shared by ``dromon`` and ``dromond``, part of the command contract."""

from typing import Final

OK: Final = 0
FAILURE: Final = 1
"""The operation was attempted and failed, with a message on standard error."""
USAGE: Final = 2
"""The arguments were invalid. This is argparse's own status."""
NOT_IMPLEMENTED: Final = 3
NOT_RUNNING: Final = 4
"""No daemon answers for the project."""
ALREADY_RUNNING: Final = 5
"""``dromond`` found another daemon holding the project's lock."""
