Type: Changed

- Task of 2026-11-05, disclosure budget. A `check_disclosure` that passes is recorded and spends
  nothing, and only failing checks and refusals for disclosure count against the
  hourly limit of 10. Once the limit is reached every check is refused, one that would
  pass too. A refused `set_brief` names the one field that matched, and still the
  generic refusal when two or more did, so no refusal names more than one field and
  none a term, line or position.
