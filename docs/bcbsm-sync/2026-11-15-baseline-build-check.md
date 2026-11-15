# BCBSM sync — baseline build check, 2026-11-15

Copy from mgganeshguptha-source/SDLC-Harness (after merge) into
BCBSM/dx_SDLC-Harness, commit to master, `git tag -f v1`, `git push -f origin v1`.

**Run it once in the Apexon environment first.** The automated tests use a
stand-in for Maven. One real run on petclinic-rest-api with a test broken on
the base branch must show that Maven honours the ignore flag (see "Check on
the first real run").

## Why

OP018-6434 (2026-11-15): tests unrelated to the story were red, the build gate
treated them as caused by the story, and `coding` changed five unplanned
production files to make them pass. A missing library was also found only after
the model phases had run.

## What changes

With `runs_build: true`, a fresh build-mode run builds the untouched base code
once before the first AI phase:

| Base code | What happens |
|---|---|
| Missing library, toolchain, network | Environment halt before any model call. Not saved. |
| Compile error, or a failing build with no per-test results | Halt "existing build failure" (gate `baseline_build`). Fix the base branch, start a fresh run. |
| Tests already failing (Maven) | Recorded; the run continues. The build gate fails only on tests not on the list. Phases are told to leave them alone. The PR lists them. |
| Everything passes | The run continues as before. |

The result is saved on `harness-metrics` under `metrics/baseline/`, keyed by
the base commit, the `dx_Artifactory` commit and `test_command`. Later runs on
the same key skip the build. A resume never repeats the check.

`runs_build: false` — no baseline check, as today. No new config key.

## Engine files — replace whole files

| File | Change |
|---|---|
| harness/baseline.py | NEW. The check, the saved result, prompt and PR-body text |
| harness/validation.py | `run_validation` takes `known_failing` and `baseline`; per-test judgement from surefire XML; `_surefire_failures` can be limited to given classes |
| harness/run.py | `autorun` runs the check before the first phase |
| harness/state_machine.py | Passes the already-failing list to the build gate; adds the list to the PR body |
| harness/sdk_runner.py | Phases are told which tests already fail |
| harness/state.py | `baseline` on the run state |
| harness/halt_gates.py | New gate `baseline_build` |
| harness/metrics.py | Schema 9: `baseline_status`, `baseline_failing_tests`, `baseline_reused` |
| tools/metrics_dashboard.py | Label for the new gate |
| harness/test_baseline.py | NEW. 18 tests |
| README.md, docs/service repo-config/config-*.yaml | Documentation |

No workflow change: `metrics/` is already published to `harness-metrics` and
kept out of the WIP branch and the PR.

## Service repos

Nothing to change. Repos with `runs_build: true` get one extra build on the
first fresh run after each change of the base branch, the library store or
`test_command`.

## Check on the first real run

1. The log shows `=== Baseline build check (before any AI phase) ===` and
   `test failures do not stop the build (-Dmaven.test.failure.ignore=true)`.
2. With a test broken on the base branch: the log lists it under "already fail
   before any change by this run", the run continues, and Maven prints
   `BUILD SUCCESS` for that build. If Maven prints `BUILD FAILURE` on the test
   failure instead, the flag did not reach surefire (the repo's pom sets
   `testFailureIgnore` itself, or the Maven in the store ignores MAVEN_OPTS):
   the run then halts as an existing build failure — safe, but stricter than
   intended. Report it.
3. `harness-metrics` has `metrics/baseline/<base>-<store>-<command>.json`.
4. A second fresh run on the same base logs "already checked ... not building
   again".

## Known limits

- Angular/Karma and Gradle builds write no surefire XML: a failing base build
  halts; already-failing tests cannot be ignored there.
- A test that already fails cannot catch a fault in the code it covers. The PR
  section makes that visible.
- A test that fails only sometimes can be recorded as already failing, or be
  blamed on the story.
- The saved result of a compile failure is reused for the same base commit;
  once the base branch is fixed it has a new commit and is built again.
