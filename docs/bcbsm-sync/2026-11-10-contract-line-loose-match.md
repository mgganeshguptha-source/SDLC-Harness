# BCBSM sync — contract line read with small wording differences, 2026-11-10

Copy from mgganeshguptha-source/SDLC-Harness (after merge) into
BCBSM/dx_SDLC-Harness, commit to master, `git tag -f v1`, `git push -f origin v1`.

## Engine files — replace whole files

| File | Change |
|---|---|
| harness/handoff.py | The contract line is also found when its label differs slightly; a close-but-unreadable line is quoted in the halt message |
| harness/test_handoff.py | More tests |

No config, caller, story template or toolkit change.
