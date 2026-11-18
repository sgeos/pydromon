Change: `run_admitted` and `run_admission_ended`, which older daemons refuse as unknown event kinds
Search: "run_admitted"
Path: src/dromon/codec.py

Change: `run_started.dirty_paths` and `run_started.admitted_by`, written whenever a run in a working directory starts with its status read or under an override, and `run_ended.coverage`, written whenever a run's output ends with a test summary
Search: _optional_int(data, "dirty_paths")
Path: src/dromon/codec.py
