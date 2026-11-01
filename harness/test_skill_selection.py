"""
test_skill_selection.py — per-stack security review skills and the
"security-review" alias (config.skills_for_phase). No SDK.
"""
from config import HarnessConfig


def _cfg(tmp_path, yaml_text=""):
    hd = tmp_path / ".harness"
    hd.mkdir()
    if yaml_text:
        (hd / "config.yaml").write_text(yaml_text, encoding="utf-8")
    return HarnessConfig.load(hd, repo_root=tmp_path)


def test_backend_default_gets_backend_security_only(tmp_path):
    c = _cfg(tmp_path, 'repo_stacks: ["backend"]\n')
    assert c.skills_for_phase("coding") == ["security-review-backend"]
    assert c.skills_for_phase("code_review") == ["security-review-backend"]


def test_angular_default_gets_angular_security_and_review(tmp_path):
    c = _cfg(tmp_path, 'repo_stacks: ["angular-frontend"]\n')
    assert c.skills_for_phase("coding") == ["security-review-angular"]
    assert c.skills_for_phase("code_review") == ["security-review-angular", "review-angular-code"]


def test_old_name_expands_and_is_filtered(tmp_path):
    c = _cfg(tmp_path, 'repo_stacks: ["backend"]\n'
                       'phase_skills:\n  code_review: ["security-review"]\n')
    # the old folder still loads if a toolkit has not renamed it yet
    assert c.skills_for_phase("code_review") == ["security-review", "security-review-backend"]
    c2 = _cfg(tmp_path / "fe", 'repo_stacks: ["ionic"]\n'
                                'phase_skills:\n  coding: ["security-review"]\n') \
        if (tmp_path / "fe").mkdir() is None else None
    assert c2.skills_for_phase("coding") == ["security-review", "security-review-angular"]
