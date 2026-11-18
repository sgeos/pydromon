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
