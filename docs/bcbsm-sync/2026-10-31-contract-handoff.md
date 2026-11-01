# BCBSM sync — contract hand-off + OpenAPI standard (PR #24 + follow-up), 2026-10-31

Copy from mgganeshguptha-source/SDLC-Harness (after merge) into
BCBSM/dx_SDLC-Harness, commit to master, `git tag -f v1`, `git push -f origin v1`.
Toolkit changes: copy into BCBSM/dx_copilot-standards-workspace (see section 4).

**Includes everything since the 29 Oct sync note** (story attachments, input
guard, .gitignore): sync that first if not done.

## 1. Engine files — replace whole files

| File | Change |
|---|---|
| harness/handoff.py | NEW — contract line gate, contract publishing, OpenAPI detection, mocked samples from OpenAPI |
| harness/test_handoff.py | NEW — tests |
| harness/attachments.py | .ts attachments, names keep subfolder path, mocked samples for an attached OpenAPI file |
| harness/boundaries.py | `!glob` in write_exclude re-includes a path |
| harness/config.py | dependency_gate, contract_handoff, contract_files, api_spec_files; coding may edit the OpenAPI file; documentation loads build-contract |
| harness/executor.py | creates .harness/contract/ for documentation |
| harness/halt_gates.py | dependencies_missing, contract_missing |
| harness/metrics.py | schema 6: dependencies, contract_output |
| harness/run.py | `contract` command (contract-only, no model); collect-audit copies the contract to audit + contracts/<feature_id>/ |
| harness/sdk_runner.py | documentation summary prompt; raise_pr OpenAPI warning; unit_testing mocked test resources; review rules (cross-repo shapes, OpenAPI not updated) |
| harness/state.py | dependencies, contract_output |
| harness/state_machine.py | dependency gate before every model phase; contract staging after documentation |
| harness/test_attachments.py, test_audit_layout.py, test_learning.py, test_state_machine.py | stories carry the contract line |

Copy the whole `harness/` folder in one go — `state_machine.py` imports
`handoff.py` and `attachments.py`.

## 2. .github/workflows/harness.yml (hand edit — keep BCBSM's list-models step)

1. Inputs `contract_only` (boolean) and `contract_branch` (string).
2. Service checkout `ref:` starts with `(inputs.contract_only && inputs.contract_branch) ||`.
3. `Initialize run` and `Rewind state` get `&& !inputs.contract_only`.
4. New step `Contract only (contract_only)` before `Autorun`; `Autorun` gets `if: ${{ !inputs.contract_only }}`.
5. `Push working branch`, `Open pull request`, `Propose toolkit change` get `!inputs.contract_only`.
6. WIP and PR steps exclude `contracts` (git reset list, EXCL list).
7. Publish step copies `contracts/` to harness-metrics, replacing each feature's folder.

## 3. Service repos

- **Every story file needs the line** `**Requires a new or changed contract from:** None`
  (or the upstream repo name). Without it the run halts. Template: `docs/story-template.md`.
- Caller (`.github/workflows/SDLC-Harness.yml`): add inputs `contract_only`,
  `contract_branch` and pass them through (see `docs/service repo-config/SDLC-Harness.yml`).
- `.harness/config.yaml`: nothing required. Optional: `api_spec_files` (when
  auto-detection picks the wrong file), `contract_files`. Frontend:
  `phase_skills.code_review: ["security-review-backend", "security-review-angular", "review-angular-code"]`;
  both: `phase_skills.documentation: ["build-contract"]`.

## 4. Toolkit (dx_copilot-standards-workspace)

From mgganeshguptha-source/copilot-toolkit (PRs #18, #19, and the follow-up):
- `skills/build-contract/SKILL.md` — NEW.
- `skills/build-context/SKILL.md`, `skills/build-context/assets/context-template.md`.
- `skills/build-prompt-steps/SKILL.md`.
- `skills/generate-unit-tests/SKILL.md` — mocked test resources rule.
- **Security review split (confirmed 1 Nov: BCBSM's `skills/security-review/` is the
  Angular security review):**
  1. `git mv skills/security-review skills/security-review-angular`
  2. In `skills/security-review-angular/SKILL.md` set the frontmatter
     `name: security-review-angular` and add to its description "Angular / Ionic
     only; backend code uses security-review-backend".
  3. If its `assets/security-review-template.md` is referenced by name in the
     SKILL.md, keep the reference; the folder move keeps the path relative.
  4. Copy `skills/security-review-backend/` (new) from the personal toolkit.
  5. Engine `config.py` (this sync) loads `security-review-backend` on backend
     repos and `security-review-angular` on Angular/Ionic repos. Repos whose
     `.harness/config.yaml` still says `security-review` keep working: the old
     name expands to both new names and is filtered by stack. Update the configs
     to the new names when convenient.

## 5. Check after the sync

- A story without the contract line halts with `DEPENDENCIES LINE MISSING`
  before any `[model]` log line.
- A backend run that changes a `*Response.java` logs `[contract] N contract file(s) …`
  and, if the OpenAPI file was not changed, `OpenAPI not updated`.
- `contract_only: true` on an approved branch publishes
  `contracts/<feature_id>/files/...` + `manifest.json` on harness-metrics with no model call.
