# BCBSM sync — reference repo clone uses the repo token, 2026-11-11

Copy from mgganeshguptha-source/SDLC-Harness (after merge) into
BCBSM/dx_SDLC-Harness, commit to master, `git tag -f v1`, `git push -f origin v1`.

## Engine files — replace whole files

| File | Change |
|---|---|
| harness/sdk_runner.py | Saves the repo token as HARNESS_REPO_TOKEN before the model token replaces COPILOT_GITHUB_TOKEN |
| harness/reference_repos.py | Clones with HARNESS_REPO_TOKEN; a refused token is named in the halt |
| harness/test_reference_repos.py | More tests |

No config, caller or toolkit change. COPILOT_GITHUB_TOKEN must be able to read
every repo in .harness/repo_list.yaml.
