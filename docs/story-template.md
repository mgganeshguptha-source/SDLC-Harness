# Story template — stories/<feature_id>.md

Commit the story to the story branch, named after its feature id:
`stories/<feature_id>.md` (or `stories/<feature_id>-story.md`). Attachments
(design screenshots, error logs, requirement documents, upstream contracts) go in
`stories/<feature_id>/` on the same branch.

Write acceptance criteria as observable results someone could test. State what
must happen for empty, missing and unexpected values, and what is out of scope.
Every gap the harness has to ask about costs a round trip.

---

```markdown
Title: <one line>

Story: As a <role>, I want <capability>, so that <benefit>.

**Requires a new or changed contract from:** None
<!-- None            = this story does not need a changed contract.
     <upstream-repo> = upstream repo name (for example: dx_mpa_responsive) if this
                       story needs a changed contract (API, store, event, schema,
                       config). When a repo is named, attach its contract file in
                       stories/<feature_id>/. -->

Expected behaviour: <what changes, inputs and outputs, edge cases>

Acceptance criteria:
- <criterion 1 - one observable, testable result>
- <criterion 2>

In scope: <classes / modules>. Out of scope: <UI, controllers, database ...>

Attachments: <optional — list the files in stories/<feature_id>/ and what each shows>
```

---

## The contract line (mandatory)

`**Requires a new or changed contract from:**` must be present in every story.
The harness checks it before any model call and halts when it is missing or not
filled in.

| Value | Use when |
|---|---|
| `None` | This story does not need a changed contract. Two repos may be connected — if the contract between them does not change for this story, the value is `None`. |
| `<upstream-repo>` (e.g. `dx_mex-coverage-service`) | This story needs a contract (API, store, event, schema, config) that another story is adding or changing in that repo. |

When a repo is named:

1. Open that repo's `harness-metrics` branch and go to
   `contracts/<upstream feature_id>/`.
2. Check the contract is the one you expect (its header shows the feature id,
   run id and commit it was produced from).
3. Copy `contract-<repo>.md` (and `sample-<repo>.json`, if present) into
   `stories/<feature_id>/` on this story's branch.

The harness halts if the contract is not attached, or if the attached contract's
header names a different repo. A contract without the harness header (written or
edited by hand) is accepted with a warning.

Write only the repo name — the story id comes from the contract file's header.

## Where contracts come from

A story whose change touches contract files (controllers, request/response DTOs,
OpenAPI files; NgRx actions, reducer, selectors, effects, state; host-bridge
services; plus any `contract_files` in `.harness/config.yaml`) gets a contract
written by the documentation phase. The harness stamps a header, scans it for
PII, and publishes it to `harness-metrics` only — the run's audit folder and
`contracts/<feature_id>/`. It never enters the code PR.

For a change that is already finished (for example an approved PR from before
this feature), run the workflow with `contract_only: true`, `contract_branch:
<the PR branch>` and `base: <the story branch>`. One read-only run writes and
publishes the contract; no code is written and no PR is raised.
