# BCBSM sync — 2026-11-15 — library-store halt message

## What changed
- `harness/validation.py`: the "dependency" environment halt no longer tells
  you to run `publish.sh` / `capture.sh` (those scripts do not exist). It now
  says the artifact must be added to the library store by whoever maintains it
  (BCBSM DevOps; by hand during the PoC), in Maven layout.
- The halt now lists each missing artifact and the store folder it belongs in,
  e.g. `com.bcbsm.mem.pom:mem-starter-parent:pom:4.2.7-SNAPSHOT ->
  <libs-folder>/com/bcbsm/mem/pom/mem-starter-parent/4.2.7-SNAPSHOT/`.
- New test `harness/test_library_store_halt.py`.
- `docs/HARNESS_ENGINEERING_CURRENT_STATE_KEY_DECISIONS.docx`: new section
  "MOVING FROM PoC TO PRODUCTION" (engine repo only; not needed at BCBSM).

## Copy to BCBSM (dx_SDLC-Harness)
- `harness/validation.py` (whole file)
- `harness/test_library_store_halt.py` (optional, tests only)

No workflow change; v1 does not need to move for the workflow, only for the
engine code.
