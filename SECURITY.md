# Security Policy

## Supported versions

pydromon is an unreleased prototype. Only `main` is supported, and nothing is published
to a package index.

## Reporting a vulnerability

Please report security issues privately, so that a fix can be prepared before public
disclosure. Open a draft advisory at
<https://github.com/sgeos/pydromon/security/advisories/new>. The maintainer is notified
and can work on the fix with you in the same place.

Please include the commit, a minimal way to reproduce the issue, the expected and
observed behaviour, and your assessment of severity if you have one. Remove any
credential, token or message body you would not publish before attaching output.

## Response

The maintainer aims to acknowledge a report within a few days, and to either confirm the
issue or explain why it is not a vulnerability. Confirmed issues are fixed on `main`,
with a disclosure timeline agreed with the reporter.

## Scope

In scope are the boundaries pydromon draws and the guarantees it states in
[`docs/protocol/`](docs/protocol/README.md).

- **The operator socket.** A request on `dromond.sock` carries operator authority. A
  process that reaches it without running as the operator's user is a vulnerability.
- **The agent socket.** An external agent acting beyond the access level the project
  file and its registration give it, a registered name claimed without its token, or a
  token recovered from the event log, which stores only digests.
- **The line socket.** A request on `lines.sock` that acts as a line without the token
  the daemon issued for it, or that reaches anything but that line's own tools.
- **Sandboxing.** A Claude Code line whose enabled sandbox, or a Codex or Grok line whose
  permission profile, lets a command read or write Dromon's state or runtime directory,
  or write `.git` where Dromon says it cannot.
- **Credential policy.** A line that starts usage billed per token without its driver's
  strategy allowing it, or Dromon reading, storing or forwarding an engine's sign-in
  token.
- **Isolation.** A line offered MCP servers, connectors or tools its configuration did
  not give it, without the turn being refused.
- **Untrusted input.** A message from a line, an external agent or an engine that gains
  operator authority.

Not in scope are the following.

- The engines' own sandboxes and permission systems, which belong to Claude Code, Codex
  and Grok Build. A defect there belongs with their vendors, although a pydromon
  configuration that relies on a documented behaviour that does not hold is in scope.
- Any process running as the operator's own user, which the operating system already
  trusts with the operator's files. `docs/protocol/CLIENT.md` records that the daemon
  has no peer credential check.
- Behaviour that `docs/protocol/` already lists as not verified or not at parity.
