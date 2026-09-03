# SDLC-Harness

A deterministic pipeline that drives the GitHub Copilot SDK through nine fixed
phases with automated gates between them. A user story goes in; a reviewed pull
request comes out. Nothing reaches a PR unless it stayed in scope, compiled,
passed its tests, met the coverage bar, cleared an independent review, and
satisfied every acceptance criterion.

The operating principle is **bounded autonomy with deterministic control
points**: the AI executes each phase; continuous integration decides whether it
may proceed. Phases never advance on the model's own assessment — they advance
only when the engine returns one of a fixed set of exit codes.

---

## What it does

Given a single user story, the harness runs a fixed sequence of phases inside a
GitHub Actions workflow. Each phase is executed by a Copilot model constrained to
a narrow set of files it is allowed to write. Between phases, the engine — not the
model — checks that the work is admissible: the write boundary held, the required
artifact exists, the plan was followed, tests pass, coverage is met, an
independent reviewer approved, and each acceptance criterion is satisfied. If any
check fails, the run either loops back a bounded number of times or halts with a
structured reason for a human to adjudicate.

## What it deliberately does NOT do

- **It never merges.** The output is a pull request; a human approves and merges.
- **It never deploys.** The harness stops at the PR.
- **It never edits build files.** No phase may write `pom.xml`. A story that needs
  a new dependency halts by design (see Known limitations).
- **It offers no gate-bypass.** Halt messages never include a "skip this gate"
  option. This is intentional.

---

## The nine phases

| # | Phase | What runs | May write | Required artifact |
|---|-------|-----------|-----------|-------------------|
| 1 | `context` | Turns the story into a context file; emits clarification + feasibility markers | `.github/story-context-files/**`, `.harness/**` | (clarification scan) |
| 2 | `design` | Technical design — **by exception only**, when context judges the story needs it | `.harness/**` | `.harness/design.md` |
| 3 | `prompt_steps` | The implementation plan (the approved scope) | `.harness/**` | `.harness/prompt-steps.md` |
| 4 | `coding` | Production source only — main code | `src/main/**`, `.harness/**` | — |
| 5 | `code_review` | An independent reviewer LLM inspects the change and writes a structured verdict | `.harness/**` | `.harness/review.md` |
| 6 | `unit_testing` | Test source only — **main code is frozen** so tests can't be gamed by editing code. The harness runs the configured test command immediately after this phase. | `src/test/**`, `.harness/**` | — |
| 7 | `validation` | AC conformance — an agent judges each acceptance criterion; the harness then parses the per-AC verdicts | `.harness/**` | `.harness/validation.md` |
| 8 | `documentation` | Documentation for the change | `docs/**`, `.harness/**` | — |
| 9 | `raise_pr` | Opens the pull request | `.harness/**` | — |

The configured test command (`mvn test` by default) is run by the **harness**, not
the agent, immediately after the `unit_testing` phase — a non-zero exit halts the
run before it can advance. The `validation` phase that follows is a separate,
per-acceptance-criterion conformance check.

`design` is skipped when the `context` phase judges the story follows an existing
pattern. This is the one branch point in the spine.

---

## The gates

Each gate sits between phases and is enforced by the engine, not the model.

- **Context gate** — scans the context file for two markers from the
  `build-context` skill. `[NEEDS CLARIFICATION]` **always blocks, in every mode.**
  The feasibility verdict (`VERDICT: GO / NO_GO` with `[BLOCKER]: (CLASS)`) blocks
  only when `blocker_gate: blocking`.
- **Write boundary (the interlock)** — every file the agent attempts to write, in
  every phase, is checked against that phase's allowed-writes globs. Any
  out-of-bounds write fails the phase immediately with `BOUNDARY_VIOLATION`. This
  is a pure function with no SDK dependency, and it is the one behaviour proven by
  automated test before any credits are spent.
- **Plan-path gate** — after `prompt_steps`, the engine reads the plan's Impacted
  Files and confirms every planned path sits under a package root that actually
  exists in the repo. A plan that guessed a package name (a path under a root that
  isn't there) halts here, before the expensive coding phase — the coding phase
  cannot create arbitrary directories, so such a plan is unexecutable. New
  sub-packages under a real root are allowed.
- **Scope gate** — after coding, the engine compares what was created against the
  approved plan. An unplanned **edit** to an existing file is allowed (flagged for
  review); **creating** a production class the plan never listed is a hard
  `SCOPE_VIOLATION`. A second check independently rejects **duplicate classes**
  (the same class name at two paths), which is the symptom that a bad plan or a
  workaround produces.
- **Review gate** — the independent reviewer's verdict must be present and parse.
  A stale verdict file from a previous attempt is deleted before the reviewer
  runs, so "the file exists" means "the reviewer wrote it this attempt." Blocking
  findings are limited to five classes: correctness, contract,
  reactive/concurrency, security, error handling. Style is downgraded to a note.
- **Validation gate** — after the `unit_testing` phase, the harness itself runs the
  configured test command (`test_command`, default `mvn test`). A non-zero exit is
  `VALIDATION_FAILED` and halts the run before it advances. The agent does not run
  the tests; the harness does.
- **Coverage gate** — line coverage on the **changed classes only** must meet
  `min_coverage` (default 90%).
- **AC conformance gate** — after the `validation` phase, reads
  `.harness/validation.md` and requires a verdict for every acceptance criterion:
  MET / NOT_MET / UNVERIFIABLE. Blocks only when `ac_gate: blocking`.

### Advisory vs blocking

`blocker_gate` and `ac_gate` ship **advisory** on purpose. Both can stop a run
early, and switching them to blocking before their verdicts have been seen to be
right on a given codebase is how a new control loses trust. Move them to blocking
once the verdicts look sound. Clarifications and the write boundary always block,
in every mode.

---

## Control limits

There are three independent ceilings. All are real; each guards a different loop.

| Limit | Config / location | Counts | Default |
|-------|-------------------|--------|---------|
| Iteration cap | `max_iterations` per phase (`phases.py`) | inner agent attempts accumulated within one phase — a credit guard | 4–12, per phase |
| Per-gate retry | `max_review_retries`, `max_ac_retries`, `max_coverage_retries` | loop-backs from a specific gate to its loopback phase | 2 each |
| Global cap | `max_phase_runs` | total phase runs across **all** loop types | 3 |

The global cap is the outer bound across all loop types; the per-gate retries
(2 each) operate beneath it.

---

## How a run is triggered

The service repository holds a thin caller workflow (~30 lines,
`workflow_dispatch` only) that calls this engine's reusable workflow at `@v1`.
Runs execute on GitHub-hosted Ubuntu runners.

Three repositories are involved:

| Repo | Contents |
|------|----------|
| `SDLC-Harness` | The Python engine + the reusable workflow (this repo) |
| `YOUR-TOOLKIT-REPO` | Skills + instruction files, fetched at run time, never committed to service repos |
| service repo | Service code + the thin caller workflow + `.harness/config.yaml` |

At run time the engine fetches the toolkit fresh, so the standards applied are
always current and are never copied into the service repo.

### Branches created per run

- `harness-wip/<feature>` — resume point, force-pushed as the run progresses
- `harness-audit/<feature>/<date>-<outcome>-<run_id>` — the audit trail
- `harness-metrics` — accumulates one metrics record per run
- `harness/<feature>/<date>-<run_id>` — the branch the pull request is opened from

---

## Models per phase

| Phase | Model | Why |
|-------|-------|-----|
| default (all phases) | `gpt-5-mini` | — |
| `code_review` | `claude-haiku-4.5` | a different vendor from the coder, deliberately |
| `design`, `validation` | `claude-sonnet-4.5` | architecture trade-offs and per-AC judgement |

Set in `.harness/config.yaml` (`model`, `phase_models`, `review_model`).

---

## Module layout

Flat package under `harness/`.

| File | Responsibility |
|------|----------------|
| `run.py` | The CLI entry point (`init`, run) |
| `state_machine.py` | The deterministic engine; walks the phases in order, advances only on pinned exit codes |
| `phases.py` | The nine-phase spine, declared: each phase's name, allowed writes, artifact, iteration cap |
| `plan_check.py` | The plan-path gate; after `prompt_steps`, verifies each planned path sits under a real package root before coding runs |
| `contracts.py` | The pinned exit-code contract the orchestrator speaks |
| `executor.py` | The PhaseExecutor: builds the prompt, runs the agent, enforces the write boundary, iteration cap, artifact and scope checks |
| `boundaries.py` | The write-boundary interlock as a pure function (`is_write_allowed`) |
| `state.py` | The run state, persisted to `.harness/run-state.json`; makes a run resumable |
| `sdk_runner.py` | The real AgentRunner, backed by the Copilot SDK — the only file that talks to Copilot |
| `fake_runner.py` | A no-network, no-credit stand-in that can be told to misbehave, for testing the interlock |
| `clarification.py` | The context gate (clarifications + feasibility) |
| `review.py` | The code-review gate (parses the independent reviewer's verdict) |
| `validation.py` | The test/coverage validation gate; the harness shells out to the configured test command after `unit_testing` — the harness runs the tests, not the agent |
| `ac_validation.py` | The AC conformance gate; reads `.harness/validation.md` after the `validation` phase and judges each acceptance criterion |
| `halt_gates.py` | The closed vocabulary of reasons a run may stop |
| `blocked.py` | Lets a phase declare a change it is not permitted to make, and halt cleanly |
| `resume.py` | Re-enters a halted run at a chosen phase without redoing passed phases |
| `execution_record.py` | Appends the actual-vs-approved execution record; backs the scope gate |
| `config.py` | Loads `.harness/config.yaml`; supplies defaults when absent |
| `story_source.py` | The swappable seam for where the story comes from |
| `metrics.py` | Writes one metrics record per run (one file per run, not a shared append log) |
| `ai_credits.py` | Reads actual AI-credit consumption from GitHub's billing API |
| `harness_report.py` | Aggregates the per-run metrics records |
| `audit_summary.py` | Prints a human-readable audit summary for the most recent run |
| `list_models.py` | Lists the models available to the Copilot login (spends no credits) |
| `test_boundaries.py` | Proves the write-boundary interlock before any SDK or credit spend (14 tests) |

---

## Configuration

All per-repo behaviour lives in `.harness/config.yaml` in the service repo. The
engine, skills and instruction files are central and shared; the build command,
quality bar and gate modes belong to the service. A fully commented
`config.yaml.sample` with all current keys ships alongside the engine. Key
settings: `test_command`, `min_coverage`, `write_exclude`, `model` /
`phase_models` / `review_model`, `blocker_gate`, `ac_gate`, `max_phase_runs`.

---

## Before you run in your environment

The engine ships with placeholder identifiers. Substitute these before the first
run in a new organization:

- `YOUR-ORG` → your GitHub organization name
- `YOUR-TOOLKIT-REPO` → your toolkit repository name/path
- The caller workflow's reusable-workflow reference → your `SDLC-Harness` location
- `.harness/config.yaml` → your test command, target module and validation scope

Environment notes:

- **Runners.** Verified on GitHub-hosted Ubuntu. A self-hosted requirement is a
  workflow-file change, not an engine change.
- **CI platform.** The engine is plain Python. Porting to another CI system
  (e.g. CloudBees) is a workflow-file change, not an engine change.
- **Credit reporting** requires the `Plan: Read` permission and is unavailable for
  org-billed Copilot seats; expect blank credit metrics on org-billed seats. The
  run itself is unaffected.

---

## Known limitations

State these openly.

- **A story needing a new dependency halts.** No phase may write `pom.xml`. The
  coding phase declares `.harness/blocked.md` and the run stops with instructions
  to add the dependency by hand and resume. Deliberate, but a real constraint.
- **Only new development and enhancement profiles exist.** A bug-fix / analysis
  profile (diagnose root cause, derive acceptance criteria, test-before-fix) is
  designed but not yet built.
- **Skill attribution is best-effort.** The SDK reports its own agent rather than
  the repo skills it loaded, so per-run skill attribution is approximate.
- **Local runs have no enforcement.** A helper exists to run phases locally with
  freshly fetched standards, but it cannot enforce the sequence or record the run.
  Enforcement is a property of the CI path only; local is for development.

---

## Running the tests

From `harness/`:

```
python -m pytest test_boundaries.py -q
```

This proves the write-boundary interlock (14 tests) with no network and no credit
spend, using the fake runner.
