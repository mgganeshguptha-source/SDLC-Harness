# BCBSM sync — reference repos for the context phase, 2026-11-04

Copy from mgganeshguptha-source/SDLC-Harness (after merge) into
BCBSM/dx_SDLC-Harness, commit to master, `git tag -f v1`, `git push -f origin v1`.

**Includes PR #46 and #47.**

## Do this first

After this engine goes live, every FRESH run halts until the repo has
`.harness/repo_list.yaml`. For every service repo on the harness:

1. Add `!.harness/repo_list.yaml` to `.gitignore`.
2. Commit `.harness/repo_list.yaml` to the story branch. `repos: []` is enough
   for a repo that needs no other repo.

Get BCBSM's approval before listing a repo: reference repos are not scanned for
PII or secrets, and what context reads goes to the model.

`COPILOT_GITHUB_TOKEN` must be able to read every listed repo.

## Engine files — replace whole files

| File | Change |
|---|---|
| harness/reference_repos.py | NEW — list, clone, prompts, job summary, PR note |
| harness/test_reference_repos.py | NEW — tests |
| harness/state_machine.py | Clone request and second context pass; clones removed before later phases |
| harness/config.py | No phase may write to .harness/reference-repos/ |
| harness/sdk_runner.py | Context prompt carries the repo list |
| harness/run.py | `init` halts when the file is missing or invalid |
| harness/gitignore.py | Adds `!.harness/repo_list.yaml` |
| harness/test_gitignore.py | Updated for the new line |
| harness/halt_gates.py | reference_repo |
| harness/state.py | reference_repos |
| harness/metrics.py | Schema 7: reference_repos |

No workflow, caller or toolkit change.
