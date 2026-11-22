Type: Fixed

- Upgrade checks validate Grok profiles against live protection paths rather than
  fresh copy directories. Deficient profiles on dormant lines warn while active
  lines still fail closed. Awakening and ownership plans validate dormant profiles
  before recording events. Document older profile migration and ancestor coverage.
- Awakening and ownership checks use the current binding and configuration after
  client changes. An empty runtime override selects the normal default directory
  during upgrade checks.
- Upgrade start checks retain host sandbox warnings as diagnostic notes without
  changing a successful outcome. Dormant Grok profile warnings still request
  repair through a warning outcome.
