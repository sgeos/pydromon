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
is exempted. **Residual.** The keychain is not a path. **Fact, reported by the audit
line on 2026-11-18.** A lookup of a made-up item from a command in an enabled sandbox
reached the keychain service and was answered that no such item exists. Whether the
item holding a sign-in is returned depends on the keychain's own access rules, and was
not tested, since the test would read a credential.

**Round 18 follow-up, item 5. Not added.** ``~/Library/Keychains``,
``~/.config/gcloud`` and ``~/.azure`` are not listed, since the list also binds Codex
and Grok lines and that could not be settled for Grok. **Fact, read from Grok 1.0.70
on 2026-11-18.** Its binary carries the Google default credentials loader, which reads
``.config/gcloud/application_default_credentials.json``, and its own sandbox code names
``Library/Keychains/login.keychain-db`` and ``metadata.keychain-db``. **Inference.**
Grok's own process may be held by the profile that carries these denials, as the
exemption of its sign-in suggests, so a denial could break a path of Grok's own that
was not traced. Codex reaches Google Cloud through the ``gcloud`` command on its own
host, by its own text, and no Dromon line is configured for that provider."""


def daemon_home() -> Path:
    """The daemon's home directory as the process names it, absolute and not
    resolved, under which the credential paths are denied. **Round 18 follow-up, item
    4.** It is not resolved here, so that a home reached through a link is named both
    ways by ``credential_paths``. A function, so that tests give a home of their own
    and a build never depends on what the machine running the tests holds."""
    return Path(os.path.abspath(Path.home()))


def credential_paths(
    home: Path, exempt: Collection[Path] = frozenset()
) -> tuple[Path, ...]:
    """Each entry of ``CREDENTIAL_PATHS`` other than those in ``exempt``, whether or not
    it exists, under ``home`` as given and under its real form, and also by the entry's
    own real path when the entry exists and a link makes that differ, in order and
    without repeats.

    **Round 18, the operator's security fix.** Several forms are given, since a deny
    rule for Claude Code's file tools matches the path the tool is handed, and a
    sandbox follows a link before it matches. **Round 18 follow-up, item 4.** An entry
    that does not exist is named too, since a line's engine is built once and kept, so
    a file made later, by a sign-in or a configuration command, was readable for the
    daemon's life. Only the real form of a missing entry is unknown, and it is
    resolved again at the line's next build. Whether the sandbox honours a rule for a
    missing path is the engine's, and ``CLAUDE_CODE_EXEMPT`` records what the bundle
    shows. An entry is tested with ``lexists``, so a link whose target is missing is
    denied by its name and by its target.

    Raises ``ValueError`` for an ``exempt`` entry that is not listed, since a misspelt
    exemption would otherwise exempt nothing and say nothing."""
    if not home.is_absolute():
        raise ValueError(f"the home {home} is not an absolute path")
    unknown = sorted(str(path) for path in exempt if path not in CREDENTIAL_PATHS)
    if unknown:
        raise ValueError(f"not a listed credential path: {', '.join(unknown)}")
    homes = (Path(os.path.normpath(home)), Path(os.path.realpath(home)))
    found: list[Path] = []
    for relative in CREDENTIAL_PATHS:
        if relative in exempt:
            continue
        for base in homes:
            named = base / relative
            forms = [named]
            if os.path.lexists(named):
                forms.append(Path(os.path.realpath(named)))
            for form in forms:
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
