"""Shared credential and project secret paths used by engine isolation."""

import os
from collections.abc import Collection
from pathlib import Path
from typing import Final

CREDENTIAL_PATHS: Final = (
    Path(".ssh"),
    Path(".aws"),
    Path(".codex/auth.json"),
    Path(".cargo/credentials.toml"),
    Path(".pi/agent/auth.json"),
    Path(".grok/auth.json"),
    Path(".claude.json"),
    Path(".claude/.credentials.json"),
    Path(".netrc"),
    Path(".git-credentials"),
    Path(".gnupg"),
    Path(".npmrc"),
    Path(".pypirc"),
    Path(".docker/config.json"),
    Path(".kube/config"),
    Path(".config/gh/hosts.yml"),
)
"""Known operator-home credential paths, not an exhaustive list."""


def existing_real_paths(paths: tuple[Path, ...] | list[Path]) -> tuple[Path, ...]:
    """Return unique real absolute paths for entries that exist, preserving order."""
    found: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        if os.path.lexists(path):
            real = (
                Path(os.path.realpath(path))
                if os.path.exists(path)
                else Path(os.path.abspath(path))
            )
            if real not in seen:
                seen.add(real)
                found.append(real)
    return tuple(found)


CLAUDE_CODE_EXEMPT: Final[frozenset[Path]] = frozenset()
"""The entries of ``CREDENTIAL_PATHS`` a Claude Code line keeps, which is none.
**Round 18, the operator's security fix.** An engine may keep only its own sign-in.

**Read from the bundled Claude Code 2.1.310 on 2026-11-18.** Claude Code keeps its
sign-in in the system keychain where one is available, under a service name ending in
``-credentials``, and otherwise in ``.credentials.json`` in its configuration
directory, ``~/.claude`` unless ``CLAUDE_CONFIG_DIR`` or
``CLAUDE_SECURESTORAGE_CONFIG_DIR`` names another, which a line cannot set. Its global
configuration, with the account it signed in as, is ``~/.claude.json``. Both are
listed in ``CREDENTIAL_PATHS``. The engine's own process reads and writes them
directly, and neither the sandbox nor the permission rules confine that process. The
sandbox wraps the commands the shell tool runs, and the rules govern the tools the
model calls. **Fact, observed on 2026-11-18.** A Claude Code line's engine writes its
transcript under ``~/.claude/projects`` while that line's sandbox denies writing
there. The same bundle also carries a list of its own that denies its tools
``Read(~/.claude/.credentials.json)`` and ``Read(~/.claude.json)``. **Inference.**
Denying both to a line's shell and file tools leaves its engine signed in, so nothing
is exempted. **Residual.** The keychain is not a path. Whether a command in an enabled
sandbox can ask the keychain for the item was not tested, since the test would read a
credential."""


def daemon_home() -> Path:
    """The real path of the daemon's home directory, under which the credential paths
    are denied. A function, so that tests give a home of their own and a build never
    depends on what the machine running the tests holds."""
    return Path(os.path.realpath(Path.home()))


def credential_paths(
    home: Path, exempt: Collection[Path] = frozenset()
) -> tuple[Path, ...]:
    """Each entry of ``CREDENTIAL_PATHS`` that exists under ``home``, other than those
    in ``exempt``, as named under the real form of ``home`` and by its own real path,
    in order and without repeats. **Round 18, the operator's security fix.** Both forms
    are given, since a deny rule for Claude Code's file tools matches the path the tool
    is handed, and a sandbox follows a link before it matches. An entry is tested with
    ``lexists``, so a link whose target is missing is still denied by its name. Raises
    ``ValueError`` for an ``exempt`` entry that is not listed, since a misspelt
    exemption would otherwise exempt nothing and say nothing."""
    unknown = sorted(str(path) for path in exempt if path not in CREDENTIAL_PATHS)
    if unknown:
        raise ValueError(f"not a listed credential path: {', '.join(unknown)}")
    real_home = Path(os.path.realpath(home))
    found: list[Path] = []
    for relative in CREDENTIAL_PATHS:
        if relative in exempt:
            continue
        named = real_home / relative
        if not os.path.lexists(named):
            continue
        for form in (named, Path(os.path.realpath(named))):
            if form not in found:
                found.append(form)
    return tuple(found)


def claude_credentials() -> tuple[Path, ...]:
    """The credential paths denied to every Claude Code line, read when its engine is
    built and when ``dromond`` checks the project file, so a path made later is denied
    from the line's next build. Looked up through the module, so ``daemon_home`` can be
    replaced in tests."""
    return credential_paths(daemon_home(), CLAUDE_CODE_EXEMPT)


def project_secret_paths(project_root: Path, workdir: Path) -> tuple[Path, ...]:
    """Existing project secret directories, by real path, from both line locations."""
    return existing_real_paths((project_root / "secret", workdir / "secret"))
