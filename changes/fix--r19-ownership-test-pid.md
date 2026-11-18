Type: Fixed

- The ownership tests' fake process tables used fixed identifiers, and the reading skips the daemon's own pid, which in a test is the test process's, so a runner whose pytest process had one of them saw a count one short. The identifiers now differ from the test process's own, and a fake table refuses it.
