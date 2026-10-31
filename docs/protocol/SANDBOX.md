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
  "excludedCommands": ["docker"],
  "filesystem": {"allowWrite": ["~/.kube"], "denyRead": ["~/.ssh"]},
  "network": {"allowedDomains": ["github.com", "*.npmjs.org"]},
  "credentials": {"envVars": [{"name": "GITHUB_TOKEN", "mode": "deny"}]}
}
```

| Key | Type | Who may set it |
|---|---|---|
| `enabled`, `failIfUnavailable`, `allowUnsandboxedCommands`, `autoAllowBashIfSandboxed` | boolean | Anyone |
| `enableWeakerNestedSandbox`, `enableWeakerNetworkIsolation` | boolean | Anyone |
| `excludedCommands` | array of command patterns | Anyone |
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

When a line's turn starts, Dromon merges the layers and derives the settings it hands to
Claude Code. Nothing is passed when no layer sets anything, and the line then has no
sandbox but any managed policy, because Dromon starts Claude Code without user or
project settings.

1. **Paths.** A relative path is resolved against the line's working directory, its
   `workdir` as [`LINE_CONFIG.md`](LINE_CONFIG.md) specifies, which is the project root
   by default. A path that starts with `/` or `~` is passed as written, and Claude Code
   expands `~`.
2. **Unattended defaults.** When the document enables the sandbox, Dromon fills in
   `failIfUnavailable` as true, `allowUnsandboxedCommands` as false and
   `network.strictAllowlist` as true, unless the document sets them. Claude Code's own
   defaults ask a person instead, or run unsandboxed when the sandbox cannot start, and
   a Dromon line has nobody at the prompt.
3. **Protected directories.** An enabled sandbox denies reading and writing the
   project's state directory and runtime directory, which hold the event log, the
   project file and the daemon's socket, and since 2026-10-28 each declared peer's
   directories, as [`PROJECT_CONFIG.md`](PROJECT_CONFIG.md) specifies under guards.
   A document whose `allowRead` or `allowWrite`
   reopens one of them, or whose `allowUnixSockets` names a socket inside one, is
   refused, after `~` expansion and symbolic link resolution. `dromond` checks the
   project file's documents before it starts, and a line whose document fails the check
   at its first turn has that turn end before any command runs.
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
   the line's own making. Only writes are denied, so the line can still read them and
   commit. A `.git` or `commondir` that cannot be read, or that is malformed, ends the
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
  `config.worktree` and a linked worktree's two pointers. Every other file under the
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
