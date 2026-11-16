# BCBSM sync — review gate, compile check, fix-loop file limit, 2026-11-16

Builds on the baseline build check (2026-11-15-baseline-build-check.md). Sync
that first, or both together.

Copy from mgganeshguptha-source/SDLC-Harness (after merge) into
BCBSM/dx_SDLC-Harness, commit to master, `git tag -f v1`, `git push -f origin v1`.

**Run it once in the Apexon environment first** (petclinic-rest-api): the
compile check has not been run against real Maven.

## Why

Three gaps seen on OP018-6434 (2026-11-15):

1. The reviewer wrote `CHANGES_REQUESTED` (a null inside `Flux.zip`); the gate
   logged `PASS`. A finding without a recognised blocking class was treated as
   a note, and nothing in the log said so.
2. Code that did not compile was reviewed and had tests written for it before
   the build gate found out — twice.
3. `coding`, sent back with failing tests, edited production files the plan
   never listed, and removed a membership check.

## What changes

| | Before | Now |
|---|---|---|
| Review gate | `CHANGES_REQUESTED` with no blocking-class `[ISSUE]` became PASS, silently | Becomes PASS only when every finding is a `[NOTE]` or carries a style class, and the log says so. An untagged or unknown-class `[ISSUE]`, or an objection written as prose, blocks |
| Compile check | First compile was after `unit_testing` | The harness compiles after `coding`, before `code_review`. A compile error goes back to `coding` (limit: `max_validation_retries`), then halts |
| Fix-loop file limit | In any loop `coding` could edit any existing production file | When the build gate or compile check sent it back, `coding` may edit the plan's files, files the run already changed, and a file the compiler names. Anything else is reverted and the run halts |

Not limited: the first coding pass, and loops ordered by the reviewer or the
AC gate (an independent phase named the need).

## Engine files — replace whole files

| File | Change |
|---|---|
| harness/review.py | Advisory classes; unclassified findings block; prose objection blocks |
| harness/compile_check.py | NEW. The compile check and its command |
| harness/fix_scope.py | NEW. The file limit, the revert, the halt and prompt text |
| harness/state_machine.py | Logs a review downgrade; runs the compile check; halts on the file limit; records which gate ordered a loop |
| harness/executor.py | Applies the file limit after a coding pass |
| harness/validation.py | `run_validation` can run a compile command |
| harness/sdk_runner.py | `coding` is told the file limit in a build-fix loop |
| harness/state.py | `compile_attempts`, `fix_loop` |
| harness/contracts.py, harness/halt_gates.py | Exit code `FIX_SCOPE_VIOLATION` (17) → gate `scope` |
| harness/config.py | `compile_command` |
| harness/resume.py | A resume clears the fix-loop state |
| harness/run.py | Audit folder keeps `unplanned-edits.patch` |
| harness/test_review_gate.py, harness/test_fix_loop_controls.py | NEW. 23 tests |
| README.md, docs/service repo-config/config-*.yaml | Documentation |

No workflow change.

## Service repos

- **Maven repos:** nothing to change. With `test_command: mvn ... test` the
  compile command becomes `mvn ... compile`.
- **Angular repos:** the compile check is skipped unless `compile_command` is
  set in `.harness/config.yaml`. Decide per repo; the build gate after
  `unit_testing` still applies either way.
- `compile_command: "off"` turns the check off for a repo.

## Check on the first real run

1. After `coding` the log shows `[compile] compiling the production code before
   code review` and the derived command.
2. A deliberate compile error is sent back to `coding` with the compiler's
   lines, and `code_review` does not run until it compiles.
3. A review with `VERDICT: CHANGES_REQUESTED` and an untagged `[ISSUE]` loops
   back to `coding`; the log says the finding carries no recognised class.

## Known limits

- The halt after a reverted edit leaves the other edits of that pass in place;
  the working branch may not compile until the developer decides.
- `.harness/unplanned-edits.patch` holds source code and is copied to the run's
  audit folder on `harness-metrics`.
- Fail-closed review: a reviewer that asks for changes without a class tag now
  costs a coding loop even when the finding is minor. The loop is bounded by
  `max_review_retries`.
