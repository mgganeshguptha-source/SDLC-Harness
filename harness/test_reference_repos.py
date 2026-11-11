"""Other repos the context phase may read (reference_repos.py). Clones are real
git clones from a local repo; no network, no SDK."""
import subprocess
from pathlib import Path

import pytest

import reference_repos as RR
from boundaries import is_write_allowed
from contracts import ExitCode
from run import cmd_init
from test_state_machine import ScriptedExecutor, _drive, _new_run, _sm

LIST = """default_branch: master
repos:
  - name: acme/mpa
    purpose: MPA monolith, SOAP clients
  - name: acme/other
    branch: PM_Sep
"""


def _git(cwd, *a):
    subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True)


@pytest.fixture
def server(tmp_path, monkeypatch):
    """A local stand-in for GitHub holding acme/mpa on master."""
    src = tmp_path / "git" / "src"
    src.mkdir(parents=True)
    _git(src, "init", "-q", "-b", "master")
    (src / "Soap.java").write_text("class Soap {}\n")
    _git(src, "add", "-A")
    _git(src, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x")
    bare = tmp_path / "git" / "acme" / "mpa.git"
    bare.parent.mkdir()
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    monkeypatch.setenv("GITHUB_SERVER_URL", f"file://{tmp_path / 'git'}")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    return tmp_path


def _repo(tmp_path, text=LIST) -> Path:
    r = tmp_path / "service"
    (r / ".harness").mkdir(parents=True)
    (r / ".harness" / "config.yaml").write_text("{}\n")
    if text is not None:
        (r / ".harness" / "repo_list.yaml").write_text(text)
    return r


# --- the list ---
def test_default_branch_and_override(tmp_path):
    a, b = RR.load(_repo(tmp_path))
    assert (a.name, a.branch, a.source) == ("acme/mpa", "master", "default")
    assert (b.name, b.branch, b.source) == ("acme/other", "PM_Sep", "override")
    assert a.purpose == "MPA monolith, SOAP clients"


def test_empty_list_is_valid_and_missing_file_is_none(tmp_path):
    assert RR.load(_repo(tmp_path, "repos: []\n")) == []
    assert RR.fresh_run_problem(tmp_path / "service") == ""
    (tmp_path / "service" / ".harness" / "repo_list.yaml").unlink()
    assert RR.load(tmp_path / "service") is None
    assert "not found" in RR.fresh_run_problem(tmp_path / "service")


@pytest.mark.parametrize("text,expected", [
    ("repos:\n  - name: a/b\n", "no default_branch"),
    ("default_branch: m\nrepos:\n  - name: a/b\n  - name: A/B\n", "listed twice"),
    ("default_branch: m\nrepos:\n  - name: 'a b'\n", "not a valid repo name"),
    ("default_branch: '-x'\nrepos: []\n", "not a valid branch"),
    ("default_branch: m\n", "'repos' list"),
    ("repos: [\n", "not valid YAML"),
])
def test_invalid_file_names_the_problem(tmp_path, text, expected):
    with pytest.raises(ValueError, match=expected):
        RR.load(_repo(tmp_path, text))


def test_fresh_run_halts_before_state_exists(tmp_path):
    repo = _repo(tmp_path, None)
    args = type("A", (), {"repo": str(repo), "feature": "PC-1", "story": "x"})()
    with pytest.raises(SystemExit) as e:
        cmd_init(args)
    assert e.value.code == 1
    assert not (repo / ".harness" / "run-state.json").exists()


def test_find_by_full_or_short_name():
    entries = [RR.RepoEntry("a/x", "m", "default"), RR.RepoEntry("b/x", "m", "default"),
               RR.RepoEntry("a/y", "m", "default")]
    assert RR.find(entries, "A/Y").name == "a/y"
    assert RR.find(entries, "y").name == "a/y"
    assert RR.find(entries, "x") is None      # ambiguous: two owners
    assert RR.find(entries, "z") is None


def test_parse_requests_forms():
    text = ("- **REFERENCE REPO NEEDED:** `acme/mpa` — SOAP classes\n"
            "REFERENCE REPO NEEDED: dx_other - endpoint\n"
            "REFERENCE REPO NEEDED: None\n"
            "REFERENCE REPO NEEDED: <repo> — template\n"
            "REFERENCE REPO NEEDED: acme/mpa\n")
    assert RR.parse_requests(text) == [
        {"repo": "acme/mpa", "reason": "SOAP classes"},
        {"repo": "dx_other", "reason": "endpoint"}]


# --- clone ---
def test_clone_gives_commit_and_no_git_folder(tmp_path, server):
    repo = _repo(tmp_path)
    commit = RR.clone(RR.RepoEntry("acme/mpa", "master", "default"), repo)
    dest = RR.clone_path(repo, "acme/mpa")
    assert commit and (dest / "Soap.java").is_file() and not (dest / ".git").exists()


def test_clone_failure_names_repo_and_branch(tmp_path, server):
    with pytest.raises(RuntimeError, match="acme/mpa on branch 'nope'"):
        RR.clone(RR.RepoEntry("acme/mpa", "nope", "default"), _repo(tmp_path))


def test_token_stays_out_of_arguments_and_url(monkeypatch, tmp_path):
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "secret-token")
    seen = {}

    def fake(cmd, **k):
        seen.update(cmd=cmd, env=k["env"])
        return subprocess.CompletedProcess(cmd, 1, "", "denied")
    monkeypatch.setattr(RR.subprocess, "run", fake)
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    with pytest.raises(RuntimeError) as e:
        RR.clone(RR.RepoEntry("a/b", "m", "default"), _repo(tmp_path))
    assert "secret-token" not in " ".join(seen["cmd"]) and "secret-token" not in str(e.value)
    assert seen["env"]["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert "secret-token" not in seen["env"]["GIT_CONFIG_VALUE_0"]   # base64


def test_no_phase_can_write_to_the_clones(tmp_path):
    from config import HarnessConfig
    from phases import PHASES
    repo = _repo(tmp_path)
    cfg = HarnessConfig.load(repo / ".harness", repo_root=repo)
    target = RR.clone_path(repo, "acme/mpa") / "Soap.java"
    other = repo / ".harness" / "notes.md"
    for p in PHASES:
        for path in (str(target), ".harness/reference-repos/acme__mpa/Soap.java"):
            assert not is_write_allowed(path, ["**"], repo_root=str(repo),
                                        exclude_globs=cfg.phase_exclude(p)), p.id
    # The rest of .harness is not affected.
    assert is_write_allowed(str(other), [".harness/**"], repo_root=str(repo),
                            exclude_globs=cfg.phase_exclude(PHASES[0]))


def test_git_ignores_the_clones(tmp_path, server):
    import gitignore as GI
    repo = _repo(tmp_path)
    _git(repo, "init", "-q")
    GI.ensure(repo, log=lambda *a: None)
    RR.clone(RR.RepoEntry("acme/mpa", "master", "default"), repo)
    out = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "-uall"],
                         capture_output=True, text=True).stdout
    assert "reference-repos" not in out and "repo_list.yaml" in out


# --- prompts ---
def test_context_prompt_carries_the_list_and_other_phases_do_not(tmp_path):
    from phases import PHASES
    from sdk_runner import _phase_instruction
    repo = _repo(tmp_path)
    by_id = {p.id: p for p in PHASES}
    assert "acme/mpa" in _phase_instruction(by_id["context"], _new_run(), repo)
    assert "acme/mpa" not in _phase_instruction(by_id["prompt_steps"], _new_run(), repo)


def test_prompt_lists_repos_then_their_paths(tmp_path):
    repo = _repo(tmp_path)
    first = RR.prompt_block(repo, [])
    assert "acme/mpa: MPA monolith" in first and "REFERENCE REPO NEEDED" in first
    rec = [{"repo": "acme/mpa", "branch": "master", "commit": "abc", "source": "default",
            "reason": "SOAP"}]
    second = RR.prompt_block(repo, rec)
    assert str(RR.clone_path(repo, "acme/mpa")) in second
    assert "Reference repo findings" in second
    assert RR.prompt_block(_repo(tmp_path / "e", "repos: []\n"), []) == ""


# --- the whole flow, through the state machine ---
class _AsksFirst(ScriptedExecutor):
    """First context pass asks; later passes write a plain context file."""

    def __init__(self, line, **k):
        super().__init__(**k)
        self.line = line
        self.seen_clone = []

    def run_phase(self, phase, run):
        code = super().run_phase(phase, run)
        d = self.repo_root / ".github" / "story-context-files"
        self.seen_clone.append(
            (phase.id, RR.clone_path(self.repo_root, "acme/mpa").is_dir()))
        if phase.id == "context" and self.calls.count("context") == 1:
            f = d / "context.md"
            f.write_text(f.read_text() + f"\n{self.line}\n[BLOCKER]: not in this repo\n")
        return code


def _run(tmp_path, line, text=LIST):
    repo = _repo(tmp_path, text)
    hd = repo / ".harness"
    ex = _AsksFirst(line)
    run = _drive(_sm(ex, hd), _new_run())
    return ex, run, repo


def test_request_clones_reruns_context_then_removes_clone(tmp_path, server):
    ex, run, repo = _run(tmp_path, "REFERENCE REPO NEEDED: mpa — SOAP classes")
    assert ex.calls.count("context") == 2
    assert ("context", True) in ex.seen_clone          # second pass had it
    assert "coding" in ex.calls
    assert not [p for p, has in ex.seen_clone if has and p != "context"]
    assert not RR.clones_root(repo).exists()
    r = run.reference_repos[0]
    assert (r["repo"], r["branch"], r["source"], r["reason"]) == (
        "acme/mpa", "master", "default", "SOAP classes")
    assert r["commit"]


def test_request_runs_before_the_blocker_gate(tmp_path, server):
    ex, run, _ = _run(tmp_path, "REFERENCE REPO NEEDED: acme/mpa — x")
    assert run.halt_gate is None or run.halt_gate != "feasibility"
    assert ex.calls.count("context") == 2


def test_no_request_means_one_pass(tmp_path, server):
    ex, run, _ = _run(tmp_path, "nothing asked")
    assert run.reference_repos == []
    assert ex.calls.count("context") == 1
    assert run.status == "halted"          # the [BLOCKER] gate still works


def test_unlisted_repo_halts(tmp_path, server):
    ex, run, _ = _run(tmp_path, "REFERENCE REPO NEEDED: acme/secret — x")
    assert run.status == "halted" and run.halt_gate == "reference_repo"
    assert "not in .harness/repo_list.yaml" in run.halt_detail
    assert ex.calls.count("context") == 1


def test_clone_failure_halts(tmp_path, server):
    text = "default_branch: nope\nrepos:\n  - name: acme/mpa\n"
    _, run, _ = _run(tmp_path, "REFERENCE REPO NEEDED: acme/mpa — x", text)
    assert run.status == "halted" and run.halt_gate == "reference_repo"
    assert "branch 'nope'" in run.halt_detail


def test_over_the_limit_halts(tmp_path, server):
    text = "default_branch: m\nrepos:\n" + "".join(f"  - name: a/r{i}\n" for i in range(4))
    line = "\n".join(f"REFERENCE REPO NEEDED: a/r{i} — x" for i in range(4))
    _, run, _ = _run(tmp_path, line, text)
    assert run.halt_gate == "reference_repo" and "limit of 3" in run.halt_detail


def test_only_one_round(tmp_path, server):
    class Twice(_AsksFirst):
        def run_phase(self, phase, run):
            code = super().run_phase(phase, run)
            if phase.id == "context" and self.calls.count("context") == 2:
                f = self.repo_root / ".github" / "story-context-files" / "context.md"
                f.write_text(f.read_text() + "\nREFERENCE REPO NEEDED: acme/other — more\n")
            return code
    repo = _repo(tmp_path)
    ex = Twice("REFERENCE REPO NEEDED: acme/mpa — x")
    run = _drive(_sm(ex, repo / ".harness"), _new_run())
    assert ex.calls.count("context") == 2
    assert [r["repo"] for r in run.reference_repos] == ["acme/mpa"]


def test_context_reentered_on_a_new_runner_gets_the_clone_back(tmp_path, server):
    repo = _repo(tmp_path)
    run = _new_run()
    run.reference_repos = [{"repo": "acme/mpa", "branch": "old", "commit": "x",
                            "source": "default", "reason": "r"}]
    ex = ScriptedExecutor(script={"context": ExitCode.OK})
    sm = _sm(ex, repo / ".harness")
    assert sm._prepare_reference_repos(run, next(p for p in __import__("phases").PHASES
                                                 if p.id == "context")) is False
    assert RR.clone_path(repo, "acme/mpa").is_dir()
    assert run.reference_repos[0]["branch"] == "master"     # the list wins


def test_pr_body_gets_the_repos_once(tmp_path):
    repo = _repo(tmp_path)
    f = repo / ".harness" / "pr-body.md"
    f.write_text("# Change\n")
    rec = [{"repo": "acme/mpa", "branch": "master", "commit": "abc", "reason": "SOAP"}]
    assert RR.add_to_pr_body(repo, rec) is True
    assert RR.add_to_pr_body(repo, rec) is False
    text = f.read_text()
    assert text.count("Reference repos read") == 1 and "| acme/mpa | master | abc |" in text
    assert RR.add_to_pr_body(repo, []) is False


def test_job_summary(tmp_path, monkeypatch):
    s = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(s))
    RR.write_job_summary([{"repo": "a/b", "branch": "m", "commit": "c",
                           "source": "override", "reason": "r"}])
    assert "| a/b | m | c | override | r |" in s.read_text()


# --- the token used for clones (OP018-6418, 2026-11-11) ---
def test_repo_token_survives_the_model_token_swap(monkeypatch):
    import sdk_runner
    monkeypatch.delenv("HARNESS_REPO_TOKEN", raising=False)
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "repo-pat")
    sdk_runner.preserve_repo_token()
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "model-token")   # what sdk_runner does next
    sdk_runner.preserve_repo_token()                           # second phase
    import os
    assert os.environ["HARNESS_REPO_TOKEN"] == "repo-pat"


def test_clone_uses_the_repo_token_not_the_model_token(monkeypatch, tmp_path):
    import base64
    monkeypatch.setenv("HARNESS_REPO_TOKEN", "repo-pat")
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "model-token")
    env = RR._clone_env("https://github.com")
    assert base64.b64decode(env["GIT_CONFIG_VALUE_0"].split()[-1]).decode() == \
        "x-access-token:repo-pat"


def test_refused_token_is_named_in_the_error(monkeypatch, tmp_path):
    monkeypatch.setattr(RR.subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(
        cmd, 128, "", "fatal: could not read Username for 'https://github.com'"))
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    with pytest.raises(RuntimeError, match="the token was refused"):
        RR.clone(RR.RepoEntry("a/b", "m", "default"), _repo(tmp_path))
