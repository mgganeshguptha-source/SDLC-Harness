"""
handoff.py — contracts handed from one story to the next across repositories.

WHY
A requirement that spans repos is split into one story per repo (the harness
works on one repo per run). The downstream story needs to know the contract the
upstream story produced — the API response, the store shape, the event payload
— or it guesses. A guess shows up as defensive code that accepts two shapes
(observed: dx_mycoverage-ui read `body ?? data` because the store shape from
dx_mpa_responsive was not known). This module makes the hand-off explicit.

THE STORY LINE (mandatory in every story)
    **Requires a new or changed contract from:** None
    **Requires a new or changed contract from:** dx_mpa_responsive
  None            = this story does not need a changed contract (repos may still
                    be connected; an unchanged contract is read from the code).
  <upstream repo> = this story needs a new or changed contract (API, store,
                    event, schema, config) from that repo. Its contract file must
                    be attached in stories/<feature_id>/ (or pasted into the story
                    under '## Upstream contract: <repo>').

DOWNSTREAM GATE (dependency_gate, deterministic, before any model call)
  - line missing or not filled in  -> halt  dependencies_missing
  - repo named, no contract file   -> halt  contract_missing
  - contract file whose engine header names a different repo -> halt
    contract_missing ("wrong contract attached")
  - contract file without an engine header -> warning only (hand-written)
The story id is never typed: it is read from the contract file's header.

UPSTREAM GENERATION (contract_handoff)
After coding, the files the run changed are matched against built-in patterns
per stack (controllers / DTOs / OpenAPI for backend; NgRx actions / reducer /
selectors / effects / state for Angular) plus the repo's own contract_files.
On a match, the documentation phase writes
    .harness/contract/contract-<repo>.md      (the contract)
    .harness/contract/sample-<repo>.json      (mocked samples, when data crosses)
The engine then stamps the header (repo, feature id, run id, commit, base,
boundary files), scans both files for PII (values masked), and — if clean —
the workflow publishes them to the harness-metrics branch only: the run's audit
folder and contracts/<feature_id>/. They never enter the code PR. A developer
copies the file into the downstream story's attachments folder.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

LINE_LABEL = "Requires a new or changed contract from"
# **Requires a new or changed contract from:** None   (bold optional, colon
# inside or outside the bold, leading list marker allowed)
_LINE_RX = re.compile(
    r"^[ \t>*-]*\**\s*requires a new or changed contract from\s*:?\s*\**\s*:?\s*(.*)$",
    re.IGNORECASE | re.MULTILINE)
_SECTION_RX = r"^#{{1,6}}\s*upstream contract\s*:\s*`?{repo}`?\s*$"

HEADER_START = "<!-- sdlc-harness-contract v1"
HEADER_END = "-->"

# Files that, when changed, mean the change probably alters a contract other
# repos rely on. Matched against repo-relative paths, case-sensitive like git.
STACK_BOUNDARY_PATTERNS = {
    "backend": [
        "**/*Controller.java", "**/*Resource.java", "**/*Endpoint.java",
        "**/*Request.java", "**/*Response.java", "**/*Dto.java", "**/*DTO.java",
        "**/openapi*.yaml", "**/openapi*.yml", "**/openapi*.json",
        "**/*-api.yaml", "**/*-api.yml",
        "**/*Event.java", "**/*Message.java",
    ],
    "angular-frontend": [
        "**/*.actions.ts", "**/*.reducer.ts", "**/*.selectors.ts", "**/*.effects.ts",
        "**/*.state.ts", "**/*.model.ts", "**/*bridge*.ts", "**/*Bridge*.ts",
    ],
}
STACK_BOUNDARY_PATTERNS["kotlin"] = STACK_BOUNDARY_PATTERNS["backend"] + [
    "**/*Controller.kt", "**/*Request.kt", "**/*Response.kt", "**/*Dto.kt"]
for _s in ("frontend", "mobile-frontend", "ionic"):
    STACK_BOUNDARY_PATTERNS[_s] = STACK_BOUNDARY_PATTERNS["angular-frontend"]


# =============================================================================
# Story line
# =============================================================================
@dataclass
class Requirement:
    status: str              # missing | unfilled | none | repos
    repos: list = field(default_factory=list)
    raw: str = ""


def parse_requirement(story: str) -> Requirement:
    m = _LINE_RX.search(story or "")
    if not m:
        return Requirement("missing")
    raw = m.group(1).strip()
    # Drop an inline HTML comment and markdown emphasis/backticks.
    val = re.sub(r"<!--.*?-->", "", raw).strip().strip("*").strip()
    val = val.replace("`", "").strip()
    if not val or "<" in val or "|" in val:
        return Requirement("unfilled", raw=raw)
    if val.lower().rstrip(".") in ("none", "n/a", "na"):
        return Requirement("none", raw=raw)
    repos = [r.strip().strip(".").strip() for r in re.split(r"[,;]| and ", val) if r.strip()]
    # A repo name is one token; prose means the line was not filled in properly.
    if not repos or any(re.search(r"\s", r) for r in repos):
        return Requirement("unfilled", raw=raw)
    return Requirement("repos", repos=repos, raw=raw)


# =============================================================================
# Contract header
# =============================================================================
def parse_header(text: str) -> dict | None:
    t = (text or "").lstrip("﻿")
    i = t.find(HEADER_START)
    if i < 0:
        return None
    j = t.find(HEADER_END, i + len(HEADER_START))
    if j < 0:
        return None
    body = t[i + len(HEADER_START):j]
    out: dict = {}
    files: list = []
    for line in body.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("- "):
            files.append(s[2:].strip())
            continue
        k, _, v = s.partition(":")
        out[k.strip()] = v.strip()
    if files:
        out["boundary_files"] = files
    return out


def make_header(meta: dict) -> str:
    lines = [HEADER_START]
    for k in ("repo", "feature_id", "run_id", "commit", "base", "generated_at"):
        lines.append(f"{k}: {meta.get(k) or ''}")
    lines.append("boundary_files:")
    lines += [f"  - {f}" for f in meta.get("boundary_files") or []]
    lines.append(HEADER_END)
    return "\n".join(lines) + "\n"


def strip_header(text: str) -> str:
    t = text or ""
    i = t.find(HEADER_START)
    if i < 0:
        return t
    j = t.find(HEADER_END, i)
    return (t[:i] + t[j + len(HEADER_END):]).lstrip("\n") if j >= 0 else t


# =============================================================================
# Downstream gate
# =============================================================================
@dataclass
class DepResult:
    ok: bool = True
    gate: str | None = None          # halt gate id when not ok
    problems: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    requirement: Requirement | None = None
    contracts: list = field(default_factory=list)  # [{repo, file, feature_id, run_id, commit}]

    def summary(self) -> dict:
        r = self.requirement
        return {"line": (r.status if r else None), "repos": (r.repos if r else []),
                "contracts": self.contracts, "ok": self.ok}


def check_dependencies(repo_root: Path, cfg, feature_id: str, story: str) -> DepResult:
    """Deterministic. Never raises."""
    import halt_gates as HG
    from attachments import discover, resolve_dir
    res = DepResult()
    try:
        req = parse_requirement(story)
        res.requirement = req
        if req.status in ("missing", "unfilled"):
            res.ok, res.gate = False, HG.DEPENDENCIES_MISSING
            res.problems.append(
                ("the story has no" if req.status == "missing" else "the story has an unfilled")
                + f" '**{LINE_LABEL}:**' line. Add it with None, or with the upstream "
                  f"repo name when this story needs that repo's new or changed contract.")
            return res
        if req.status == "none":
            return res

        folder = resolve_dir(repo_root, cfg, feature_id)
        files = discover(folder)
        for repo in req.repos:
            want = f"contract-{repo}".lower()
            found = [p for p in files
                     if p.suffix.lower() == ".md" and p.name.lower().startswith(want)]
            section = re.search(_SECTION_RX.format(repo=re.escape(repo)), story or "",
                                re.IGNORECASE | re.MULTILINE)
            if not found and not section:
                res.ok, res.gate = False, HG.CONTRACT_MISSING
                res.problems.append(
                    f"{repo}: no contract attached. Copy contract-{repo}.md from that repo's "
                    f"harness-metrics branch (contracts/<upstream feature_id>/) into "
                    f"{_rel(folder, repo_root)}/, or paste it under '## Upstream contract: {repo}'.")
                continue
            if not found:
                res.warnings.append(f"{repo}: contract pasted into the story (no engine "
                                    f"header) — make sure it is the current version")
                res.contracts.append({"repo": repo, "file": "story section"})
                continue
            for p in found:
                hdr = parse_header(p.read_text(encoding="utf-8", errors="replace"))
                rel = _rel(p, repo_root)
                if hdr is None:
                    res.warnings.append(f"{rel}: no engine header (written or edited by "
                                        f"hand) — make sure it is the current version")
                    res.contracts.append({"repo": repo, "file": rel})
                elif hdr.get("repo", "").lower() != repo.lower():
                    res.ok, res.gate = False, HG.CONTRACT_MISSING
                    res.problems.append(
                        f"{rel}: wrong contract attached — its header says it was produced "
                        f"by '{hdr.get('repo')}', but the story names '{repo}'")
                else:
                    res.contracts.append({
                        "repo": repo, "file": rel, "feature_id": hdr.get("feature_id"),
                        "run_id": hdr.get("run_id"), "commit": hdr.get("commit")})
    except Exception as e:  # fail CLOSED
        import halt_gates as HG2
        res.ok, res.gate = False, HG2.DEPENDENCIES_MISSING
        res.problems.append(f"the dependency check could not run ({type(e).__name__}: {e})")
    return res


def gate_message(res: DepResult, feature_id: str) -> str:
    import halt_gates as HG
    title = ("DEPENDENCIES LINE MISSING" if res.gate == HG.DEPENDENCIES_MISSING
             else "UPSTREAM CONTRACT MISSING")
    lines = ["", f"  ================ {title}: HALTED ================",
             "  Checked before any model call. Nothing was sent to a model."]
    for p in res.problems:
        lines.append(f"    - {p}")
    lines += [
        "  Template (in the story file on the story branch):",
        f"    **{LINE_LABEL}:** None",
        "      None            = this story does not need a changed contract",
        "      <upstream-repo> = upstream repo name (for example: dx_mpa_responsive) if",
        "                        this story needs a changed contract (API, store, event,",
        "                        schema, config). When a repo is named, attach its",
        "                        contract file in stories/<feature_id>/.",
        f"  Resume : feature_id={feature_id}, resume=true (the story and its",
        "           attachments are re-read from the story branch)",
        "  ==========================================================", ""]
    return "\n".join(lines)


# =============================================================================
# Upstream generation
# =============================================================================
def service_repo_name(repo_root: Path) -> str:
    full = os.environ.get("GITHUB_REPOSITORY", "")
    return full.split("/")[-1] if full else Path(repo_root).resolve().name


def boundary_files(changed: list, cfg) -> list:
    stacks = [str(s).strip().lower() for s in (getattr(cfg, "repo_stacks", None) or [])] or ["backend"]
    if "*" in stacks:
        stacks = list(STACK_BOUNDARY_PATTERNS)
    pats: list = []
    for st in stacks:
        pats += [p for p in STACK_BOUNDARY_PATTERNS.get(st, []) if p not in pats]
    pats += [p for p in (getattr(cfg, "contract_files", None) or []) if p not in pats]

    def match(path: str, pat: str) -> bool:
        if fnmatch.fnmatchcase(path, pat):
            return True
        # "**/x" also matches "x" at the repo root
        return pat.startswith("**/") and fnmatch.fnmatchcase(path, pat[3:])

    out = []
    for f in changed or []:
        rel = str(f).replace("\\", "/").lstrip("./")
        if any(match(rel, p) for p in pats) and rel not in out:
            out.append(rel)
    return out


def contract_dir(repo_root: Path) -> Path:
    return Path(repo_root) / ".harness" / "contract"


def contract_paths(repo_root: Path) -> tuple[Path, Path]:
    name = service_repo_name(repo_root)
    d = contract_dir(repo_root)
    return d / f"contract-{name}.md", d / f"sample-{name}.json"


def prompt_block(repo_root: Path, boundary: list) -> str:
    """Instructions appended to the documentation (or contract-only) prompt."""
    if not boundary:
        return ""
    md, js = contract_paths(repo_root)
    files = "\n".join(f"      - {f}" for f in boundary)
    return (
        "\n\nCONTRACT HAND-OFF (required). This change touches files that form a "
        "contract other repositories rely on:\n"
        f"{files}\n"
        f"Write the contract for the NEXT story (in another repo) to this EXACT path "
        f"(the folder ALREADY EXISTS, shell is disallowed): {md}\n"
        "Use these sections, in this order:\n"
        "  # Contract: <one line naming what is provided>\n"
        "  ## Boundary type      — one of: REST API | Shared store (NgRx) | Event/message | "
        "Shared library/DTO | DB schema | Config/flag\n"
        "  ## Contract           — what a consumer can rely on. REST API: method, path, "
        "base URL per environment if known, request, response schema (every field, type, "
        "nullability), status codes. Store: state shape, selectors, actions (load / "
        "success / failure), what failure puts in state, bridge streams. Event: topic, "
        "key, payload schema. Library: package, version, changed types. Schema: tables, "
        "columns, nullability. Config: key, values, default.\n"
        "  ## Samples            — a success sample and at least one failure sample\n"
        "  ## Negative cases     — what each error / empty case looks like to a consumer\n"
        "  ## Rules for the consumer — e.g. 'read only through <selector>; handle exactly "
        "this shape; do not accept alternative shapes'\n"
        "  ## What changed       — compared with the contract before this story\n"
        "Describe the contract AS IMPLEMENTED in the code of this change — read the "
        "changed files, do not guess. Mark anything you could not confirm as "
        "'UNCONFIRMED: <what>'.\n"
        f"When data crosses the boundary, also write the samples as JSON to {js} "
        "(an object with keys 'success' and 'failure').\n"
        "MOCKED VALUES ONLY in both files: invented names and ids such as 'Jane "
        "Sample' / 'MEM-0001', no real member data, no SSN, date of birth, phone "
        "number or email address. The harness scans both files and refuses to "
        "publish them on a match.\n"
        "Do not write a header block; the harness adds it."
    )


def finalize(repo_root: Path, cfg, run, boundary: list, log=print) -> dict:
    """Stamp, scan and stage the contract the phase wrote. Never raises.
    Returns {status: published|missing|blocked_pii|none, ...} and stores it on
    run.contract_output."""
    import attachments as AT
    md, js = contract_paths(repo_root)
    info = {"status": "none", "file": None, "sample": None, "boundary_files": boundary}
    try:
        if not boundary:
            log("  [contract] no contract-boundary files changed — no contract to hand off")
            return _store(run, info)
        if not md.is_file():
            info["status"] = "missing"
            log(f"  [contract] ! expected {md.name} was not written — no contract published")
            print(f"::warning title=Contract not written::This change touches contract "
                  f"files ({', '.join(boundary[:3])}) but no {md.name} was produced.")
            return _store(run, info)

        body = strip_header(md.read_text(encoding="utf-8", errors="replace"))
        sample = js.read_text(encoding="utf-8", errors="replace") if js.is_file() else ""
        kinds = [k for k in (getattr(cfg, "attachment_pii_patterns", None)
                             or ["ssn", "dob", "phone", "email"])]
        allow = list(getattr(cfg, "attachment_pii_allow", None) or [])
        findings = AT.scan_text(body, md.name, kinds, allow)
        if sample:
            findings += AT.scan_text(sample, js.name, kinds, allow)
        if findings:
            info["status"] = "blocked_pii"
            info["findings"] = [str(f) for f in findings[:20]]
            for p in (md, js):
                try:
                    p.unlink()
                except Exception:
                    pass
            log("  [contract] ! possible PII in the generated contract — NOT published "
                "(values masked):")
            for f in findings[:20]:
                log(f"      - {f}")
            print("::warning title=Contract not published::The generated contract held "
                  "possible PII and was discarded. See the run log (values masked).")
            return _store(run, info)

        meta = {
            "repo": service_repo_name(repo_root),
            "feature_id": getattr(run, "feature_id", ""),
            "run_id": os.environ.get("GITHUB_RUN_ID") or "local",
            "commit": _git(repo_root, "rev-parse", "HEAD"),
            "base": os.environ.get("HARNESS_BASE_BRANCH", ""),
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "boundary_files": boundary,
        }
        md.write_text(make_header(meta) + "\n" + body.lstrip(), encoding="utf-8")
        if sample:
            try:
                json.loads(sample)
            except Exception:
                log(f"  [contract] note: {js.name} is not valid JSON — kept as written")
        info.update(status="published", file=md.name, sample=js.name if sample else None,
                    repo=meta["repo"], commit=meta["commit"])
        log(f"  [contract] {md.name}{' + ' + js.name if sample else ''} ready for "
            f"harness-metrics (audit folder + contracts/{meta['feature_id']}/)")
        return _store(run, info)
    except Exception as e:
        info["status"] = "missing"
        info["error"] = f"{type(e).__name__}: {e}"
        log(f"  [contract] could not finalize the contract ({info['error']})")
        return _store(run, info)


def _store(run, info: dict) -> dict:
    try:
        run.contract_output = info
    except Exception:
        pass
    return info


def _git(repo_root: Path, *args) -> str:
    try:
        p = subprocess.run(["git", "-C", str(repo_root), *args], capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        return p.stdout.strip() if p.returncode == 0 else ""
    except Exception:
        return ""


def changed_since_base(repo_root: Path, base_ref: str) -> list:
    """Files changed on this branch since it left base_ref (contract-only mode)."""
    mb = _git(repo_root, "merge-base", base_ref, "HEAD")
    if not mb:
        return []
    out = _git(repo_root, "diff", "--name-only", "--diff-filter=ACMRT", mb, "HEAD")
    return [l for l in out.splitlines() if l.strip()]


def _rel(p: Path, root: Path) -> str:
    try:
        return str(Path(p).resolve().relative_to(Path(root).resolve())).replace("\\", "/")
    except Exception:
        return str(p)
