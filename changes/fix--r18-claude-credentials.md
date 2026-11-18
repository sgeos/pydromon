Type: Fixed

- Every Claude Code line is now denied the known credential paths under the
  daemon's real home, such as the ssh directory and other engines' sign-in files.
  The denial covers reading and writing, by name and by real path. Its sandbox
  denies them to the shell, and `Read` and `Edit` rules deny the file tools each
  path and what lies under it, even when its sandbox is disabled. Codex and Grok
  lines already had this. A sandbox document that grants one of them back is
  refused, at the line's build and when `dromond` checks the project file.
  Previously only lines given a project file profile with such rules had them, so
  lines the client created could list the ssh directory and read Codex's sign-in.
  Claude Code keeps none of them, since its engine reads its own sign-in outside
  both the sandbox and the rules.
- A Claude Code line that no layer enables or disables now runs with an enabled
  sandbox, so its shell is held by the credential denials too. Its shell then
  writes only where the sandbox allows and reaches no host unless a layer allows
  one. An explicit `enabled = false` is kept, and `dromond`, `dromon config` and
  the line's build warn that its shell can read the credential paths.
- `allowUnsandboxedCommands` is forced false for every Claude Code line, and an
  enabled sandbox that names `excludedCommands` is refused, since both let a
  command run outside the sandbox.
- A sandbox grant of a credential path, a directory that holds one, such as the
  home, or a pattern whose literal base is any of those, such as `~/.s*`, is
  refused. Before, only a grant inside one was.
- Every credential path is denied whether or not it exists, and under the home as
  named and by its real path, so a file made after a line's engine was built, or
  reached through a linked home, is no longer open.
- A sandbox grant that differs from a credential path or a protected directory
  only in letter case, such as `~/.SSH`, is refused, since a volume that ignores
  case names the same directory by it.
- Claude Code lines alone are also denied `~/Library/Keychains`,
  `~/.config/gcloud` and `~/.azure`, with the same refusal of grants. Codex and
  Grok lines are not, since Grok reads two of them itself. The keychain service
  is not a path, and a query to it is not stopped.
- `dromon config` reports each line's sandbox as the build gives it, such as
  `sandboxed by default`, rather than the merge of its layers.
- A new `[daemon] default_profile` names a profile that lines created or
  configured through the client take, when they run on the profile's engine, so
  they get the profile's sandbox denials and ask rules. Lines that already exist
  take it when next configured.
- On a Linux host without `bwrap` or `socat`, a start, `dromond --check` and
  `dromon check-engines` warn of every Claude Code line whose sandbox is enabled,
  since each would fail at its first turn.
