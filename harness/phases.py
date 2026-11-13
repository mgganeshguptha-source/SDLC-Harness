"""
phases.py — the phase spine of the harness.

Each Phase declares, DECLARATIVELY:
  - name
  - whether it runs the model (probabilistic) or is a pure gate
  - allowed write globs   -> the interlock the permission handler enforces
  - required artifact      -> the pinned output that must exist to advance
  - human_gate             -> whether a human approve/reject follows

The sequence here is the single source of truth. The state machine walks this
list; it cannot skip, reorder, or jump. That is the "sequence lives in code"
guarantee from the harness thesis.
"""
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional


class PhaseKind(Enum):
    MODEL = auto()   # spawns a Copilot session to do fuzzy work
    GATE = auto()    # pure human checkpoint, no model call


@dataclass(frozen=True)
class Phase:
    id: str
    title: str
    kind: PhaseKind
    # Write boundary: glob patterns the model MAY write to during this phase.
    # Empty list = no file writes allowed at all (read-only phase).
    # The permission handler denies any write whose path matches none of these.
    allowed_writes: tuple = field(default_factory=tuple)
    # Artifact that must exist (relative to repo root) for the phase to return OK.
    required_artifact: Optional[str] = None
    # Does a human approve/reject gate follow this phase?
    human_gate: bool = False
    # Per-phase iteration budget (model turns) — the credit guard.
    max_iterations: int = 6
    # If True, the HARNESS runs the validation gate (mvn test) after this phase
    # returns OK. Non-zero exit => VALIDATION_FAILED => halt before advancing.
    validate_after: bool = False
    # Directories (repo-relative) the HARNESS ensures exist before the phase runs.
    # Deterministic setup so the agent never needs shell to mkdir. These dirs must
    # fall within the phase's allowed_writes.
    pre_create_dirs: tuple = field(default_factory=tuple)
    # Files (repo-relative) the HARNESS deletes BEFORE the phase runs, so that a
    # fresh write is the only way the artifact can exist afterwards.
    #
    # This is what makes a "was it written THIS attempt?" gate sound. Without it a
    # retry can silently inherit the previous attempt's file: in run 31235571294 the
    # reviewer read the existing review.md, judged its PASS still accurate, wrote
    # nothing, and the gate correctly-but-unhelpfully halted on STALE VERDICT.
    # Only paths inside the phase's own allowed_writes belong here.
    clear_before_run: tuple = field(default_factory=tuple)
    # If True, after this phase the harness scans the context output for
    # [NEEDS CLARIFICATION] markers and halts (needs_input) if any remain.
    # Run the CONTEXT GATE after this phase: scans the written context file for
    # BOTH [NEEDS CLARIFICATION] (story ambiguous) and [BLOCKER] (this repo cannot
    # build it). One scan, one halt path — see clarification.scan_context.
    scan_clarifications: bool = False
    # If True, after this phase the harness appends an EXECUTION RECORD to the
    # plan audit file (actual files touched vs approved Impacted Files; flags
    # scope additions). Used on the coding phase.
    record_execution: bool = False
    # If True, after this phase the harness parses the reviewer's structured
    # verdict (.harness/review.md). Issues => loop back to coding with the issues
    # as feedback (bounded by max_review_retries); clean => advance.
    review_gate: bool = False
    # Run the AC-CONFORMANCE gate after this phase: reads .harness/validation.md
    # and requires a verdict for every acceptance criterion. Distinct from
    # review_gate, which judges the code rather than the criteria.
    validation_gate: bool = False
    # Run the ANALYSIS gate after this phase (analysis mode): an open
    # [NEEDS CLARIFICATION] line in the analysis document halts the run;
    # otherwise the harness stamps the document and copies it to the story
    # folder for the document-only pull request. See analysis.py.
    analysis_gate: bool = False


# ---- THE SPINE ----------------------------------------------------------
# .harness/ is the machine-local workspace (gitignored).
# src/main/** and src/test/** are the petclinic app under change.

PHASES = (
    # Runs BY EXCEPTION, like design: only when the service repo has no
    # .github/copilot-instructions.md (the state machine skips it otherwise).
    # About half of stories never go through the harness; developers working
    # them by hand in the IDE get the same repo conventions only if this file
    # exists, so the harness creates it the first time it meets a repo. The
    # file ships in the harness PR and is reviewed there.
    Phase(
        id="repo_instructions",
        title="Repository Copilot instructions (when missing)",
        kind=PhaseKind.MODEL,
        allowed_writes=(".github/copilot-instructions.md", ".harness/**"),
        required_artifact=".github/copilot-instructions.md",
        human_gate=False,                           # reviewed in the PR instead
        max_iterations=8,
        pre_create_dirs=(".github",),
    ),
    Phase(
        id="context",
        title="Story -> Context building",
        kind=PhaseKind.MODEL,
        # Real toolkit output path (copilot-output-naming convention) + workspace.
        allowed_writes=(".github/story-context-files/**", ".harness/**"),
        required_artifact=None,   # checked via clarification scan instead of fixed name
        human_gate=True,                            # -> review context
        max_iterations=6,
        pre_create_dirs=(".github/story-context-files",),
        scan_clarifications=True,                   # context gate: clarifications + feasibility
    ),
    Phase(
        id="design",
        title="Technical design (by exception)",
        kind=PhaseKind.MODEL,
        allowed_writes=(".harness/**",),
        required_artifact=".harness/design.md",
        # Cleared before the phase runs so "the file exists" means "written this
        # attempt" — the same guard the review gate needs, for the same reason.
        clear_before_run=(".harness/design.md",),
        human_gate=True,                            # -> review the decisions
        # Higher than the other authoring phases: design must read the existing
        # code before it can describe it, and a brownfield design that skipped
        # that reading would be a guess. 6 turns proved too few — the phase spent
        # them all exploring and never reached the write.
        max_iterations=12,
    ),
    Phase(
        id="prompt_steps",
        title="Prompt steps (implementation plan)",
        kind=PhaseKind.MODEL,
        allowed_writes=(".harness/**",),
        required_artifact=".harness/prompt-steps.md",
        human_gate=True,                            # -> review steps
        max_iterations=6,
    ),
    Phase(
        id="coding",
        title="Coding (main source only)",
        kind=PhaseKind.MODEL,
        # THE KEY INTERLOCK: may write app code, but NOT tests.
        allowed_writes=("src/main/**", ".harness/**"),
        required_artifact=None,
        human_gate=True,                            # -> review code
        max_iterations=10,
        record_execution=True,                      # append actual-vs-approved audit
    ),
    Phase(
        id="code_review",
        title="Code review (independent reviewer LLM)",
        kind=PhaseKind.MODEL,
        # READ-ONLY on source: the reviewer inspects code but may write ONLY its
        # verdict file into the workspace. Source stays frozen — a reviewer must
        # not edit the code it is judging.
        allowed_writes=(".harness/**",),
        required_artifact=".harness/review.md",
        # Delete last attempt's verdict first: the review gate asserts the reviewer
        # wrote a verdict for THIS code, and a surviving file makes a live PASS
        # indistinguishable from a stale one.
        clear_before_run=(".harness/review.md",),
        human_gate=False,                           # review gate (harness-owned) follows
        max_iterations=8,
        review_gate=True,                           # harness parses verdict, may loop to coding
    ),
    Phase(
        id="unit_testing",
        title="Unit testing (test source only)",
        kind=PhaseKind.MODEL,
        # THE MIRROR INTERLOCK: may write tests, but main is now FROZEN
        # (can't quietly edit code to make tests pass).
        allowed_writes=("src/test/**", ".harness/**"),
        required_artifact=None,
        human_gate=False,                           # validation gate follows instead
        max_iterations=10,
        validate_after=True,                        # harness runs mvn test here
    ),
    Phase(
        id="validation",
        title="AC conformance validation",
        kind=PhaseKind.MODEL,
        # READ-ONLY on source: validation observes, it never fixes. A validator
        # that edits the thing it is judging has no standing.
        allowed_writes=(".harness/**",),
        required_artifact=".harness/validation.md",
        clear_before_run=(".harness/validation.md",),
        human_gate=False,                           # harness gate follows
        max_iterations=8,
        validation_gate=True,                       # parse per-AC verdicts
    ),
    Phase(
        id="documentation",
        title="Create documentation",
        kind=PhaseKind.MODEL,
        # copilot-instructions.md: the service rules this story's answers
        # established are added to it, so IDE users outside the harness get
        # them too (see sdk_runner's learning block).
        allowed_writes=("docs/**", ".github/copilot-instructions.md", ".harness/**"),
        required_artifact=None,
        human_gate=True,                            # -> review docs
        max_iterations=6,
        pre_create_dirs=("docs",),                  # harness makes docs/ so agent needn't shell
    ),
    Phase(
        id="raise_pr",
        title="Raise PR request",
        kind=PhaseKind.MODEL,
        allowed_writes=(".harness/**",),
        required_artifact=None,
        human_gate=False,
        max_iterations=4,
    ),
)


# ---- ANALYSIS MODE (bug-fix analysis) -------------------------------------
# A second, much shorter spine, chosen per RUN (the workflow's `mode` input,
# stored on RunState.mode). It produces one document — the root cause and the
# impact of a bug — and nothing else: no code, no tests, no docs. A person
# reviews that document in a pull request; a later, separate BUILD run reads it
# from the story folder and makes the fix.
#
# The model may write exactly ONE file, the analysis document in the run
# workspace. The harness, not the model, copies it to the story folder
# (analysis.publish) and writes the PR body, so analysis mode cannot touch
# source, tests, build files or the story.
ANALYSIS_DOC = ".harness/bug-analysis.md"

ANALYSIS_PHASES = (
    Phase(
        id="bug_analysis",
        title="Bug analysis (root cause and impact)",
        kind=PhaseKind.MODEL,
        allowed_writes=(ANALYSIS_DOC,),
        required_artifact=ANALYSIS_DOC,
        # "The file exists" must mean "written this attempt" (same guard as
        # design.md / review.md).
        clear_before_run=(ANALYSIS_DOC,),
        human_gate=False,                           # reviewed in the PR instead
        # Like design: it must read the code before it can explain the defect.
        max_iterations=12,
        analysis_gate=True,
    ),
)

MODE_BUILD = "build"
MODE_ANALYSIS = "analysis"
MODES = (MODE_BUILD, MODE_ANALYSIS)
_SEQUENCES = {MODE_BUILD: PHASES, MODE_ANALYSIS: ANALYSIS_PHASES}
# Every phase of every mode. Phase ids are unique across modes.
ALL_PHASES = PHASES + ANALYSIS_PHASES


def normalize_mode(mode) -> str:
    """'build' for anything that is not a known mode (None, '', old run-state
    files written before modes existed)."""
    m = str(mode or "").strip().lower()
    return m if m in MODES else MODE_BUILD


def phases_for(mode=None) -> tuple:
    """The phase sequence a run in `mode` walks."""
    return _SEQUENCES[normalize_mode(mode)]


def first_phase(mode=None) -> Phase:
    return phases_for(mode)[0]


def phase_by_id(pid: str) -> Phase:
    for p in ALL_PHASES:
        if p.id == pid:
            return p
    raise KeyError(pid)


def next_phase(pid: str, mode=None) -> Optional[Phase]:
    """The phase after `pid` in the sequence of `mode` (default: build)."""
    seq = phases_for(mode)
    ids = [p.id for p in seq]
    i = ids.index(pid)
    return seq[i + 1] if i + 1 < len(seq) else None
