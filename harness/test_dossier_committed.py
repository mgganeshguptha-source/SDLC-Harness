"""On a resume, the review dossier includes files committed on the WIP branch
earlier in the run, not only the new uncommitted ones (petclinic-angular-demo
PC-103, 2026-11-02: the reviewer saw 5 of 7 changed files and halted on wiring
that was already done)."""
import subprocess
from pathlib import Path

from sdk_runner import _build_review_dossier


def _git(repo: Path, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_dossier_has_committed_and_new_files(tmp_path: Path):
    r = tmp_path
    (r / ".harness").mkdir()
    (r / ".harness" / "config.yaml").write_text("repo_stacks: ['frontend']\n")
    (r / "src/app/store/visits").mkdir(parents=True)
    (r / "src/app/app.component.ts").write_text("export class AppComponent {} // wired\n")
    _git(r, "init", "-q")
    _git(r, "-c", "user.email=a@b", "-c", "user.name=bot", "add", "-A")
    _git(r, "-c", "user.email=a@b", "-c", "user.name=bot", "commit", "-qm", "wip")
    # New, uncommitted file from this resume's coding pass.
    (r / "src/app/store/visits/visits.actions.ts").write_text("export const x = 1;\n")

    out = _build_review_dossier(
        r, log=lambda *_: None,
        changed_hint=["src/app/app.component.ts",
                      "src/app/store/visits/visits.actions.ts"])
    assert out is not None
    assert "FULL FILE (current content): src/app/store/visits/visits.actions.ts" in out
    assert "FULL FILE (current content): src/app/app.component.ts" in out
    assert out.count("FULL FILE (current content): src/app/store/visits/visits.actions.ts") == 1
