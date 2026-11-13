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

Write only the repo name.

## What to attach when a repo is named — rule of thumb

| The upstream story was… | Attach in `stories/<feature_id>/` |
|---|---|
| a **backend** story | that repo's **OpenAPI file** (e.g. `mex-coverage-service.yml`) |
| an **Angular frontend** story | that repo's **NgRx store folder** (any folder named `store/`, e.g. `src/app/store/` or `mpa-web/src/app/mpa/store/`, with all its subfolders) **and the model files the store imports** (e.g. `shared/data/model/visitsummary.ts`) |

- Take the files from the upstream branch that holds the change (e.g. the
  approved PR branch). A subfolder is optional.
- If the upstream story ran through the harness, the same files are already on
  that repo's `harness-metrics` branch under `contracts/<upstream feature_id>/files/`
  (with `manifest.json` showing the commit they came from).
- If the upstream story was done by hand, run the workflow on the upstream repo
  with `contract_only: true`, `contract_branch: <branch with the change>` and
  `base: <the branch it was made against>`. It copies the same files to
  `harness-metrics` — no AI, no code change, no PR.

The harness halts if no contract file is attached (`.yml`, `.yaml`, `.json`,
`.ts`, or a `## Upstream contract: <repo>` section in the story). It warns when the
attached files cannot be tied to the named repo by name.

For an attached OpenAPI file the harness generates mocked request/response
samples from the schema (no AI, invented values). Postman collections or test
JSON may be attached as extra input; the schema wins when they disagree, and
their values are never copied.

## API changes (backend, code-first)

When a story changes an API request or response, the harness updates the repo's
OpenAPI file in the same change (it may edit that file even inside the
`-openapi-code` module). If API files change without the OpenAPI file, the run
log and the PR body show a warning, and code review reports it. New or changed
JSON test resources use invented values only.
