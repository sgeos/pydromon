#!/usr/bin/env bash
# The local gate. Continuous integration runs this same script, so a green run here
# means what a green run there means, apart from operating system and tool versions.
#
# It exits zero only when every check passes. Each check prints one line on success
# and an explanation on failure, and the script stops at the first failure.
#
# Repository hygiene runs first, as sgeos/project-template defines it, and needs git,
# python3 and shellcheck. The project's checks follow, in the order that fails fastest.
#
# Usage:
#   scripts/check.sh                                  # hygiene, then every check
#   scripts/check.sh -m "not socket and not live"     # inside an agent sandbox
#   scripts/check.sh --hygiene-only                   # hygiene alone, for a cheap CI job
#
# Any arguments other than --hygiene-only are passed to pytest. The in-sandbox form
# exists because Claude Code's sandbox denies binding a Unix domain socket. Without
# arguments the socket tests run, and live tests stay deselected by pyproject.toml. Live
# tests spend the operator's subscriptions and are never run by this script by default.
# Continuous integration sets UV_PYTHON so that `uv sync` installs for its matrix
# interpreter.

set -euo pipefail

hygiene_only=false
if [[ "${1:-}" == "--hygiene-only" ]]; then
    [[ $# -eq 1 ]] || { printf 'usage: %s --hygiene-only, alone\n' "$0" >&2; exit 2; }
    hygiene_only=true
fi

cd "$(git rev-parse --show-toplevel)"

fail() {
    printf 'check.sh: FAIL: %s\n' "$1" >&2
    exit 1
}

pass() {
    printf 'check.sh: ok: %s\n' "$1"
}

# --- Repository hygiene -------------------------------------------------------------

# secret/ is ignored whole. A tracked file there was added deliberately with
# `git add -f` or before the ignore rule existed, and either way it is a disclosure.
tracked_secret="$(git ls-files -- secret)"
if [[ -n "$tracked_secret" ]]; then
    fail "files under secret/ are tracked and must be removed from the index and history:
$tracked_secret"
fi
pass "nothing under secret/ is tracked"

# tmp/ is scratch. Only its marker is tracked, so the directory exists in a clone.
tracked_tmp="$(git ls-files -- tmp | grep -vx 'tmp/.gitkeep' || true)"
if [[ -n "$tracked_tmp" ]]; then
    fail "files under tmp/ other than tmp/.gitkeep are tracked:
$tracked_tmp"
fi
if [[ -z "$(git ls-files -- tmp/.gitkeep)" ]]; then
    fail "tmp/.gitkeep is not tracked, so tmp/ will not exist in a fresh clone"
fi
pass "only tmp/.gitkeep is tracked under tmp/"

# Unresolved merge markers in tracked text files. Binary files are skipped by -I. Only
# the opening and closing markers are matched, because a line of exactly seven equals
# signs is also a valid Markdown setext heading underline.
if git grep -nI -E '^(<<<<<<< |>>>>>>> )' -- . ':!scripts/check.sh' >/dev/null; then
    git grep -nI -E '^(<<<<<<< |>>>>>>> )' -- . ':!scripts/check.sh' >&2
    fail "unresolved merge markers are present"
fi
pass "no unresolved merge markers"

# A repository made from the template must not keep the template's README title or CI
# badge. The badge would show the template's status as if it were the project's, and
# the title would name the wrong project. These are steps 1 and 2 of the README's new
# project checklist. The repository's name
# is taken from the origin remote, so this check is skipped when origin is missing or
# is not on GitHub, such as a clone of a local path.
origin="$(git remote get-url origin 2>/dev/null || true)"
repository=""
if [[ "$origin" =~ github\.com[:/][^/]+/([^/]+)$ ]]; then
    repository="${BASH_REMATCH[1]%.git}"
fi
if [[ -n "$repository" && "$repository" != "project-template" ]] &&
    grep -q 'sgeos/project-template/actions' README.md 2>/dev/null; then
    fail "README.md still shows the template's CI badge. Point it at sgeos/$repository"
fi
if [[ -n "$repository" && "$repository" != "project-template" ]] &&
    grep -qx '# Project Template' README.md 2>/dev/null; then
    fail "README.md still has the template's title. Replace it with the project's name"
fi
if [[ -n "$repository" ]]; then
    pass "the README's title and CI badge are not the template's, or this is the template"
else
    printf 'check.sh: note: origin is not a GitHub repository, so the README title and CI badge were not checked\n'
fi

# Relative links in tracked Markdown files must resolve to a tracked or present path.
# External links, mail links and links to an anchor in the same file are not checked,
# and neither are anchors within another file. Links inside fenced code blocks and
# inline code spans are skipped. This needs python3, which GitHub's runners and macOS
# both provide. The program is passed with -c rather than a heredoc, because some agent
# sandboxes forbid the temporary file that bash writes for a heredoc.
python3 -I -c '
import pathlib
import re
import subprocess
import sys

files = subprocess.run(
    ["git", "ls-files", "-z", "--", "*.md"], check=True, capture_output=True
).stdout.split(b"\0")
link = re.compile(r"\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
# A fence opens with three or more backticks or tildes, and closes only with a run of
# the same character that is at least as long, so a ``` line inside a ~~~ fence does
# not close it. Inline code spans are removed before links are matched.
fence_line = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
code_span = re.compile(r"(`+)(?:(?!\1).)+?\1")
broken = []
for raw in filter(None, files):
    path = pathlib.Path(raw.decode())
    fence = None
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        opener = fence_line.match(line)
        if fence is None and opener:
            fence = opener.group(1)
            continue
        if fence is not None:
            closer = fence_line.match(line)
            if closer and closer.group(1)[0] == fence[0] and len(closer.group(1)) >= len(fence) and not closer.group(2).strip():
                fence = None
            continue
        for target in link.findall(code_span.sub("", line)):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target = target.split("#", 1)[0].strip("<>")
            # GitHub resolves a link that starts with a slash from the repository root.
            base = pathlib.Path(".") if target.startswith("/") else path.parent
            if target and not (base / target.lstrip("/")).exists():
                broken.append(f"{path}:{number}: {target}")
for entry in broken:
    print(entry, file=sys.stderr)
sys.exit(1 if broken else 0)
' || fail "broken relative links in Markdown, listed above"
pass "relative Markdown links resolve"

# Text and file hygiene, matching .editorconfig and .gitattributes. Every tracked text
# file ends with a newline and has no carriage returns, except Windows batch files,
# which need them, and files other than Markdown have no trailing whitespace, since
# Markdown uses it for line breaks. No tracked file exceeds MAX_FILE_BYTES, which
# defaults to 5 MiB, so a large binary is added deliberately or not at all. A file is
# text when its first 8 KiB holds no NUL byte, the same test git uses.
#
# This step also finds the shell scripts for the shellcheck step below, and requires
# them to be executable. A shell script is a tracked file named *.sh, a file under
# scripts/hooks/, or any file whose first line is a sh, bash, dash or ksh shebang,
# which covers scripts without an extension. It prints their paths, one per line.
if ! shell_list="$(MAX_FILE_BYTES="${MAX_FILE_BYTES:-5242880}" python3 -I -c '
import os
import re
import subprocess
import sys

shebang = re.compile(rb"^#!\s*(?:\S*/)?(?:env\s+(?:-S\s+)?)?(?:sh|bash|dash|ksh)(?:\s|$)")

limit = int(os.environ["MAX_FILE_BYTES"])
listing = subprocess.run(
    ["git", "ls-files", "-s", "-z"], check=True, capture_output=True
).stdout.split(b"\0")
problems = []
shells = []
for entry in filter(None, listing):
    meta, raw_path = entry.split(b"\t", 1)
    mode = meta.split()[0].decode()
    path = raw_path.decode()
    if mode == "120000" or not os.path.isfile(path):
        continue
    size = os.path.getsize(path)
    if size > limit:
        problems.append(f"{path}: {size} bytes, above the limit of {limit}")
    with open(path, "rb") as handle:
        data = handle.read()
    first_line = data.split(b"\n", 1)[0]
    if path.endswith(".sh") or path.startswith("scripts/hooks/") or shebang.match(first_line):
        shells.append(path)
        if mode != "100755":
            problems.append(f"{path}: not executable, fix with git update-index --chmod=+x")
    if b"\0" in data[:8192] or not data:
        continue
    if b"\r" in data and not path.lower().endswith((".bat", ".cmd")):
        problems.append(f"{path}: contains carriage returns")
    if not data.endswith(b"\n"):
        problems.append(f"{path}: does not end with a newline")
    if not path.endswith(".md"):
        for number, line in enumerate(data.split(b"\n"), 1):
            if line.rstrip(b" \t") != line:
                problems.append(f"{path}:{number}: trailing whitespace")
for problem in problems:
    print(problem, file=sys.stderr)
for path in shells:
    print(path)
sys.exit(1 if problems else 0)
')"; then
    fail "text or file hygiene problems, listed above"
fi
pass "tracked files end with newlines, have no stray whitespace, and are within size"

# Shell scripts and hooks pass shellcheck. A missing shellcheck is a failure, not a
# skip, so that a green gate never means "not checked". GitHub's Ubuntu runners include
# it. Elsewhere, install it with the system package manager, such as
# `sudo port install shellcheck`, `brew install shellcheck` or `apt install shellcheck`.
# The list comes from the hygiene step above. A read loop rather than mapfile, because
# macOS ships bash 3.2, which lacks mapfile. A path containing a newline is not
# supported.
shell_files=()
while IFS= read -r file; do
    [[ -n "$file" ]] && shell_files+=("$file")
done < <(printf '%s\n' "$shell_list")
if ((${#shell_files[@]} > 0)); then
    command -v shellcheck >/dev/null ||
        fail "shellcheck is not installed, so ${#shell_files[@]} shell files went unchecked"
    shellcheck -- "${shell_files[@]}" || fail "shellcheck reported problems, listed above"
fi
pass "shellcheck is clean on ${#shell_files[@]} shell files"

# Optional disclosure scan. If secret/disclosure-terms.txt exists, no tracked file may
# contain any of its terms, matched as fixed strings without regard to case. Blank
# lines and lines starting with # are ignored. The scan prints only the file and line
# of each match, never the matched text, so that the gate's own output does not
# disclose a term. The list lives under secret/, so the scan cannot run in a clone or
# in continuous integration, and it says so rather than passing silently.
terms_file="secret/disclosure-terms.txt"
accepted_file="secret/disclosure-accepted.txt"
if [[ -f "$terms_file" ]]; then
    patterns=()
    while IFS= read -r term || [[ -n "$term" ]]; do
        [[ "$term" =~ ^[[:space:]]*(#|$) ]] && continue
        patterns+=(-e "$term")
    done < "$terms_file"
    if ((${#patterns[@]} > 0)); then
        hits="$(git grep -n -I -i -F "${patterns[@]}" -- . | cut -d: -f1,2 || true)"
        accepted_pairs=()
        if [[ -f "$accepted_file" ]]; then
            accepted_line=0
            while IFS= read -r accepted_entry || [[ -n "$accepted_entry" ]]; do
                ((accepted_line += 1))
                [[ "$accepted_entry" =~ ^[[:space:]]*(#|$) ]] && continue
                if [[ "$accepted_entry" != *$'\t'* ]]; then
                    fail "$accepted_file:$accepted_line must contain a path, a tab, and a line number"
                fi
                accepted_path="${accepted_entry%%$'\t'*}"
                accepted_number="${accepted_entry#*$'\t'}"
                if [[ -z "$accepted_path" || ! "$accepted_number" =~ ^[1-9][0-9]*$ || "$accepted_number" == *$'\t'* ]]; then
                    fail "$accepted_file:$accepted_line must contain a path, a tab, and a positive line number"
                fi
                accepted_pairs+=("$accepted_path:$accepted_number")
            done < "$accepted_file"
        fi
        if [[ -n "$hits" ]]; then
            remaining_hits=()
            accepted_hits=0
            while IFS= read -r hit; do
                is_accepted=false
                for accepted_pair in "${accepted_pairs[@]}"; do
                    if [[ "$hit" == "$accepted_pair" ]]; then
                        is_accepted=true
                        ((accepted_hits += 1))
                        break
                    fi
                done
                [[ "$is_accepted" == true ]] || remaining_hits+=("$hit")
            done <<< "$hits"
            if ((${#remaining_hits[@]} > 0)); then
                printf '%s\n' "${remaining_hits[@]}" >&2
                fail "tracked files contain terms from $terms_file, at the lines listed above"
            fi
            pass "disclosure scan skipped $accepted_hits accepted path and line match(es) from $accepted_file"
        else
            pass "disclosure scan found no listed term in tracked files"
            pass "disclosure scan skipped 0 accepted path and line match(es) from $accepted_file"
        fi
    else
        printf 'check.sh: note: %s lists no terms, so the disclosure scan did nothing\n' "$terms_file"
    fi
else
    printf 'check.sh: note: no %s, so the disclosure scan was skipped\n' "$terms_file"
fi

if [[ "$hygiene_only" == true ]]; then
    printf 'check.sh: note: --hygiene-only, so the project checks were not run\n'
    exit 0
fi

# --- Project checks -----------------------------------------------------------------

# A stale environment tests old code, so the installed environment must match the lock.
uv sync --locked
pass "the environment matches uv.lock"

uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
pass "ruff lint and format"

# Both platforms the daemon supports, since pyright skips a branch for another
# platform as unreachable, which once let a Linux-only type error reach main.
uv run --no-sync pyright --pythonplatform Linux
uv run --no-sync pyright --pythonplatform Darwin
pass "pyright strict, for Linux and macOS"

# Warnings are errors. pytest exits non-zero when it collects nothing, so an empty
# selection fails here rather than passing.
uv run --no-sync pytest -W error "$@"
pass "pytest"
