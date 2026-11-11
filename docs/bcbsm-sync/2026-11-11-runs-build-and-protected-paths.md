# BCBSM sync — runs_build in config.yaml, node_modules failures, protected paths, 2026-11-11

Copy from mgganeshguptha-source/SDLC-Harness (after merge) into
BCBSM/dx_SDLC-Harness, commit to master, `git tag -f v1`, `git push -f origin v1`.
Includes PR #49 and #50.

## Engine files — replace whole files

| File | Change |
|---|---|
| .github/workflows/harness.yml | WIP and PR steps leave out paths no phase may write |
| harness/protected_paths.py | NEW — restore / unstage / filter protected paths |
| harness/config.py | runs_build read from .harness/config.yaml first |
| harness/state_machine.py | Passes the config to runs_build; restores protected files after the build |
| harness/sdk_runner.py | Passes the config to runs_build |
| harness/validation.py | "Can't resolve 'node_modules/...'" halts as an environment failure |
| harness/boundaries.py | '**/x/**' globs now match (e.g. '**/node_modules/**') |
| harness/test_protected_paths.py, harness/test_runs_build_and_node_modules.py | NEW — tests |
| harness/test_frontend_support.py, harness/test_pr_build_status.py | Updated for the new signature |

## Service repos
- Optional now: add `runs_build: true|false` to `.harness/config.yaml`. It wins
  over the caller's `runs_build`. Repos without it behave as before.
- dx_mpa_responsive-harness: `runs_build: false` in config.yaml; if the build is
  ever turned on, use `npm install`, not `npm ci` (node_modules/ is committed).

## Behaviour change to check
`**/node_modules/**`-style write_exclude globs used to match nothing; they now
block writes as the samples intended.
