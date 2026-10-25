# BCBSM sync — frontend support (BUILT #32–#33), 2026-10-25

Copy these changes from mgganeshguptha-source/SDLC-Harness (after merge) into
BCBSM/dx_SDLC-Harness, commit, then `git tag -f v1`, push master, `git push -f origin v1`.

## 1. Check each file, then replace whole files

In the BCBSM engine clone run, for each file:

    git rev-parse HEAD:<file>

If it prints the hash below, the BCBSM file is identical to the personal repo's
version before this change: replace it with the merged version. If it differs,
the BCBSM copy has its own edits — apply the PR diff for that file by hand.

| File | Hash before this change |
|---|---|
| harness/boundaries.py | 222cf62ff9ab2595c705d66a0159ca5ee447030e |
| harness/config.py | d59aca5e10eee149eaacaa479dc93bbb42ba721c |
| harness/config.yaml.sample | 9e3d67846e12ca889d34f72a180e57af8ff68610 |
| harness/execution_record.py | e9d3f9e924d3dbfa3e320df91422071c2d963e32 |
| harness/executor.py | 8f13d42ace3e496658f38b94d64815b85dfad6cf |
| harness/sdk_runner.py | 11f222307bcd02620d17e0bf79eef15da50d9f11 |
| harness/state_machine.py | a13cde3accda6d6b031b5064c9486c69d74618d4 |
| harness/validation.py | 601b33ca887d84e3ddbf4820f8db5d3b305aac29 |

New files (add as-is): `harness/test_frontend_support.py`, `scripts/karma.harness.conf.js`.

The whole `harness/` folder must be in step — copy all of the files above, not a subset
(a partial copy caused a ModuleNotFoundError at BCBSM before).

## 2. .github/workflows/harness.yml — one edit by hand (BCBSM copy has the list-models step)

Directly under the job-level line

          JAVA_TOOL_OPTIONS: -XX:+IgnoreUnrecognizedVMOptions

add (same 6-space indentation):

          HARNESS_KARMA_CONFIG: ${{ github.workspace }}/engine/scripts/karma.harness.conf.js

## 3. Service repo dx_mycoverage-ui

Copy `docs/service repo-config/config-frontend.yaml` to `.harness/config.yaml` and pick
mode A or B (see the file header). Before mode A, run on the base branch:

    npm install && npx ng test --watch=false --browsers=ChromeHeadless

## 4. Expected on the first Angular run

- `[module] ... allowed_writes=['src/app/**', '.harness/**'] exclude=[..., 'src/app/**/*.spec.ts']` in coding.
- `[harness] changed main source (coverage scope): src/app/...ts` after coding.
- Mode A: `[harness] coverage report: coverage/harness/coverage-summary.json` and a changed-file LINE %.
- Mode B: `'unit_testing' skipped by config (skip_phases) — no AI work; its gates still run.`

## Backend repos

No config change. Prompts are unchanged apart from "pom.xml or package.json". One behaviour
change: the scope gate now works in multi-module repos (it never fired before), so a NEW
production class the plan's Impacted Files does not list halts the coding phase.
