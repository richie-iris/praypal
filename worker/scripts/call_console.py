#!/usr/bin/env python3
"""call_console.py — build the Iris call console: one page, everything behind the scenes.

Pulls every traced call, its events, and the current database state, and writes a
single self-contained HTML file. The data is embedded, so the page holds no keys
and needs no server — but it is a snapshot: re-run this to refresh.

    .venv/bin/python scripts/call_console.py                 # dev
    .venv/bin/python scripts/call_console.py --stage
    .venv/bin/python scripts/call_console.py --out /tmp/x.html --limit 50
    .venv/bin/python scripts/call_console.py --serve      # live on localhost:8899

In --serve mode the page polls this process for fresh data every few seconds, so
a call can be watched as it happens. The key stays in this process; the page has
none, and the server listens on 127.0.0.1 only.
"""
from __future__ import annotations

import argparse
import contextlib
import html
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env.local")

def _ref_of(env_file: str, fallback: str = "") -> str:
    from dotenv import dotenv_values
    return (dotenv_values(ROOT / env_file) or {}).get("SUPABASE_PROJECT_REF", "").strip() or fallback


PROJECTS = {
    "dev": (_ref_of(".env.dev"), "dev — the realtime lane"),
    "stage": (_ref_of(".env.test"), "stage — the test lane"),
}


def _fetch(ref: str, limit: int) -> dict:
    """Read through the service-role console RPCs."""
    import urllib.request

    env_url = os.getenv("SUPABASE_URL", "")
    if env_url and "//" in env_url:
        ref = env_url.split("//")[1].split(".")[0]

    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not key and ref == PROJECTS["stage"][0]:
        cfg = Path.home() / ".iris" / "iris-test.json"
        if cfg.exists():
            key = json.loads(cfg.read_text()).get("service_role_key")
    if not key:
        raise SystemExit("no service role key available for that project")

    def rpc(fn: str, body: dict):
        req = urllib.request.Request(
            f"https://{ref}.supabase.co/rest/v1/rpc/{fn}",
            data=json.dumps(body).encode(),
            headers={"apikey": key, "Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=40) as r:  # noqa: S310 - fixed https Supabase URL
            return json.loads(r.read().decode() or "null")

    calls = rpc("rt_console_calls", {"p_limit": limit}) or []
    events = rpc("rt_console_events", {"p_limit": 6000}) or []

    import rt_costs
    by_call: dict = {}
    for e in events:
        by_call.setdefault(e.get("call_id"), []).append(e)
    for c in calls:
        c["cost"] = rt_costs.estimate(c, by_call.get(c.get("call_id"), []))
    return {
        "calls": calls, "events": events,
        "state": rpc("rt_console_state", {}) or {},
        "costs": rt_costs.summarize(calls, events),
    }


CSS = """
:root{--bg:#0f1117;--panel:#171a23;--panel2:#1d2130;--ink:#e8eaf2;--muted:#8b90a3;
--line:#272c3a;--accent:#7c6cf0;--accent2:#43d2a8;--warn:#ffb454;--bad:#ff7b72;--good:#57d9a3;
--code:#11141c}
@media(prefers-color-scheme:light){:root{--bg:#f7f7fb;--panel:#fff;--panel2:#f2f3f8;
--ink:#1c1e27;--muted:#6b7080;--line:#e3e5ee;--code:#f0f1f6}}
:root[data-theme=light]{--bg:#f7f7fb;--panel:#fff;--panel2:#f2f3f8;--ink:#1c1e27;
--muted:#6b7080;--line:#e3e5ee;--code:#f0f1f6}
:root[data-theme=dark]{--bg:#0f1117;--panel:#171a23;--panel2:#1d2130;--ink:#e8eaf2;
--muted:#8b90a3;--line:#272c3a;--code:#11141c}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
header{padding:.8rem 1.1rem;border-bottom:1px solid var(--line);display:flex;
gap:1rem;align-items:baseline;flex-wrap:wrap;background:var(--panel)}
h1{font-size:1rem;margin:0;letter-spacing:-.01em}
.env{font-size:.72rem;color:var(--accent2);border:1px solid var(--line);border-radius:99px;padding:.1rem .55rem}
.stamp{color:var(--muted);font-size:.76rem;margin-left:auto}
.wrap{display:grid;grid-template-columns:minmax(260px,340px) 1fr;height:calc(100vh - 49px)}
@media(max-width:820px){.wrap{grid-template-columns:1fr;height:auto}}
.list{border-right:1px solid var(--line);overflow-y:auto;background:var(--panel)}
.search{padding:.55rem .7rem;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--panel);z-index:2}
.search input{width:100%;background:var(--panel2);border:1px solid var(--line);color:var(--ink);
border-radius:7px;padding:.4rem .6rem;font:inherit}
.call{padding:.6rem .8rem;border-bottom:1px solid var(--line);cursor:pointer}
.call:hover{background:var(--panel2)}
.call.sel{background:var(--panel2);box-shadow:inset 3px 0 0 var(--accent)}
.call .who{font-weight:600}
.call .meta{color:var(--muted);font-size:.75rem;display:flex;gap:.5rem;flex-wrap:wrap;margin-top:.15rem}
.detail{overflow-y:auto;padding:1rem 1.2rem 4rem}
.empty{color:var(--muted);padding:3rem 1rem;text-align:center}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:.5rem;margin:.6rem 0 1rem}
.kv div{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:.45rem .6rem}
.kv b{display:block;font-size:.68rem;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);font-weight:600}
.kv span{font-variant-numeric:tabular-nums}
details{background:var(--panel);border:1px solid var(--line);border-radius:9px;margin:.55rem 0}
details>summary{cursor:pointer;padding:.5rem .75rem;font-weight:600;list-style:none;display:flex;gap:.5rem;align-items:center}
details>summary::-webkit-details-marker{display:none}
details>summary:before{content:"▸";color:var(--accent);font-size:.8rem}
details[open]>summary:before{content:"▾"}
details>summary .n{color:var(--muted);font-weight:400;font-size:.78rem}
.body{padding:0 .75rem .75rem}
pre{background:var(--code);border:1px solid var(--line);border-radius:7px;padding:.6rem .7rem;
overflow-x:auto;font:12px/1.5 ui-monospace,"SF Mono",Menlo,monospace;white-space:pre-wrap;
word-break:break-word;margin:.4rem 0}
.turn{padding:.3rem .5rem;border-radius:6px;margin:.18rem 0}
.turn.caller{background:color-mix(in srgb,var(--accent) 12%,transparent)}
.turn.agent{background:var(--panel2)}
.turn.line{background:color-mix(in srgb,var(--warn) 16%,transparent)}
.turn .r{font-size:.68rem;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);margin-right:.4rem}
.ev{border-left:2px solid var(--line);padding:.35rem .6rem;margin:.2rem 0;cursor:pointer}
.ev:hover{background:var(--panel2)}
.ev.tool{border-color:var(--accent)}.ev.guard{border-color:var(--bad)}
.ev.bridge{border-color:var(--accent2)}.ev.postcall{border-color:var(--warn)}
.ev .t{color:var(--muted);font-variant-numeric:tabular-nums;font-size:.72rem;margin-right:.5rem}
.ev .k{font-size:.66rem;text-transform:uppercase;letter-spacing:.06em;padding:.05rem .35rem;
border-radius:4px;background:var(--panel2);color:var(--muted);margin-right:.4rem}
.tag{display:inline-block;font-size:.68rem;padding:.05rem .4rem;border-radius:4px;
background:var(--panel2);color:var(--muted);margin-right:.3rem}
.tag.bad{color:var(--bad)}.tag.good{color:var(--good)}.tag.warn{color:var(--warn)}
.tag.live{color:var(--good);background:color-mix(in srgb,var(--good) 18%,transparent)}
h2{font-size:.95rem;margin:1.4rem 0 .3rem}
h2:first-child{margin-top:0}
table{width:100%;border-collapse:collapse;font-size:.82rem}
td,th{text-align:left;padding:.3rem .5rem;border-top:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-size:.7rem;text-transform:uppercase;letter-spacing:.06em}
"""

JS = """
const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
let DATA=EMBEDDED||{calls:[],events:[],state:{}};
let evByCall={},selected=null,qval='',dbView=false;
function index(){evByCall={};(DATA.events||[]).forEach(e=>{(evByCall[e.call_id]=evByCall[e.call_id]||[]).push(e)});}
index();
const fmtDur=s=>s==null?'—':(s<60?`${Math.round(s)}s`:`${Math.floor(s/60)}m ${Math.round(s%60)}s`);
const when=t=>t?new Date(t).toLocaleString(undefined,{month:'short',day:'numeric',
  hour:'numeric',minute:'2-digit'}):'—';
const j=o=>esc(JSON.stringify(o,null,2));

function block(title,note,inner,open){return `<details ${open?'open':''}><summary>${esc(title)}
  <span class="n">${esc(note||'')}</span></summary><div class="body">${inner}</div></details>`}

function transcriptHTML(t){
  if(!t) return '<p style="color:var(--muted)">No transcript recorded.</p>';
  return t.split('\\n').filter(Boolean).map(l=>{
    const m=l.match(/^(caller|agent|line):\\s*(.*)$/s);
    if(!m) return `<div class="turn">${esc(l)}</div>`;
    const who=m[1]==='line'?'third party':m[1];
    return `<div class="turn ${m[1]}"><span class="r">${who}</span>${esc(m[2])}</div>`;
  }).join('');
}

function eventsHTML(evs){
  if(!evs||!evs.length) return '<p style="color:var(--muted)">No events recorded.</p>';
  return evs.map((e,i)=>{
    const d=e.detail||{};
    const summary=d.action?`${d.action} ${d.item||''}`:(d.query||d.number||d.asked||d.proposed||d.reason||'');
    return `<div class="ev ${esc(e.kind)}" onclick="this.querySelector('pre').style.display=
      this.querySelector('pre').style.display==='none'?'block':'none'">
      <span class="t">+${((e.elapsed_ms||0)/1000).toFixed(1)}s</span>
      <span class="k">${esc(e.kind)}</span><b>${esc(e.name)}</b>
      <span style="color:var(--muted)"> ${esc(String(summary).slice(0,90))}</span>
      <pre style="display:none">${j(d)}</pre></div>`;
  }).join('');
}

function dbSummary(ch){
  if(!ch) return 'not captured';
  const a=Object.keys(ch.added||{}).length,g=Object.keys(ch.changed||{}).length;
  return `${a} added · ${g} changed · ${(ch.unchanged||[]).length} untouched`;
}
function dbChangesHTML(ch){
  if(!ch||(!Object.keys(ch.added||{}).length&&!Object.keys(ch.changed||{}).length))
    return '<p style="color:var(--muted)">Nothing was written on this call.</p>';
  const rows=o=>Object.entries(o||{}).map(([k,v])=>
    `<tr><td><b>${esc(k)}</b></td><td><span style="color:var(--muted)">${esc(String(v.before??'—')).slice(0,160)}</span></td>
     <td>${esc(String(v.after??'—')).slice(0,220)}</td></tr>`).join('');
  let h='';
  if(Object.keys(ch.added||{}).length)
    h+=`<h3 style="font-size:.8rem;color:var(--good);margin:.5rem 0 .2rem">Added</h3>
        <table><tr><th>field</th><th>was</th><th>now</th></tr>${rows(ch.added)}</table>`;
  if(Object.keys(ch.changed||{}).length)
    h+=`<h3 style="font-size:.8rem;color:var(--warn);margin:.7rem 0 .2rem">Changed</h3>
        <table><tr><th>field</th><th>was</th><th>now</th></tr>${rows(ch.changed)}</table>`;
  if((ch.unchanged||[]).length)
    h+=`<p style="color:var(--muted);margin-top:.6rem">Left alone: ${
        (ch.unchanged||[]).map(esc).join(', ')}</p>`;
  return h;
}

function render(c){
  if(!c) return; selected=c.call_id; dbView=false;
  const m=c.meta||{},cost=c.cost||{breakdown:{},workings:{}};
  const evs=evByCall[c.call_id]||[];
  const tools=evs.filter(e=>e.kind==='tool'),guards=evs.filter(e=>e.kind==='guard');
  const pc=c.postcall||{};
  $('#detail').innerHTML=`
    <h2>${esc(c.display_name||'Unknown caller')} · visit #${c.call_number??'?'}</h2>
    <div class="kv">
      <div><b>when</b><span>${when(c.started_at)}</span></div>
      <div><b>duration</b><span>${fmtDur(c.duration_sec)}</span></div>
      <div><b>agent build</b><span>${esc(c.agent_version||'—')}</span></div>
      <div><b>model</b><span>${esc((c.model||'—').replace('gemini-',''))}</span></div>
      <div><b>voice</b><span>${esc(c.voice||'—')}</span></div>
      <div><b>she is called</b><span>${esc(c.agent_alias||'your companion')}</span></div>
      <div><b>tokens in / out</b><span>${(c.in_tokens||0).toLocaleString()} / ${(c.out_tokens||0).toLocaleString()}</span></div>
      <div><b>estimated cost</b><span style="color:var(--accent2)">$${(cost.total||0).toFixed(4)}</span></div>
      <div><b>per minute</b><span>${cost.per_minute?'$'+cost.per_minute.toFixed(4):'—'}</span></div>
      <div><b>tools · guards</b><span>${tools.length} · ${guards.length}</span></div>
      ${c.bridge_number?`<div><b>called out to</b><span>${esc(c.bridge_number)} (${esc(c.bridge_mode||'')})</span></div>`:''}
    </div>
    ${block('Transcript',`${(c.transcript||'').split('\\n').filter(Boolean).length} turns`,
        transcriptHTML(c.transcript),true)}
    ${block('What she did',`${evs.length} events — click any to expand`,eventsHTML(evs),true)}
    ${block('System prompt she was given',`${(c.system_prompt||'').length} chars`,
        `<pre>${esc(c.system_prompt||'(not captured)')}</pre>`)}
    ${block('Greeting played','pre-rendered dynamic clip (name & rotation)',`<pre>${esc(c.greeting||'—')}</pre>`)}
    ${block('Post-call · pass 1 (what was extracted)',
        pc.status?`status ${pc.status} · ${pc.domains_saved??0} saved / ${pc.domains_skipped??0} skipped`:'',
        `<pre>${j(pc.pass1_extraction||pc)}</pre>`)}
    ${block('Post-call · pass 2 (canvas for next call)',
        pc.compiled_canvas?`${String(pc.compiled_canvas).length} chars`:'not compiled',
        `<pre>${esc(pc.compiled_canvas||'—')}</pre>`)}
    ${block('▶ Next call will open with',m.next_greeting?'the clip already rendered':'—',
        `<pre>${esc(m.next_greeting||'—')}</pre>`)}
    ${block("▶ Next call's system prompt",m.next_prompt?`${m.next_prompt.length} chars`:'—',
        `<pre>${esc(m.next_prompt||'—')}</pre>`)}
    ${block('▶ What this call changed in the database',
        dbSummary(m.db_changes),dbChangesHTML(m.db_changes),true)}
    ${block('What this call cost (Itemized Pricing)',`$${(cost.total||0).toFixed(4)} total — rates in rt_costs.py`,
        `<table><tr><th>Cost Component</th><th>Cost</th><th>Share</th></tr>${
          Object.entries(cost.breakdown||{}).map(([k,v])=>
            `<tr><td>${esc(k)}</td><td>$${v.toFixed(4)}</td><td>${
              cost.total?Math.round(100*v/cost.total):0}%</td></tr>`).join('')
        }</table><div style="margin-top:.6rem;font-size:.78rem;color:var(--muted)"><b>Workings & Telephony Rates:</b></div><pre>${j(cost.workings)}</pre>`, true)}
    ${block('Everything else recorded','raw row',`<pre>${j(c)}</pre>`)}`;
  document.querySelectorAll('.call').forEach(n=>n.classList.toggle('sel',n.dataset.id===c.call_id));
}

function list(filter){
  const f=(filter||'').toLowerCase();
  const rows=DATA.calls.filter(c=>!f||JSON.stringify(c).toLowerCase().includes(f));
  $('#list').innerHTML=rows.map(c=>{
    const evs=evByCall[c.call_id]||[];
    const g=evs.filter(e=>e.kind==='guard').length;
    return `<div class="call" data-id="${esc(c.call_id)}">
      <div class="who">${esc(c.display_name||'Unknown')} <span class="tag">#${c.call_number??'?'}</span></div>
      <div class="meta"><span>${when(c.started_at)}</span>
      <span>${c.ended_at?fmtDur(c.duration_sec):'<span class=\"tag live\">live now</span>'}</span>
      <span>${evs.filter(e=>e.kind==='tool').length} tools</span>
      ${g?`<span class="tag bad">${g} guard</span>`:''}
      ${c.bridge_number?'<span class="tag good">bridged</span>':''}
      <span class="tag">${esc(c.agent_version||'')}</span>
      <span class="tag good">$${((c.cost||{}).total||0).toFixed(3)}</span></div></div>`}).join('')
      ||'<div class="empty">No calls match.</div>';
  document.querySelectorAll('.call').forEach(n=>n.onclick=()=>
    render(DATA.calls.find(c=>c.call_id===n.dataset.id)));
  if(!rows.length){selected=null;return;}
  const keep=rows.find(c=>c.call_id===selected);
  if(dbView) return;
  render(keep||rows[0]);
}
$('#q').addEventListener('input',e=>{qval=e.target.value;list(qval);});
list('');

async function refresh(){
  if(!LIVE) return;
  try{
    const r=await fetch('/data.json',{cache:'no-store'});
    if(!r.ok) throw new Error(r.status);
    const fresh=await r.json();
    const changed=JSON.stringify(fresh.calls.map(c=>[c.call_id,c.ended_at,c.duration_sec]))
      !==JSON.stringify(DATA.calls.map(c=>[c.call_id,c.ended_at,c.duration_sec]))
      || (fresh.events||[]).length!==(DATA.events||[]).length;
    DATA=fresh; index();
    if(changed) list(qval);
    const ct=DATA.costs||{};
    $('#stamp').textContent=`${DATA.calls.length} calls · $${(ct.total||0).toFixed(2)} total · `
      +`$${(ct.avg_per_call||0).toFixed(3)}/call · $${(ct.monthly_per_daily_caller||0).toFixed(2)}/mo per daily caller`
      +` · ${new Date().toLocaleTimeString()}`;
    $('#dot').style.background='var(--good)';
  }catch(e){ $('#dot').style.background='var(--bad)'; }
}
if(LIVE){ refresh(); setInterval(refresh,4000); }

// The database as it stands right now, under its own tab
$('#dbBtn').onclick=()=>{
  dbView=true; const s=DATA.state||{};
  $('#detail').innerHTML=`<h2>What the database holds right now</h2>
    ${block('Callers',`${(s.callers||[]).length} rows`,`<pre>${j(s.callers)}</pre>`,true)}
    ${block('Memory (schemas)',`${(s.schemas||[]).length} rows`,`<pre>${j(s.schemas)}</pre>`)}
    ${block('Reminders',`${(s.reminders||[]).length} rows`,`<pre>${j(s.reminders)}</pre>`)}
    ${block('Write audit trail',`${(s.audit||[]).length} most recent writes`,`<pre>${j(s.audit)}</pre>`)}`;
  document.querySelectorAll('.call').forEach(n=>n.classList.remove('sel'));
};
"""


def build(data: dict, env_label: str, live: bool = False) -> str:
    stamp = datetime.now(timezone.utc).astimezone().strftime("%b %d, %Y at %I:%M %p")
    payload = "null" if live else json.dumps(data, default=str).replace("</", "<\\/")
    mode_label = "live ·" if live else "snapshot"
    return f"""<title>Phone-Pal — Call Console</title>
<style>{CSS}</style>
<header>
  <h1>Phone-Pal · Call Console</h1>
  <span class="env">{html.escape(env_label)}</span>
  <button id="dbBtn" style="background:var(--panel2);color:var(--ink);border:1px solid var(--line);
    border-radius:7px;padding:.25rem .6rem;cursor:pointer;font:inherit">Database now</button>
  <span class="stamp"><span id="dot" style="display:inline-block;width:7px;height:7px;
    border-radius:50%;background:var(--muted);margin-right:.4rem;vertical-align:middle"></span>
    <span id="stamp">{len(data.get('calls') or [])} calls · {mode_label} {html.escape(stamp)}</span></span>
</header>
<div class="wrap">
  <div class="list">
    <div class="search"><input id="q" placeholder="Search calls, numbers, transcripts…"></div>
    <div id="list"></div>
  </div>
  <div class="detail" id="detail"><div class="empty">Select a call.</div></div>
</div>
<script>const EMBEDDED={payload};const LIVE={str(live).lower()};{JS}</script>"""


def serve(ref: str, label: str, limit: int, port: int) -> None:
    """Run the console live on localhost.

    The page itself never holds a database key: it polls this process, which
    holds the service-role key and talks to Supabase. Bound to 127.0.0.1 only —
    caller data must not be reachable from the network.
    """
    import http.server
    import webbrowser

    shell = build({"calls": []}, label, live=True).encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def _send(self, body: bytes, ctype: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path.startswith("/data.json"):
                try:
                    body = json.dumps(_fetch(ref, limit), default=str).encode()
                except Exception as e:
                    body = json.dumps({"calls": [], "events": [], "state": {},
                                       "error": str(e)}).encode()
                self._send(body, "application/json")
            elif self.path in ("/", "/index.html"):
                self._send(shell, "text/html; charset=utf-8")
            else:
                self.send_error(404)

        def log_message(self, *a) -> None:
            pass

    url = f"http://127.0.0.1:{port}/"
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Iris call console — {label}\n  {url}   (refreshes every 4s; Ctrl-C to stop)")
    with contextlib.suppress(Exception):
        webbrowser.get("chrome").open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", action="store_true", help="stage instead of dev")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--out", default=None)
    ap.add_argument("--serve", action="store_true", help="live, on localhost")
    ap.add_argument("--port", type=int, default=8899)
    a = ap.parse_args()

    which = "stage" if a.stage else "dev"
    ref, label = PROJECTS[which]
    if a.serve:
        serve(ref, label, a.limit, a.port)
        return
    data = _fetch(ref, a.limit)
    out = Path(a.out) if a.out else ROOT / f"call-console-{which}.html"
    out.write_text(build(data, label))
    print(f"{len(data['calls'])} calls, {len(data['events'])} events → {out}")


if __name__ == "__main__":
    main()
