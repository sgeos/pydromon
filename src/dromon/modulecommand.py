"""The command that runs a module of this same ``dromon`` in a fresh interpreter.

**Round 17, item 6.** The daemon starts its launcher and its run supervisor as
``python -I -m dromon.launcher`` and ``python -I -m dromon.runs``. Isolated mode is
kept, since it stops the child importing from the working directory, from
``PYTHONPATH`` or from the user's site directory, any of which a line could write. But
it also made the child import whichever ``dromon`` the interpreter's site directory
installs, which need not be the code the parent runs. A test run of an extracted
review tree, with that tree on ``PYTHONPATH``, tested the tree in process and the
installed package in every child, so its tests of the launcher and the supervisor
failed or passed for the wrong code.

The child now puts the directory holding the parent's own ``dromon`` first on its
path, and runs the module from there. **Trust.** That directory is the one the parent
already runs its code from, so the child trusts nothing the parent does not. Where
``dromon`` is installed into the site directory, as in every deployment, it is that
same directory and nothing changes. **Assumption.** The parent's ``dromon`` is a
package directory on disk, as it is for a wheel or an editable install, and not one
imported from a zip archive.

**Porting note.** A port starts its own executable, which holds its code, so it needs
none of this. The process table still shows the module's name, which
``dromon.launcher.LAUNCHER_NAMES`` matches.
"""

import sys
from pathlib import Path
from typing import Final

import dromon

BOOT: Final = (
    "import runpy, sys; "
    "sys.path.insert(0, sys.argv[1]); "
    "module = sys.argv[2]; "
    "sys.argv = [module, *sys.argv[3:]]; "
    "runpy.run_module(module, run_name='__main__', alter_sys=True)"
)
"""The isolated child's whole program. ``sys.argv[1]`` is the directory to import
from and ``sys.argv[2]`` the module, so neither is ever written into code."""


def package_root() -> str:
    """The directory that holds the running ``dromon`` package."""
    return str(Path(dromon.__file__).resolve().parent.parent)


def module_command(module: str, *arguments: str) -> list[str]:
    """``python -I`` running ``module`` of this same ``dromon`` with ``arguments``.
    Raises ``ValueError`` for a module outside the package, so the helper can never
    start other code."""
    if module != "dromon" and not module.startswith("dromon."):
        raise ValueError(f"{module} is not a module of dromon")
    return [sys.executable, "-I", "-c", BOOT, package_root(), module, *arguments]
