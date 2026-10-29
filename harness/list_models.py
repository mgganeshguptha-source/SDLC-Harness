"""
list_models.py — print the models available to your Copilot login via the SDK,
with whether each one accepts images (vision) and its image limits.

Run:  python list_models.py            (table)
      python list_models.py --json     (raw capabilities, for the record)

Spends no credits (just a metadata call).

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


async def main(as_json: bool):
    async with CopilotClient(**_client_kwargs()) as client:
        models = await client.list_models()

    if as_json:
        out = []
        for m in models:
            to_dict = getattr(m, "to_dict", None)
            out.append(to_dict() if callable(to_dict) else m)
        print(json.dumps(out, indent=2, default=str))
        return

    print("Available models:\n")
    print(f"  {'id':<28} {'name':<24} {'vision':<7} {'max imgs':>8} "
          f"{'max size':>9}  media types")
    for m in models:
        mid = _get(m, "id") or "?"
        name = _get(m, "name") or ""
        v = _vision(m)
        types = ",".join(t.replace("image/", "") for t in v["media_types"]) or "-"
        print(f"  {mid:<28} {name[:23]:<24} {'yes' if v['vision'] else 'no':<7} "
              f"{v['max_images'] if v['max_images'] is not None else '-':>8} "
              f"{_fmt_bytes(v['max_image_bytes']):>9}  {types}")
    print("\nvision = the model reports it accepts image input (capabilities."
          "supports.vision). Limits are per prompt, as reported by the service.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--json", action="store_true", help="print raw model records")
    asyncio.run(main(ap.parse_args().json))
