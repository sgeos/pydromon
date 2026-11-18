Type: Fixed

- A launch record whose harness's start time cannot be read, while its process is there,
  is no longer removed at once, so a live harness whose launcher has gone is looked at
  again. The record is kept for up to `MAX_START_RETRIES` sweeps, the count is written
  into it as `start_retries`, and it is removed when the time reads and differs, when no
  process holds the pid, when the count is spent or cannot be written. Nothing is
  signalled on a time that does not read.
