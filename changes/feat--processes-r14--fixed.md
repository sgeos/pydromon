Type: Fixed

- The kill-all pre-turn check no longer holds a line on one slow `ps`. It retries a
  timeout with attempts of 10, 20 and 30 seconds within 65 seconds, and the hold names
  each measured time. `list_processes` no longer returns an empty list when `ps` timed
  out or no environment could be read, and answers `unavailable` with the reason.
