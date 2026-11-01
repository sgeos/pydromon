Type: Changed

- Round 14. The result of `write_scratch` gives the bytes written and the absolute path
  of the file, in place of the relative path. A budget that is unknown is a named entry
  with `status` `unknown` in `role_status` and `line_status`, never `null`, and a
  field of a stated budget that the statement left out says `unknown` too. The
  `estimate` of `cost_usd` says `unknown` in the status tools where it was `null`,
  while the client protocol's `cost_estimate_usd` stays `null`. The budget line at the
  head of each turn's prompt says that turns of unknown cost are counted as $0.00. A
  file for `check_disclosure` may now exceed 1 MiB, up to 16 MiB combined.
