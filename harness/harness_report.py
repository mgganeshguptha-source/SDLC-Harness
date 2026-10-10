#!/usr/bin/env python3
"""
harness_report.py — aggregate the per-run metrics records.

Usage:
    git fetch origin harness-metrics && git checkout harness-metrics
    python harness_report.py metrics/

    python harness_report.py metrics/ --jsonl > runs.jsonl   # for pandas/a DB

Reads metrics/<yyyy>/<mm>/*.json and answers the questions the raw records
cannot answer one at a time: which gate stops runs most often, what a story
costs, whether story quality is improving.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from pathlib import Path


def load(root: Path) -> list:
    recs = []
    for p in sorted(root.rglob("*.json")):
        try:
            recs.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception as e:
            print(f"  ! skipped {p.name}: {e}", file=sys.stderr)
    return recs


def _nums(recs, key):
    return [r[key] for r in recs if isinstance(r.get(key), (int, float))]


def _med(vals):
    return round(statistics.median(vals), 1) if vals else None


def report(recs: list) -> None:
    if not recs:
        print("No metrics records found.")
        return

    total = len(recs)
    done = [r for r in recs if r.get("status") == "done"]
    halted = [r for r in recs if r.get("status") != "done"]

    print(f"\nHARNESS METRICS — {total} run(s)\n" + "=" * 52)
    print(f"  completed : {len(done):>4}  ({100*len(done)//total}%)")
    print(f"  halted    : {len(halted):>4}  ({100*len(halted)//total}%)")

    # --- halts by gate. The infra split is the point: those are OUR failures,
    # the rest are the harness correctly refusing to ship something.
    if halted:
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            from halt_gates import is_infra
        except Exception:
            def is_infra(_):
                return False
        gates = Counter(r.get("halt_gate") or "unrecorded" for r in halted)
        infra = sum(c for g, c in gates.items() if is_infra(g))
        print(f"\n  WHY RUNS HALTED")
        for g, c in gates.most_common():
            tag = "  (infrastructure)" if is_infra(g) else ""
            print(f"    {g:<20} {c:>3}  {100*c//len(halted):>3}%{tag}")
        if infra:
            print(f"\n    {infra} of {len(halted)} halts were infrastructure, not quality.")

    # --- cost
    credits = _nums(recs, "credits_actual")
    if credits:
        print(f"\n  COST (actual credits)")
        print(f"    median {_med(credits)}   min {min(credits)}   max {max(credits)}"
              f"   total {round(sum(credits),2)}")
        worst = sorted((r for r in recs if isinstance(r.get("credits_actual"), (int, float))),
                       key=lambda r: -r["credits_actual"])[:3]
        for r in worst:
            print(f"      {r.get('feature_id'):<12} {r['credits_actual']:>7}"
                  f"   {r.get('status')}  loops="
                  f"{sum(r.get(k, 0) or 0 for k in r if k.startswith('loopback_'))}")

    # --- SDK-reported usage (schema v3, credit_source "sdk"). Raw SDK units, not
    # AI credits — GitHub documents no conversion, so none is applied here.
    for key, title, fmt in (("nano_aiu_total", "USAGE (SDK nano-AI units per run)", ",.0f"),
                            ("premium_cost_total", "USAGE (SDK premium-request cost per run)", ".4f")):
        vals = _nums(recs, key)
        if vals:
            print(f"\n  {title}")
            print(f"    median {format(_med(vals), fmt)}   min {format(min(vals), fmt)}"
                  f"   max {format(max(vals), fmt)}   total {format(sum(vals), fmt)}")
            worst = sorted((r for r in recs if isinstance(r.get(key), (int, float))),
                           key=lambda r: -r[key])[:3]
            for r in worst:
                print(f"      {str(r.get('feature_id')):<12} {format(r[key], fmt):>16}"
                      f"   {r.get('status')}  loops="
                      f"{sum(r.get(k, 0) or 0 for k in r if k.startswith('loopback_'))}")
    _phase_na = sorted({k for r in recs for k in r
                        if k.startswith("nano_aiu_") and k != "nano_aiu_total"})
    if _phase_na:
        print("\n  SDK nano-AI units by phase (median across runs)")
        for k in _phase_na:
            v = _nums(recs, k)
            if v:
                print(f"    {k[len('nano_aiu_'):]:<16} {format(_med(v), ',.0f'):>16}")

    # --- duration
    durs = _nums(recs, "duration_sec")
    if durs:
        print(f"\n  DURATION (seconds)")
        print(f"    median {_med(durs)}   max {max(durs)}")
        phase_keys = sorted({k for r in recs for k in r if k.startswith("dur_")})
        for k in phase_keys:
            v = _nums(recs, k)
            if v:
                print(f"    {k[4:]:<16} median {_med(v):>7}")

    # --- story quality: the adoption signal. Rising here means developers are
    # writing better stories, which no other metric captures.
    qs = _nums(recs, "quality_score")
    if qs:
        print(f"\n  STORY QUALITY (0-100)")
        print(f"    median {_med(qs)}   min {min(qs)}   max {max(qs)}")
        if len(qs) >= 6:
            half = len(qs) // 2
            print(f"    first half {_med(qs[:half])} -> second half {_med(qs[half:])}")

    # --- gate effectiveness
    ac_nm = _nums(recs, "ac_not_met")
    if ac_nm:
        caught = sum(1 for v in ac_nm if v > 0)
        print(f"\n  AC CONFORMANCE")
        print(f"    runs with an unmet criterion: {caught}/{len(ac_nm)}")

    loops = Counter()
    for r in recs:
        for k, v in r.items():
            if k.startswith("loopback_") and isinstance(v, int) and v:
                loops[k[9:]] += v
    if loops:
        print(f"\n  LOOPBACKS (total across runs)")
        for g, c in loops.most_common():
            print(f"    {g:<16} {c:>4}")

    _clarification_report(recs)

    resumed = sum(1 for r in recs if r.get("resumed"))
    if resumed:
        print(f"\n  {resumed} run(s) were resumed rather than restarted.")
    print()


# Two questions are "the same" when their wording is this similar. Questions are
# model-written, so exact matching would miss a reworded repeat.
_SAME_QUESTION = 0.8


def _norm_q(q: str) -> str:
    import re
    q = re.sub(r"\[NEEDS\s+CLARIFICATION\]\s*:?", "", str(q), flags=re.I)
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", q.lower()).split())


def _similar(a: str, b: str) -> bool:
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a, b).ratio() >= _SAME_QUESTION


def clarification_stats(recs: list) -> dict:
    """Is the harness learning from answers? Per feature and per repo, from the
    per-run records (oldest first):

      rounds            clarification halts per feature
      unanswered        halts on the SAME story text as the feature's previous
                        clarification halt (nobody changed the story)
      repeat_same       questions asked again in a later round of the SAME story
      repeat_other      questions already asked in ANOTHER story of the same
                        repo — the number that should fall to zero as answers
                        become decision files
    """
    ordered = sorted(recs, key=lambda r: (r.get("started_at") or "",
                                          r.get("finished_at") or ""))
    feats: dict = {}
    seen_by_repo: dict = {}      # repo -> [(feature, normalised question)]
    out = {"features": feats, "repeat_other": 0, "repeat_same": 0,
           "questions": 0, "halts": 0}
    for r in ordered:
        fid = r.get("feature_id") or "?"
        repo = r.get("repo") or "?"
        f = feats.setdefault(fid, {"repo": repo, "rounds": 0, "unanswered": 0,
                                   "questions": 0, "repeat_same": 0,
                                   "repeat_other": 0, "context_nano_aiu": 0.0,
                                   "done": False, "last_hash": None, "asked": []})
        if isinstance(r.get("nano_aiu_context"), (int, float)):
            f["context_nano_aiu"] += r["nano_aiu_context"]
        if r.get("status") == "done":
            f["done"] = True
        if r.get("halt_gate") != "clarification":
            continue
        out["halts"] += 1
        f["rounds"] += 1
        h = r.get("story_sha256")
        if h and h == f["last_hash"]:
            f["unanswered"] += 1
        f["last_hash"] = h or f["last_hash"]
        others = seen_by_repo.setdefault(repo, [])
        for q in (r.get("clarification_items") or []):
            nq = _norm_q(q)
            if not nq:
                continue
            out["questions"] += 1
            f["questions"] += 1
            if any(_similar(nq, p) for p in f["asked"]):
                f["repeat_same"] += 1
                out["repeat_same"] += 1
            elif any(of != fid and _similar(nq, oq) for of, oq in others):
                f["repeat_other"] += 1
                out["repeat_other"] += 1
            f["asked"].append(nq)
            others.append((fid, nq))
    return out


def _clarification_report(recs: list) -> None:
    st = clarification_stats(recs)
    if not st["halts"]:
        return
    print("\n  CLARIFICATIONS (is the harness learning from answers?)")
    print(f"    halts on questions        : {st['halts']}")
    print(f"    questions asked           : {st['questions']}")
    print(f"    asked again, same story   : {st['repeat_same']}")
    print(f"    asked again, other story  : {st['repeat_other']}   <- should fall to 0")
    print(f"\n    {'feature':<16} {'rounds':>6} {'unans.':>6} {'qs':>4} "
          f"{'rep-same':>8} {'rep-other':>9} {'ctx nano-AIU':>14}  done")
    for fid, f in sorted(st["features"].items()):
        if not f["rounds"]:
            continue
        print(f"    {fid:<16} {f['rounds']:>6} {f['unanswered']:>6} {f['questions']:>4} "
              f"{f['repeat_same']:>8} {f['repeat_other']:>9} "
              f"{format(f['context_nano_aiu'], ',.0f'):>14}  {'yes' if f['done'] else 'no'}")
    print("    (unans. = re-run on an unchanged story; rep-* = similar wording "
          f"\u2265 {int(_SAME_QUESTION*100)}%)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", default="metrics")
    ap.add_argument("--jsonl", action="store_true",
                    help="emit one record per line instead of a report")
    a = ap.parse_args()
    root = Path(a.path)
    if not root.exists():
        print(f"No such path: {root}", file=sys.stderr)
        return 1
    recs = load(root)
    if a.jsonl:
        for r in recs:
            print(json.dumps(r, sort_keys=True))
    else:
        report(recs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
