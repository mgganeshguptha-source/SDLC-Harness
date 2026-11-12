# BCBSM sync — Angular store contract found at any depth, 2026-11-12

Copy from mgganeshguptha-source/SDLC-Harness (after merge) into
BCBSM/dx_SDLC-Harness, commit to master, `git tag -f v1`, `git push -f origin v1`.

## Engine files — replace whole files

| File | Change |
|---|---|
| harness/handoff.py | Store contract: any `store/` folder (was `src/app/store/` at the root only), its .ts files, plus the local files they import (models); clearer messages |
| harness/test_handoff.py | More tests |
| docs/story-template.md | Attach rule for Angular stories updated |

No config or caller change. Per-repo copies for dx_mpa_responsive-harness are in
`docs/service repo-config/bcbsm/dx_mpa_responsive-harness/`.
