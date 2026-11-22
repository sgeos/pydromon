Type: Fixed

- The stalled start test observes the original pipe rather than a numeric
  descriptor that another thread may reuse. A forced reuse case covers the race.
- The exited harness test waits for the test to observe its live child before
  exiting, so successful adapter cleanup cannot race that observation.
