#!/usr/bin/env python3
"""NYC Job Scout web app. Run `python3 app.py` and a page opens at http://127.0.0.1:8765.

The page lets you edit the saved resume, upload a new one, choose job categories and re-run the search.
It only listens on this computer (127.0.0.1), so nobody else on your network can reach it.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import job_scout as js

HERE = Path(__file__).resolve().parent
RESUME = HERE / "resume.txt"
HISTORY = HERE / "resume_history"
UPLOADS = HERE / "uploads"
SETTINGS = HERE / "settings.json"
LATEST = js.OUT_DIR / "latest.json"
PORT = 8765

DEFAULT_SETTINGS = {
    "categories": list(js.CATEGORIES),
    "include_other": True,
    "titles": "",
    "years": "",
    "exclude": "intern, internship, summer analyst, phd, director, vice president, head of, principal",
    "include_remote": False,
    "web": True,
    "refresh": False,
    "ai": False,
    "ai_top": 30,
    "company_weight": 0.25,
}
run_lock = threading.Lock()


def load_settings():
    try:
        return {**DEFAULT_SETTINGS, **json.loads(SETTINGS.read_text())}
    except (OSError, ValueError):
        return dict(DEFAULT_SETTINGS)


def save_resume(text: str):
    """Save the resume, keeping the previous version in resume_history/."""
    if RESUME.exists() and RESUME.read_text() != text:
        HISTORY.mkdir(exist_ok=True)
        shutil.copy(RESUME, HISTORY / f"resume_{dt.datetime.now():%Y-%m-%d_%H%M%S}.txt")
    RESUME.write_text(text)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # keep the terminal quiet
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (json.dumps(body) if ctype == "application/json" else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        if self.path == "/":
            return self._send(200, PAGE, "text/html")
        if self.path == "/api/state":
            resume = RESUME.read_text() if RESUME.exists() else ""
            ok, why = js.ai_status()
            latest = json.loads(LATEST.read_text()) if LATEST.exists() else None
            return self._send(200, {
                "resume": resume, "settings": load_settings(), "latest": latest,
                "estimated_years": js.estimate_years(resume) if resume else None,
                "categories": {k: v[0] for k, v in js.CATEGORIES.items()},
                "ai_ok": ok, "ai_why": why,
                "history": sorted((p.name for p in HISTORY.glob("*.txt")), reverse=True)[:20] if HISTORY.exists() else []})
        if self.path.startswith("/api/history/"):
            name = Path(self.path.rsplit("/", 1)[-1]).name
            p = HISTORY / name
            return self._send(200, {"text": p.read_text()}) if p.exists() else self._send(404, {"error": "not found"})
        if self.path == "/api/results.csv":
            if not LATEST.exists():
                return self._send(404, {"error": "run a search first"})
            tmp = js.OUT_DIR / "latest.csv"
            js.write_csv(json.loads(LATEST.read_text())["jobs"], tmp)
            data = tmp.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            self.send_header("Content-Disposition", 'attachment; filename="nyc_job_matches.csv"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return self.wfile.write(data)
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/api/resume":
            text = json.loads(self._body() or b"{}").get("text", "")
            if len(text.strip()) < 200:
                return self._send(400, {"error": "That looks too short to be a full resume."})
            save_resume(text)
            return self._send(200, {"ok": True, "estimated_years": js.estimate_years(text)})

        if self.path == "/api/upload":
            name = Path(self.headers.get("X-Filename") or "resume.txt").name
            if not re.search(r"\.(pdf|docx?|rtf|odt|pages|txt|md|html?)$", name, re.I):
                return self._send(400, {"error": "Upload a .pdf, .docx, .doc, .rtf, .txt or .md file."})
            UPLOADS.mkdir(exist_ok=True)
            path = UPLOADS / f"{dt.datetime.now():%Y%m%d_%H%M%S}_{name}"
            path.write_bytes(self._body())
            try:
                text = js.read_resume(path).strip()
            except (ValueError, OSError) as e:
                return self._send(400, {"error": str(e)})
            if len(text) < 200:
                return self._send(400, {"error": "Couldn't pull much text out of that file. Try a PDF or paste the text."})
            save_resume(text)
            return self._send(200, {"ok": True, "text": text, "estimated_years": js.estimate_years(text)})

        if self.path == "/api/run":
            settings = {**DEFAULT_SETTINGS, **json.loads(self._body() or b"{}")}
            SETTINGS.write_text(json.dumps({k: v for k, v in settings.items() if k != "refresh"}, indent=2))
            if not RESUME.exists():
                return self._send(400, {"error": "Add your resume first."})
            if not run_lock.acquire(blocking=False):
                return self._send(409, {"error": "A search is already running."})
            log = []
            try:
                jobs, meta = js.run_search(RESUME.read_text(), settings, log=log.append)
            except ValueError as e:
                return self._send(400, {"error": str(e), "log": log})
            finally:
                run_lock.release()
            js.OUT_DIR.mkdir(exist_ok=True)
            payload = {"meta": meta, "jobs": js.public(jobs), "log": log, "ran_at": dt.datetime.now().isoformat(timespec="minutes")}
            LATEST.write_text(json.dumps(payload))
            return self._send(200, payload)

        self._send(404, {"error": "not found"})


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>NYC Job Scout</title>
<style>
:root{--bg:#f6f6f3;--card:#fff;--ink:#1c1c1e;--muted:#6b6b72;--line:#e3e3de;--accent:#2457d6;--accent-ink:#fff;
--hi:#1f8a4c;--mid:#b7791f;--lo:#b33a3a;--chip:#eef2fb;--warn:#fff7e6}
@media (prefers-color-scheme:dark){:root{--bg:#121214;--card:#1c1c1f;--ink:#ececef;--muted:#9a9aa3;--line:#2d2d33;
--accent:#7aa2ff;--accent-ink:#0d1020;--hi:#4cc184;--mid:#e0a84a;--lo:#ef7171;--chip:#232a3a;--warn:#2b2414}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif}
header{padding:20px 16px 4px;max-width:1280px;margin:0 auto}h1{font-size:22px;margin:0}header p{margin:4px 0 0;color:var(--muted)}
main{max-width:1280px;margin:0 auto;padding:12px 16px 60px;display:grid;grid-template-columns:400px 1fr;gap:16px;align-items:start}
@media (max-width:900px){main{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:16px}
.card h2{font-size:15px;margin:0 0 10px;display:flex;justify-content:space-between;align-items:center;gap:8px}
textarea{width:100%;min-height:300px;font:13px/1.45 ui-monospace,Menlo,monospace;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);resize:vertical}
input[type=text],input[type=number],input[type=search],select{font:inherit;padding:7px 9px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink);width:100%}
label{display:block;font-size:13px;color:var(--muted);margin:10px 0 4px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
button,.btn{font:inherit;font-weight:600;padding:8px 14px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer;text-decoration:none;display:inline-block}
button.primary{background:var(--accent);color:var(--accent-ink);border-color:var(--accent)}button:disabled{opacity:.5;cursor:default}
.checks{display:grid;grid-template-columns:1fr 1fr;gap:4px 10px}.checks label,.inline{display:flex;gap:6px;align-items:center;margin:2px 0;color:var(--ink);font-size:14px}
.small{font-size:12px;color:var(--muted)}.status{font-size:13px;color:var(--muted);min-height:18px;margin-top:8px;white-space:pre-wrap}
.warn{background:var(--warn);border-radius:8px;padding:8px 10px;font-size:13px;margin-top:8px}
.bar{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px;align-items:center}.bar input[type=search]{flex:1;min-width:180px;width:auto}.bar select{width:auto}
.job{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin-bottom:10px;display:grid;grid-template-columns:64px 1fr;gap:12px}
.score{font-size:24px;font-weight:700;text-align:center;line-height:1}.score small{display:block;font-size:11px;font-weight:500;color:var(--muted);margin-top:4px}
.meter{height:5px;border-radius:3px;background:var(--line);margin-top:8px;overflow:hidden}.meter i{display:block;height:100%}
.t{font-size:16px;font-weight:600;color:var(--accent);text-decoration:none}.t:hover{text-decoration:underline}
.m{color:var(--muted);font-size:13px;margin:2px 0 6px}.chips{display:flex;flex-wrap:wrap;gap:5px;margin:4px 0}
.chip{font-size:12px;padding:2px 8px;border-radius:999px;background:var(--chip)}.chip.miss{background:transparent;border:1px dashed var(--line);color:var(--muted)}
.chip.cat{background:transparent;border:1px solid var(--line)}.note{font-size:12px;color:var(--mid)}
.sub{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin-top:6px}.sub b{color:var(--ink)}
details.co{font-size:13px;margin-top:6px}details.co summary{cursor:pointer;color:var(--muted)}details.co a{color:var(--accent)}
.ai{font-size:13px;margin-top:8px;padding-top:8px;border-top:1px solid var(--line)}
.empty{color:var(--muted);padding:40px 0;text-align:center}
@media (max-width:560px){.job{grid-template-columns:1fr}.score{text-align:left}.checks{grid-template-columns:1fr}}
</style></head><body>
<header><h1>NYC Job Scout</h1><p>Edit or replace your resume, pick what to look for, and score every NYC posting against it.</p></header>
<main>
<section>
 <div class="card">
  <h2>Resume <span class="small" id="yrs"></span></h2>
  <textarea id="resume" spellcheck="false" placeholder="Paste your resume here, or upload a file below."></textarea>
  <div class="row" style="margin-top:8px">
   <button id="save">Save changes</button>
   <label class="btn" style="margin:0">Upload new resume<input type="file" id="file" accept=".pdf,.docx,.doc,.rtf,.txt,.md,.pages" hidden></label>
   <select id="hist" style="width:auto"><option value="">Earlier versions…</option></select>
  </div>
  <div class="status" id="rstatus"></div>
 </div>
 <div class="card">
  <h2>What to look for</h2>
  <div class="checks" id="cats"></div>
  <label class="inline"><input type="checkbox" id="include_other"> Also show strong matches outside these categories</label>
  <label for="titles">Specific job titles to search (optional, comma-separated)</label>
  <input type="text" id="titles" placeholder="e.g. FP&A analyst, sanctions analyst, underwriter">
  <label for="exclude">Leave out titles containing</label>
  <input type="text" id="exclude">
  <div class="row"><div style="flex:1"><label for="years">Years of experience</label><input type="number" id="years" min="0" step="0.5" placeholder="auto"></div>
  <div style="flex:1"><label for="cw">Company outlook weight</label><select id="cw"><option value="0">0% (fit only)</option><option value="0.15">15%</option><option value="0.25">25%</option><option value="0.35">35%</option><option value="0.5">50%</option></select></div></div>
  <label class="inline" style="margin-top:10px"><input type="checkbox" id="include_remote"> Include US-remote roles</label>
  <label class="inline"><input type="checkbox" id="web"> Search LinkedIn, Indeed &amp; Built In via Tavily</label>
  <label class="inline"><input type="checkbox" id="refresh"> Re-scan employer job boards now (otherwise reused for 6 hours)</label>
  <label class="inline"><input type="checkbox" id="ai"> Have Claude re-score the top 30</label>
  <div class="warn" id="aiwarn" hidden></div>
  <div class="row" style="margin-top:12px"><button class="primary" id="run">Find matching jobs</button></div>
  <div class="status" id="status"></div>
 </div>
 <div class="card small">
  <b>How the score works.</b> Job fit (skills you share with the posting, overall similarity, title, seniority) is blended with
  <b>company outlook</b>: 40% company health, 30% industry potential, 30% job security. Company ratings come from
  <code>company_profiles.json</code>: Sayari corporate records (status, regulatory and sanctions flags) plus Tavily news on
  funding, earnings and layoffs. Unresearched companies get a neutral 60.
 </div>
</section>
<section>
 <div class="bar">
  <input type="search" id="q" placeholder="Filter by title, company, skill…">
  <select id="catf"><option value="">All categories</option></select>
  <select id="min"><option value="0">Any score</option><option value="50">50+</option><option value="60" selected>60+</option><option value="70">70+</option><option value="80">80+</option></select>
  <select id="sort"><option value="final_score">Best overall</option><option value="fit">Best fit</option><option value="outlook">Best company outlook</option><option value="company">Company A–Z</option></select>
  <a class="btn" href="/api/results.csv">Download CSV</a>
 </div>
 <div class="small" id="meta" style="margin-bottom:10px"></div>
 <div id="list"><div class="empty">Run a search to see matches.</div></div>
</section>
</main>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const col=s=>s>=75?"var(--hi)":s>=55?"var(--mid)":"var(--lo)";
let JOBS=[],CATS={},META=null;
async function api(path,opts={}){const r=await fetch(path,opts);const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.error||r.statusText);return d}
function setYears(y){$("yrs").textContent=y!=null?`~${y} yrs of experience detected`:""}
async function init(){
 const s=await api("/api/state");CATS=s.categories;
 $("resume").value=s.resume;setYears(s.estimated_years);
 $("cats").innerHTML=Object.entries(CATS).map(([k,v])=>`<label><input type="checkbox" value="${k}"> ${esc(v)}</label>`).join("");
 $("catf").innerHTML+=Object.entries(CATS).map(([k,v])=>`<option value="${k}">${esc(v)}</option>`).join("");
 const st=s.settings;
 document.querySelectorAll("#cats input").forEach(i=>i.checked=st.categories.includes(i.value));
 ["titles","exclude","years"].forEach(k=>$(k).value=st[k]??"");
 ["include_other","include_remote","web","ai"].forEach(k=>$(k).checked=!!st[k]);
 $("cw").value=String(st.company_weight??0.25);
 if(!s.ai_ok){$("ai").checked=false;$("ai").disabled=true;$("aiwarn").hidden=false;$("aiwarn").textContent=s.ai_why}
 $("hist").innerHTML='<option value="">Earlier versions…</option>'+s.history.map(h=>`<option>${esc(h)}</option>`).join("");
 if(s.latest)show(s.latest);
}
function settings(){return{categories:[...document.querySelectorAll("#cats input:checked")].map(i=>i.value),
 include_other:$("include_other").checked,titles:$("titles").value,exclude:$("exclude").value,years:$("years").value,
 include_remote:$("include_remote").checked,web:$("web").checked,refresh:$("refresh").checked,ai:$("ai").checked,
 company_weight:parseFloat($("cw").value)}}
$("save").onclick=async()=>{try{const d=await api("/api/resume",{method:"POST",body:JSON.stringify({text:$("resume").value})});
 setYears(d.estimated_years);$("rstatus").textContent="Saved. The previous version is kept under Earlier versions.";init()}catch(e){$("rstatus").textContent=e.message}};
$("file").onchange=async e=>{const f=e.target.files[0];if(!f)return;$("rstatus").textContent="Reading "+f.name+"…";
 try{const d=await api("/api/upload",{method:"POST",headers:{"X-Filename":f.name},body:f});$("resume").value=d.text;setYears(d.estimated_years);
 $("rstatus").textContent="Loaded "+f.name+". Check the text, edit if needed, then run the search.";init()}catch(err){$("rstatus").textContent=err.message}e.target.value=""};
$("hist").onchange=async e=>{if(!e.target.value)return;const d=await api("/api/history/"+encodeURIComponent(e.target.value));
 $("resume").value=d.text;$("rstatus").textContent="Loaded "+e.target.value+". Click Save changes to make it current.";e.target.value=""};
$("run").onclick=async()=>{$("run").disabled=true;$("status").textContent="Searching… this takes 20–60 seconds (longer with a job-board re-scan or Claude).";
 try{await api("/api/resume",{method:"POST",body:JSON.stringify({text:$("resume").value})});
 const d=await api("/api/run",{method:"POST",body:JSON.stringify(settings())});show(d);$("status").textContent=d.log.join("\n")}
 catch(e){$("status").textContent=e.message}finally{$("run").disabled=false;$("refresh").checked=false}};
function show(d){JOBS=d.jobs;META=d.meta;
 $("meta").textContent=`${META.total} NYC postings scored ${d.ran_at?("on "+d.ran_at.replace("T"," at ")):""} · ~${META.years} yrs experience · company outlook counts ${Math.round((META.company_weight??0.25)*100)}%${META.ai?" · top matches re-scored by Claude":""}`;render()}
function render(){
 const q=$("q").value.toLowerCase(),min=+$("min").value,by=$("sort").value,cf=$("catf").value;
 let rows=JOBS.filter(j=>j.final_score>=min&&(!cf||(j.categories||[]).includes(cf))&&(!q||[j.title,j.company,j.location,...(j.matched||[])].join(" ").toLowerCase().includes(q)));
 rows.sort((a,b)=>by==="company"?a.company.localeCompare(b.company):(b[by]??0)-(a[by]??0));
 $("list").innerHTML=rows.slice(0,300).map(j=>{const c=j.company_info||{};const sy=c.sayari;return `<div class="job">
 <div><div class="score" style="color:${col(j.final_score)}">${j.final_score}<small>overall</small></div><div class="meter"><i style="width:${j.final_score}%;background:${col(j.final_score)}"></i></div></div>
 <div><a class="t" href="${esc(j.url)}" target="_blank" rel="noopener">${esc(j.title)}</a>
 <div class="m">${esc(j.company)} · ${esc(j.location)}${j.salary?" · "+esc(j.salary):""}${j.posted?" · posted "+esc(j.posted):""} · via ${esc(j.source)}</div>
 <div class="chips">${(j.categories||[]).map(k=>`<span class="chip cat">${esc(CATS[k]||k)}</span>`).join("")}${(j.matched||[]).map(s=>`<span class="chip">${esc(s)}</span>`).join("")}</div>
 ${(j.missing||[]).length?`<div class="chips">${j.missing.map(s=>`<span class="chip miss">${esc(s)}</span>`).join("")}</div>`:""}
 ${j.notes?`<div class="note">${esc(j.notes)}</div>`:""}
 <div class="sub"><span>Job fit <b>${j.fit}</b></span><span>Company outlook <b>${j.outlook}</b></span><span>Health ${j.company_health}</span><span>Industry ${j.industry_outlook}</span><span>Job security ${j.job_security}</span></div>
 <details class="co"><summary>Company details${c.basis?" ("+esc(c.basis)+")":""}</summary>
  ${c.type?`<div>${esc(c.type)}</div>`:""}<div>${esc(c.notes||"")}</div>
  ${c.industry?`<div><b>${esc(c.industry)}:</b> ${esc(c.industry_note)}</div>`:""}
  ${sy?`<div><b>Sayari:</b> ${sy.url?`<a href="${esc(sy.url)}" target="_blank" rel="noopener">${esc(sy.entity)}</a>`:esc(sy.entity)}: ${esc(sy.finding)}</div>`:""}
  ${(c.sources||[]).map(s=>`<div><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.label)}</a></div>`).join("")}
 </details>
 ${j.ai_summary?`<div class="ai"><b>${esc(j.ai_verdict)}</b>: ${esc(j.ai_summary)}${j.ai_gaps?.length?`<br><span class="small">Gaps: ${j.ai_gaps.map(esc).join("; ")}</span>`:""}</div>`:""}
 </div></div>`}).join("")||'<div class="empty">No matches with these filters.</div>'}
["q","min","sort","catf"].forEach(id=>$(id).addEventListener("input",render));
init();
</script></body></html>"""


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"NYC Job Scout is running at {url}  (press Ctrl+C to stop)")
    if "--no-open" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
