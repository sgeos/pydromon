Type: Fixed

- A term that spans the join of a run's note and the notice that its summary or tail was
  withheld no longer reaches the event log. The joined text is screened last, and a
  note that holds a term is replaced by a fixed notice.
- `Read(/*/secret)` and other one-slash rules with a pattern in the first segment are
  reported, and a project that has a directory named like one at the root, such as
  `tmp`, is no longer warned of wrongly for a rule that means its own.
