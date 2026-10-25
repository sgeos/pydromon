"""Dromon, a multi-agent harness daemon. This package is the Python prototype.

The seed provides version information and the two command entry points only. See
``docs/architecture/DESIGN.md`` for the planned system.
"""

from importlib.metadata import version

DISTRIBUTION = "pydromon"
"""The distribution name. The import package is ``dromon``."""

__version__: str = version(DISTRIBUTION)

__all__ = ["DISTRIBUTION", "__version__"]
