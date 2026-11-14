#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""djisync web UI - FastAPI on 127.0.0.1:8775, served at /djisync via tailscale.
Reads pocket4p.db, runs sync commands, generates thumbnails. Read-first design.
"""
import os, re, sys, json, sqlite3, subprocess, hashlib, threading, html
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response, RedirectResponse
import uvicorn

DB_PATH = r"D:\pocket4p\pocket4p.db"
SCRIPT  = r"D:\pocket4p\scripts\pocket4p_sync.py"
BACKUP  = Path(r"G:\pocket4p_back\back_up")
WORK    = Path(r"D:\pocket4p\DCIM")
MIC     = Path(r"D:\pocket4p\mic")
THUMBS  = Path(r"D:\pocket4p\thumbs")
PY = sys.executable
BASE_URL = "/djisync"

app = FastAPI()
_proc = None          # running sync subprocess
_proc_name = None
_proc_out = []
_lock = threading.Lock()

def db():
    c = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c

# ---------------- sync runner ----------------
def run_sync(cmd: str, extra: list[str] | None = None):
    global _proc, _proc_name, _proc_out
    with _lock:
        if _proc and _proc.poll() is None:
            return {"ok": False, "msg": f"{_proc_name} still running"}
        args = [PY, "-X", "utf8", SCRIPT, cmd] + (extra or [])
        _proc_out = [f"$ {' '.join(args)}"]
        _proc_name = cmd
        _proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, encoding="utf-8", errors="replace",
                                 cwd=os.path.dirname(SCRIPT))
    # drain in background thread
    def drain():
        for line in _proc.stdout:
            _proc_out.append(line.rstrip())
    threading.Thread(target=drain, daemon=True).start()
    return {"ok": True, "msg": f"{cmd} started"}

# NOTE: run_sync uses sys.executable directly; the weird args line above is a no-op.

# ---------------- routes ----------------
# tailscale serve --set-path=/djisync STRIPS the prefix before proxying,
# so the app must also answer on "/" (tailnet path) while keeping /djisync (direct).

@app.get(BASE_URL + "/", response_class=HTMLResponse)
@app.get(BASE_URL, response_class=HTMLResponse)
@app.get("/", response_class=HTMLResponse)
def index():
    dates = []
    try:
        c = db()
        for r in c.execute("""SELECT shot_date d, count(*) n,
                sum(backup_status='verified') v,
                sum(mimo_uploaded='no') mimo_no,
                sum(CASE WHEN type='DNG' THEN 1 ELSE 0 END) dng,
                sum(CASE WHEN type='JPG' THEN 1 ELSE 0 END) jpg
                FROM files GROUP BY shot_date ORDER BY d DESC"""):
            dates.append(dict(r))
        c.close()
    except Exception as e:
        return HTMLResponse(f"<h1>db error</h1><pre>{html.escape(str(e))}</pre>")
    rows = "".join(f"""
      <tr onclick="openDate('{r['d']}')" class="drow">
        <td>{r['d']}</td><td>{r['n']}</td><td>{r['v']}</td>
        <td>{'✓' if r['n']==r['v'] else '⚠'}</td>
        <td>{r['jpg']}+{r['dng']} raw</td>
        <td>{r['mimo_no']} skipped-by-mimo</td>
      </tr>""" for r in dates)
    proc_html = f"""
    <div class="card"><h2>Sync</h2>
      <div class="btns">
        <button onclick="sync('usb')">📷 USB sync</button>
        <button onclick="sync('refile')">📥 Sort auto-upload inbox</button>
        <button onclick="sync('mic')">🎤 Mic sync</button>
        <button onclick="sync('report')">📊 Report</button>
      </div>
      <pre id="proc" style="display:none"></pre>
    </div>"""
    return HTMLResponse(f"""<!doctype html><html><head><meta charset=utf-8>
    <title>djisync</title><style>
    body{{font:14px system-ui;margin:24px;background:#111;color:#ddd}}
    table{{border-collapse:collapse;margin-top:12px}}
    td,th{{padding:6px 14px;border-bottom:1px solid #333;text-align:left}}
    th{{color:#888;font-size:12px}}
    .drow{{cursor:pointer}} .drow:hover{{background:#1c2733}}
    .card{{background:#181e26;border-radius:8px;padding:14px 18px;max-width:760px}}
    button{{background:#2563eb;color:#fff;border:0;border-radius:6px;padding:8px 14px;margin:2px;cursor:pointer}}
    button:disabled{{background:#555}}
    pre{{background:#0b0f14;padding:10px;max-height:300px;overflow:auto;font-size:12px}}
    .grid{{display:flex;flex-wrap:wrap;gap:8px}}
    .tile{{width:180px;background:#181e26;border-radius:6px;padding:6px;font-size:11px}}
    .tile img{{width:168px;height:110px;object-fit:cover;border-radius:4px;background:#222}}
    .badges span{{display:inline-block;background:#2a3441;border-radius:4px;padding:1px 6px;margin:1px;font-size:10px}}
    a{{color:#7ab8ff}}
    </style></head><body>
    <h1>djisync <span style="color:#666;font-size:13px">DJI Pocket 4P manager</span></h1>
    {proc_html}
    <div class="card"><h2>Library (by shoot date)</h2>
    <table><tr><th>date</th><th>files</th><th>verified</th><th>ok</th><th>jpg+raw</th><th>mimo skipped</th></tr>
    {rows}</table></div>
    <div class="card" id="detail" style="display:none"><h2 id="dtitle"></h2><div class="grid" id="dgrid"></div></div>
    <script>
    async function sync(cmd){{
      const r = await (await fetch('{BASE_URL}/api/sync?cmd='+cmd)).json();
      if(!r.ok) alert(r.msg);
      poll();
    }}
    async function poll(){{
      const r = await (await fetch('{BASE_URL}/api/proc')).json();
      const p = document.getElementById('proc');
      if(r.running||r.out.length){{
        p.style.display='block';
        p.textContent = r.out.join('\\n');
        p.scrollTop = p.scrollHeight;
      }}
      document.querySelectorAll('button').forEach(b=>b.disabled=r.running);
      if(r.running) setTimeout(poll, 1500);
      else if(r.out.length && !window._reloaded) {{ window._reloaded = true; setTimeout(()=>location.reload(), 4000); }}
    }}
    poll();
    async function openDate(d){{
      document.getElementById('detail').style.display='block';
      document.getElementById('dtitle').textContent = d;
      const r = await (await fetch('{BASE_URL}/api/date?d='+d)).json();
      document.getElementById('dgrid').innerHTML = r.files.map(f=>{{
        const img = f.thumb ? `<img loading="lazy" src="${{f.thumb}}">` : `<div style="width:168px;height:110px;background:#222;border-radius:4px;display:flex;align-items:center;justify-content:center;color:#666">${{f.type}}</div>`;
        return `<div class="tile">${{img}}<div style="margin-top:4px;word-break:break-all">${{f.name}}</div>
          <div class="badges"><span>${{f.type}}</span><span>${{f.status}}</span><span>${{f.mimo}}</span></div></div>`;
      }}).join('');
      document.getElementById('detail').scrollIntoView({{behavior:'smooth'}});
    }}
    </script></body></html>""")

@app.get(BASE_URL + "/api/date")
@app.get("/api/date")
def api_date(d: str):
    if not re.fullmatch(r"\d{8}", d): return JSONResponse({"files": []})
    out = []
    c = db()
    for r in c.execute("SELECT name,size,type,backup_status,mimo_uploaded,dest_backup FROM files WHERE shot_date=? ORDER BY name", (d,)):
        f = dict(r)
        f["status"] = f["backup_status"]
        thumb = None
        ext = f["name"].rsplit(".", 1)[-1].upper()
        if f["dest_backup"] and ext == "JPG" and Path(f["dest_backup"]).exists():
            thumb = f"{BASE_URL}/file?p={f['dest_backup'].replace(chr(92), '/')}"
        f["thumb"] = thumb
        out.append(f)
    c.close()
    return JSONResponse({"files": out})

@app.get(BASE_URL + "/api/sync")
@app.get("/api/sync")
def api_sync(cmd: str):
    if cmd not in ("usb", "refile", "mic", "report", "rescan"):
        return JSONResponse({"ok": False, "msg": "bad cmd"})
    return JSONResponse(run_sync(cmd))

@app.get(BASE_URL + "/api/proc")
@app.get("/api/proc")
def api_proc():
    with _lock:
        running = bool(_proc and _proc.poll() is None)
        return JSONResponse({"running": running, "name": _proc_name,
                             "out": _proc_out[-120:], "done": None if running else (_proc.returncode if _proc else None)})

@app.get(BASE_URL + "/thumbs/{name}")
@app.get("/thumbs/{name}")
def thumbs(name: str):
    if not re.fullmatch(r"[0-9a-f]{16}\.jpg", name): return Response(status_code=404)
    p = THUMBS / name
    if not p.exists(): return Response(status_code=404)
    return FileResponse(p, media_type="image/jpeg")

@app.get(BASE_URL + "/file")
@app.get("/file")
def file(p: str):
    """Serve a real file (read-only allowlist: backup + working + mic roots)."""
    real = Path(p)
    roots = [BACKUP, WORK, MIC]
    rp = real.resolve()
    if not any(str(rp).lower().startswith(str(r.resolve()).lower()) for r in roots):
        return Response(status_code=403)
    if not rp.exists(): return Response(status_code=404)
    media = "image/jpeg" if rp.suffix.lower() in (".jpg", ".jpeg") else (
            "video/mp4" if rp.suffix.lower() == ".mp4" else "application/octet-stream")
    return FileResponse(rp, media_type=media)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8775, log_level="warning")
