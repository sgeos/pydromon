# Sandbox

> **Navigation**: [Protocol](./README.md) | [Documentation Root](../README.md)

**Status.** Draft, written on 2026-10-25. Normative. The Python module `dromon.sandbox`
implements it. It is part of line configuration version 1 and project file version 1,
as an optional key, so documents written before it remain valid.

## Purpose

**Directed by the operator on 2026-10-25.** Dromon should reach sandboxing parity with
Claude Code, configurable per session line and per project. A line's shell commands run
inside the operating system sandbox that Claude Code provides, Seatbelt on macOS and
bubblewrap on Linux and WSL2, configured by a sandbox document.

## Document

A sandbox document is a Claude Code `sandbox` settings object, with Claude Code's key
names, so that one can be copied from `.claude/settings.json` unchanged.

```json
{
  "enabled": true,
  "filesystem": {"allowWrite": ["~/.cache/uv"], "denyRead": ["~/.ssh"]},
  "network": {"allowedDomains": ["github.com", "*.npmjs.org"]},
  "credentials": {"envVars": [{"name": "GITHUB_TOKEN", "mode": "deny"}]}
}
```

| Key | Type | Who may set it |
|---|---|---|
| `enabled`, `failIfUnavailable`, `allowUnsandboxedCommands`, `autoAllowBashIfSandboxed` | boolean | Anyone |
| `enableWeakerNestedSandbox`, `enableWeakerNetworkIsolation` | boolean | Anyone |
| `excludedCommands` | array of command patterns | Anyone, and refused in an enabled document from round 18 |
| `ignoreViolations` | table of command pattern to array of text | Anyone |
| `filesystem.allowWrite`, `denyWrite`, `denyRead`, `allowRead` | array of paths | Anyone |
| `network.allowedDomains`, `deniedDomains` | array of domains, `*.` allowed in front | Anyone |
| `network.strictAllowlist`, `allowLocalBinding` | boolean | Anyone |
| `network.allowUnixSockets` | array of paths | Anyone |
| `network.allowMachLookup` | array of service names, `*` allowed at the end | Anyone |
| `network.httpProxyPort`, `socksProxyPort` | integer in [1, 65535] | Anyone |
| `credentials.files` | array of `{path, mode}`, mode `deny` | Anyone |
| `credentials.envVars` | array of `{name, mode}`, mode `deny` | Anyone |
| `allowNestedUserNamespaces`, `allowAppleEvents` | boolean | Operator only |
| `filesystem.disabled`, `network.allowAllUnixSockets`, `network.tlsTerminate` | boolean | Operator only |

The meaning of each key is Claude Code's, as documented at
<https://code.claude.com/docs/en/sandboxing> and in the settings reference at
<https://code.claude.com/docs/en/settings-reference>.

- **Strict keys.** An unknown key is an error. Arrays are sorted and deduplicated, and
  documents are stored as canonical JSON.
- **Operator only.** Claude Code accepts these keys only from user or managed settings,
  because each removes a boundary. Dromon's project file is committed with a project,
  like a repository's `.claude/settings.json`, so it may not set them. A document given
  through the client may.
- **Refused.** `bwrapPath`, `socatPath`, `filesystem.allowManagedReadPathsOnly` and
  `network.allowManagedDomainsOnly` apply only to managed settings, and Dromon has no
  managed layer. `ripgrep`, credential masking, `credentials.awsPairs`,
  `credentials.sigv4` and `credentials.allowPlaintextInject` are not yet supported.

## Layers

| Layer | Where | Applies to |
|---|---|---|
| Project | `[sandbox]` in `.dromon/dromon.toml` | Every line, including lines created through the client |
| Profile | `[profiles.NAME.sandbox]` | Lines of that profile in the file |
| Line | `[lines.ID.sandbox]`, or `--sandbox-config FILE` on `dromon create` and `dromon configure` | One line |

Layers merge from least to most specific, as Claude Code merges settings scopes. A
scalar in a more specific layer replaces the less specific one, and arrays are united,
so no layer removes an entry that another adds. This differs from the tool keys of
`PROJECT_CONFIG.md`, which replace a profile's whole, because a sandbox's deny entries
must survive every layer above them. A `--sandbox-config` file holds a sandbox document,
or a settings file whose `sandbox` key is taken and whose other keys are ignored.

## What reaches the engine

For a line configured with `workdir_access: read`, Dromon requires Claude Code's
sandbox and adds a deny-write rule for the complete working directory, together with
permission denials for direct file-editing tools. It sets `failIfUnavailable` true and
`allowUnsandboxedCommands` false after every other layer, since a shell command run
outside the sandbox is not held by the deny-write rule. The line is refused when
configured MCP servers, account MCP inheritance, allow-all tools, project settings or
plugins, or command hooks could bypass those controls, and when the project's or the
line's sandbox sets `allowUnsandboxedCommands` true, `failIfUnavailable` false,
`excludedCommands`, which run outside the sandbox, or `filesystem.disabled`. Review of
read-only seats, findings 1 and 2. Dromon's `write_scratch` tool remains
available for scratch files, under the line's own `tmp/lines/LINE`, as
[`LINE_CONFIG.md`](LINE_CONFIG.md) specifies.

**Links in the path lists, round 16, item 5.** **Fact, reported by another project on a
daemon about two weeks old.** A line's sandbox could not write its worktree when that
directory was a link to another volume, because the sandbox follows the link to the real
path before it matches a rule. **Change.** Where the sandbox is enabled, Dromon keeps
each absolute or home-relative entry of the four lists as written, and for the two deny
lists only adds its real path when that differs. It adds the real path of each protected
directory and of each guarded path to the deny lists, and grants write access to the real
path of the line's working directory when that is a link. **Review of round 16, finding
1.** An allow entry is never given a real form, because a line that can write a link
where an allow entry lives, such as its reports directory, could then point the link at a
directory it may not write and have the grant follow at its next engine build. A real
form in a deny list only denies more. The working directory is the one allow entry
granted by its real path, since the operator chose it and a line does not write the link
that names it. **Residual.** A line with write access to the directory that holds another
line's working directory can still replace that working directory by a link, and the grant
follows it. Placing worktrees where no line can write their parent is the operator's
protection. **The one-writer bound, round 17, item 6.** The protection is a bound that
this document names and the daemon does not enforce. (1) A working directory is written
by one line at a time, the owner of its role, and a role's other seats are read only,
as [`ROLES.md`](../architecture/ROLES.md) says. (2) No line is given write access to the
parent of any line's working directory, or to any directory above it, since a line that
can write there can replace the working directory by a link, and the grant of its real
path then follows the link to wherever the writing line chose. (3) The parent is
therefore the operator's own, and a worktree is created by the operator, never by a line.
**Inference, not shown by a test.** Two writers to one working directory, or a line that
can write a parent, would let one line widen another's write access to a directory of
its choice. Nothing in the daemon checks (2) today, and a check at the building of a
line's sandbox, which would refuse a working directory whose parent lies in another
line's write list, is the follow-up if the layout ever changes. A
`denyWrite` entry for the working directory still wins over the grant, so a read line
stays read only. **Failure modes.** The real path is read once, when the line's sandbox
is built, so a link changed afterwards, or created below an entry, is not followed. A
path that does not exist resolves as far as it can. A link to a directory the line must
not write is a decision of the operator, since the grant follows where the link leads
and the working directory's own path is what configures it. The check that refuses an
entry inside a protected directory compares real paths and is unchanged.

**A temporary directory and package cache, round 14, item 6.** Where a line's sandbox is
enabled, Dromon also grants write access to two directories of the line's own outside
its working directory, and sets `TMPDIR` and `UV_CACHE_DIR` to them. They are outside
because the deny-write rule above covers the whole working directory of a read line, and
no layer lifts it. [`LINE_CONFIG.md`](LINE_CONFIG.md) specifies them, with the checks that
refuse a planted link or another user's directory. **Fact, observed live on 2026-11-01**
by the operator, on a Claude Code line on Sonnet 5.5 under the daemon at commit c47d51a.
Claude Code replaces `TMPDIR` in the line's shell with its own directory, `/tmp/claude-UID`
for the user's number, which the sandbox grants, and a here-document written there
worked. `UV_CACHE_DIR` stays Dromon's. **Inference.** That directory is shared by every
Claude Code line of the user, so such lines may read one another's temporary files
there, which Dromon does not control.

**The shell's `TMPDIR`, stated per engine, round 15, item 2.** The turn-start note names
both directories and says which one the shell has. Under Claude Code the shell's
`TMPDIR` is Claude Code's own, observed as `/tmp/claude-UID` and shared, and Dromon's
directory for the line is named in full beside it, with `UV_CACHE_DIR`, so a line that
needs a file other lines must not read writes to the directory the note names. The path
`dromon.linetemp.claude_shell_tmpdir` gives is an observation of Claude Code's behaviour
and not a contract, and the note and `line_status` say so. Other engines do not receive a
directory from Dromon and so have no such note. **Hypothesis.** A Claude Code release
that changes its directory makes the note wrong until `claude_shell_tmpdir` is updated.

**A byte bound on the directories, round 15, item 5.** The sandbox has no quota, so the
daemon applies one between turns. Before a line's turn starts it measures the line's
`tmp` and its package cache, in allocated bytes with no link followed and at most
100,000 entries. A directory over its bound, one gibibyte for `tmp` and four for the
cache, loses whole top-level entries, oldest first, until it holds at most half of the
bound. **Review of 3344ca6.** A walk that reaches the limit has counted a lower bound
of the bytes, so removal starts only when that count already exceeds the bound, and
stops once it is at most half of it. A directory of more entries whose counted bytes are
within the bound loses nothing, and the prompt says it could not be measured in full,
which leaves such a directory over its bound unseen, a residual. Before this rule it was
purged to its oldest ten thousand entries whatever its size. Age is the latest
modification time found in the entry, by a walk that shares one budget of 100,000
entries, so a tree written deep below is not taken for an old one, and an entry past the
budget is aged by its own time. A command that outlives the turn and writes into an
entry as it is removed may see files vanish or recreate a directory. A removal that
fails because it did so is reported and not counted. A link is unlinked and its
target is kept, and an entry another user owns is left and reported. The turn's prompt
then says how many entries and bytes were removed, naming no file. **Residuals.** The
bound holds between turns only, so a line can exceed it within a turn as far as the disk
allows. A file in use by a command that outlives the turn can be removed. Hard links count
once for each name. The bounds are constants and not a project setting. The sweep at
start still removes what has gone unchanged for a week.

**A check of one rule, round 15, item 6.** The tool `sandbox_probe` takes `read` or
`write` and a path inside the line's own scratch directory, and answers `allowed` or
`denied` with the entry of the effective sandbox document that decides it. **It checks
the document the engine was given and is not a probe by the operating system**, which
every answer says, because the daemon cannot run a process under Claude Code's
confinement. **Review of 3344ca6.** A write is `allowed` only by an `allowWrite` entry of
the document and `denied` by a `denyWrite` entry or by no coverage. The document does not
list the working directory, which Claude Code may grant by default, so a path only the
working directory would cover is `unknown`, with the directory named. It reads and writes nothing of the path, resolves links only to name the
deciding rule, and refuses a path that leaves the scratch directory without looking at
it, so it cannot widen what the line may do. A rule that the engine applies differently
from the document is not seen. It is offered to every line and refuses a line whose
engine holds no sandbox document Dromon reads.

The sandbox also denies the line reads of the directory that holds every line's
directory, and lifts the denial for its own, so one line cannot read what another's
commands wrote. **Fact, observed live on 2026-11-01** on the same line and daemon.
Listing that directory and listing another line's `tmp` were refused with "Operation
not permitted", while a file written to the line's own `tmp` was listed and read back by
its full path. An `allowRead` therefore lifts a `denyRead` above it on that engine.

When a line's turn starts, Dromon merges the layers and derives the settings it hands to
Claude Code. **Round 18 follow-up, item 1.** When no layer says whether the sandbox
is enabled, a Claude Code line is given an enabled one, merged last as the read-only
mode merges its own, so that its shell is held by the denials below. Before, nothing was
passed when no layer set anything, and the line then had no sandbox but any managed
policy, because Dromon starts Claude Code without user or project settings. An explicit
`enabled` false in any layer stays the operator's choice. `dromond`, `dromon config`
and the line's build then warn that the line's shell can read the credential paths that
only its file tools are denied. **Effect, an inference from Claude Code's
documentation.** Such a line's shell now writes only its working directory and what a
layer grants, and reaches no host, since `network.strictAllowlist` is filled in with no
domain allowed. A Claude Code line in a checkout whose `.git` pointer cannot be followed
is now refused, as a sandboxed line always was. **Round 18, last pass, item 2.**
`dromon config` reports each line's sandbox as its build gives it, through the function
the daemon's checks use, as `sandboxed by default` for a Claude Code line that no layer
enables or disables, and as `not sandboxed, its shell can read the credential paths`
for one a layer disables. A line whose build would be refused, such as one naming
`excludedCommands`, makes `dromon config` fail with the build's reason.

**A host that cannot start the sandbox, round 18, last pass, item 7.** **Fact, read
from the bundled Claude Code 2.1.327.** Its sandbox needs `bwrap` and `socat` on the
engine's `PATH` on Linux, and reports `bubblewrap (bwrap) not installed` or `socat not
installed` when either is missing, and seatbelt on the other supported platform is built
in. Since `failIfUnavailable` is filled in as true, every Claude Code line with an
enabled sandbox, including each one enabled only by the default above, fails at its
first turn on a Linux host that lacks either. A start, `dromond --check` and
`dromon check-engines`, which runs that check, print a warning naming every such line,
declared or created through the client, before any turn. It is a warning and not a
refusal, since the host's other lines can still run, and a refusal would also stop
`dromon restart`, whose preflight runs the same check. **Assumption.** The engine runs
with the daemon's `PATH`. **Residual.** Only missing commands are found. A host whose
kernel refuses the user namespaces bubblewrap needs, as some containers do, passes the
check and still fails at the first turn, since finding that would start a sandbox.

1. **Paths.** A relative path is resolved against the line's working directory, its
   `workdir` as [`LINE_CONFIG.md`](LINE_CONFIG.md) specifies, which is the project root
   by default. A path that starts with `/` or `~` is passed as written, and Claude Code
   expands `~`.
2. **Unattended defaults.** When the document enables the sandbox, Dromon fills in
   `failIfUnavailable` as true, `allowUnsandboxedCommands` as false and
   `network.strictAllowlist` as true, unless the document sets them. Claude Code's own
   defaults ask a person instead, or run unsandboxed when the sandbox cannot start, and
   a Dromon line has nobody at the prompt. **Round 18 follow-up, item 2.**
   `allowUnsandboxedCommands` is forced false, whatever a layer sets, the client's
   included, since a command run outside the sandbox reads every path it denies, and
   `dromon config` and `dromond` warn of a layer that sets it true. An enabled document
   that names `excludedCommands` is refused, at `dromond`'s start for the project file
   and at the line's build, since an excluded command runs outside the sandbox. It is
   refused rather than dropped, so the operator learns that the command would be
   confined. **Residuals.** `failIfUnavailable` false is still honoured, and runs every
   command outside the sandbox where it cannot start. `filesystem.disabled`, which only
   the client may set, still lifts the filesystem rules.
3. **Protected directories.** An enabled sandbox denies reading and writing the
   project's state directory and runtime directory, which hold the event log, the
   project file and the daemon's socket, and since 2026-10-28 each declared peer's
   directories, as [`PROJECT_CONFIG.md`](PROJECT_CONFIG.md) specifies under guards.
   A document whose `allowRead` or `allowWrite`
   reopens one of them, or whose `allowUnixSockets` names a socket inside one, is
   refused, after `~` expansion and symbolic link resolution. `dromond` checks the
   project file's documents before it starts, and a line whose document fails the check
   at its first turn has that turn end before any command runs.
   **Round 17, review of 0db077c, finding 1, decided by the operator.** The project's
   `secret` directories, at the project root and in the line's working directory, are
   protected in the same way for every Claude Code line, by real path, where they
   exist when the line's engine is built. Codex and Grok lines have had them since
   round 9. So the disclosure list under `secret/` is closed to every line, and the
   gate a line runs in its own sandbox skips the disclosure scan with its reason,
   while `full-gate`, which runs outside every sandbox, scans. **Where a line's own
   documents could remove it.** A sandbox document's `allowRead` or `allowWrite`
   entry inside one of them is refused as above, at `dromond`'s start for the project
   file and at the line's build for an override. A settings `allow` rule does not
   reopen it, since Claude Code applies a `deny` rule first. A document that leaves the
   sandbox disabled removes the shell's denial, as for every protected directory,
   while the file tools stay denied by the settings. **Residuals.** A directory made
   after the build is denied from the line's next build. A line whose sandbox is
   disabled can read them with its shell.
   **Round 18, the operator's security fix.** The known credential paths,
   `security_paths.CREDENTIAL_PATHS`, are protected in the same way for every Claude
   Code line. Each is taken under the real path of the daemon's home, where it exists
   when the line's engine is built, by that name and by its real path, a rule the
   follow-up below widens. Codex and Grok
   lines have had them since round 9. The file tools are denied each path itself as
   well as what lies under it, with `Read(//PATH)` and `Edit(//PATH)` beside the
   `/**` rules, since several entries, such as `~/.netrc` and `~/.claude.json`, are
   files. A grant of a path inside one, or of the path itself, is refused as above,
   and so is an agent socket of `[git_ssh]` inside one. A grant of a directory that
   holds one, such as the home, was accepted, which the follow-up below reverses.
   **Why the lines differed.** **Fact, from `projectconfig.py`.** A `profile` is
   expanded only for the lines the project file declares. **Fact, from the
   confinement checks of 2026-11-18.** Lines the client created reported the home
   ssh directory listable and Codex's sign-in file readable. Lines the project file
   declares reported both denied. **Inference.** The dogfood project's Claude Code
   profile, as [`DOGFOOD.md`](../process/DOGFOOD.md) gives it, carries `Read` deny
   rules for those paths, and Claude Code also applies a `Read` deny rule to its
   sandbox, so only the lines given that profile had them. Before this fix the rule
   came from the project's own documents. Dromon itself denied a Claude Code line
   only the protected and secret directories. **What Claude Code keeps.** Nothing,
   as `security_paths.CLAUDE_CODE_EXEMPT` records, read from the bundled Claude Code
   2.1.310. Its sign-in lives in the system keychain, or else in
   `~/.claude/.credentials.json`, and its account in `~/.claude.json`. The engine's
   own process reads both, and neither the sandbox nor the rules confine that
   process. **Residuals.** The list is not exhaustive. The keychain is not a path,
   and whether a command in an enabled sandbox can ask it for an item was not
   tested. A line whose sandbox is disabled can read every listed path with its
   shell, as it can every protected one.
   **Round 18 follow-up, from the audit line's review, each defect confirmed in the
   code or a test before the change.** *Missing and linked paths, item 4.* Every
   listed path is denied whether or not it exists, under the daemon's home as the
   process names it and under its real path, and also by the entry's own real path
   when it exists and a link makes that differ. A line's engine is built once and
   kept, so a file made later, by a sign-in, was readable for the daemon's life.
   **Fact, read from the bundled Claude Code 2.1.310.** Its Linux sandbox places a
   placeholder over a missing deny path, and logs where it cannot, and no step that
   skips a missing deny path was found for Seatbelt, whose rules match paths and need
   no file. **Residual.** The real form of a path made after the build is added at the
   line's next build. *Grants that could reach one, item 3.* An `allowRead` or
   `allowWrite` entry is refused when it is a listed path, a directory that holds one,
   such as the home or `/`, or a path inside one, and when it is a pattern whose
   literal base is any of those. The base of a pattern is the directory of the text
   before its first `*`, `?`, `[` or `{`, so `~/.s*` has the home as its base and is
   refused, while `~/.cache/*` and `~/.cache/uv` are accepted. **Fact, from the same
   bundle.** It expands an `allowRead` pattern into the paths it matches. **Fact,
   recorded above, observed live on 2026-11-01.** An `allowRead` lifts a `denyRead`
   above it. The protected and secret directories keep the rule above, which refuses
   only a grant inside them, since a line's working directory holds its own `secret`.
   **Residual.** A pattern under one of those, such as a grant of `PROJECT/s*`, is
   not refused. *The keychain, item 7.* **Fact, reported by the audit line.** A
   lookup of a made-up item from a command in an enabled sandbox reached the keychain
   service and was told no such item exists. Whether the item that holds a sign-in is
   returned rests on the keychain's own access rules and was not tested, since the
   test would read a credential. It stays a residual. *Paths not added, item 5.*
   `~/Library/Keychains`, `~/.config/gcloud` and `~/.azure` are not in the list every
   engine takes, since it also binds Codex and Grok lines and that was not settled
   for Grok. The last pass below adds them for Claude Code lines alone.

   **Round 18, last pass.** *Letter case, item 1.* **Fact, confirmed before the
   change.** A grant of `~/.SSH` was accepted, and on a volume that ignores case it
   names the ssh directory. Both refusals now compare real paths in folded case, the
   credential rule and the rule for protected and secret directories, on every host.
   Folding always was chosen over asking whether a volume ignores case, since that is
   a property of each directory, a volume may be mounted from elsewhere, and the
   answer would differ between hosts and so between test runs. **Cost.** On a volume
   that keeps case apart, a grant of another directory whose name differs from a
   listed path only in case is refused too. **Residual, from the audit line,
   untested.** `$HOME/.ssh` and `~other/.ssh` are accepted as text that is neither
   absolute nor home-relative. Whether the engine expands either was not tested.
   *A second list for Claude Code, item 5.* `security_paths.CLAUDE_CODE_ONLY_PATHS`
   holds `~/Library/Keychains`, `~/.config/gcloud` and `~/.azure`. They are denied to
   every Claude Code line's shell and file tools as the list above is, a grant that
   could reach one is refused as above, and no Codex or Grok line takes them.
   **Fact, read from Grok 1.0.70.** Its binary carries the Google default credentials
   loader and its sandbox code names the login keychain's files, so its own list is
   left as it was. **Residual.** The keychain service is not a path. A command in the
   sandbox that asks the service for an item is answered by the service's own
   process, which no file denial binds, so denying the keychain files does not stop a
   query to the service. **Hypothesis, not tested.** A shell command whose keychain
   library opens the login keychain's file itself now fails, and a command that signs
   in to Google Cloud or Azure from the shell finds no credentials.
4. **Git hooks and configuration.** When the line's working directory lies in a git
   checkout, an enabled sandbox also denies writes to the hooks and configuration git
   would act on there, whether or not any layer grants the git directory. A command
   that could write them would plant code that git later runs outside every sandbox, in
   the operator's own commit or push. The paths are found by reading the checkout's
   `.git`, a directory or a file of the form `gitdir: PATH`, and the git directory's
   `commondir` file, following relative paths as git does, without running git. They
   are the common git directory's `hooks` and `config`, and the git directory's
   `config.worktree`. For a linked worktree, whose `.git` is a file, they also include
   that file and its `commondir`, since rewriting either would hand git a directory of
   the line's own making. **Round 14, review of 1449c5c, C1.** They also include the
   common directory's `refs/remotes` and `packed-refs`, where git's files backend
   keeps the remote-tracking refs, so that only the operator's fetch moves the refs a
   commit run may name, as [`RUNS.md`](../architecture/RUNS.md) gives. Only writes
   are denied, so the line can still read them and commit.
   **Round 14, re-review of 522c146, item 3. What the `packed-refs` deny costs.**
   Git rewrites `packed-refs` whole, under a new inode, whenever a packed ref is
   removed or the refs are packed. So for a line with an enabled sandbox, deleting or
   renaming a branch that is in `packed-refs`, `git tag -d` of a packed tag, `git
   update-ref -d` of a packed ref, `git pack-refs` and `git gc` fail. A commit on a
   packed branch, deleting a branch that is only a loose ref and creating a tag leave
   `packed-refs` unchanged and keep working. **Fact, observed by the audit line in a
   scratch repository on 2026-11-01, watching the inode.** `git gc --auto`, which a
   commit or a merge may start, is expected to fail at its packing step and print an
   error without failing the commit. **Hypothesis, not observed.** These tasks are
   left to the operator's own sessions. Fetching, which writes `refs/remotes`, was
   already out of reach, since a sandboxed line has no network and cannot add a
   remote with `config` guarded.
   **Round 14, re-review of 522c146, C1a. Renaming a parent.** A deny on a path
   would not hold if a line could rename its parent, such as `refs`, aside and put a
   directory or a link of its own in its place. Claude Code also denies
   `file-write-unlink`, which a rename of its source needs, on each parent of a
   denied path that lies inside a writable root, so `refs` and the common directory
   itself cannot be renamed or removed while `refs/heads` stays writable. **Fact,
   read on 2026-11-01 in the sandbox profile generator bundled with Claude Code
   2.1.316, and by the audit line in 2.1.310, which denies `file-write-unlink` and
   `file-write-create` on each ancestor. Inference, not tested live, that Seatbelt
   then refuses the rename.** Linux, whose sandbox mounts each denied path, a line
   whose sandbox is disabled, and the other engines were not read. Claude Code's
   settings have no separate rule for renames, so Dromon adds none. The daemon also
   checks the refs it trusts with `lstat` before a commit run, which refuses a link
   but not a real directory a line made. **Round 14, final check of 951b9e7, item 2.**
   It therefore also refuses a commit run when `refs` or `refs/remotes` is not the
   directory it found as it started, as [`RUNS.md`](../architecture/RUNS.md) gives,
   so a swap after the start is refused even where the sandbox's rule does not hold.
   A `.git` or `commondir` that cannot be read, or that is malformed, ends the
   turn before any command runs, and `dromond` refuses a project file with such a line.
   Git itself refuses such a checkout. **Fact, observed on 2026-10-26 with Claude
   Code 2.1.310 on macOS.** A deny entry wins over an allow entry. A Haiku line in a
   linked worktree, granted the whole common git directory, committed, while its
   writes to that directory's `hooks` and `config` failed with `Operation not
   permitted`.
5. **Unsandboxed retries.** Dromon's permission gate refuses a Bash, PowerShell or
   Monitor call that sets `dangerouslyDisableSandbox` while the sandbox is enabled
   without `allowUnsandboxedCommands`, whatever the line's tool policy.
6. **Git over SSH.** For a line the project file gives its `[git_ssh]` agent, an
   enabled sandbox gets one layer merged after every other, before the steps above
   apply, which allows the agent's socket and `github.com` and denies writing the
   `known_hosts` and public key files, as [`PROJECT_CONFIG.md`](PROJECT_CONFIG.md)
   specifies. Added on 2026-10-29.

## Parity and its limits

**Fact, verified on 2026-10-25 on macOS.** A live turn on Claude Haiku 5.5 with only
`{"enabled": true}` ran two Bash commands. The one that wrote inside the project
succeeded and the one that wrote in the home directory was refused.

**Not at parity.**

- Claude Code merges `Read` and `Edit` permission rules into the sandbox and lets
  `WebFetch` rules pre-allow domains. Since 2026-10-25 a line's permission rules reach
  Claude Code, as [`SETTINGS.md`](SETTINGS.md) specifies, so this is expected to apply,
  but it is an inference that has not been tested. As in Claude Code, `denyRead` does
  not restrict the built-in Read tool and `allowedDomains` does not restrict WebFetch.
  Dromon's own deny rules keep the built-in file tools out of its protected
  directories.
- There is no user or managed layer, so nothing can make the sandbox mandatory against
  the operator's own configuration.
- The keys refused above, including credential masking, are missing.
- As in Claude Code, Dromon's own tools, MCP servers and hooks run outside the sandbox.
  Dromon's tools run inside the daemon.
- A line without an enabled sandbox can still reach the daemon's socket through its
  shell. The daemon's peer credential check, as `CLIENT.md` specifies, refuses only a
  process of another user, and a line runs as the operator's user. A project can require an enabled sandbox for every Claude Code line
  with `daemon.require_sandbox`, as [`PROJECT_CONFIG.md`](PROJECT_CONFIG.md) specifies.
- The git guard protects only the files of its set, the hooks directory, `config`,
  `config.worktree`, `refs/remotes`, `packed-refs` and a linked worktree's two
  pointers. Every other file under the
  git directory stays as writable as the line's grants make it. Git runs no other file
  there as code unless configuration tells it to. A filter or merge driver, for
  example, is named in `.gitattributes` but defined in configuration, which is guarded.
  The exception is a file that the existing configuration already includes with
  `include.path` or `includeIf`, which the guard does not follow.
  **Inference, from git's documentation of attributes, drivers and includes, not
  tested.**
- The git guard covers the checkout of the line's working directory only. A
  `core.hooksPath` in the operator's global git configuration, a `GIT_DIR` set in the
  environment, and other repositories the line can write are outside it. Codex and Grok
  lines keep their own rules, which leave `.git` read-only in their workspace, as
  [`CODEX.md`](CODEX.md) and [`GROK.md`](GROK.md) specify.
- Linux and WSL2 behaviour is specified by Claude Code and has not been verified live
  by this line. Continuous integration runs the unit tests on Linux.

The repository gate clears `GIT_DIR`, `GIT_COMMON_DIR` and `GIT_WORK_TREE` before
its first Git call and before selecting its working directory. Clearing them only
at disclosure lookup allows the initial directory selection to follow a decoy.
This protects checkout selection from those environment variables. It assumes the
Git executable and the checkout metadata are trusted, subject to the worktree
back-pointer checks in the gate.

The repository gate supports the platform Bash, including version 3.2 on macOS.
With unset-variable checking enabled, Bash 3.2 treats an empty array expansion as
an unset variable. Array arguments therefore use guarded expansion so an empty
list supplies zero arguments. Gate tests select `/bin/bash` when it exists and
otherwise use Bash from the executable search path. An alternate shell selected
by the environment is not evidence that the platform shell works.
