# BCBSM sync — COPY_FROM_REFERENCE in the External Classes check, 2026-11-13

Copy from mgganeshguptha-source/SDLC-Harness (after merge) into
BCBSM/dx_SDLC-Harness, commit to master, `git tag -f v1`, `git push -f origin v1`.

## Engine files — replace whole files

| File | Change |
|---|---|
| harness/plan_check.py | New source COPY_FROM_REFERENCE: halt before coding with the exact folders to copy and the commands |
| harness/state_machine.py | Passes the reference repos the run read (for the branch) |
| harness/sdk_runner.py | prompt_steps prompt: COPY_FROM_REFERENCE, no placeholder names, follow this repo's existing integration pattern |
| harness/test_external_classes.py | More tests |

## Toolkit (dx_copilot-standards-workspace) — apply by hand
From mgganeshguptha-source/copilot-toolkit PR (same date):
- skills/build-context/SKILL.md: "Code this repo does not have — look in the
  reference repos first" (+ `## Reference repo findings` format, the exception
  in "What never goes in context.md", two preflight lines).
- skills/build-prompt-steps/SKILL.md: existing-integration pattern rule in the
  inventory step, `## External Classes` block in the plan structure, two
  self-check lines.
BCBSM's toolkit has Tarun's v2 instructions; check these two skills there
before replacing — merge the sections rather than overwrite if they differ.
