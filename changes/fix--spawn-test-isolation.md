Type: Fixed

- Every test starts with an empty list of stranded child starts in `dromon.spawn`, and the real list is put back afterwards, through an autouse fixture. A real `git` start that stalled past the bound under load left the count raised, and later tests that count stranded starts or start past the cap failed for a cause that was not theirs. The bound is unchanged.
