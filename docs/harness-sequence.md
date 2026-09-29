# SDLC Harness - End-to-End Sequence

```mermaid
sequenceDiagram
  autonumber
  participant GA as GitHub Actions - harness.yml
  participant TK as Toolkit repo - skills + instructions
  participant RUN as run.py
  participant CFG as config.yaml - (via config.py)
  participant SM as state_machine.py
  participant PH as phases.py
  participant ST as state.py
  participant EX as executor.py
  participant SDK as sdk_runner.py
  participant BD as boundaries.py
  participant CP as Copilot
  participant G as Gate modules

  GA->>TK: fetch skills + instructions (fresh each run)
  GA->>RUN: python run.py init (story)
  GA->>RUN: python run.py autorun
  RUN->>CFG: load settings (models, gates, test cmd, coverage)
  RUN->>RUN: choose runner (Copilot SDK)
  RUN->>SM: start run

  rect rgb(235, 245, 255)
  Note over SM,CP: PHASE EXECUTION - identical for every phase
  SM->>PH: read phase definition - (allowed writes, artifact, max attempts)
  SM->>EX: run phase
  EX->>EX: iteration cap check
  EX->>SDK: phase + story (+ rejection feedback)
  SDK->>SDK: build prompt = skills/instructions + phase task
  SDK->>CP: invoke model (phase-specific model)
  loop every file write
    CP->>SDK: request permission to write path
    SDK->>BD: is_write_allowed(path)?
    BD-->>SDK: yes / no
    SDK-->>CP: approve / reject (shell always rejected)
  end
  CP-->>SDK: events: usage, skills, tools, finished
  SDK-->>EX: AgentResult (attempted writes, tokens, errors)
  EX->>EX: re-check writes, required artifact exists
  EX-->>SM: exit code (OK / BOUNDARY_VIOLATION / ITERATION_CAP / ARTIFACT_MISSING / SDK_ERROR)
  SM->>ST: save run-state.json (resumable)
  end

  Note over SM: Phase 1 - CONTEXT
  SM->>G: clarification.py - [NEEDS CLARIFICATION]? GO / NO_GO?
  G-->>SM: pass / HALT

  opt Phase 2 - DESIGN (only if context says needed)
    Note over SM: run design phase -> design.md
  end

  Note over SM: Phase 3 - PLAN (prompt_steps) -> prompt-steps.md
  SM->>G: plan_check.py - planned paths under real package roots?
  G-->>SM: pass / HALT

  Note over SM: Phase 4 - CODING (src/main/** only)
  EX->>G: execution_record.py - scope gate (unplanned new classes, duplicates)
  G-->>SM: pass / SCOPE_VIOLATION

  Note over SM: Phase 5 - CODE REVIEW (different-vendor model) -> review.md
  SM->>G: review.py - parse verdict
  alt blocking findings
    G-->>SM: fail -> loop back to CODING (max 2)
  else clean
    G-->>SM: pass
  end

  Note over SM: Phase 6 - UNIT TESTING (src/test/** only, main code frozen)
  SM->>G: validation.py - harness runs mvn test + coverage >= 90%
  alt tests fail / low coverage
    G-->>SM: loop back to UNIT TESTING (max 2) or HALT
  else pass
    G-->>SM: pass
  end

  Note over SM: Phase 7 - AC VALIDATION -> validation.md
  SM->>G: ac_validation.py - MET / NOT_MET / UNVERIFIABLE per AC
  alt NOT_MET (blocking mode)
    G-->>SM: loop back to CODING (max 2)
  else pass
    G-->>SM: pass
  end

  Note over SM: Phase 8 - DOCUMENTATION (docs/** only)
  Note over SM: Phase 9 - RAISE PR (prepares PR body)

  Note over SM: Global cap: max_phase_runs across all loops -> HALT
  SM-->>RUN: done / halted (reason from halt_gates.py)
  RUN-->>GA: exit

  GA->>RUN: python run.py collect-audit
  GA->>GA: push audit + metrics branches
  GA->>GA: gh pr create (if done)
  Note over GA: Human reviews and merges - harness never merges
```

**Notes**
- The blue box runs once per phase; the phase sections below only show what's different (the gate).
- Human approval gates (after context, design, plan, coding, docs) are auto-approved in CI by `autorun`; locally use `run.py approve / reject`.
- A halted run resumes at a chosen phase via `resume.py`.
