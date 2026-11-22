Type: Fixed

- Upgrade checks validate Grok profiles against live protection paths rather than
  fresh copy directories. Deficient profiles on dormant lines warn while active
  lines still fail closed. Awakening and ownership plans validate dormant profiles
  before recording events. Document older profile migration and ancestor coverage.
