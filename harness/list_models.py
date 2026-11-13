"""
list_models.py — print the models available to your Copilot login via the SDK,
with whether each one accepts images (vision) and its image limits; optionally
check a service repo's .harness/config.yaml against that list.

Run:  python list_models.py                       (table)
      python list_models.py --json                (raw capabilities, for the record)
      python list_models.py --repo ../../service  (table + config check)

Spends no credits (just a metadata call).

The engine workflow runs it with --repo before the first phase ("List available
models" step), so every run log shows which models the run's credential can
call and whether each model the config pins for a phase is one of them. The
check only WARNS — it never stops the run; an unavailable model still fails its
phase as before ('Model "<name>" is not available').

Credential, same order as the engine (sdk_runner.py):
  COPILOT_MODEL_TOKEN, COPILOT_GITHUB_TOKEN, GH_TOKEN, GITHUB_TOKEN
  — none set => the Copilot CLI's logged-in user (local laptop).
In Actions, run it under the MODEL credential (COPILOT_MODEL_TOKEN ||
github.token), not the repo PAT: the model list differs per credential/org.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path

from copilot import CopilotClient


def _client_kwargs() -> dict:
    token = (os.environ.get("COPILOT_MODEL_TOKEN")
             or os.environ.get("COPILOT_GITHUB_TOKEN")
             or os.environ.get("GH_TOKEN")
             or os.environ.get("GITHUB_TOKEN"))
    if token:
        os.environ["COPILOT_GITHUB_TOKEN"] = token
        return {"github_token": token}
    return {"use_logged_in_user": True}


def _credential_kind() -> str:
    """Which kind of credential the list was read with (prefix only, never the value)."""
    token = (os.environ.get("COPILOT_MODEL_TOKEN")
             or os.environ.get("COPILOT_GITHUB_TOKEN")
             or os.environ.get("GH_TOKEN")
             or os.environ.get("GITHUB_TOKEN"))
    if not token:
        return "Copilot CLI logged-in user"
    kinds = {
        "ghs_": "GitHub Actions / App installation token (ghs_)",
        "gho_": "OAuth login token (gho_)",
        "ghu_": "GitHub App user token (ghu_)",
        "github_pat_": "fine-grained PAT (github_pat_)",
        "ghp_": "classic PAT (ghp_) — Copilot rejects this for model calls",
    }
    for prefix, label in kinds.items():
        if token.startswith(prefix):
            return label
    return "unrecognised token type"


def _get(obj, *path):
    """Walk attributes or dict keys defensively; None when absent."""
    cur = obj
    for p in path:
        if cur is None:
            return None
        cur = cur.get(p) if isinstance(cur, dict) else getattr(cur, p, None)
    return cur


def _vision(m) -> dict:
    lim = _get(m, "capabilities", "limits", "vision")
    return {
        "vision": bool(_get(m, "capabilities", "supports", "vision")),
        "max_images": _get(lim, "max_prompt_images"),
        "max_image_bytes": _get(lim, "max_prompt_image_size"),
        "media_types": _get(lim, "supported_media_types") or [],
    }


def _fmt_bytes(n) -> str:
    if not n:
        return "-"
    return f"{n / (1024 * 1024):.1f} MB" if n >= 1024 * 1024 else f"{n // 1024} KB"


def print_models(models) -> None:
    print(f"Available models (credential: {_credential_kind()}): {len(models)}\n")
    print(f"  {'id':<28} {'name':<24} {'vision':<7} {'max imgs':>8} "
          f"{'max size':>9}  media types")
    for m in sorted(models, key=lambda x: str(_get(x, "id") or "")):
        mid = _get(m, "id") or "?"
        name = _get(m, "name") or ""
        v = _vision(m)
        types = ",".join(t.replace("image/", "") for t in v["media_types"]) or "-"
        print(f"  {mid:<28} {name[:23]:<24} {'yes' if v['vision'] else 'no':<7} "
              f"{v['max_images'] if v['max_images'] is not None else '-':>8} "
              f"{_fmt_bytes(v['max_image_bytes']):>9}  {types}")
    print("\nvision = the model reports it accepts image input (capabilities."
          "supports.vision). Limits are per prompt, as reported by the service.")


# ----------------------------------------------------------------- config check
def config_rows(cfg, models, phase_ids) -> list:
    """One row per model phase the config would run: what model it resolves to,
    whether this credential can call it, whether it takes images, and whether
    the phase receives story attachments. Pure (no I/O) so it can be tested.

    status:
      ok          model is in the list (and takes images where the phase gets them)
      auto        "auto" — Copilot picks per task; cannot be checked in advance
      missing     model is not in this credential's list — the phase will fail
      no-vision   model is available but does not accept images, and the phase
                  receives attachments — a story WITH images will halt there
    """
    by_id = {str(_get(m, "id")): m for m in models}
    skip = set(getattr(cfg, "skip_phases", []) or [])
    att = set(getattr(cfg, "attachment_phases", []) or [])
    rows = []
    for pid in phase_ids:
        if pid in skip:
            continue
        model = str(cfg.model_for_phase(pid) or "")
        gets_att = pid in att
        info = by_id.get(model)
        if model.lower() == "auto":
            status, vision = "auto", None
        elif info is None:
            status, vision = "missing", None
        else:
            vision = _vision(info)["vision"]
            status = "no-vision" if (gets_att and not vision) else "ok"
        rows.append({"phase": pid, "model": model, "status": status,
                     "vision": vision, "attachments": gets_att})
    return rows


def print_config_check(rows, in_actions: bool) -> int:
    """Print the config table and warnings. Returns the number of problems."""
    print("\nModels in .harness/config.yaml (per phase):\n")
    print(f"  {'phase':<18} {'model':<28} {'available':<10} {'vision':<7} attachments")
    for r in rows:
        avail = {"ok": "yes", "no-vision": "yes", "missing": "NO", "auto": "auto"}[r["status"]]
        vis = "-" if r["vision"] is None else ("yes" if r["vision"] else "no")
        print(f"  {r['phase']:<18} {r['model']:<28} {avail:<10} {vis:<7} "
              f"{'yes' if r['attachments'] else 'no'}")

    problems = 0
    for r in rows:
        msg = ""
        if r["status"] == "missing":
            msg = (f"Phase '{r['phase']}' uses model '{r['model']}', which this run's "
                   f"credential cannot call — the phase will fail. Pick one from the "
                   f"list above (phase_models / review_model / model in .harness/config.yaml).")
            title = "Model not available"
        elif r["status"] == "no-vision":
            msg = (f"Phase '{r['phase']}' receives story attachments but its model "
                   f"'{r['model']}' does not accept images — a story with image "
                   f"attachments will halt there. Pin a vision model for this phase "
                   f"or remove it from attachment_phases.")
            title = "Model does not accept images"
        elif r["status"] == "auto" and r["attachments"]:
            print(f"  note: phase '{r['phase']}' uses 'auto' and receives attachments — "
                  f"image support cannot be confirmed in advance; pin a vision model "
                  f"if the story has images.")
        if msg:
            problems += 1
            print(f"::warning title={title}::{msg}" if in_actions else f"  WARNING: {msg}")
    if not problems:
        print("\nAll pinned models are available to this credential.")
    return problems


def _model_phase_ids() -> list:
    from phases import ALL_PHASES, PhaseKind
    return [p.id for p in ALL_PHASES if p.kind == PhaseKind.MODEL]


async def main(as_json: bool, repo: str | None):
    async with CopilotClient(**_client_kwargs()) as client:
        models = await client.list_models()

    if as_json:
        out = []
        for m in models:
            to_dict = getattr(m, "to_dict", None)
            out.append(to_dict() if callable(to_dict) else m)
        print(json.dumps(out, indent=2, default=str))
        return

    print_models(models)

    if repo:
        from config import HarnessConfig
        root = Path(repo)
        cfg = HarnessConfig.load(root / ".harness", repo_root=root)
        rows = config_rows(cfg, models, _model_phase_ids())
        print_config_check(rows, in_actions=os.environ.get("GITHUB_ACTIONS") == "true")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--json", action="store_true", help="print raw model records")
    ap.add_argument("--repo", default=None,
                    help="service repo root: also check its .harness/config.yaml models")
    a = ap.parse_args()
    asyncio.run(main(a.json, a.repo))
