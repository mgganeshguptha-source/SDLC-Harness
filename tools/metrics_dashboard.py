#!/usr/bin/env python3
"""
metrics_dashboard.py - build a static HTML dashboard from SDLC-Harness run metrics.

Reads the per-run metrics JSON files that every run publishes to the
harness-metrics branch (metrics/<yyyy>/<mm>/<feature>-<run>.json) and writes ONE
self-contained HTML file (no network, no external files) that can be opened in
any browser and circulated.

USAGE
    python tools/metrics_dashboard.py <input_dir> [-o dashboard.html] [--csv runs.csv]

    <input_dir>  a folder holding metrics JSON files copied from one or more
                 service repos. It is searched recursively, so any layout works
                 (e.g. one sub-folder per repo). run-state.json, run-summary.json
                 and any JSON that is not a run-metrics file are skipped.

RULES APPLIED (stated on the page as well)
    - A STORY is (repo, feature_id). Its runs are ordered by started_at.
    - Story usage  = sum of nano_aiu_total over its runs. Each file's total covers
      only the phases that run executed (a resumed run carries earlier phases'
      per-phase values forward, so per-phase values are NOT summed blindly).
    - Phases a run executed = phases with a tokens_<phase> key, accepted only when
      their nano-AIU adds up to nano_aiu_total (1% tolerance). Otherwise all
      phases with usage are tried; if neither adds up the run is counted as
      "not reconciled" and reported in the page's data notes.
    - A story is COMPLETED when its latest run has status "done". The AC verdict
      is shown beside it and is not used to decide completion.
    - Halt gate / reason are read only from runs whose status is not "done"
      (a resumed run can carry a stale halt_gate from the earlier attempt).
    - Usage is shown in SDK units (nano-AIU / 1e9). No money conversion is made.

Python 3.8+, standard library only.
"""

import argparse
import csv
import datetime
import json
import os
import sys

CANONICAL_PHASES = [
    "repo_instructions", "context", "design", "prompt_steps", "coding",
    "code_review", "unit_testing", "validation", "documentation", "raise_pr",
]
SKIP_FILENAMES = {"run-state.json", "run-summary.json"}
TOLERANCE = 0.01


def num(value):
    """Return a float for int/float values, else None (bool is not a number)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def is_metrics_file(doc):
    """A run-metrics file is a flat dict with feature_id, run_id and schema_version."""
    if not isinstance(doc, dict):
        return False
    if "phase_token_log" in doc or "clarification_log" in doc:
        return False  # run-state.json
    return "feature_id" in doc and "run_id" in doc and "schema_version" in doc


def phase_names(doc):
    names = list(CANONICAL_PHASES)
    seq = doc.get("phase_seq")
    if isinstance(seq, str):
        for name in seq.split(","):
            name = name.strip()
            if name and name not in names:
                names.append(name)
    for key in doc:
        if key.startswith("model_"):
            name = key[len("model_"):]
            if name and name not in names:
                names.append(name)
    return names


def close(part, total):
    return total > 0 and abs(part - total) <= TOLERANCE * total


def normalise(doc, source):
    """Turn one metrics JSON into the compact record the page uses."""
    names = phase_names(doc)
    nano = {}
    for name in names:
        value = num(doc.get("nano_aiu_" + name))
        if value:
            nano[name] = value
    with_tokens = [n for n in names if num(doc.get("tokens_" + n)) is not None]
    with_nano = [n for n in names if n in nano]
    total = num(doc.get("nano_aiu_total"))

    reconciled = None  # None = no total to check against
    if total:
        if with_tokens and close(sum(nano.get(n, 0.0) for n in with_tokens), total):
            executed, reconciled = with_tokens, True
        elif close(sum(nano.values()), total):
            executed, reconciled = with_nano, True
        else:
            executed, reconciled = (with_tokens or with_nano), False
    else:
        executed = with_tokens or with_nano
        total = sum(nano.get(n, 0.0) for n in executed)

    ac_values = [num(doc.get(k)) for k in ("ac_met", "ac_not_met", "ac_unverifiable")]
    ac = None
    if any(v for v in ac_values):
        ac = [int(v or 0) for v in ac_values]

    clar = num(doc.get("clarifications"))
    if clar is None and isinstance(doc.get("clarification_items"), list):
        clar = float(len(doc["clarification_items"]))

    loopbacks = {}
    for key, value in doc.items():
        if key.startswith("loopback_") and num(value):
            loopbacks[key[len("loopback_"):]] = int(value)

    started = doc.get("started_at") if isinstance(doc.get("started_at"), str) else ""
    return {
        "repo": str(doc.get("repo") or "unknown repo"),
        "f": str(doc.get("feature_id")),
        "id": str(doc.get("run_id")),
        "actor": str(doc.get("actor") or ""),
        "t": started,
        "d": started[:10],
        "st": str(doc.get("status") or "unknown"),
        "resumed": bool(doc.get("resumed")),
        "gate": doc.get("halt_gate") or "",
        "phase": doc.get("halt_phase") or "",
        "detail": doc.get("halt_detail") or "",
        "cl": int(clar or 0),
        "ac": ac,
        "verdict": doc.get("ac_verdict") or "",
        "lb": loopbacks,
        "dur": num(doc.get("duration_sec")) or 0.0,
        "aiu": (total or 0.0) / 1e9,
        "premium": num(doc.get("premium_cost_total")),
        "tokens": num(doc.get("tokens_total")),
        "ph": executed,
        "pn": {n: nano[n] / 1e9 for n in executed if n in nano},
        "pd": {n: num(doc.get("dur_" + n)) for n in executed if num(doc.get("dur_" + n))},
        "pm": {n: doc.get("model_" + n) for n in executed if doc.get("model_" + n)},
        "ok": reconciled,
        "schema": doc.get("schema_version"),
        "engine": str(doc.get("engine_ref") or ""),
        "src": source,
    }


def load_runs(input_dir):
    runs, skipped, unreadable = {}, 0, []
    for root, _dirs, files in os.walk(input_dir):
        for filename in sorted(files):
            if not filename.lower().endswith(".json"):
                continue
            path = os.path.join(root, filename)
            if filename in SKIP_FILENAMES:
                skipped += 1
                continue
            try:
                with open(path, "r", encoding="utf-8-sig") as handle:
                    doc = json.load(handle)
            except (OSError, ValueError) as exc:
                unreadable.append("%s (%s)" % (os.path.relpath(path, input_dir), exc))
                continue
            if not is_metrics_file(doc):
                skipped += 1
                continue
            run = normalise(doc, os.path.relpath(path, input_dir))
            key = (run["repo"], run["f"], run["id"], run["t"])
            runs[key] = run  # the same file copied twice is counted once
    ordered = sorted(runs.values(), key=lambda r: (r["t"], r["repo"], r["f"], r["id"]))
    return ordered, skipped, unreadable


def write_csv(runs, path):
    columns = ["repo", "feature_id", "run_id", "actor", "started_at", "status", "resumed",
               "halt_gate", "halt_phase", "halt_detail", "clarifications", "ac_met",
               "ac_not_met", "ac_unverifiable", "ac_verdict", "loopbacks_total",
               "duration_sec", "usage_aiu", "phases_executed", "usage_reconciled",
               "schema_version", "engine_ref", "source_file"]
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for r in runs:
            ac = r["ac"] or ["", "", ""]
            writer.writerow([
                r["repo"], r["f"], r["id"], r["actor"], r["t"], r["st"], r["resumed"],
                r["gate"], r["phase"], r["detail"], r["cl"], ac[0], ac[1], ac[2],
                r["verdict"], sum(r["lb"].values()), r["dur"], round(r["aiu"], 3),
                " ".join(r["ph"]), "" if r["ok"] is None else r["ok"],
                r["schema"], r["engine"], r["src"],
            ])


def build_html(runs, skipped, unreadable, title):
    phases = list(CANONICAL_PHASES)
    for r in runs:
        for name in r["ph"]:
            if name not in phases:
                phases.append(name)
    meta = {
        "title": title,
        "generated": datetime.datetime.now().strftime("%d %b %Y %H:%M"),
        "phases": phases,
        "files": len(runs),
        "skipped": skipped,
        "unreadable": unreadable,
        "schemas": sorted({str(r["schema"]) for r in runs}),
    }
    payload = json.dumps({"meta": meta, "runs": runs}, ensure_ascii=True)
    payload = payload.replace("</", "<\\/")
    return TEMPLATE.replace("__TITLE__", html_escape(title)).replace("/*__DATA__*/null", payload)


def html_escape(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


TEMPLATE = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{
  color-scheme: light;
  --bg:#ffffff; --panel:#f5f7fb; --line:#e2e7ef; --axis:#c5ccd8;
  --ink:#172440; --ink-2:#47536b; --muted:#77819a;
  --accent:#1D6EE4; --navy:#0B3D7A;
  --good:#1B9E8F; --bad:#E54A24; --wait:#8a6400; --neutral:#a3abba;
  --font: Arial, "Helvetica Neue", Helvetica, sans-serif;
}
*{box-sizing:border-box}
html,body{margin:0}
body{background:var(--bg);color:var(--ink);font:14px/1.45 var(--font)}
.wrap{max-width:1180px;margin:0 auto;padding:0 20px 40px}
header{display:flex;flex-wrap:wrap;gap:16px 32px;align-items:flex-end;justify-content:space-between;padding:24px 0 16px;border-bottom:2px solid var(--navy)}
h1{font-size:24px;line-height:1.2;margin:0 0 4px;color:var(--navy)}
.meta{color:var(--ink-2);font-size:13px}
.filters{display:flex;flex-wrap:wrap;gap:12px}
.filters label{display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--muted)}
select{font:14px var(--font);color:var(--ink);background:#fff;border:1px solid var(--axis);border-radius:6px;padding:6px 10px;min-width:170px}
select:focus-visible,button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:20px 0}
.tile{background:var(--panel);border-radius:8px;padding:14px 16px;border-left:4px solid var(--accent)}
.tile.good{border-left-color:var(--good)} .tile.bad{border-left-color:var(--bad)}
.tile b{display:block;font-size:30px;line-height:1.1;color:var(--ink)}
.tile span{font-size:13px;color:var(--ink-2)}
.tile small{display:block;font-size:12px;color:var(--muted);margin-top:2px;min-height:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:16px;margin-bottom:16px}
section{border:1px solid var(--line);border-radius:8px;padding:16px;min-width:0}
h2{font-size:16px;margin:0 0 2px;color:var(--navy)}
.sub{font-size:12.5px;color:var(--muted);margin:0 0 14px}
h3{font-size:13px;margin:16px 0 8px;color:var(--ink-2)}
.bars{display:grid;gap:8px}
.bar{display:grid;grid-template-columns:128px 1fr 150px;gap:10px;align-items:center;font-size:13px}
.bar .lab{color:var(--ink-2);overflow-wrap:anywhere}
.bar .track{height:16px;border-left:1px solid var(--axis);display:flex;gap:2px}
.bar .fill{height:100%;background:var(--accent);border-radius:0 4px 4px 0;min-width:2px}
.bar .seg{height:100%;min-width:2px}
.bar .seg:last-child{border-radius:0 4px 4px 0}
.bar .val{color:var(--ink);font-variant-numeric:tabular-nums;font-size:12.5px}
.bar .val em{font-style:normal;color:var(--muted)}
.fill:hover,.seg:hover{filter:brightness(.88)}
.legend{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:12.5px;color:var(--ink-2);margin-bottom:10px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px}
.empty{color:var(--muted);font-size:13px;padding:8px 0}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px}
th{text-align:left;font-size:12px;color:var(--muted);font-weight:normal;padding:6px 10px;border-bottom:1px solid var(--axis);white-space:nowrap}
td{padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
td.nw{white-space:nowrap}
tr.runs>td{background:var(--panel);padding:0}
tr.runs table{font-size:12.5px}
tr.runs th,tr.runs td{border-bottom:1px solid var(--line);padding:6px 10px}
.pill{display:inline-flex;align-items:center;gap:5px;border-radius:999px;padding:2px 9px;font-size:12px;white-space:nowrap;border:1px solid currentColor;background:#fff}
.pill.done{color:var(--good)} .pill.halted{color:var(--bad)} .pill.needs_input{color:var(--wait)}
button.tog{font:13px var(--font);color:var(--accent);background:none;border:0;padding:0;cursor:pointer;font-weight:bold;text-align:left}
button.tog span{display:inline-block;width:14px}
.notes{font-size:12.5px;color:var(--ink-2);border-top:1px solid var(--line);margin-top:24px;padding-top:12px}
.notes li{margin:3px 0;max-width:110ch}
#tip{position:fixed;pointer-events:none;background:var(--ink);color:#fff;font-size:12px;padding:6px 9px;border-radius:5px;max-width:260px;display:none;z-index:9}
@media (max-width:520px){.bar{grid-template-columns:96px 1fr}.bar .val{grid-column:2}}
@media print{.filters{display:none} section{break-inside:avoid}}
</style>
</head>
<body>
<div class="wrap">
<header>
  <div>
    <h1 id="title"></h1>
    <div class="meta" id="meta"></div>
  </div>
  <div class="filters">
    <label for="fRepo">Repository<select id="fRepo"></select></label>
    <label for="fMonth">Month<select id="fMonth"></select></label>
  </div>
</header>

<div class="tiles" id="tiles"></div>

<div class="grid">
  <section>
    <h2>Where runs stop</h2>
    <p class="sub">Runs that did not finish, by the gate that stopped them.</p>
    <div class="bars" id="halts"></div>
    <h3>Reasons recorded</h3>
    <div class="scroll"><table id="haltTable"></table></div>
  </section>
  <section>
    <h2>Story readiness</h2>
    <p class="sub">Clarification questions the context gate raised, per story.</p>
    <div class="bars" id="clar"></div>
  </section>
</div>

<div class="grid">
  <section>
    <h2>Acceptance criteria outcome</h2>
    <p class="sub">Verdict per criterion, from the most recent run of each story that judged them.</p>
    <div class="legend">
      <span><i style="background:var(--good)"></i>Met</span>
      <span><i style="background:var(--bad)"></i>Not met</span>
      <span><i style="background:var(--neutral)"></i>Could not be verified</span>
    </div>
    <div class="bars" id="ac"></div>
  </section>
  <section>
    <h2>Rework inside the run</h2>
    <p class="sub">Times a gate sent the work back for another attempt, by gate.</p>
    <div class="bars" id="loops"></div>
  </section>
</div>

<div class="grid">
  <section>
    <h2>Usage by phase</h2>
    <p class="sub">SDK-reported AI units (nano-AIU &divide; 10&#8313;), summed over the runs shown. Most-used model in grey.</p>
    <div class="bars" id="usage"></div>
  </section>
  <section>
    <h2>Time by phase</h2>
    <p class="sub">Average minutes per run, for each phase the run executed.</p>
    <div class="bars" id="time"></div>
  </section>
</div>

<section>
  <h2>Stories</h2>
  <p class="sub">One row per story. Open a row to see each run behind it.</p>
  <div class="scroll"><table id="stories"></table></div>
</section>

<ul class="notes" id="notes"></ul>
</div>
<div id="tip"></div>

<script>
const DATA=/*__DATA__*/null;
const META=DATA.meta, RUNS=DATA.runs, PH=META.phases;
const LABEL={done:'Completed',halted:'Halted',needs_input:'Needs input'};
const ICON={done:'✓',halted:'■',needs_input:'?'};
const GATE={clarification:'Clarification',config_error:'Environment / config',coverage:'Coverage',scope:'Scope',review:'Review',ac:'Acceptance criteria',validation:'Tests',plan_path:'Plan path',blocker:'Blocker'};
const $=id=>document.getElementById(id);
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const short=r=>r.indexOf('/')>=0?r.split('/').slice(1).join('/'):r;
const f1=n=>Number(n).toLocaleString('en-US',{minimumFractionDigits:1,maximumFractionDigits:1});
const kind=st=>st==='done'?'done':(st==='needs_input'?'needs_input':'halted');
const gateOf=r=>r.gate||(r.st==='needs_input'?'clarification':'not recorded');
const gateName=g=>GATE[g]||g;
const pill=r=>`<span class="pill ${kind(r.st)}" title="status: ${esc(r.st)}">${ICON[kind(r.st)]} ${LABEL[kind(r.st)]}</span>`;
const plural=(v,w)=>v+' '+w+(v===1?'':'s');
const openRows=new Set();

function fill(sel,opts){sel.innerHTML=opts.map(o=>`<option value="${esc(o[0])}">${esc(o[1])}</option>`).join('')}
const MONTHS=['January','February','March','April','May','June','July','August','September','October','November','December'];
const monthName=m=>(MONTHS[parseInt(m.slice(5,7),10)-1]||m)+' '+m.slice(0,4);
fill($('fRepo'),[['','All repositories']].concat([...new Set(RUNS.map(r=>r.repo))].sort().map(r=>[r,short(r)])));
fill($('fMonth'),[['','All months']].concat([...new Set(RUNS.map(r=>r.d.slice(0,7)).filter(Boolean))].sort().map(m=>[m,monthName(m)])));
$('title').textContent=META.title;

function bars(el,rows,fmt){
  if(!rows.length){el.innerHTML='<div class="empty">Nothing to show for this selection.</div>';return}
  const max=Math.max.apply(null,rows.map(r=>r.v))||1;
  el.innerHTML=rows.map(r=>`<div class="bar"><div class="lab">${esc(r.k)}</div><div class="track"><div class="fill" style="width:${r.v/max*100}%" data-tip="${esc(r.k)}: ${esc(fmt(r.v))}"></div></div><div class="val">${esc(fmt(r.v))}${r.note?` <em>${esc(r.note)}</em>`:''}</div></div>`).join('');
}
function topKey(counts){let best='',n=0;for(const k in counts){if(counts[k]>n){n=counts[k];best=k}}return best}

function render(){
  const repo=$('fRepo').value, month=$('fMonth').value;
  const runs=RUNS.filter(r=>(!repo||r.repo===repo)&&(!month||r.d.slice(0,7)===month));
  const by={}, order=[];
  runs.forEach(r=>{const k=r.repo+'|'+r.f;if(!by[k]){by[k]=[];order.push(k)}by[k].push(r)});
  const stories=order.map(k=>{
    const rs=by[k], last=rs[rs.length-1];
    let acRun=null; for(let i=rs.length-1;i>=0;i--){if(rs[i].ac){acRun=rs[i];break}}
    return {k,f:last.f,repo:last.repo,rs,last,
      cl:rs.reduce((a,r)=>a+r.cl,0),aiu:rs.reduce((a,r)=>a+r.aiu,0),
      lb:rs.reduce((a,r)=>a+Object.keys(r.lb).reduce((x,y)=>x+r.lb[y],0),0),
      ac:acRun?acRun.ac:null,verdict:acRun?acRun.verdict:''};
  });
  const multi=new Set(stories.map(s=>s.repo)).size>1;
  const name=s=>multi&&stories.filter(x=>x.f===s.f).length>1?s.f+' ('+short(s.repo)+')':s.f;
  const done=stories.filter(s=>s.last.st==='done').length;
  const stopped=runs.filter(r=>r.st!=='done');
  const days=runs.map(r=>r.d).filter(Boolean).sort();
  $('meta').textContent='Generated '+META.generated+(runs.length&&days.length?' · runs from '+days[0]+' to '+days[days.length-1]:'')+' · '+plural(new Set(runs.map(r=>r.repo)).size,'repository').replace('repositorys','repositories');

  $('tiles').innerHTML=[
    ['',stories.length,'Stories attempted',''],
    ['good',done,'Stories completed',stories.length?done+' of '+stories.length+' (latest run finished)':''],
    ['',runs.length,'Runs',stories.length?f1(runs.length/stories.length)+' per story on average':''],
    ['bad',stopped.filter(r=>kind(r.st)==='halted').length,'Runs halted by a gate',''],
    ['',stopped.filter(r=>kind(r.st)==='needs_input').length,'Runs waiting on clarification','']
  ].map(t=>`<div class="tile ${t[0]}"><b>${t[1]}</b><span>${t[2]}</span><small>${t[3]}</small></div>`).join('');

  const g={}; stopped.forEach(r=>{const k=gateOf(r);g[k]=(g[k]||0)+1});
  bars($('halts'),Object.keys(g).map(k=>({k:gateName(k),v:g[k]})).sort((a,b)=>b.v-a.v),v=>plural(v,'run'));
  $('haltTable').innerHTML=stopped.length?'<tr><th>Story</th><th>Date</th><th>Gate</th><th>Phase</th><th>Reason</th></tr>'+stopped.map(r=>`<tr><td class="nw">${esc(r.f)}</td><td class="nw">${esc(r.d)}</td><td>${esc(gateName(gateOf(r)))}</td><td>${esc(r.phase)}</td><td>${esc(r.detail)}</td></tr>`).join(''):'';

  bars($('clar'),stories.filter(s=>s.cl).map(s=>({k:name(s),v:s.cl})).sort((a,b)=>b.v-a.v),v=>plural(v,'question'));

  const acs=stories.filter(s=>s.ac);
  const maxAc=Math.max.apply(null,[1].concat(acs.map(s=>s.ac[0]+s.ac[1]+s.ac[2])));
  const names=['met','not met','could not be verified'], cols=['var(--good)','var(--bad)','var(--neutral)'];
  $('ac').innerHTML=acs.length?acs.map(s=>{const t=s.ac[0]+s.ac[1]+s.ac[2];
    return `<div class="bar"><div class="lab">${esc(name(s))}</div><div class="track">${s.ac.map((n,i)=>n?`<div class="seg" style="width:${n/maxAc*100}%;background:${cols[i]}" data-tip="${esc(s.f)}: ${n} ${names[i]}"></div>`:'').join('')}</div><div class="val">${s.ac[0]} of ${t} met</div></div>`}).join(''):'<div class="empty">No run in this selection judged acceptance criteria.</div>';

  const l={}; runs.forEach(r=>{for(const k in r.lb){l[k]=(l[k]||0)+r.lb[k]}});
  bars($('loops'),Object.keys(l).map(k=>({k:gateName(k),v:l[k]})).sort((a,b)=>b.v-a.v),v=>plural(v,'loopback'));

  const u={},tm={},tc={},mc={};
  runs.forEach(r=>r.ph.forEach(p=>{
    if(r.pn[p]){u[p]=(u[p]||0)+r.pn[p]}
    if(r.pd[p]){tm[p]=(tm[p]||0)+r.pd[p];tc[p]=(tc[p]||0)+1}
    if(r.pm[p]){mc[p]=mc[p]||{};mc[p][r.pm[p]]=(mc[p][r.pm[p]]||0)+1}
  }));
  bars($('usage'),PH.filter(p=>u[p]).map(p=>({k:p,v:u[p],note:topKey(mc[p]||{})})),v=>f1(v));
  bars($('time'),PH.filter(p=>tc[p]).map(p=>({k:p,v:tm[p]/tc[p]/60})),v=>f1(v)+' min');

  $('stories').innerHTML=stories.length?'<tr><th>Story</th><th>Repository</th><th>Latest status</th><th>AC verdict</th><th class="n">Runs</th><th class="n">Questions</th><th class="n">Criteria met</th><th class="n">Loopbacks</th><th class="n">Usage</th><th>Last run</th></tr>'+stories.map(s=>{
    const o=openRows.has(s.k);
    return `<tr><td class="nw"><button class="tog" data-k="${esc(s.k)}" aria-expanded="${o}"><span>${o?'▾':'▸'}</span>${esc(s.f)}</button></td><td>${esc(short(s.repo))}</td><td>${pill(s.last)}</td><td>${esc(s.verdict)}</td><td class="n">${s.rs.length}</td><td class="n">${s.cl}</td><td class="n">${s.ac?s.ac[0]+' of '+(s.ac[0]+s.ac[1]+s.ac[2]):''}</td><td class="n">${s.lb}</td><td class="n">${f1(s.aiu)}</td><td class="nw">${esc(s.last.d)}</td></tr>`+
    (o?`<tr class="runs"><td colspan="10"><table><tr><th>Started (UTC)</th><th>Status</th><th>By</th><th>Stopped at</th><th>Reason</th><th>Phases executed</th><th class="n">Usage</th><th class="n">Wall clock</th></tr>${s.rs.map(r=>`<tr><td class="nw">${esc(r.t.replace('T',' ').replace('Z','').slice(0,16))}</td><td>${pill(r)}${r.resumed?' <span title="resumed run">↻</span>':''}</td><td>${esc(r.actor)}</td><td>${r.st!=='done'?esc(gateName(gateOf(r))):''}</td><td>${r.st!=='done'?esc(r.detail):''}</td><td>${esc(r.ph.join(', '))}</td><td class="n">${f1(r.aiu)}${r.ok===false?' *':''}</td><td class="n">${Math.round(r.dur/60)} min</td></tr>`).join('')}</table></td></tr>`:'');
  }).join(''):'<tr><td class="empty">No stories in this selection.</td></tr>';

  const bad=runs.filter(r=>r.ok===false).length, nototal=runs.filter(r=>r.ok===null).length;
  const notes=[
    'Usage is in units reported by the Copilot SDK. It is comparable between runs but is not a dollar or AI-credit figure until reconciled with the organisation billing report.',
    'Story usage is the sum of its runs. A story is completed when its latest run finished with status done; the AC verdict is shown separately. ↻ marks a resumed run.',
    'Wall clock is the whole workflow run and is longer than the sum of phase times.',
    'Figures describe pilot runs and are observations, not benchmarks.',
    'Data: '+META.files+' run files read (schema version '+META.schemas.join(', ')+'), '+META.skipped+' other JSON files skipped'+(META.unreadable.length?', '+META.unreadable.length+' unreadable: '+META.unreadable.join('; '):'')+'.'
  ];
  if(bad){notes.push('* '+plural(bad,'run')+' in this selection: per-phase usage did not add up to the run total, so the phase split for those runs is approximate.')}
  if(nototal){notes.push(plural(nototal,'run')+' in this selection had no run total; usage was summed from the phases.')}
  $('notes').innerHTML=notes.map(n=>'<li>'+esc(n)+'</li>').join('');
}

$('fRepo').addEventListener('change',render);
$('fMonth').addEventListener('change',render);
$('stories').addEventListener('click',e=>{const b=e.target.closest('button.tog');if(!b)return;const k=b.dataset.k;if(openRows.has(k)){openRows.delete(k)}else{openRows.add(k)}render()});
const tip=$('tip');
document.addEventListener('mousemove',e=>{const t=e.target.closest('[data-tip]');
  if(!t){tip.style.display='none';return}
  tip.textContent=t.dataset.tip;tip.style.display='block';
  tip.style.left=Math.min(e.clientX+12,innerWidth-270)+'px';tip.style.top=(e.clientY+14)+'px'});
render();
</script>
</body>
</html>
'''


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Build a static HTML dashboard from SDLC-Harness run metrics JSON files.")
    parser.add_argument("input_dir", help="folder holding metrics JSON files (searched recursively)")
    parser.add_argument("-o", "--output", default="harness-dashboard.html",
                        help="HTML file to write (default: harness-dashboard.html)")
    parser.add_argument("--csv", default=None, help="also write one row per run to this CSV file")
    parser.add_argument("--title", default="SDLC-Harness Run Insights", help="page title")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.input_dir):
        print("ERROR: not a folder: %s" % args.input_dir, file=sys.stderr)
        return 2

    runs, skipped, unreadable = load_runs(args.input_dir)
    if not runs:
        print("ERROR: no run-metrics JSON files found under %s" % args.input_dir, file=sys.stderr)
        return 1

    with open(args.output, "w", encoding="utf-8") as handle:
        handle.write(build_html(runs, skipped, unreadable, args.title))
    if args.csv:
        write_csv(runs, args.csv)

    stories = {(r["repo"], r["f"]) for r in runs}
    not_reconciled = [r for r in runs if r["ok"] is False]
    print("[dashboard] %d runs, %d stories, %d repos -> %s"
          % (len(runs), len(stories), len({r["repo"] for r in runs}), args.output))
    print("[dashboard] skipped %d non-metrics JSON, %d unreadable" % (skipped, len(unreadable)))
    for item in unreadable:
        print("[dashboard]   unreadable: %s" % item)
    for r in not_reconciled:
        print("[dashboard]   phase usage does not add up to run total: %s" % r["src"])
    if args.csv:
        print("[dashboard] csv -> %s" % args.csv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
