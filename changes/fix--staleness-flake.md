Type: Fixed

- The staleness test of a prompt during a hung git no longer fails on a loaded runner. It waited a fixed 30 seconds for git's worker, which failed once on macOS with Python 3.15, and measured that the turn took under five seconds. It now waits for a bound derived from the worker's own constants, and proves the turn ended while git was still outstanding. This is a test change only, and the cause is a hypothesis, a slow process start, since the failed log was not read.
