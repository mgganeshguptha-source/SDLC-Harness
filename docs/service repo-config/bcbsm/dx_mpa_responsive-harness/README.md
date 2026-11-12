# dx_mpa_responsive-harness — harness files as used at BCBSM

Copies of the files in `BCBSM/dx_mpa_responsive-harness`, branch
`MPR_AWS_STG_PM_202611_DEV_OP018_6418`, kept here so the setup can be reused and
reviewed. The BCBSM repo is the source of truth; update these when it changes.

| File here | Goes to |
|---|---|
| `config.yaml` | `.harness/config.yaml` |
| `repo_list.yaml` | `.harness/repo_list.yaml` |

## What is particular to this repo
- **Angular monolith front end in `mpa-web/`** (`angular.json`, `package.json`
  there, not at the root), so `target_module: "mpa-web"`.
- **`mpa-js` is committed under `mpa-web/node_modules/`**, not in
  `package.json`. `npm ci` deletes `node_modules/` first, so `test_command` uses
  `npm install`. The build still cannot run on a GitHub runner today, so
  `runs_build: false` (developer builds locally).
- **NgRx store in `mpa-web/src/app/mpa/store/`**, model in
  `mpa-web/src/app/mpa/shared/data/model/`. The engine finds a store folder at
  any depth and publishes it with the models it imports (from PR #52). Before
  that, the contract hand-off published nothing for this repo.
- `unit_testing` is skipped: the front-end team writes no unit tests in
  sprint delivery.

## History
- OP018-6418 Story 2, 2026-11-10/11: first runs; findings fixed in engine
  PRs #49 (contract line wording), #50 (reference repo token), #51
  (runs_build in config, node_modules failures, protected paths), #52 (store
  contract at any depth).
