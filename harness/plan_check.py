"""
plan_check.py — the PLAN PATH CHECK gate.

Runs after prompt_steps returns OK, before the expensive coding phase. It reads
the plan's "Impacted Files" table and confirms that every file path the plan
intends to touch lands under a package ROOT that actually exists in the repo.

WHY THIS EXISTS
On run OP0-014 the planner invented a package path (com.example.bookservice)
by inferring it from the project name without reading the tree. The coding phase
cannot create arbitrary directories (write boundary), so a plan pointing at a
package that does not exist is unexecutable — and discovering that inside coding
costs a full, expensive phase. A directory listing costs nothing, so we catch it
here.

WHAT IT DOES AND DOES NOT FLAG
- A NEW leaf sub-package under a real root (e.g. adding cache/ under
  src/main/java/com/example/book/) is ALLOWED — new development legitimately
  creates new sub-packages, and coding writes them within its src/main/** glob.
- A WRONG base package (e.g. com/example/bookservice/... when the tree is
  com/example/book/...) is FLAGGED — the root itself is missing, which means the
  plan is addressing a package that does not exist.

The check therefore verifies, for each planned path, that the longest existing
ancestor directory reaches a meaningful package depth — not that the exact leaf
directory already exists.

EXTERNAL CLASSES (second check, same point in the run)
The plan's "External Classes" table lists classes the planned code needs from
outside this repo, and where each comes from. A row marked UNKNOWN or
NEW_DEPENDENCY halts here, before coding: coding cannot change a build file, so
finding this out there costs a full phase (OP018-6434, 2026-11-04). The table is
written by the planning model, so this check is only as good as that table. A
plan with no such section is not halted.

PLACEHOLDER PATHS (third check, same point in the run)
A planned file such as webservice/<MPA-totals-SOAP-interface>.java is not a file
coding can create. It means the planner lacked a name it needed, so the run halts
here, whatever the External Classes table says (OP018-6434, 2026-11-04).

INTERFACE (consumed by state_machine.py)
    check_plan(repo_root, plan_file, target_module, log=print) -> PlanCheckResult
        .missing_dirs : list[MissingDir]  (truthy => halt)
        .checked      : int               (paths whose root resolved)
    halt_message(result, plan_file) -> str
    check_external(plan_file, log=print) -> list[ExternalClass]  (truthy => halt)
    external_declaration(flagged, repo_root, target_module) -> BlockedDeclaration
    check_placeholders(plan_file, log=print) -> list[str]  (truthy => halt)
    placeholder_message(paths, plan_file) -> str
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


# How many path segments beneath src/main/java (or src/test/java) must exist for
# a planned file's package to count as "rooted". com/example/book => 3 segments.
# We require the plan's path to share an existing ancestor at least this deep;
# a brand-new leaf below that depth is allowed.
_MIN_ROOTED_SEGMENTS = 2

# Source roots we understand. A planned path is checked relative to whichever of
# these it contains.
_SOURCE_ROOTS = ("src/main/java", "src/test/java", "src/main/kotlin",
                 "src/test/kotlin", "src/main/resources", "src/test/resources")


@dataclass
class MissingDir:
    """One planned path whose package root does not exist in the repo."""
    planned_path: str          # path exactly as the plan wrote it (repo-relative)
    expected_root: str         # the package root we looked for and did not find
    deepest_existing: str      # the deepest ancestor that DOES exist (for the message)


@dataclass
class PlanCheckResult:
    missing_dirs: List[MissingDir] = field(default_factory=list)
    checked: int = 0
    # Paths we could not interpret (no recognised source root) — reported, not fatal.
    skipped: List[str] = field(default_factory=list)


def _normalise(p: str) -> str:
    return p.strip().strip("`").replace("\\", "/").lstrip("/")


def _module_prefix(target_module: Optional[str]) -> str:
    """If a target module is configured and the planned path is module-relative,
    the on-disk path is <module>/<planned>. Blank => no prefix (single-module or
    the plan already includes the module)."""
    if not target_module:
        return ""
    return target_module.strip().strip("/").replace("\\", "/")


def _extract_impacted_paths(plan_text: str) -> List[str]:
    """Pull file paths out of the '## Impacted Files' markdown table.

    The table looks like:
        | ID | Path | Role |
        |----|------|------|
        | F1 | src/main/java/com/example/book/service/BookService.java | ... |

    We take column 2 of each data row in that section. If no Impacted Files
    section is present we fall back to scanning the whole document for
    src/{main,test}/... paths, so a differently-shaped plan still gets checked.
    """
    paths: List[str] = []

    # Isolate the Impacted Files section (up to the next '## ' heading).
    section = None
    m = re.search(r"^#{1,6}\s*Impacted Files.*?$", plan_text,
                  flags=re.IGNORECASE | re.MULTILINE)
    if m:
        start = m.end()
        nxt = re.search(r"^#{1,6}\s", plan_text[start:], flags=re.MULTILINE)
        section = plan_text[start:start + nxt.start()] if nxt else plan_text[start:]

    scan = section if section is not None else plan_text

    if section is not None:
        for line in section.splitlines():
            if not line.lstrip().startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            # header/separator rows
            if len(cells) < 2:
                continue
            if set(cells[1].replace(" ", "")) <= set("-:"):
                continue
            if cells[1].lower() == "path":
                continue
            cand = _normalise(cells[1])
            if any(root in cand for root in _SOURCE_ROOTS):
                paths.append(cand)

    # Fallback / supplement: catch any source paths mentioned anywhere.
    for mm in re.finditer(r"(src/(?:main|test)/(?:java|kotlin|resources)/[^\s`|)]+)",
                          scan):
        cand = _normalise(mm.group(1))
        if cand not in paths:
            paths.append(cand)

    return paths


def _split_after_source_root(rel_path: str):
    """Return (source_root, package_segments, filename) or None if no root found."""
    for root in _SOURCE_ROOTS:
        idx = rel_path.find(root)
        if idx == -1:
            continue
        after = rel_path[idx + len(root):].strip("/")
        parts = after.split("/")
        if len(parts) < 1:
            return root, [], ""
        *pkg, fname = parts
        return root, pkg, fname
    return None


_SRC_EXTS = (".java", ".kt")


def _discover_base_packages(source_root_dir: Path) -> set:
    """Return the set of real base packages under a source root, as '/'-joined
    segment strings relative to the source root.

    The base package is the deepest directory chain that is unambiguous: we walk
    down while a directory has exactly one sub-directory and no source files. The
    moment a directory contains source files OR branches into multiple
    sub-packages, that directory is the base — everything below it is application
    structure where new sub-packages are legitimate.

    Example: com/ -> example/ -> book/ {config, controller, service, ...} and
    BookServiceApplication.java  =>  base package "com/example/book".
    """
    if not source_root_dir.is_dir():
        return set()

    def has_source(d: Path) -> bool:
        return any(f.suffix in _SRC_EXTS for f in d.iterdir() if f.is_file())

    def subdirs(d: Path):
        return [c for c in d.iterdir() if c.is_dir()]

    cur = source_root_dir
    segs: list = []
    while True:
        subs = subdirs(cur)
        if has_source(cur) or len(subs) != 1:
            break
        cur = subs[0]
        segs.append(cur.name)
    return {"/".join(segs)} if segs else set()


def _under_a_real_base(pkg_segments: list, real_bases: set) -> bool:
    """True if the planned package sits under (or exactly at) a discovered base."""
    if not real_bases:
        # No base discovered (empty/new source root) — cannot contradict; allow.
        return True
    planned = "/".join(pkg_segments)
    for base in real_bases:
        if planned == base or planned.startswith(base + "/"):
            return True
    return False


def check_plan(repo_root, plan_file, target_module, log=print) -> PlanCheckResult:
    repo_root = Path(repo_root)
    plan_file = Path(plan_file)
    result = PlanCheckResult()

    try:
        plan_text = plan_file.read_text(encoding="utf-8")
    except Exception as e:
        log(f"  [harness] plan path check: cannot read {plan_file} ({e})")
        return result

    prefix = _module_prefix(target_module)
    planned = _extract_impacted_paths(plan_text)

    for raw in planned:
        parsed = _split_after_source_root(raw)
        if parsed is None:
            result.skipped.append(raw)
            continue
        source_root, pkg, _fname = parsed

        # On-disk base: repo_root [+ module prefix] + source_root
        base = repo_root
        if prefix and not raw.startswith(prefix + "/"):
            base = base / prefix
        base = base / source_root

        # Discover the REAL base package(s) under this source root by walking down
        # until a directory contains a source file OR branches — that is the point
        # below which sub-packages are legitimately new. For com/example/book that
        # yields "com/example/book"; a planned com/example/bookservice/... then
        # fails to match any real base and is flagged.
        real_bases = _discover_base_packages(base)

        if _under_a_real_base(pkg, real_bases):
            result.checked += 1
        else:
            # Report the deepest ancestor that DOES exist, for the message.
            existing = base
            for seg in pkg:
                nxt = existing / seg
                if nxt.is_dir():
                    existing = nxt
                else:
                    break
            expected = "/".join(
                ([prefix] if prefix and not raw.startswith(prefix + "/") else [])
                + [source_root] + (sorted(real_bases)[0].split("/") if real_bases else [])
            )
            deepest = str(existing.relative_to(repo_root)).replace("\\", "/") \
                if existing.exists() else "(source root missing)"
            result.missing_dirs.append(MissingDir(
                planned_path=raw,
                expected_root=expected,
                deepest_existing=deepest,
            ))

    if result.checked:
        log(f"  [harness] plan paths: {result.checked} checked, all resolve")
    if result.skipped:
        log(f"  [harness] plan path check: {len(result.skipped)} path(s) "
            f"not interpretable, skipped")

    return result


# =============================================================================
# External classes
# =============================================================================
# COPY_FROM_REFERENCE: the classes exist in a reference repo (OP018-6434: MPA's
# committed, generated Totals 2.0 SOAP classes) and a person copies them in
# unchanged. No phase copies code out of a reference repo: only context reads
# them, and what is copied is a decision someone signs off (2026-11-13).
_HALT_SOURCES = ("UNKNOWN", "NEW_DEPENDENCY", "COPY_FROM_REFERENCE")
_EVIDENCE_RX = re.compile(r"^`?(?P<repo>[A-Za-z0-9_./-]+?)(?:@(?P<branch>[A-Za-z0-9_./-]+))?:(?P<path>[^\s`;,]+)`?")


@dataclass
class ExternalClass:
    """One row of the plan's External Classes table that needs a human."""
    name: str
    source: str
    evidence: str = ""


def _external_section(plan_text: str) -> Optional[str]:
    m = re.search(r"^#{1,6}\s*External Classes.*?$", plan_text,
                  flags=re.IGNORECASE | re.MULTILINE)
    if not m:
        return None
    start = m.end()
    nxt = re.search(r"^#{1,6}\s", plan_text[start:], flags=re.MULTILINE)
    return plan_text[start:start + nxt.start()] if nxt else plan_text[start:]


def check_external(plan_file, log=print) -> List[ExternalClass]:
    """Rows of the External Classes table marked UNKNOWN, NEW_DEPENDENCY or
    COPY_FROM_REFERENCE."""
    try:
        plan_text = Path(plan_file).read_text(encoding="utf-8")
    except Exception as e:
        log(f"  [harness] external class check: cannot read {plan_file} ({e})")
        return []
    section = _external_section(plan_text)
    if section is None:
        log("  [harness] plan has no External Classes section; check skipped")
        return []
    flagged: List[ExternalClass] = []
    rows = 0
    for line in section.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2 or set(cells[1].replace(" ", "")) <= set("-:"):
            continue
        if cells[1].lower() == "source":
            continue
        rows += 1
        source = re.sub(r"[\s-]+", "_", cells[1].strip(" *`").upper())
        for wanted in _HALT_SOURCES:
            if source.startswith(wanted):
                flagged.append(ExternalClass(
                    name=cells[0].strip(" `"), source=wanted,
                    evidence=cells[2].strip(" `") if len(cells) > 2 else ""))
                break
    log(f"  [harness] external classes: {rows} listed, {len(flagged)} need a person")
    return flagged


@dataclass
class CopyItem:
    """One folder or file to copy, unchanged, from a reference repo."""
    repo: str
    branch: str
    source: str
    dest: str


def _copy_dest(src: str, prefix: str) -> str:
    """Where a copied path lands in this repo: the package path is kept, under
    this repo's source root (src/main/java or src/main/resources)."""
    src = src.strip("/")
    base = f"{prefix}/" if prefix else ""
    for root in ("java", "resources"):
        marker = f"/{root}/"
        if marker in f"/{src}":
            rest = f"/{src}".rsplit(marker, 1)[1]
            return f"{base}src/main/{root}/{rest}"
    return f"{base}{src.rsplit('/', 1)[-1]}"


def copy_items(flagged: List[ExternalClass], target_module, reference_repos=None) -> List[CopyItem]:
    """COPY_FROM_REFERENCE rows as copy instructions. The branch comes from the
    evidence (repo@branch:path), else from the reference repo the run read."""
    branches = {}
    for r in reference_repos or []:
        name = str(r.get("repo", ""))
        branches[name.lower()] = r.get("branch", "")
        branches[name.lower().rsplit("/", 1)[-1]] = r.get("branch", "")
    prefix = _module_prefix(target_module)
    out: List[CopyItem] = []
    for c in flagged:
        if c.source != "COPY_FROM_REFERENCE":
            continue
        m = _EVIDENCE_RX.match(c.evidence.strip())
        if not m:
            continue
        repo, path = m.group("repo"), m.group("path")
        branch = m.group("branch") or branches.get(repo.lower()) \
            or branches.get(repo.lower().rsplit("/", 1)[-1], "")
        item = CopyItem(repo, branch or "?", path, _copy_dest(path, prefix))
        if all((i.repo, i.source) != (item.repo, item.source) for i in out):
            out.append(item)
    return out


def _build_file(repo_root: Path, prefix: str) -> str:
    for folder in ([prefix] if prefix else []) + [""]:
        for name in ("pom.xml", "package.json", "build.gradle", "build.gradle.kts"):
            if (repo_root / folder / name).is_file():
                return f"{folder}/{name}" if folder else name
    return ""


def external_declaration(flagged: List[ExternalClass], repo_root, target_module,
                         reference_repos=None):
    """The manual-change declaration for flagged classes, so the developer gets
    the same message and the same steps as a coding-phase halt. Classes found
    in a reference repo come with the exact folders to copy and where to."""
    from blocked import BlockedDeclaration
    repo_root = Path(repo_root)
    prefix = _module_prefix(target_module)
    copies = copy_items(flagged, target_module, reference_repos)
    others = [c for c in flagged if c.source != "COPY_FROM_REFERENCE"]
    unparsed = [c for c in flagged if c.source == "COPY_FROM_REFERENCE"
                and not _EVIDENCE_RX.match(c.evidence.strip())]
    others += unparsed

    files, why, needs = [], [], []
    if copies:
        files += sorted({i.dest for i in copies})
        needs.append("classes that exist in a reference repo, copied in unchanged")
        why.append("only the context phase can read reference repos, and no phase "
                   "copies code out of them: a person checks and commits the copy")
    if others:
        bf = _build_file(repo_root, prefix)
        if bf:
            files.append(bf)
        needs.append("classes with no confirmed source in this repo")
        why.append("no phase may change a build file, and coding cannot work "
                   "without these classes")

    suggested = ""
    if copies:
        lines = ["Copy these, UNCHANGED, from a local clone of each repo on the "
                 "branch shown, into this repo:"]
        for i in copies:
            lines.append(f"  - {i.repo} (branch {i.branch}): {i.source}")
            lines.append(f"      -> {i.dest}")
        lines.append("Commands (Git Bash, from this repo's root; set REF to your clone):")
        last_repo = None
        for i in copies:
            parent = i.dest.rsplit("/", 1)[0] if "/" in i.dest else "."
            short = i.repo.rsplit("/", 1)[-1]
            if short != last_repo:
                lines.append(f"  REF=<path to your {short} clone>")
                last_repo = short
            lines.append(f"  mkdir -p {parent} && cp -r \"$REF/{i.source}\" {parent}/")
        lines.append("Keep the package names. Then commit, push and resume as below.")
        suggested = "\n    ".join(lines)

    evidence = [f"{c.name}: {c.evidence}" for c in flagged if c.evidence]
    return BlockedDeclaration(
        declared=True,
        needs="; ".join(needs),
        missing=", ".join(f"{c.name} ({c.source})" for c in flagged),
        searched="; ".join(evidence),
        file=", ".join(files),
        why="; ".join(why),
        suggested=suggested,
    )


# =============================================================================
# Placeholder paths
# =============================================================================
_PLACEHOLDER = re.compile(r"<[^<>/]+>")


def check_placeholders(plan_file, log=print) -> List[str]:
    """Impacted Files paths that still contain a <placeholder>."""
    from execution_record import _approved_paths_from_plan
    try:
        plan_text = Path(plan_file).read_text(encoding="utf-8")
    except Exception as e:
        log(f"  [harness] placeholder check: cannot read {plan_file} ({e})")
        return []
    return sorted(p for p in _approved_paths_from_plan(plan_text)
                  if _PLACEHOLDER.search(p))


def placeholder_message(paths: List[str], plan_file) -> str:
    lines = ["  ================ PLAN PLACEHOLDERS: HALTED ================",
             "  The plan uses placeholder names instead of real file names, so the",
             "  coding phase cannot follow it. This usually means the story or its",
             "  attachments leave out something the plan needs, such as a contract,",
             "  an operation or a class name.",
             "",
             "  Placeholder path(s):"]
    lines += [f"    - {p}" for p in paths]
    lines += ["",
              "  To continue:",
              "    1. add the missing detail to the story on the base branch, and",
              "       attach any contract file",
              "    2. start a fresh run (resume unticked). A resume at this phase",
              "       keeps the original story, so it would not see this.",
              "",
              f"  Plan file : {plan_file}",
              "  ==========================================================="]
    return "\n".join(lines)


def halt_message(result: PlanCheckResult, plan_file) -> str:
    lines = []
    lines.append("  ================ PLAN PATH CHECK: HALTED ================")
    lines.append("  The implementation plan references package paths that do NOT")
    lines.append("  exist in this repository. The coding phase cannot create a")
    lines.append("  package that isn't there, so the plan is unexecutable as written.")
    lines.append("")
    lines.append("  This usually means the plan GUESSED a package name instead of")
    lines.append("  reading the source tree. Fix the paths in the plan to match the")
    lines.append("  real package, then resume.")
    lines.append("")
    lines.append("  Offending path(s):")
    for md in result.missing_dirs:
        lines.append(f"    - {md.planned_path}")
        lines.append(f"        expected package root : {md.expected_root}")
        lines.append(f"        deepest existing dir  : {md.deepest_existing or '(source root only)'}")
    lines.append("")
    lines.append(f"  Plan file : {plan_file}")
    lines.append("  =========================================================")
    return "\n".join(lines)
