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

When a line's turn starts, Dromon merges the layers and derives the settings it hands to
Claude Code. Nothing is passed when no layer sets anything, and the line then has no
sandbox but any managed policy, because Dromon starts Claude Code without user or
project settings.

1. **Paths.** A relative path is resolved against the project root. A path that starts
   with `/` or `~` is passed as written, and Claude Code expands `~`.
2. **Unattended defaults.** When the document enables the sandbox, Dromon fills in
   `failIfUnavailable` as true, `allowUnsandboxedCommands` as false and
   `network.strictAllowlist` as true, unless the document sets them. Claude Code's own
   defaults ask a person instead, or run unsandboxed when the sandbox cannot start, and
   a Dromon line has nobody at the prompt.
3. **Protected directories.** An enabled sandbox denies reading and writing the
   project's state directory and runtime directory, which hold the event log, the
   project file and the daemon's socket. A document whose `allowRead` or `allowWrite`
   reopens one of them, or whose `allowUnixSockets` names a socket inside one, is
   refused, after `~` expansion and symbolic link resolution. `dromond` checks the
   project file's documents before it starts, and a line whose document fails the check
   at its first turn has that turn end before any command runs.
4. **Unsandboxed retries.** Dromon's permission gate refuses a Bash, PowerShell or
   Monitor call that sets `dangerouslyDisableSandbox` while the sandbox is enabled
   without `allowUnsandboxedCommands`, whatever the line's tool policy.

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
  shell, because the daemon does not check peer credentials. This is recorded as debt
  in `CLIENT.md`.
- Linux and WSL2 behaviour is specified by Claude Code and has not been verified live
  by this line. Continuous integration runs the unit tests on Linux.
