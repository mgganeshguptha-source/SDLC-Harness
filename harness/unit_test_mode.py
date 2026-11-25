"""
unit_test_mode.py — the unit-test run mode: add unit tests to EXISTING code.

WHY
Many service repos have little or no unit-test coverage, and teams get sprint
tasks of the form "add unit tests for X". That is not a story: there is nothing
to design or build, and production code must not change. Build mode cannot do
it (its coverage gate measures only the classes the coding phase changed), so
this mode walks a short spine of its own (phases.UNIT_TEST_PHASES):

    [baseline build + coverage before]   deterministic, no model call
    unit_testing                          writes TEST files only
      -> build gate: tests + coverage     loops back to unit_testing on red
                                          tests or coverage below target
    code_review                           judges the TESTS; loops back to
                                          unit_testing on blocking findings
    raise_pr                              PR body + harness coverage table

WHAT IT TESTS (config: `unit_test:` in .harness/config.yaml, or the workflow's
test_targets input, which wins over config for one run)
    scope: repo | packages | classes
    packages / classes: what to cover when scope is packages / classes
    exclude: extra globs to leave out (added to the defaults below)
    min_coverage (default 90), coverage_metric (default: the repo's)
    max_classes_per_run (default 5): a repo with no tests is far too big for
        one session, so each run takes a BATCH — the classes furthest below the
        target first — and the PR says how many are left. A later run (new
        feature_id) re-measures and takes the next batch.

The coverage target is checked over the batch's classes TAKEN TOGETHER (lines
covered / lines in those classes), the same rule as build mode's per-change
gate, which this mode reuses unchanged by handing it the batch as the classes
to measure.

GUARANTEES
  - production code is frozen: unit_testing may write test globs only (the
    write interlock), and nothing else in this mode writes code at all
  - every red build and every review finding goes back to unit_testing, never
    to a phase that could change production code
  - a class whose behaviour looks wrong is not "fixed" and not asserted as
    correct: the test author lists it in .harness/suspected-defects.md and the
    harness copies that list into the PR
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

MODE = "unit-test"
SUSPECTED_DEFECTS = ".harness/suspected-defects.md"
PR_MARKER = "<!-- harness:unit-test-coverage -->"
SCOPES = ("repo", "packages", "classes")

# Code with nothing worth unit testing, or tested better another way. Applied
# unless use_default_excludes is false; `exclude` adds to them.
DEFAULT_EXCLUDES_BACKEND = (
    "**/*Application.java", "**/config/**", "**/*Config.java",
    "**/*Configuration.java", "**/*Properties.java", "**/dto/**", "**/*Dto.java",
    "**/*DTO.java", "**/package-info.java", "**/module-info.java",
)
DEFAULT_EXCLUDES_FRONTEND = (
    "**/*.module.ts", "**/*-routing.module.ts", "**/main.ts", "**/polyfills.ts",
    "**/test.ts", "**/environments/**", "**/*.model.ts", "**/*.d.ts",
    "**/index.ts", "**/*.routes.ts", "**/*.config.ts",
)
_LIST_CAP = 40
_FILE_CAP = 40_000
_TOTAL_CAP = 200_000


# ------------------------------------------------------------------ settings
@dataclass
class Settings:
    scope: str = "repo"
    packages: list = field(default_factory=list)
    classes: list = field(default_factory=list)
    exclude: list = field(default_factory=list)
    use_default_excludes: bool = True
    min_coverage: float = 90.0
    coverage_metric: str = "LINE"
    max_classes_per_run: int = 5
    max_phase_runs: int = 5
    # Opt-in: tests of classes in the batch that already fail on the base
    # branch and that the test author marks STALE may be repaired
    # (failing_tests.py). Off: they are triaged, never changed.
    repair_failing: bool = False


def _pct(v, default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if 0 < f <= 1.0:          # 0.9 written as a fraction
        f *= 100.0
    return f


def settings(cfg) -> Settings:
    """The `unit_test:` block with defaults applied. Never raises; a bad value
    falls back to its default and problems() reports it."""
    raw = dict(getattr(cfg, "unit_test", None) or {})
    base_min = getattr(cfg, "min_coverage", 0.0) or 0.0

    def _list(key):
        v = raw.get(key)
        if v is None:
            return []
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return [str(x).strip() for x in v if str(x).strip()]

    s = Settings(
        scope=str(raw.get("scope", "repo") or "repo").strip().lower(),
        packages=_list("packages"),
        classes=_list("classes"),
        exclude=_list("exclude"),
        use_default_excludes=str(raw.get("use_default_excludes", True)).strip().lower()
                              not in ("false", "0", "no", "off"),
        min_coverage=_pct(raw.get("min_coverage"), base_min if base_min > 0 else 90.0),
        coverage_metric=str(raw.get("coverage_metric")
                            or getattr(cfg, "coverage_metric", "LINE") or "LINE").upper(),
        max_classes_per_run=5,
        max_phase_runs=5,
        repair_failing=str(raw.get("repair_failing", False)).strip().lower()
                       in ("true", "1", "yes", "on"),
    )
    for key in ("max_classes_per_run", "max_phase_runs"):
        try:
            v = int(raw.get(key, getattr(s, key)))
            if v > 0:
                setattr(s, key, v)
        except (TypeError, ValueError):
            pass
    return s


def problems(cfg, s: Settings) -> list:
    """What is wrong with the unit_test settings, as messages for the developer."""
    out = []
    if s.scope not in SCOPES:
        out.append(f"unit_test.scope must be one of {', '.join(SCOPES)} (got '{s.scope}').")
    if s.scope == "packages" and not s.packages:
        out.append("unit_test.scope is 'packages' but unit_test.packages is empty.")
    if s.scope == "classes" and not s.classes:
        out.append("unit_test.scope is 'classes' but unit_test.classes is empty.")
    if not (0 < s.min_coverage <= 100):
        out.append(f"unit_test.min_coverage must be between 1 and 100 (got {s.min_coverage}).")
    if s.coverage_metric not in ("LINE", "BRANCH", "INSTRUCTION", "METHOD"):
        out.append(f"unit_test.coverage_metric must be LINE, BRANCH, INSTRUCTION or METHOD "
                   f"(got '{s.coverage_metric}').")
    return out


def is_unit_test(run) -> bool:
    from phases import normalize_mode
    return normalize_mode(getattr(run, "mode", None)) == MODE


def coverage_targets(run) -> list:
    """The production files the coverage gate measures: this batch in unit-test
    mode, the files the coding phase changed otherwise."""
    if is_unit_test(run):
        return list((getattr(run, "unit_test", None) or {}).get("targets") or [])
    return list(getattr(run, "changed_main_files", None) or [])


def apply_overrides(cfg, run):
    """Make a loaded HarnessConfig behave for a unit-test run. Returns cfg.

    Production code is frozen, so every loopback goes to unit_testing; the
    coverage gate measures the batch ("changed" scope over the targets); a repo
    that skips unit_testing in build mode does not skip it here (it is the
    whole point of the run)."""
    if cfg is None or not is_unit_test(run):
        return cfg
    s = settings(cfg)
    cfg.min_coverage = s.min_coverage
    cfg.coverage_metric = s.coverage_metric
    cfg.coverage_scope = "changed"
    cfg.skip_phases = [p for p in (cfg.skip_phases or []) if p != "unit_testing"]
    cfg.validation_loopback_phase = "unit_testing"
    cfg.test_failure_loopback_phase = "unit_testing"
    cfg.coverage_loopback_phase = "unit_testing"
    cfg.review_loopback_phase = "unit_testing"
    cfg.max_phase_runs = max(int(cfg.max_phase_runs or 0), s.max_phase_runs)
    return cfg


# ------------------------------------------------------------------- targets
def _is_frontend(cfg) -> bool:
    return getattr(cfg, "resolved_coverage_format", lambda: "jacoco")() == "istanbul"


def _git_files(repo_root: Path) -> list:
    try:
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                             cwd=str(repo_root), capture_output=True, text=True, timeout=60)
        if out.returncode == 0:
            return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    except Exception:
        pass
    skip = {".git", "node_modules", "target", ".harness", "dist", "build"}
    files = []
    for p in Path(repo_root).rglob("*"):
        rel = p.relative_to(repo_root)
        if p.is_file() and not (skip & set(rel.parts)):
            files.append(rel.as_posix())
    return files


def candidates(repo_root, cfg, s: Settings | None = None) -> list:
    """Every production file this repo could get unit tests for, after
    excludes. Backend: .java under src/main/java. Frontend: .ts that is not a
    spec (templates and styles carry no coverage)."""
    from boundaries import _matches
    s = s or settings(cfg)
    front = _is_frontend(cfg)
    ex = list(getattr(cfg, "write_exclude", None) or [])
    if s.use_default_excludes:
        ex += list(DEFAULT_EXCLUDES_FRONTEND if front else DEFAULT_EXCLUDES_BACKEND)
    ex += list(s.exclude)
    out = []
    for rel in _git_files(Path(repo_root)):
        rel = rel.replace("\\", "/")
        if front:
            if not rel.endswith(".ts") or rel.endswith(".spec.ts"):
                continue
        else:
            if not rel.endswith(".java") or "/src/main/java/" not in f"/{rel}":
                continue
        if not cfg.is_source_file(rel):
            continue
        if any(_matches(rel, g) for g in ex):
            continue
        out.append(rel)
    return sorted(out)


def _java_parts(rel: str) -> tuple:
    """('com.x.y', 'Cls') for a .java path, else ('', '')."""
    from validation import _java_file_to_class_key
    k = _java_file_to_class_key(rel)
    return k if k else ("", "")


def _classify_entry(e: str) -> str:
    """'path' | 'class' | 'package' for one test_targets / config entry."""
    e = e.strip()
    if "/" in e or e.endswith((".java", ".ts", ".kt")):
        return "path"
    last = e.rsplit(".", 1)[-1]
    if last[:1].isupper():
        return "class"
    if "." in e:
        return "package"
    # a bare lowercase word: an Angular file stem like 'claims.service' has a
    # dot; a bare word is most likely a folder / package segment
    return "package"


def _matches_entry(rel: str, entry: str, kind: str, front: bool) -> bool:
    e = entry.strip().strip("/")
    if kind == "path":
        return rel == e or rel.startswith(e + "/") or rel.endswith("/" + e)
    if front:
        stem = Path(rel).name[:-3] if rel.endswith(".ts") else Path(rel).name
        if kind == "class":
            return stem == e or stem.replace(".", "").lower() == e.replace(".", "").lower()
        # package for frontend: a folder name or dotted folder path
        folder = "/" + str(Path(rel).parent).replace("\\", "/") + "/"
        return ("/" + e.replace(".", "/") + "/") in folder or stem == e
    pkg, cls = _java_parts(rel)
    if kind == "class":
        return (f"{pkg}.{cls}" == e) or ("." not in e and cls == e)
    return pkg == e or pkg.startswith(e + ".")


def parse_requested(text: str) -> list:
    """The workflow's test_targets text -> entries. Comma, semicolon or newline
    separated; blank -> []."""
    if not text:
        return []
    raw = str(text).replace(";", ",").replace("\n", ",")
    return [x.strip() for x in raw.split(",") if x.strip()]


def resolve(repo_root, cfg, requested: str = "", s: Settings | None = None) -> tuple:
    """(files in scope, unmatched entries, how the scope was chosen)."""
    s = s or settings(cfg)
    front = _is_frontend(cfg)
    pool = candidates(repo_root, cfg, s)
    entries = parse_requested(requested)
    if entries:
        source = "workflow input test_targets"
    elif s.scope == "packages":
        entries, source = list(s.packages), "unit_test.packages"
    elif s.scope == "classes":
        entries, source = list(s.classes), "unit_test.classes"
    else:
        return pool, [], "unit_test.scope: repo"
    picked, unmatched = [], []
    for e in entries:
        kind = _classify_entry(e)
        hits = [f for f in pool if _matches_entry(f, e, kind, front)]
        if not hits:
            unmatched.append(e)
        for h in hits:
            if h not in picked:
                picked.append(h)
    return sorted(picked), unmatched, source


# ------------------------------------------------------------------- coverage
def measure(repo_root, cfg, files: list, s: Settings | None = None) -> tuple:
    """Per-file coverage from the report the last build wrote.

    Returns (report_found, {file: {"pct": float|None, "covered": int,
    "total": int}}). pct None = the file has no row in the report. A file with
    nothing to cover (an interface) is 100."""
    s = s or settings(cfg)
    repo_root = Path(repo_root)
    metric = s.coverage_metric
    out = {f: {"pct": None, "covered": 0, "total": 0} for f in files}
    if _is_frontend(cfg):
        from validation import _find_istanbul_summary, _ISTANBUL_METRIC
        path = _find_istanbul_summary(repo_root, getattr(cfg, "coverage_json", "") or "")
        if path is None:
            return False, out
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return False, out
        key = _ISTANBUL_METRIC.get(metric, "lines")
        for fk, entry in data.items():
            if fk == "total":
                continue
            k = fk.replace("\\", "/")
            for f in files:
                if k.endswith("/" + f) or k == f:
                    m = (entry or {}).get(key) or {}
                    out[f]["total"] += int(m.get("total") or 0)
                    out[f]["covered"] += int(m.get("covered") or 0)
                    out[f]["_seen"] = True
    else:
        csv_path = cfg.resolved_coverage_csv(repo_root)
        if csv_path is None:
            return False, out
        want = {}
        for f in files:
            pk = _java_parts(f)
            if pk[1]:
                want[pk] = f
        try:
            import csv as _csv
            with csv_path.open(encoding="utf-8") as fh:
                for row in _csv.DictReader(fh):
                    pkg = row.get("PACKAGE", "")
                    top = (row.get("CLASS", "") or "").split("$", 1)[0].split(".", 1)[0]
                    f = want.get((pkg, top))
                    if not f:
                        continue
                    out[f]["total"] += int(row.get(f"{metric}_MISSED") or 0) + \
                        int(row.get(f"{metric}_COVERED") or 0)
                    out[f]["covered"] += int(row.get(f"{metric}_COVERED") or 0)
                    out[f]["_seen"] = True
        except Exception:
            return False, out
    for f, v in out.items():
        if v.pop("_seen", False):
            v["pct"] = round(100.0 * v["covered"] / v["total"], 1) if v["total"] else 100.0
    return True, out


def aggregate(per_file: dict, files: list) -> float | None:
    cov = tot = 0
    seen = False
    for f in files:
        v = per_file.get(f) or {}
        if v.get("pct") is None:
            continue
        seen = True
        cov += int(v.get("covered") or 0)
        tot += int(v.get("total") or 0)
    if not seen:
        return None
    return round(100.0 * cov / tot, 1) if tot else 100.0


def _run_coverage_command(repo_root, cfg, log) -> None:
    cmd = (getattr(cfg, "coverage_command", "") or "").strip()
    if not cmd:
        return       # e.g. Karma/Jest: the test command writes the report itself
    log(f"  [unit-test] coverage report: {cmd}")
    try:
        import baseline as _BL
        subprocess.run(cmd, cwd=str(repo_root), shell=True, capture_output=True,
                       text=True, timeout=int(getattr(cfg, "test_timeout", 1800) or 1800),
                       env=_BL.tolerant_env())
    except Exception as e:
        log(f"  [unit-test] coverage command error: {e}")


def select_batch(per_file: dict, files: list, target: float, limit: int,
                 report_found: bool) -> tuple:
    """(batch, remaining below target, skipped as not in the report)."""
    below, skipped = [], []
    for f in files:
        pct = (per_file.get(f) or {}).get("pct")
        if pct is None:
            if report_found:
                skipped.append(f)          # the coverage tool does not measure it
                continue
            below.append((0.0, f))         # no report at all: treat as untested
        elif pct < target:
            below.append((pct, f))
    below.sort(key=lambda t: (t[0], t[1]))
    ordered = [f for _, f in below]
    return ordered[:limit], ordered[limit:], skipped


def prepare(run, repo_root, harness_dir, log=print) -> bool:
    """After the baseline build: measure coverage before, pick this run's
    batch. True = carry on with the phases; False = the run stopped here
    (status 'done' when everything already meets the target, 'halted'
    otherwise) and run-state.json says why. A resumed run keeps its batch."""
    from config import HarnessConfig
    ut = run.unit_test = dict(getattr(run, "unit_test", None) or {})
    if ut.get("targets"):
        log(f"  [unit-test] batch already chosen when this run started "
            f"({len(ut['targets'])} file(s)) — not re-measuring")
        return True
    repo_root, harness_dir = Path(repo_root), Path(harness_dir)
    cfg = HarnessConfig.load(harness_dir, repo_root=repo_root)
    s = settings(cfg)
    files = list(ut.get("candidates") or [])
    if not files:
        files, _unmatched, _src = resolve(repo_root, cfg, ut.get("requested") or "", s)
    log("\n=== Unit-test scope and coverage before (no AI) ===")
    log(f"  [unit-test] {len(files)} production file(s) in scope; target "
        f"{s.min_coverage:.1f}% {s.coverage_metric} coverage; at most "
        f"{s.max_classes_per_run} per run")
    _run_coverage_command(repo_root, cfg, log)
    found, per_file = measure(repo_root, cfg, files, s)
    if not found:
        log("  ! [unit-test] no coverage report after the baseline build — every file in "
            "scope is treated as untested. If the repo has tests already, check that the "
            "coverage tool runs with test_command / coverage_command; the build gate "
            "will halt if it still finds no report.")
    # Tests that already fail on the base branch (baseline.py) and belong to
    # classes in scope: recorded for triage; with repair on, their classes go
    # first in the batch (failing_tests.py).
    first = []
    try:
        import baseline as _BLu
        import failing_tests as _FTu
        first = _FTu.prepare(run, repo_root, files, _BLu.known_failing(run), s, log)
    except Exception as e:
        log(f"  [failing-tests] scope not recorded ({type(e).__name__}: {e})")
    first = first[:s.max_classes_per_run]
    batch, remaining, skipped = select_batch(
        per_file, [f for f in files if f not in first], s.min_coverage,
        max(0, s.max_classes_per_run - len(first)), found)
    if first:
        # A class at target that only needs a repair still belongs in the batch.
        batch = first + batch
    if skipped:
        log(f"  [unit-test] {len(skipped)} file(s) have no row in the coverage report "
            f"(excluded by the coverage tool?) and are left out, e.g. {skipped[0]}")
    ut.update({
        "metric": s.coverage_metric,
        "min_coverage": s.min_coverage,
        "report_found_before": found,
        "coverage_before": {f: (per_file.get(f) or {}).get("pct") for f in batch},
        "aggregate_before": aggregate(per_file, batch) if found else None,
        "targets": batch,
        "remaining": remaining,
        "skipped_not_in_report": skipped[:_LIST_CAP],
        "already_at_target": max(0, len(files) - len(batch) - len(remaining) - len(skipped)),
    })
    ut.pop("candidates", None)    # the batch is what matters from here on
    run.unit_test = ut
    if not batch:
        log(f"  [unit-test] every file in scope already meets {s.min_coverage:.1f}% "
            f"{s.coverage_metric} coverage — nothing to do, no AI phase runs.")
        run.status = "done"
        run.save(harness_dir)
        return False
    log(f"  [unit-test] this run's batch ({len(batch)}), lowest coverage first:")
    for f in batch:
        p = ut["coverage_before"].get(f)
        log(f"    - {f}  ({'no data' if p is None else f'{p:.1f}%'})")
    if remaining:
        log(f"  [unit-test] {len(remaining)} more file(s) below target are left for a "
            f"later run")
    run.save(harness_dir)
    return True


def record_after(run, repo_root, harness_dir, log=print) -> None:
    """After a green build gate: per-file coverage of the batch, for the PR."""
    try:
        from config import HarnessConfig
        cfg = HarnessConfig.load(Path(harness_dir), repo_root=Path(repo_root))
        s = settings(cfg)
        targets = coverage_targets(run)
        found, per_file = measure(Path(repo_root), cfg, targets, s)
        if not found:
            return
        run.unit_test["coverage_after"] = {f: (per_file.get(f) or {}).get("pct")
                                           for f in targets}
        run.unit_test["aggregate_after"] = aggregate(per_file, targets)
    except Exception as e:
        log(f"  [unit-test] coverage-after not recorded ({type(e).__name__})")


# ------------------------------------------------------------------ init
def init_problem(repo_root, harness_dir, requested: str = "") -> tuple:
    """Checks a fresh unit-test run makes before anything else.
    Returns (problem or '', candidates list, note for the log)."""
    from config import HarnessConfig, harness_runs_build
    repo_root, harness_dir = Path(repo_root), Path(harness_dir)
    if not harness_runs_build(harness_dir):
        return ("mode unit-test needs the harness to build and run the tests (it measures "
                "coverage), but runs_build is false for this repo. Set runs_build: true "
                "in .harness/config.yaml.", [], "")
    cfg = HarnessConfig.load(harness_dir, repo_root=repo_root)
    s = settings(cfg)
    if not parse_requested(requested):
        bad = problems(cfg, s)
        if bad:
            return (" ".join(bad), [], "")
    files, unmatched, source = resolve(repo_root, cfg, requested, s)
    note = f"scope from {source}: {len(files)} production file(s)"
    if unmatched:
        note += f"; matched nothing: {', '.join(unmatched)}"
    if not files:
        what = (f"the entries {', '.join(unmatched)}" if unmatched else "the configured scope")
        return (f"{what} matched no production file that can get unit tests (after "
                f"excludes). Check the names: packages as com.x.y, classes as "
                f"com.x.y.MyService or MyService, or repo-relative paths.", [], note)
    return ("", files, note)


def story_text(feature_id: str, requested: str, s: Settings, notes: str = "") -> str:
    scope = requested.strip() or {"repo": "the whole repository",
                                  "packages": "packages " + ", ".join(s.packages),
                                  "classes": "classes " + ", ".join(s.classes)}.get(s.scope, s.scope)
    text = (f"# {feature_id} — add unit tests\n\n"
            f"Add unit tests for existing production code in {scope} so that the classes "
            f"in this run's batch reach at least {s.min_coverage:.0f}% "
            f"{s.coverage_metric.lower()} coverage. Production code must not change.\n")
    if notes.strip():
        text += "\n## Task notes\n\n" + notes.strip() + "\n"
    return text


def task_notes(repo_root, feature_id: str, story_file: str) -> tuple:
    """(text, repo-relative path) of the task's own story file, if there is one.
    The shared fallback file (stories/current-story.md) is never used here: it
    belongs to some other story."""
    try:
        from story_source import resolve_story_file
        path, per_story = resolve_story_file(Path(repo_root), feature_id, story_file)
        if per_story and path.is_file():
            rel = path.relative_to(repo_root) if path.is_relative_to(repo_root) else path
            return path.read_text(encoding="utf-8").strip(), str(rel).replace("\\", "/")
    except Exception:
        pass
    return "", None


# ------------------------------------------------------------------ phase help
def ensure_test_dirs(repo_root, cfg, run, log=print) -> list:
    """Make the test-package folders for the batch (Java): shell is denied and
    the create tool cannot make a missing folder, and a repo with no tests has
    no src/test/java/... tree at all."""
    from boundaries import _matches
    made = []
    root = Path(repo_root)
    for f in coverage_targets(run):
        if "/src/main/java/" not in f"/{f}":
            continue
        test_rel = (f.replace("/src/main/java/", "/src/test/java/", 1) if "/src/main/java/" in f
                    else f.replace("src/main/java/", "src/test/java/", 1))
        parent = (root / test_rel).parent
        if parent.exists():
            continue
        rel_dir = parent.relative_to(root).as_posix()
        if not any(_matches(rel_dir + "/X.java", g) for g in cfg.test_paths()):
            continue
        try:
            parent.mkdir(parents=True, exist_ok=True)
            made.append(rel_dir)
        except OSError as e:
            log(f"  [unit-test] could not create {rel_dir}: {e}")
    if made:
        log(f"  [unit-test] created test folder(s): {', '.join(sorted(set(made)))}")
    return made


def _fmt(p) -> str:
    return "no data" if p is None else f"{p:.1f}%"


def target_lines(run) -> str:
    ut = getattr(run, "unit_test", None) or {}
    before = ut.get("coverage_before") or {}
    return "\n".join(f"  - {f}  (coverage now: {_fmt(before.get(f))})"
                     for f in coverage_targets(run))


def unit_testing_task(run, rr: str, src_main: str, src_test: str, dossier: str | None) -> str:
    ut = getattr(run, "unit_test", None) or {}
    target = ut.get("min_coverage", 90.0)
    metric = str(ut.get("metric", "LINE")).lower()
    notes = ""
    if "## Task notes" in (run.story or ""):
        notes = "\nTask notes from the developer:\n" + run.story.split("## Task notes", 1)[1].strip() + "\n"
    text = (
        f"You are adding unit tests to EXISTING production code that has little or no "
        f"test coverage. This is not a feature: nothing about the production code changes.\n"
        f"CLASSES TO COVER (this run's batch):\n{target_lines(run)}\n"
        f"GOAL: these classes taken together must reach at least {target:.0f}% {metric} "
        f"coverage. The harness builds the code, runs the tests and measures coverage "
        f"after you finish; below the goal you are sent back with the numbers.\n"
        f"{notes}"
        f"RULES:\n"
        f"  - Write test files ONLY under {src_test}. You MUST NOT create or edit any file "
        f"under {src_main}; the harness refuses such writes.\n"
        f"  - One test class per production class, named and placed the way the existing "
        f"tests are (Java: <Class>Test in the same package under src/test/java; Angular: "
        f"<name>.spec.ts next to the file). Extend an existing test file rather than "
        f"creating a second one for the same class.\n"
        f"  - Test behaviour through the public API. Every test must assert something the "
        f"code does — a return value, a state change, an emitted value, an exception, an "
        f"interaction that matters. A test that only calls a method to raise coverage, "
        f"or asserts only what a mock was stubbed to return, is rejected at review.\n"
        f"  - Unit tests only: mock collaborators (Mockito for Java, StepVerifier for "
        f"Mono/Flux; TestBed with jasmine spies or HttpTestingController for Angular). No "
        f"real network, database, file system, sleeps or wall-clock time.\n"
        f"  - Cover the branches: null/empty input, error paths, each condition both ways.\n"
        f"  - Test data must be invented ('sample-<field>', 1, '2026-01-01'). Never copy "
        f"values from existing resource files — they may hold real member data.\n"
        f"  - Do NOT use reflection to reach private fields or methods, and do not weaken "
        f"an existing test.\n"
        f"  - If the code's behaviour looks WRONG, do not assert the wrong behaviour as "
        f"correct and do not write a test that fails. Leave that case out and add one "
        f"line to {rr}/{SUSPECTED_DEFECTS} (the .harness folder exists): "
        f"`- <file> <method>: <what looks wrong>`. A person decides.\n"
        f"  - You MAY read production source under {src_main} for collaborators, DTOs and "
        f"interfaces the tests need. Read only what you need, then write.\n"
    )
    if dossier:
        text += ("\n================ TEST CONTEXT (inlined by harness) ================\n\n"
                 f"{dossier}\n\n================ END TEST CONTEXT ================\n")
    return text


def review_rules(rr: str) -> str:
    return (
        f"You are an INDEPENDENT reviewer of UNIT TESTS that were written for existing "
        f"production code. The production code did not change and is not under review; "
        f"do not request production changes. Judge only whether the TESTS are sound.\n"
        f"Write the verdict to this EXACT path (parent .harness ALREADY EXISTS, shell "
        f"disallowed): {rr}/.harness/review.md\n"
        f"  - First line MUST be:  VERDICT: PASS   or   VERDICT: CHANGES_REQUESTED\n"
        f"  - Each blocking problem on its own line:  [ISSUE]: (<CLASS>) <test, method, "
        f"what is wrong, what to do>\n"
        f"  - Non-blocking observations:  [NOTE]: <observation>\n"
        f"BLOCKING CLASSES (only these may block):\n"
        f"    (ASSERTION) a test with no meaningful assertion: it only calls code, asserts "
        f"true/not-null where a value is known, or asserts only what a mock was stubbed "
        f"to return — coverage without checking anything;\n"
        f"    (CORRECTNESS) a test asserts behaviour the production code does not have, "
        f"names one thing and checks another, or asserts behaviour that looks like a bug "
        f"as if it were correct;\n"
        f"    (ISOLATION) real network, database, file system, sleeps or wall-clock time, "
        f"or order-dependent tests — anything flaky;\n"
        f"    (SECURITY) test data that looks like real personal data (names with dates of "
        f"birth, SSNs, member ids, real emails or phone numbers) or real secrets;\n"
        f"    (CONTRACT) reflection into private members, or a test that changes or "
        f"weakens an existing test.\n"
        f"  Style, naming, duplication between tests, helper structure and how many tests "
        f"there are are [NOTE]s. Coverage is measured by the harness: never block on it.\n"
        f"  Each [ISSUE] MUST start with its class in parentheses. If you cannot name a "
        f"blocking class, it is a [NOTE]. If the tests are sound, the verdict is PASS.\n"
        f"MANDATORY: write review.md on THIS attempt as a full overwrite; an existing file "
        f"is from an earlier attempt and is stale. Write ONLY {rr}/.harness/review.md."
    )


def review_task(run, rr: str, dossier: str | None, src_main: str, src_test: str) -> str:
    head = f"Unit-test task {run.feature_id}. Classes under test:\n{target_lines(run)}\n"
    if dossier:
        return (head + "ALL review material is INLINE below — every test file this run "
                "wrote or changed, the production classes under test and any suspected "
                "defects the test author recorded. Do NOT read any other file; reads are "
                "disabled for this phase.\n" + review_rules(rr) +
                "\n\n================ REVIEW MATERIAL (inlined by harness) ================\n\n"
                f"{dossier}\n\n================ END REVIEW MATERIAL ================\n")
    return (head + f"Read (read-only) the tests under {src_test} that test the classes "
            f"above, and those classes under {src_main}.\n" + review_rules(rr))


def raise_pr_task(run, pr_body: str, build_status_pr: str) -> str:
    return (
        f"Write a pull request body for unit tests added to existing code, to this EXACT "
        f"file (parent .harness ALREADY EXISTS, shell disallowed): {pr_body}\n"
        f"Task: {run.feature_id}. Classes covered:\n{target_lines(run)}\n"
        f"Say in plain words which classes got tests and what behaviour the tests cover "
        f"(main paths, error paths, edge cases). State that production code was not "
        f"changed. Do NOT write coverage figures: the harness appends the measured "
        f"before/after table itself. {build_status_pr}"
        f"Write ONLY {pr_body}."
    )


def review_dossier(repo_root, run, cfg, log=print) -> str | None:
    """Test files this run wrote + the classes under test + suspected defects."""
    import handoff as _HO
    root = Path(repo_root)
    changed = []
    try:
        changed = _HO.changed_files(root, _HO.base_ref_from_env())
    except Exception:
        changed = []
    if not changed:
        try:
            out = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                                 cwd=str(root), capture_output=True, text=True, timeout=60)
            changed = [ln[3:].strip() for ln in out.stdout.splitlines() if ln.strip()]
        except Exception:
            changed = []
    tests = [f for f in dict.fromkeys(changed) if cfg.is_test_file(f) and (root / f).is_file()]
    if not tests:
        return None
    parts = []
    for rel in tests:
        try:
            t = (root / rel).read_text(encoding="utf-8")
        except Exception:
            continue
        if len(t) > _FILE_CAP:
            t = t[:_FILE_CAP] + "\n... [TRUNCATED by harness — file cap reached]"
        parts.append(f"--- TEST FILE WRITTEN BY THIS RUN: {rel} ---\n{t}")
    for rel in coverage_targets(run):
        try:
            t = (root / rel).read_text(encoding="utf-8")
        except Exception:
            continue
        if len(t) > _FILE_CAP:
            t = t[:_FILE_CAP] + "\n... [TRUNCATED by harness — file cap reached]"
        parts.append(f"--- PRODUCTION CLASS UNDER TEST (unchanged): {rel} ---\n{t}")
    try:
        import failing_tests as _FTr
        _ftm = _FTr.review_material(root, run)
        if _ftm:
            parts.append(_ftm)
    except Exception:
        pass
    sd = root / SUSPECTED_DEFECTS
    if sd.is_file():
        try:
            parts.append("--- SUSPECTED DEFECTS RECORDED BY THE TEST AUTHOR ---\n"
                         + sd.read_text(encoding="utf-8"))
        except Exception:
            pass
    d = "\n\n".join(parts)
    if len(d) > _TOTAL_CAP:
        d = d[:_TOTAL_CAP] + "\n... [TRUNCATED by harness — dossier cap reached]"
    log(f"  [review] unit-test dossier: {len(tests)} test file(s), "
        f"{len(coverage_targets(run))} class(es) under test, {len(d)} chars")
    return d


def review_feedback(issues_block: str) -> str:
    return ("An INDEPENDENT reviewer requested changes to the TESTS you wrote. Fix the "
            "tests to address every issue below. Production code stays unchanged.\n"
            f"Reviewer issues:\n{issues_block}")


RED_BUILD_INSTRUCTION = (
    "The build FAILED after you wrote the tests. Production code is frozen in this run, "
    "so the fix is in the TESTS.\n"
    "Failing tests: {names}\n"
    "Fix these tests (edit them in place). If a test fails because the production code "
    "really behaves that way, the test's expectation is wrong: assert what the code does "
    "if that is clearly intended, or remove the test and record the case in "
    f"{SUSPECTED_DEFECTS}. Never ask for production changes. A test that already "
    "failed on the base branch is never removed: if your repair of one does not pass, "
    "put it back exactly as it was and change its triage line to DEFECT. "
    "Failure output:\n"
)


# ------------------------------------------------------------------ PR body
def pr_section(repo_root, run) -> str:
    ut = getattr(run, "unit_test", None) or {}
    targets = coverage_targets(run)
    before = ut.get("coverage_before") or {}
    after = ut.get("coverage_after") or {}
    metric = str(ut.get("metric", "LINE")).lower()
    lines = [PR_MARKER, "", f"## Coverage (measured by the harness, {metric})", "",
             "| Class | Before | After |", "|---|---|---|"]
    for f in targets:
        lines.append(f"| `{f}` | {_fmt(before.get(f))} | {_fmt(after.get(f))} |")
    lines.append(f"| **Batch total** | {_fmt(ut.get('aggregate_before'))} | "
                 f"**{_fmt(ut.get('aggregate_after'))}** |")
    lines += ["", f"Target: {float(ut.get('min_coverage', 0)):.0f}% for the batch taken "
              "together. Production code was not changed by this run."]
    try:
        import failing_tests as _FTp
        lines += _FTp.pr_section(repo_root, run)
    except Exception:
        pass
    rem = ut.get("remaining") or []
    if rem:
        lines += ["", f"### Not in this batch ({len(rem)})", "",
                  "These files are still below the target. Run the harness again in "
                  "unit-test mode with a new task id to take the next batch:", ""]
        lines += [f"- `{f}`" for f in rem[:_LIST_CAP]]
        if len(rem) > _LIST_CAP:
            lines.append(f"- ... and {len(rem) - _LIST_CAP} more")
    sd = Path(repo_root) / SUSPECTED_DEFECTS
    if sd.is_file():
        try:
            body = sd.read_text(encoding="utf-8").strip()
        except Exception:
            body = ""
        if body:
            lines += ["", "### Suspected defects (not fixed, not asserted)", "",
                      "The test author found behaviour that looks wrong. No test asserts it "
                      "and the code was not changed. Please review:", "", body]
    return "\n".join(lines) + "\n"


def add_to_pr_body(repo_root, run) -> bool:
    if not is_unit_test(run):
        return False
    p = Path(repo_root) / ".harness" / "pr-body.md"
    try:
        text = p.read_text(encoding="utf-8") if p.is_file() else ""
        if PR_MARKER in text:
            text = text.split(PR_MARKER, 1)[0].rstrip() + "\n"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text((text.rstrip() + "\n\n" if text.strip() else "")
                     + pr_section(repo_root, run), encoding="utf-8")
        return True
    except Exception:
        return False


def metrics_fields(run) -> dict:
    ut = getattr(run, "unit_test", None) or {}
    if not ut:
        return {}
    return {
        "unit_test_targets": len(ut.get("targets") or []),
        "unit_test_remaining": len(ut.get("remaining") or []),
        "unit_test_min_coverage": ut.get("min_coverage"),
        "unit_test_metric": ut.get("metric"),
        "unit_test_coverage_before": ut.get("aggregate_before"),
        "unit_test_coverage_after": ut.get("aggregate_after"),
    }
