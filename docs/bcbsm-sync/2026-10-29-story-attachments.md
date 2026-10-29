# BCBSM sync — story attachments + input guard (PII/PHI), 2026-10-29

Copy these changes from mgganeshguptha-source/SDLC-Harness (after merge) into
BCBSM/dx_SDLC-Harness, commit, then `git tag -f v1`, push master, `git push -f origin v1`.

**Order matters.** Sync the ENGINE first, then add the checkbox to each service
repo's caller workflow (section 4). A caller that passes `confirm_no_phi` to an
engine that does not declare it fails at once ("Unexpected input"). And once the
engine is synced, every run whose caller does not pass `confirm_no_phi: true`
stops at its first step — so update the callers straight after the engine.

This sync also carries PR #18 (clarification message names the story branch,
cost-table column) for `harness/run.py`, `harness/state_machine.py` and
`.github/workflows/harness.yml`, which BCBSM (synced at 021600d) does not have.
Include PR #20 (npm halt messages, `harness/validation.py`) if it is merged.

## 1. Check each file, then replace whole files

In the BCBSM engine clone, for each file run `git rev-parse HEAD:<file>`.
If it prints the hash below (the 021600d version BCBSM was synced to), replace
the file with the merged version. If it differs, the BCBSM copy has its own
edits — apply the PR diff for that file by hand.

| File | Hash at 021600d |
|---|---|
| harness/config.py | 670fc8014152be70d8112005fc4f7b7374ecac0b |
| harness/halt_gates.py | 9b50b46bc126d4e4f9d9d4b799f60e27c954e29e |
| harness/metrics.py | 9e8d3bf183a108af9f4121fa1befad5bea7837c1 |
| harness/resume.py | 998518a20584b4f32355ff7bd0feacd1f4b6f12c |
| harness/run.py | 45b88185a0dd89879d929f1baa1b4188374d80e8 |
| harness/sdk_runner.py | 19c228b569bf93df8626fb475011e9c6d87f54a7 |
| harness/state.py | 7144974cd3bf286bf3e0c1603dff77c5678e811b |
| harness/state_machine.py | fdf2d202f84019fe4e36bc972bd9bbe2ece1ac1d |
| requirements.txt | 0b29cbe7efd7063d29aa6f000d5b082dcf4125df |

New files (add as-is): `harness/attachments.py`, `harness/test_attachments.py`.

Copy the whole set — `state_machine.py` imports `attachments.py`, and a partial
copy fails with ModuleNotFoundError.

## 2. .github/workflows/harness.yml

BCBSM's copy has its own edits (list-models step), so apply by hand from the
merged personal-repo file. Five changes:

1. **Input** `confirm_no_phi` (boolean, default false) after `toolkit_proposals`.
2. **Step** "Confirm no PII/PHI in the input" directly after "Set run stamp" —
   fails the job when the box is unticked.
3. **Step** "Install OCR (story has images)" between "Rewind state (resume
   only)" and "Autorun (all phases)".
4. **Autorun env**: `HARNESS_CONFIRM_NO_PHI: ${{ inputs.confirm_no_phi }}`.
5. **Raise PR step**: the `ATT_DIR=...` lines and `":(exclude)$ATT_DIR"` added
   to the `EXCL=(...)` list, so the attachments folder never enters the PR.

(Plus PR #18's `HARNESS_BASE_BRANCH` env line and resume-notice wording, if not
already applied.)

## 3. Service repos — .harness/config.yaml

Nothing is required: the defaults are attachments folder
`stories/{feature_id}`, PII scan ON (ssn, dob, phone, email), OCR ON. To change
them, copy the "story attachments" section from
`docs/service repo-config/config-frontend.yaml` (or `config-backend.yaml`).

## 4. Service repos — caller workflow (.github/workflows/SDLC-Harness.yml)

On each service repo's default branch, add the checkbox input and pass it on
(see `docs/service repo-config/SDLC-Harness.yml`):

    on:
      workflow_dispatch:
        inputs:
          ...
          confirm_no_phi:
            description: "I confirm the story and all attachments contain no real PII or PHI (mocked test data only). Required — the harness will not start without it."
            required: true
            default: false
            type: boolean

    jobs:
      harness:
        with:
          ...
          confirm_no_phi: ${{ inputs.confirm_no_phi }}

## 5. Check after the sync

- Run once with the box **unticked**: the job must stop at "Confirm no PII/PHI
  in the input" before any checkout.
- Run with the box ticked and no attachments: the log shows
  `[input-guard] confirm_no_phi=yes | PII scan on | story + 0 attachment file(s) | findings 0`
  before the first phase's model call.
- Optional: put a `stories/<feature_id>/mock.txt` holding `123-45-6789` on the
  story branch — the run must halt with `INPUT GUARD: HALTED` and the value
  masked (`***-**-6789`), with no model call. Remove the file afterwards.
