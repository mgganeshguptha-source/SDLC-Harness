"""
handoff.py — contracts handed from one story to the next across repositories.

WHY
A requirement that spans repos is split into one story per repo (the harness
works on one repo per run). The downstream story needs the contract the
upstream story produced — the API, the store shape — or it guesses. A guess
shows up as defensive code that accepts two shapes (observed: dx_mycoverage-ui
read `body ?? data` because the store shape from dx_mpa_responsive was not
known). This module makes the hand-off explicit.

THE CONTRACT IS THE UPSTREAM REPO'S OWN FILES
Not every story goes through the harness, so the contract is never a file only
the harness can write. Rule of thumb for the developer:
  - upstream story was a BACKEND story  -> attach that repo's OpenAPI file
  - upstream story was an ANGULAR story -> attach that repo's NgRx store folder
                                           (a folder named store/ at any depth, e.g.
                                           src/app/store/ or mpa-web/src/app/mpa/store/,
                                           with its subfolders, plus the model files
                                           it imports)
Files go in the downstream story's attachments folder (stories/<feature_id>/);
a subfolder is optional.

THE STORY LINE (mandatory in every story)
    **Requires a new or changed contract from:** None
    **Requires a new or changed contract from:** dx_mpa_responsive
  None            = this story does not need a changed contract
  <upstream repo> = this story needs a changed contract (API, store, event,
                    schema, config) from that repo; attach its contract files

DOWNSTREAM GATE (dependency_gate, deterministic, before any model call)
  - line missing or not filled in            -> halt dependencies_missing
  - repo named, no contract file attached    -> halt contract_missing
    (contract files: .yml .yaml .json .ts, contract-*.md, or a
     '## Upstream contract: <repo>' section in the story)
  - attached files cannot be tied to the named repo -> warning only
  - a harness summary (contract-*.md) whose header names another repo -> halt
The story id is never typed.

UPSTREAM PUBLISHING (contract_handoff)
When a harness run changes contract files (backend: controllers, DTOs, the
OpenAPI file; Angular: **/store/**), the engine copies the CURRENT
contract files of the repo (backend: the OpenAPI file(s); Angular: every store/
folder at any depth, its .ts files and the local files they import, such as the
model; plus contract_files from config) to
.harness/contract/files/, PII-scans them, writes manifest.json (repo, feature,
run, commit, base, files) and an optional model-written summary
(contract-<repo>.md). collect-audit and the workflow publish them to the
harness-metrics branch only — the run's audit folder and contracts/<feature_id>/.
Contract-only mode does the same copy for a change made outside the harness,
with no model call.

SAMPLES
Mocked samples are made in the DOWNSTREAM run, from an attached OpenAPI file,
by a script (openapi_samples) — no model, invented values only.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
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
# Any "<label>: <value>" line (list marker, heading, bold optional). Used to find
# the contract line when its wording differs from LINE_LABEL (OP018-6418 Story 2
# wrote "Requires new or changed contract from" and halted on the missing "a").
_LABEL_LINE_RX = re.compile(
    r"^[ \t>*#-]*\**\s*(?P<label>[A-Za-z][^:\n]{2,80}?)\s*\**\s*:\s*\**\s*(?P<val>.*)$",
    re.MULTILINE)

HEADER_START = "<!-- sdlc-harness-contract v1"
HEADER_END = "-->"

# Attached files that count as a contract.
CONTRACT_EXTS = (".yml", ".yaml", ".json", ".ts")
# Folders never searched for OpenAPI files. `stories` holds story inputs, such
# as an UPSTREAM repo's OpenAPI file attached for a hand-off; publishing those
# as this repo's contract passed petclinic-rest-api's spec on to PC-104 as if
# petclinic-angular-demo owned it (PC-103, 2026-11-02).
_SKIP_DIRS = {"node_modules", "target", "dist", "build", ".git", ".harness", ".angular",
              "audit", "metrics", "contracts", "coverage", ".idea", ".vscode", "stories"}
_OPENAPI_RX = re.compile(r"^(openapi|swagger)\s*:", re.MULTILINE)

# Changed files that mean the change probably alters a contract other repos
# rely on. Matched against repo-relative paths.
STACK_BOUNDARY_PATTERNS = {
    "backend": [
        "**/*Controller.java", "**/*Resource.java", "**/*Endpoint.java",
        "**/*Request.java", "**/*Response.java", "**/*Dto.java", "**/*DTO.java",
        "**/*Event.java", "**/*Message.java",
    ],
    "angular-frontend": [
        "**/store/**",
        "**/*.actions.ts", "**/*.reducer.ts", "**/*.selectors.ts", "**/*.effects.ts",
        "**/*.state.ts", "**/*bridge*.ts", "**/*Bridge*.ts",
    ],
}
STACK_BOUNDARY_PATTERNS["kotlin"] = STACK_BOUNDARY_PATTERNS["backend"] + [
    "**/*Controller.kt", "**/*Request.kt", "**/*Response.kt", "**/*Dto.kt"]
for _s in ("frontend", "mobile-frontend", "ionic"):
    STACK_BOUNDARY_PATTERNS[_s] = STACK_BOUNDARY_PATTERNS["angular-frontend"]
# API-shaped backend files: changing one without the OpenAPI file is flagged.
_API_FILE_PATTERNS = ["**/*Controller.java", "**/*Request.java", "**/*Response.java",
                      "**/*Dto.java", "**/*DTO.java", "**/*Controller.kt",
                      "**/*Request.kt", "**/*Response.kt", "**/*Dto.kt"]
# What is published as the contract, per stack (besides OpenAPI files).
# Any folder named store/, at any depth: dx_mpa_responsive keeps its NgRx store
# in mpa-web/src/app/mpa/store/, which a root-anchored "src/app/store/**" never
# found, so OP018-6418 Story 2 published no contract (2026-11-12).
_STORE_GLOB = "**/store/**"
STACK_CONTRACT_SOURCES = {
    "angular-frontend": [_STORE_GLOB],
    "frontend": [_STORE_GLOB],
    "mobile-frontend": [_STORE_GLOB],
    "ionic": [_STORE_GLOB],
}
_RELATIVE_IMPORT_RX = re.compile(r"""(?:import|export)[^'"]*?from\s*['"](\.{1,2}/[^'"]+)['"]""")


def _stacks(cfg) -> list:
    st = [str(s).strip().lower() for s in (getattr(cfg, "repo_stacks", None) or [])] or ["backend"]
    if "*" in st:
        st = list(STACK_BOUNDARY_PATTERNS)
    return st


def _match(path: str, pat: str) -> bool:
    path = path.replace("\\", "/").lstrip("./")
    if pat.endswith("/**"):
        pre = pat[:-3]
        if pre.startswith("**/"):
            seg = pre[3:]
            return path.startswith(seg + "/") or ("/" + seg + "/") in ("/" + path)
        return path.startswith(pre + "/")
    if fnmatch.fnmatchcase(path, pat):
        return True
    return pat.startswith("**/") and fnmatch.fnmatchcase(path, pat[3:])


# =============================================================================
# OpenAPI files
# =============================================================================
def is_openapi_text(text: str) -> bool:
    return bool(_OPENAPI_RX.search((text or "")[:4000]))


def detect_api_specs(repo_root: Path) -> list:
    """Repo-relative paths of OpenAPI / Swagger files (.yml/.yaml/.json whose
    content starts an OpenAPI document). Build output and tooling folders are
    skipped. Never raises."""
    out = []
    root = Path(repo_root)
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
            for fn in filenames:
                if not fn.lower().endswith((".yml", ".yaml", ".json")):
                    continue
                p = Path(dirpath) / fn
                if "/src/test/" in "/" + str(p.relative_to(root)).replace("\\", "/"):
                    continue
                try:
                    with open(p, "r", encoding="utf-8", errors="replace") as fh:
                        head = fh.read(4000)
                except Exception:
                    continue
                if is_openapi_text(head):
                    out.append(str(p.relative_to(root)).replace("\\", "/"))
    except Exception:
        pass
    return sorted(out)


def openapi_samples(spec: dict, max_depth: int = 10) -> dict:
    """Mocked request/response samples for every operation in an OpenAPI 3 (or
    Swagger 2) document, from the SCHEMA only. Values are invented from the
    field name and type; `example` values in the document are ignored on
    purpose (they may hold real data). Deterministic, no model."""
    comps = (spec.get("components") or {}).get("schemas") or spec.get("definitions") or {}

    def resolve(ref: str):
        name = ref.rsplit("/", 1)[-1]
        return name, comps.get(name) or {}

    def value(schema, name: str, depth: int, seen: tuple):
        if not isinstance(schema, dict) or depth > max_depth:
            return None
        if "$ref" in schema:
            rn, rs = resolve(schema["$ref"])
            if rn in seen:
                return {}
            return value(rs, name, depth + 1, seen + (rn,))
        if "allOf" in schema:
            merged: dict = {}
            for part in schema.get("allOf") or []:
                v = value(part, name, depth + 1, seen)
                if isinstance(v, dict):
                    merged.update(v)
            for k, ps in (schema.get("properties") or {}).items():
                merged[k] = value(ps, k, depth + 1, seen)
            return merged
        for key in ("oneOf", "anyOf"):
            if schema.get(key):
                return value(schema[key][0], name, depth + 1, seen)
        if schema.get("enum"):
            return schema["enum"][0]
        t = schema.get("type")
        if t is None and "properties" in schema:
            t = "object"
        fmt = str(schema.get("format") or "").lower()
        if t == "object":
            obj = {k: value(ps, k, depth + 1, seen)
                   for k, ps in (schema.get("properties") or {}).items()}
            ap = schema.get("additionalProperties")
            if isinstance(ap, dict) and not obj:
                obj = {"sample-key": value(ap, name, depth + 1, seen)}
            return obj
        if t == "array":
            return [value(schema.get("items") or {}, name, depth + 1, seen)]
        if t == "string":
            if fmt == "date":
                return "2026-01-01"
            if fmt == "date-time":
                return "2026-01-01T00:00:00Z"
            if fmt == "uuid":
                return "00000000-0000-0000-0000-000000000001"
            return f"sample-{name}" if name else "sample"
        if t == "integer":
            return 1
        if t in ("number", "float", "double"):
            return 1.0
        if t == "boolean":
            return False
        return None

    out: dict = {}
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if method.lower() not in ("get", "post", "put", "patch", "delete") or not isinstance(op, dict):
                continue
            entry: dict = {}
            rb = ((op.get("requestBody") or {}).get("content") or {}).get("application/json")
            if rb and rb.get("schema"):
                entry["request"] = value(rb["schema"], "request", 0, ())
            responses = {}
            for code, resp in (op.get("responses") or {}).items():
                if not isinstance(resp, dict):
                    continue
                sch = ((resp.get("content") or {}).get("application/json") or {}).get("schema") \
                    or resp.get("schema")
                responses[str(code)] = (value(sch, "response", 0, ()) if sch
                                        else f"(no body schema; {resp.get('description', '')})")
            entry["responses"] = responses
            out[f"{method.upper()} {path}"] = entry
    return out


# =============================================================================
# Story line
# =============================================================================
@dataclass
class Requirement:
    status: str              # missing | unfilled | none | repos
    repos: list = field(default_factory=list)
    raw: str = ""
    label: str = ""          # the label as written, when it differs from LINE_LABEL
    near_miss: str = ""      # status missing: a line that looks like the label


def _edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _label_words(label: str) -> list:
    return re.findall(r"[a-z]+", label.lower())


def _is_contract_label(label: str) -> bool:
    """A short label that ends with "from" and has a word close to "contract":
    "Requires new or changed contract from", "Requires a changed contarct from",
    "Contract from". The "from" ending keeps out prose and labels such as
    "Contract" or "Upstream contract". A misspelt "contract" also needs one of
    the template's other words, so "Contact details from" is not read as it."""
    words = _label_words(label)
    if not words or len(words) > 10 or words[-1] != "from":
        return False
    body = [w.rstrip("s") for w in words[:-1]]
    if "contract" in body:
        return True
    close = any(len(w) >= 6 and _edit_distance(w, "contract") <= 2 for w in body)
    return close and bool({"requires", "require", "new", "changed", "change"} & set(words))


def _near_miss(story: str) -> str:
    """A label line that looks like the contract line but is not accepted, so
    the halt message can point at it."""
    import difflib
    want = " ".join(_label_words(LINE_LABEL))
    best, best_score = "", 0.0
    for m in _LABEL_LINE_RX.finditer(story or ""):
        label = " ".join(_label_words(m.group("label")))
        if not label:
            continue
        score = difflib.SequenceMatcher(None, label, want).ratio()
        if any(len(w) >= 6 and _edit_distance(w.rstrip("s"), "contract") <= 2
               for w in label.split()):
            score = max(score, 0.6)
        if score >= 0.6 and score > best_score:
            best, best_score = m.group(0).strip(), score
    return best


def parse_requirement(story: str) -> Requirement:
    m = _LINE_RX.search(story or "")
    label = ""
    if not m:
        loose = next((x for x in _LABEL_LINE_RX.finditer(story or "")
                      if _is_contract_label(x.group("label"))), None)
        if loose is None:
            return Requirement("missing", near_miss=_near_miss(story))
        m, label = loose, loose.group("label").strip().strip("*").strip()
    raw = (m.group("val") if label else m.group(1)).strip()
    val = re.sub(r"<!--.*?-->", "", raw).strip().strip("*").strip()
    val = val.replace("`", "").strip()
    if not val or "<" in val or "|" in val:
        return Requirement("unfilled", raw=raw, label=label)
    if val.lower().rstrip(".") in ("none", "n/a", "na"):
        return Requirement("none", raw=raw, label=label)
    repos = [r.strip().strip(".").strip() for r in re.split(r"[,;]| and ", val) if r.strip()]
    if not repos or any(re.search(r"\s", r) for r in repos):
        return Requirement("unfilled", raw=raw, label=label)
    return Requirement("repos", repos=repos, raw=raw, label=label)


# =============================================================================
# Summary header (harness-written contract-<repo>.md only)
# =============================================================================
def parse_header(text: str) -> dict | None:
    t = (text or "").lstrip("﻿")
    i = t.find(HEADER_START)
    if i < 0:
        return None
    j = t.find(HEADER_END, i + len(HEADER_START))
    if j < 0:
        return None
    out: dict = {}
    files: list = []
    for line in t[i + len(HEADER_START):j].splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("- "):
            files.append(s[2:].strip())
            continue
        k, _, v = s.partition(":")
        out[k.strip()] = v.strip()
    if files:
        out["files"] = files
    return out


def make_header(meta: dict) -> str:
    lines = [HEADER_START]
    for k in ("repo", "feature_id", "run_id", "commit", "base", "generated_at"):
        lines.append(f"{k}: {meta.get(k) or ''}")
    lines.append("files:")
    lines += [f"  - {f}" for f in meta.get("files") or []]
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
    gate: str | None = None
    problems: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    requirement: Requirement | None = None
    contracts: list = field(default_factory=list)   # [{file, matched_repo}]

    def summary(self) -> dict:
        r = self.requirement
        return {"line": (r.status if r else None), "repos": (r.repos if r else []),
                "contracts": self.contracts, "warnings": len(self.warnings), "ok": self.ok}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _repo_token(repo: str) -> str:
    r = (repo or "").strip().lower().split("/")[-1]
    r = re.sub(r"^dx[_-]", "", r)
    return _norm(r)


def _is_contract_file(p: Path) -> bool:
    n = p.name.lower()
    return n.endswith(CONTRACT_EXTS) or (n.startswith("contract-") and n.endswith(".md"))


def check_dependencies(repo_root: Path, cfg, feature_id: str, story: str) -> DepResult:
    """Deterministic. Never raises (fails closed)."""
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
                + f" '**{LINE_LABEL}:**' line. Add it with None, or with the upstream repo "
                  f"name when this story needs that repo's new or changed contract.")
            if req.near_miss:
                res.problems.append(
                    f"did you mean this line? '{req.near_miss[:160]}' — change its label "
                    f"to '**{LINE_LABEL}:**'")
            return res
        if req.label:
            res.warnings.append(
                f"read '{req.label}' as the '{LINE_LABEL}' line; consider using the "
                f"exact wording in future stories")
        if req.status == "none":
            return res

        folder = resolve_dir(repo_root, cfg, feature_id)
        files = [p for p in discover(folder) if _is_contract_file(p)]
        sections = {r: bool(re.search(_SECTION_RX.format(repo=re.escape(r)), story or "",
                                      re.IGNORECASE | re.MULTILINE)) for r in req.repos}
        if not files and not any(sections.values()):
            res.ok, res.gate = False, HG.CONTRACT_MISSING
            res.problems.append(
                f"the story names {', '.join(req.repos)} but no contract file is attached in "
                f"{_rel(folder, repo_root)}/. Attach the upstream repo's OpenAPI file (backend "
                f"story) or its NgRx store folder and the model files it imports (Angular "
                f"story, e.g. src/app/store/) — from the upstream branch, or from "
                f"contracts/<upstream feature_id>/ on its harness-metrics branch.")
            return res

        # Tie files to repos: file path, an OpenAPI info.title, a manifest.json
        # or a harness summary header. Untied files are accepted with a warning.
        tied: dict = {r: [] for r in req.repos}
        for p in files:
            rel = _rel(p, repo_root)
            text = ""
            try:
                text = p.read_text(encoding="utf-8", errors="replace")[:6000]
            except Exception:
                pass
            hdr = parse_header(text) if p.name.lower().endswith(".md") else None
            if hdr is not None:
                hr = hdr.get("repo", "")
                match = [r for r in req.repos if r.lower() == hr.lower()]
                if not match:
                    res.ok, res.gate = False, HG.CONTRACT_MISSING
                    res.problems.append(f"{rel}: wrong contract attached — its header says it "
                                        f"was produced by '{hr}', but the story names "
                                        f"{', '.join(req.repos)}")
                    continue
            else:
                title = ""
                m = re.search(r"^\s*title\s*:\s*(.+)$", text, re.MULTILINE) if is_openapi_text(text) else None
                if m:
                    title = m.group(1).strip().strip("'\"")
                if p.name.lower() == "manifest.json":
                    try:
                        title = json.loads(p.read_text(encoding="utf-8")).get("repo", "")
                    except Exception:
                        pass
                hay = _norm(rel) + " " + _norm(title)
                match = [r for r in req.repos if _repo_token(r) and _repo_token(r) in hay]
            for r in match:
                tied[r].append(rel)
            res.contracts.append({"file": rel, "repo": match[0] if match else None})
        # One upstream repo named: every attached contract file is its contract.
        # Developers copy the upstream file as it is (e.g. openapi.yml), so its
        # name need not mention the repo. The name check only matters when a
        # story names several repos and the files must be told apart.
        if len(req.repos) == 1:
            r0 = req.repos[0]
            for c in res.contracts:
                if c.get("repo") is None and c.get("file") != "story section":
                    c["repo"] = r0
                    tied[r0].append(c["file"])
        for r in req.repos:
            if not tied[r] and not sections[r]:
                res.warnings.append(
                    f"none of the attached contract files could be tied to '{r}' by name — "
                    f"check that the attached files are that repo's OpenAPI file or store folder")
            if sections[r]:
                res.contracts.append({"file": "story section", "repo": r})
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
        "                        contract files in stories/<feature_id>/:",
        "                          backend upstream story -> its OpenAPI file",
        "                          Angular upstream story  -> its NgRx store folder",
        "                            (any store/ folder, e.g. src/app/store/) and the",
        "                            model files it imports",
        f"  Resume : feature_id={feature_id}, resume=true (the story and its",
        "           attachments are re-read from the story branch)",
        "  ==========================================================", ""]
    return "\n".join(lines)


# =============================================================================
# Upstream publishing
# =============================================================================
def service_repo_name(repo_root: Path) -> str:
    full = os.environ.get("GITHUB_REPOSITORY", "")
    return full.split("/")[-1] if full else Path(repo_root).resolve().name


def api_specs(repo_root: Path, cfg) -> list:
    """OpenAPI files of this repo: config api_spec_files, else auto-detected."""
    v = getattr(cfg, "api_spec_files", None)
    if v is not None:
        return [str(x) for x in v]
    return detect_api_specs(repo_root)


def boundary_files(changed: list, cfg, specs: list | None = None) -> list:
    pats: list = []
    for st in _stacks(cfg):
        pats += [p for p in STACK_BOUNDARY_PATTERNS.get(st, []) if p not in pats]
    pats += [p for p in (getattr(cfg, "contract_files", None) or []) if p not in pats]
    specs = set(specs or [])
    out = []
    for f in changed or []:
        rel = str(f).replace("\\", "/").lstrip("./")
        if (rel in specs or any(_match(rel, p) for p in pats)) and rel not in out:
            out.append(rel)
    return out


def contract_sources(repo_root: Path, cfg, specs: list | None = None) -> list:
    """Repo-relative files that ARE this repo's contract: OpenAPI files, the
    stack's contract folders (Angular: every store/ folder at any depth, its .ts
    files and the local files they import), contract_files."""
    root = Path(repo_root)
    out: list = list(specs if specs is not None else api_specs(root, cfg))
    stack_globs: list = []
    for st in _stacks(cfg):
        stack_globs += [g for g in STACK_CONTRACT_SOURCES.get(st, []) if g not in stack_globs]
    own_globs = [g for g in (getattr(cfg, "contract_files", None) or [])]
    store_files: list = []
    if stack_globs or own_globs:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
            for fn in filenames:
                rel = str((Path(dirpath) / fn).relative_to(root)).replace("\\", "/")
                if rel.endswith(".spec.ts") or rel in out:
                    continue
                if any(_match(rel, g) for g in own_globs):
                    out.append(rel)
                elif rel.endswith(".ts") and any(_match(rel, g) for g in stack_globs):
                    out.append(rel)
                    store_files.append(rel)
    # The store's state and selectors usually type their data with a model kept
    # outside store/ (mpa-web/src/app/mpa/shared/data/model/visitsummary.ts).
    # Add the local .ts files the store imports, one level deep.
    for rel in store_files:
        for imp in _local_imports(root, rel):
            if imp not in out:
                out.append(imp)
    return sorted(dict.fromkeys(out))


def _local_imports(root: Path, rel: str) -> list:
    """Repo-relative .ts files that `rel` imports with a relative path."""
    try:
        text = (root / rel).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    found = []
    base = (root / rel).parent
    for spec in _RELATIVE_IMPORT_RX.findall(text):
        target = os.path.normpath(base / spec)
        for cand in (target + ".ts", os.path.join(target, "index.ts")):
            if os.path.isfile(cand):
                r = str(Path(cand).resolve().relative_to(root.resolve())).replace("\\", "/")
                parts = set(r.split("/"))
                if not r.endswith(".spec.ts") and not (parts & _SKIP_DIRS):
                    found.append(r)
                break
    return found


def contract_dir(repo_root: Path) -> Path:
    return Path(repo_root) / ".harness" / "contract"


def summary_path(repo_root: Path) -> Path:
    return contract_dir(repo_root) / f"contract-{service_repo_name(repo_root)}.md"


def changed_files(repo_root: Path, base_ref: str | None) -> list:
    """Files this branch changed since base_ref, INCLUDING uncommitted and
    untracked work (a harness run commits only at the end)."""
    if not base_ref:
        return []
    mb = _git(repo_root, "merge-base", base_ref, "HEAD")
    if not mb:
        return []
    out = _git(repo_root, "diff", "--name-only", "--diff-filter=ACMRT", mb).splitlines()
    out += _git(repo_root, "ls-files", "--others", "--exclude-standard").splitlines()
    return sorted({l.strip() for l in out if l.strip()
                   and not l.startswith((".harness/", "audit/", "metrics/", "contracts/"))})


def base_ref_from_env() -> str:
    b = (os.environ.get("HARNESS_BASE_BRANCH") or "").strip()
    return f"origin/{b}" if b and not b.startswith("origin/") else b


def swagger_gap(changed: list, specs: list) -> list:
    """API-shaped backend files changed while no OpenAPI file changed."""
    if not specs or any(c in specs for c in changed):
        return []
    return [c for c in changed if any(_match(c, p) for p in _API_FILE_PATTERNS)]


def prompt_block(repo_root: Path, boundary: list) -> str:
    """Optional summary request appended to the documentation prompt."""
    if not boundary:
        return ""
    md = summary_path(repo_root)
    files = "\n".join(f"      - {f}" for f in boundary)
    return (
        "\n\nCONTRACT SUMMARY (optional, short). This change touches files that form a "
        "contract other repositories read:\n"
        f"{files}\n"
        "The harness publishes the repo's contract files themselves (the OpenAPI file, or "
        "the NgRx store folder) for the next story. If it helps a consumer, write a SHORT "
        f"summary to this EXACT path (the folder ALREADY EXISTS, shell is disallowed): {md}\n"
        "Sections: '# Contract: <what is provided>', '## What changed' (added / renamed / "
        "removed / retyped fields, new status values, new actions or selectors), '## Rules "
        "for the consumer' (e.g. read only through <selector>; one shape only). Describe what "
        "IS implemented, from the code. MOCKED VALUES ONLY — no real member data, no SSN, "
        "date of birth, phone number or email. Do not write a header block."
    )


def finalize(repo_root: Path, cfg, run, changed: list, log=print, force: bool = False) -> dict:
    """Stage this repo's contract files for publishing. Never raises.
    force=True (contract-only mode) publishes even when no contract file changed.
    Returns {status: published|none|missing, ...}; stored on run.contract_output."""
    import attachments as AT
    root = Path(repo_root)
    cdir = contract_dir(root)
    info = {"status": "none", "files": [], "skipped": [], "summary": None}
    try:
        specs = api_specs(root, cfg)
        boundary = boundary_files(changed, cfg, specs)
        info["changed_contract_files"] = boundary
        gap = swagger_gap(changed, specs)
        if gap:
            info["swagger_not_updated"] = gap
            log(f"  [contract] ! API files changed ({', '.join(gap[:4])}) but the OpenAPI "
                f"file ({', '.join(specs)}) was not updated")
            print(f"::warning title=OpenAPI not updated::API files changed but "
                  f"{', '.join(specs)} did not. Update the OpenAPI file with the API change.")
        if not boundary and not force:
            log("  [contract] no contract files changed — nothing to hand off")
            return _store(run, info)

        sources = contract_sources(root, cfg, specs)
        if not sources:
            info["status"] = "missing"
            log("  [contract] ! no contract files found to publish (no OpenAPI file, no "
                "store/ folder, no contract_files)")
            return _store(run, info)

        files_dir = cdir / "files"
        if files_dir.exists():
            shutil.rmtree(files_dir, ignore_errors=True)
        kinds = list(getattr(cfg, "attachment_pii_patterns", None) or ["ssn", "dob", "phone", "email"])
        allow = list(getattr(cfg, "attachment_pii_allow", None) or [])
        for rel in sources:
            src = root / rel
            if not src.is_file():
                continue
            text = src.read_text(encoding="utf-8", errors="replace")
            found = AT.scan_text(text, rel, kinds, allow)
            if found:
                info["skipped"].append({"file": rel, "findings": [str(f) for f in found[:10]]})
                log(f"  [contract] ! {rel} NOT published — possible PII (values masked):")
                for f in found[:10]:
                    log(f"      - {f}")
                continue
            dst = files_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            info["files"].append(rel)

        meta = {
            "repo": service_repo_name(root),
            "feature_id": getattr(run, "feature_id", ""),
            "run_id": os.environ.get("GITHUB_RUN_ID") or "local",
            "commit": _git(root, "rev-parse", "HEAD"),
            "base": os.environ.get("HARNESS_BASE_BRANCH", ""),
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "files": info["files"],
            "changed_contract_files": boundary,
        }

        # Optional model-written summary: stamp, scan.
        md = summary_path(root)
        if md.is_file():
            body = strip_header(md.read_text(encoding="utf-8", errors="replace"))
            found = AT.scan_text(body, md.name, kinds, allow)
            if found:
                md.unlink()
                info["skipped"].append({"file": md.name, "findings": [str(f) for f in found[:10]]})
                log(f"  [contract] ! {md.name} NOT published — possible PII (values masked)")
            else:
                md.write_text(make_header(meta) + "\n" + body.lstrip(), encoding="utf-8")
                info["summary"] = md.name

        if not info["files"] and not info["summary"]:
            info["status"] = "missing"
            log("  [contract] ! nothing could be published (all files held possible PII)")
            return _store(run, info)
        (cdir / "manifest.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        info.update(status="published", repo=meta["repo"], commit=meta["commit"])
        log(f"  [contract] {len(info['files'])} contract file(s)"
            f"{' + summary' if info['summary'] else ''} ready for harness-metrics "
            f"(contracts/{meta['feature_id']}/)")
        return _store(run, info)
    except Exception as e:
        info["status"] = "missing"
        info["error"] = f"{type(e).__name__}: {e}"
        log(f"  [contract] could not stage the contract ({info['error']})")
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


def _rel(p: Path, root: Path) -> str:
    try:
        return str(Path(p).resolve().relative_to(Path(root).resolve())).replace("\\", "/")
    except Exception:
        return str(p)
