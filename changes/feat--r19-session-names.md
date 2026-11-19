Type: Added

- Each Claude Code session Dromon starts is given a display name with `--name`, from
  the project file's new `daemon.session_name` template, whose default gives names such
  as `PDr00-pydromon-review-luna-Ha55lo` from the version, project, role, seat variant,
  model and effort. Two live lines are never given one name, since digits are appended
  to the variant. A template is checked when the file is read, and `false` turns naming
  off. A name that is not one safe argument, is too long, holds a disclosure term or
  needs a model without a code is not given, with a warning. `PROJECT_CONFIG.md`
  records the rule and its residuals.
