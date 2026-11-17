Type: Fixed

- Recognise a running engine launcher before shortening its process command for
  display. The module bootstrap could put the launcher marker beyond that prefix
  and cause the daemon to terminate a healthy engine during its record sweep.
- Keep defunct process markers and the process table reader marker through the
  same display truncation so that tree walks and child ledgers retain those checks.
