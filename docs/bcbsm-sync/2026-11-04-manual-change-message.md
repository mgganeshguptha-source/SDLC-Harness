# BCBSM sync — clearer MANUAL CHANGE REQUIRED message + external class check, 2026-11-04

Copy from mgganeshguptha-source/SDLC-Harness (after merge) into
BCBSM/dx_SDLC-Harness, commit to master, `git tag -f v1`, `git push -f origin v1`.

## Engine files — replace whole files

| File | Change |
|---|---|
| harness/blocked.py | Reads MISSING and SEARCHED; the halt message shows them, asks for a developer check, gives routes for "needed" and "not needed", and no longer asks for start_phase |
| harness/sdk_runner.py | Coding prompt: search before declaring a class missing; new MISSING and SEARCHED lines; never guess Maven coordinates. prompt_steps prompt: new External Classes section |
| harness/plan_check.py | External class check after prompt_steps: halts on UNKNOWN or NEW_DEPENDENCY |
| harness/attachments.py | Input guard message: no start_phase |
| harness/state_machine.py | Run cap, SDK error, coverage and environment messages: no start_phase. Calls the external class check before coding |
| harness/resume.py | Changed-story log line: start a fresh run; no start_phase |
| harness/test_blocked_message.py | NEW — tests |
| harness/test_external_classes.py | NEW — tests |

No config, caller or toolkit change. If a BCBSM file differs from this repo, apply
the change by hand instead of replacing the file.
