# BCBSM sync — analysis mode (bug-fix analysis), step 1, 2026-11-13

**Not for client bugs yet.** This is the minimal mode for testing with seeded
bugs in the Apexon environment. Sync to BCBSM only after the evidence controls
(engine-side caller evidence, evidence gates, the build run's drift check) are
built and benchmarked. Recorded here so the file list is ready.

Copy from mgganeshguptha-source/SDLC-Harness (after merge) into
BCBSM/dx_SDLC-Harness, commit to master, `git tag -f v1`, `git push -f origin v1`.

## Engine files — replace whole files

| File | Change |
|---|---|
| .github/workflows/harness.yml | New input `mode` (build / analysis), checked at the start; passed to `init`; the PR step opens a document-only PR for an analysis run |
| harness/phases.py | Analysis sequence (`bug_analysis`), run modes, `phases_for` / `first_phase`; `next_phase` takes the mode |
| harness/analysis.py | NEW. Analysis gate, analysed-source stamp, publish to the story folder, PR body |
| harness/state.py | `mode`, `analysis_doc` on the run state |
| harness/state_machine.py | Follows the run's mode; runs the analysis gate after `bug_analysis` |
| harness/run.py | `init --mode`; audit folder keeps `bug-analysis.md`; run summary records the mode |
| harness/resume.py | A resume keeps the run's mode |
| harness/config.py | `bug_analysis` defaults: skill `analyze-bug`, context file scope, attachments always sent, knowledge loaded |
| harness/sdk_runner.py | Task prompt for `bug_analysis` |
| harness/metrics.py | Schema 8: `mode`, `analysis_doc` |
| harness/fake_runner.py, harness/list_models.py | Know the new phase |
| harness/test_analysis_mode.py | NEW. 27 tests |
| harness/config.yaml.sample, README.md, docs/bug-report-template.md | Documentation |

## Service repos

`docs/service repo-config/SDLC-Harness.yml` has a new `mode` dropdown on the
form and passes `mode: ${{ inputs.mode }}`. A caller without it keeps working:
the engine input defaults to `build`.

Optional in `.harness/config.yaml`: `phase_models: {bug_analysis: <model>}`.

## Toolkit (dx_copilot-standards-workspace) — add by hand

From mgganeshguptha-source/copilot-toolkit PR (same date), two NEW files:
- skills/analyze-bug/SKILL.md
- skills/analyze-bug/assets/bug-analysis-template.md

Without them an analysis run still starts, but the model has no template and
the document will not have the expected sections.
