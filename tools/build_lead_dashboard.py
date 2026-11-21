#!/usr/bin/env python3
"""Generate the private ChatGPT lead-hunting dashboard from operations records."""
import argparse, csv, json, os
from datetime import datetime, timezone
from pathlib import Path

def load(path):
    try:
        value=json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value,dict) else {}
    except (OSError,json.JSONDecodeError): return {}

def jsonl(path):
    rows=[]
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row=json.loads(line)
                if isinstance(row,dict): rows.append(row)
            except json.JSONDecodeError: pass
    except OSError: pass
    return rows

def dt(value):
    if not isinstance(value,str) or not value: return None
    try:
        result=datetime.fromisoformat(value.replace("Z","+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result
    except ValueError: return None

def stamp(value): return value.astimezone(timezone.utc).isoformat(timespec="seconds") if value else ""

def terminal(value): return str(value or "").lower() not in {"","pending","queued","running","in_progress","planned"}

def batch_rows(root):
    result=[]
    for manifest_path in (root/"runs").glob("*/RUN_MANIFEST.json"):
        manifest=load(manifest_path); folder=manifest_path.parent
        summary=load(folder/"RUN_SUMMARY.json"); progress=load(folder/"LOCATION_CATEGORY_PROGRESS.json")
        queries=jsonl(folder/"QUERY_LEDGER.jsonl")
        lifecycle=manifest.get("lifecycle") if isinstance(manifest.get("lifecycle"),dict) else {}
        starts=[dt(q.get("started_at")) for q in queries if dt(q.get("started_at"))]
        ends=[dt(q.get("finished_at") or q.get("timestamp")) for q in queries if dt(q.get("finished_at") or q.get("timestamp"))]
        start=dt(lifecycle.get("started_at")) or (min(starts) if starts else None)
        end=dt(lifecycle.get("finished_at")) or (max(ends) if ends else None)
        failures=[str(q.get("error")) for q in queries if q.get("error")]
        failures += [str(e) for e in summary.get("errors",[]) if e]
        terminal_count=sum(1 for q in queries if terminal(q.get("terminal_status")))
        active=str(lifecycle.get("status") or progress.get("status") or summary.get("progress_status") or "").lower() in {"in_progress","running"}
        if active and start: end=datetime.now(timezone.utc)
        seconds=int((end-start).total_seconds()) if start and end else None
        status=lifecycle.get("status") or progress.get("status") or summary.get("progress_status") or "unknown"
        result.append({"run_key":manifest.get("run_key",folder.name),"category":(manifest.get("categories") or [""])[0],
          "cities":", ".join(manifest.get("locations") or []),"status":status,
          "processed_queries":lifecycle.get("completed_count",terminal_count),
          "expected_queries":lifecycle.get("target_count") or progress.get("total_queries") or summary.get("expected_queries") or len(queries),
          "completed_count":lifecycle.get("completed_count",terminal_count),"failed_count":lifecycle.get("failed_count",progress.get("errors",0)),
          "pending_count":lifecycle.get("pending_count",progress.get("pending",0)),
          "attempt_count":lifecycle.get("attempt_count",0),"failure_category":lifecycle.get("failure_category") or "",
          "failure_details":lifecycle.get("failure_details") or "; ".join(dict.fromkeys(failures)),
          "source_run_ids":lifecycle.get("source_run_ids",[]),
          "started_at":stamp(start),"finished_at":stamp(end),"duration_seconds":seconds,
          "failure_reason":"; ".join(dict.fromkeys(failures))})
    return sorted(result,key=lambda r:r["started_at"],reverse=True)

def build(root):
    states=[]
    for path in root.rglob("CITY_CAMPAIGN_STATE.json"):
        state=load(path)
        if state: states.append(state)
    campaign=max(states,key=lambda x:dt(x.get("updated_at")) or datetime.min.replace(tzinfo=timezone.utc)) if states else {}
    categories=campaign.get("categories") or []; cities=campaign.get("city_names") or []
    completed=set()
    for item in campaign.get("completed_categories",[]):
        try: completed.add(int(item.get("index") if isinstance(item,dict) else item))
        except (TypeError,ValueError): pass
    index=campaign.get("current_category_index")
    try: index=int(index)
    except (TypeError,ValueError): index=None
    current_key=str(campaign.get("current_run_key") or "")
    queries=jsonl(root/"runs"/current_key/"QUERY_LEDGER.jsonl") if current_key else []
    by_city={str(q.get("location")):q for q in queries if q.get("location")}
    matrix=[]
    for category in categories:
        if not isinstance(category,dict): continue
        try: ci=int(category.get("index"))
        except (TypeError,ValueError): continue
        for city in cities:
            city=str(city); query=by_city.get(city) if ci==index else None
            if campaign.get("status")=="complete" or ci in completed: state="complete"
            elif ci==index and query and terminal(query.get("terminal_status")):
                state="failed" if query.get("error") or str(query.get("terminal_status")).lower() in {"error","failed","blocked"} else "complete"
            elif ci==index: state="in progress"
            else: state="not started"
            matrix.append({"category_index":ci,"category":category.get("business_type",""),"city":city,"status":state,
              "query_status":(query or {}).get("terminal_status",""),"failure_reason":(query or {}).get("error","")})
    processed=sum(1 for r in matrix if r["status"] in {"complete","failed"})
    current=[r for r in matrix if r["category_index"]==index]
    current_done=sum(1 for r in current if r["status"] in {"complete","failed"})
    campaign_view={k:campaign.get(k) for k in ["campaign_key","status","stage","region","created_at","updated_at","current_category_index","current_category","current_run_key","pause_reason","city_names"]}
    campaign_view.update({"category_count":len(categories),"city_count":len(cities),"processed_pairs":processed,"total_pairs":len(matrix),
      "progress_pct":round(processed*100/len(matrix),1) if matrix else 0,"current_category_processed":current_done,
      "current_category_total":len(current),"current_category_pct":round(current_done*100/len(current),1) if current else 0})
    return {"generated_at":stamp(datetime.now(timezone.utc)),"source_revision":os.environ.get("GITHUB_SHA",""),
      "campaign":campaign_view,"category_city":matrix,"batches":batch_rows(root)}

def write_csv(path,rows,fields):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open("w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction="ignore"); w.writeheader(); w.writerows(rows)

def render(data):
    packed=json.dumps(data,ensure_ascii=False,separators=(",",":")).replace("</","<\\/")
    html = """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ChatGPT Lead Hunting</title><style>
body{margin:0;background:#f3f6fa;color:#17283d;font:15px/1.45 system-ui,sans-serif}header{background:#17314e;color:white;padding:24px 5vw}main{max-width:1400px;margin:auto;padding:24px}h1{margin:0 0 4px}section{background:white;border:1px solid #dce4ed;border-radius:10px;padding:18px;margin:14px 0}.cards{display:flex;gap:12px;flex-wrap:wrap}.card{background:#f6f8fb;border:1px solid #e1e7ef;border-radius:8px;padding:14px;min-width:170px}.card b{font-size:24px;display:block}.muted{color:#62738a;font-size:13px}.bar{height:10px;background:#e7edf4;border-radius:9px;overflow:hidden;margin:10px 0}.bar i{display:block;height:100%;background:#2670cf}table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;padding:8px;border-bottom:1px solid #e7edf3;vertical-align:top}th{background:#f4f7fa;position:sticky;top:0}.scroll{overflow:auto;max-height:560px;border:1px solid #e0e7ef;border-radius:7px}.pill{border-radius:15px;padding:3px 8px;background:#edf2f7}.complete{color:#11734e;background:#e5f5ec}.failed{color:#ad3038;background:#fdebed}.in.progress{color:#9a6100;background:#fff4dc}input{padding:9px;border:1px solid #ccd7e4;border-radius:6px;width:min(100%,400px);margin:0 0 10px}.reason{white-space:normal;min-width:230px}a{color:#1b64bb}.charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:20px}.chart{background:#fafbfd;border:1px solid #e4eaf1;border-radius:8px;padding:14px}.chart h3{margin:0 0 12px;font-size:16px}.chartrow{display:grid;grid-template-columns:minmax(95px,150px) 1fr 48px;align-items:center;gap:9px;margin:9px 0;font-size:13px}.chartlabel{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.track{height:14px;background:#e8eef5;border-radius:8px;overflow:hidden}.fill{height:100%;background:#3478c8;border-radius:8px}.fill.alt{background:#23a078}.fill.warn{background:#d99a2b}.fill.bad{background:#cf5961}.chartvalue{text-align:right;font-variant-numeric:tabular-nums}
</style><header><h1>ChatGPT Lead-Hunting Dashboard</h1><div>Private campaign progress, category-city coverage, batch timings, and failures</div></header><main><div class="muted" id="generated"></div><section id="summary"></section><section><h2>Progress charts</h2><div class="charts"><div class="chart" id="coverageChart"></div><div class="chart" id="categoryChart"></div><div class="chart" id="durationChart"></div></div></section><section><h2>Category × city progress</h2><input id="f" placeholder="Filter category, city, or status"><div class="scroll" id="matrix"></div></section><section><h2>Lead-hunt batches</h2><div class="scroll" id="batches"></div></section><div class="muted">Generated from private campaign checkpoints, query ledgers, run summaries, and query timestamps. A failed query is terminal processing and is shown separately from successful results.</div></main><script>const D=__DATA__;const esc=x=>String(x==null?'':x).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));const table=(rows,cols)=>'<table><thead><tr>'+cols.map(c=>'<th>'+c[1]+'</th>').join('')+'</tr></thead><tbody>'+rows.map(r=>'<tr>'+cols.map(c=>{let v=r[c[0]]||'';if(c[0]==='status')v='<span class="pill '+esc(r.status)+'">'+esc(r.status)+'</span>';return '<td>'+((c[0]==='status')?v:esc(r[c[0]]))+'</td>'}).join('')+'</tr>').join('')+'</tbody></table>';const tally=(rows,key)=>Object.entries(rows.reduce((a,r)=>{let k=String(r[key]||'unknown');a[k]=(a[k]||0)+1;return a},{})).sort((a,b)=>b[1]-a[1]);const chart=(id,title,rows)=>{let max=Math.max(1,...rows.map(x=>Number(x[1])||0));document.getElementById(id).innerHTML='<h3>'+esc(title)+'</h3>' +(rows.length?rows:[['No data',0]]).map((x,i)=>'<div class="chartrow"><span class="chartlabel" title="'+esc(x[0])+'">'+esc(x[0])+'</span><div class="track"><div class="fill '+(i===0?'alt':i===1?'warn':i===2?'bad':'')+'" style="width:'+Math.max(0,(Number(x[1])||0)*100/max)+'%"></div></div><span class="chartvalue">'+esc(x[1])+'</span></div>').join('')};let M=D.category_city||[],B=D.batches||[];chart('coverageChart','Category-city cells by state',tally(M,'status'));chart('categoryChart','Cells by category',tally(M,'category'));const durations=B.filter(r=>Number.isFinite(Number(r.duration_seconds))).sort((a,b)=>Number(b.duration_seconds)-Number(a.duration_seconds)).slice(0,8).map(r=>[String(r.category||r.run_key||'Batch'),Math.round(Number(r.duration_seconds)/60)]);chart('durationChart','Longest batch duration (minutes)',durations);document.getElementById('generated').textContent='Generated '+new Date(D.generated_at).toLocaleString()+' · '+D.source_revision;let C=D.campaign||{};document.getElementById('summary').innerHTML='<h2>Current campaign</h2><div class="cards"><div class="card"><b>'+esc(C.progress_pct||0)+'%</b><span>Overall category-city progress</span></div><div class="card"><b>'+esc(C.processed_pairs||0)+' / '+esc(C.total_pairs||0)+'</b><span>Terminal cells of '+esc(C.category_count||0)+' categories × '+esc(C.city_count||0)+' cities</span></div><div class="card"><b>'+esc(C.current_category_pct||0)+'%</b><span>Current category: '+esc((C.current_category||{}).business_type||'—')+'</span></div></div><div class="bar"><i style="width:'+Math.min(100,C.progress_pct||0)+'%"></i></div><p>'+esc(C.campaign_key||'No campaign checkpoint found')+' · '+esc(C.status||'')+' / '+esc(C.stage||'')+' · current run '+esc(C.current_run_key||'—')+'</p>'+(C.pause_reason?'<p><b>Pause reason:</b> '+esc(C.pause_reason)+'</p>':'');const MC=[['category','Category'],['city','City'],['status','State'],['query_status','Query state'],['failure_reason','Failure reason']];function draw(){let q=document.getElementById('f').value.toLowerCase();document.getElementById('matrix').innerHTML=table(M.filter(r=>Object.values(r).join(' ').toLowerCase().includes(q)),MC)}draw();document.getElementById('f').addEventListener('input',draw);document.getElementById('batches').innerHTML=table(B,[['run_key','Batch/run key'],['category','Category'],['cities','Cities'],['status','State'],['processed_queries','Processed queries'],['expected_queries','Expected queries'],['started_at','Started UTC'],['finished_at','Finished UTC'],['duration_seconds','Duration seconds'],['failure_reason','Failure reason']]);</script></html>"""
    return html.replace("__DATA__",packed)

def main():
    p=argparse.ArgumentParser();p.add_argument("--operations",type=Path,required=True);p.add_argument("--output",type=Path,required=True);a=p.parse_args()
    data=build(a.operations);a.output.mkdir(parents=True,exist_ok=True)
    (a.output/"snapshot.json").write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    (a.output/"index.html").write_text(render(data),encoding="utf-8")
    write_csv(a.output/"category_city_progress.csv",data["category_city"],["category_index","category","city","status","query_status","failure_reason"])
    write_csv(a.output/"lead_hunt_batches.csv",data["batches"],["run_key","category","cities","status","processed_queries","expected_queries","completed_count","failed_count","pending_count","attempt_count","failure_category","failure_details","source_run_ids","started_at","finished_at","duration_seconds","failure_reason"])

if __name__=="__main__": main()
