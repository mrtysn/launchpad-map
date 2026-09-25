#!/usr/bin/env python3
"""Watch a `phone write` run in the browser, and pause, resume or stop it.

A small HTTP server on localhost serves one page that polls the driver's
progress file (layouts/phone/.write-log/progress.json) and the tail of its
run log every second, and turns the buttons into the control file the driver
reads between operations. Nothing leaves the machine; the page is only
reachable from this Mac. Stop the server with Ctrl-C; the run does not need it.
"""

import argparse
import http.server
import json
import os
import subprocess
import sys
import urllib.parse
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import phone_write as pw  # noqa: E402

PAGE = r"""<!doctype html>
<meta charset="utf-8">
<title>phone write</title>
<style>
:root { --bg:#111; --fg:#eee; --dim:#888; --line:#2a2a2a; --ok:#3fb950; --run:#58a6ff; --bad:#f85149; --pause:#d29922; }
body { margin:0; padding:20px 16px; background:var(--bg); color:var(--fg); font:15px/1.45 -apple-system, system-ui, sans-serif; }
h1 { font-size:18px; margin:0 0 4px; }
.sub { color:var(--dim); margin-bottom:12px; }
.bar { height:8px; background:var(--line); border-radius:4px; overflow:hidden; margin:10px 0; }
.bar i { display:block; height:100%; background:var(--ok); width:0; transition:width .4s; }
.controls { display:flex; gap:8px; margin:12px 0 18px; flex-wrap:wrap; align-items:center; }
button { font:inherit; padding:8px 16px; border-radius:8px; border:1px solid var(--line); background:#1b1b1b; color:var(--fg); cursor:pointer; }
button:disabled { opacity:.4; cursor:default; }
button.stop { border-color:var(--bad); }
.status { padding:4px 10px; border-radius:6px; background:var(--line); }
.status.running { color:var(--run); } .status.paused { color:var(--pause); } .status.failed, .status.stopped { color:var(--bad); } .status.done { color:var(--ok); }
ol { list-style:none; padding:0; margin:0; }
li { padding:6px 8px; border-bottom:1px solid var(--line); display:grid; grid-template-columns:2.2em 1fr auto; gap:8px; }
li .n { color:var(--dim); } li .t small { display:block; color:var(--dim); }
li.running { background:#16202c; } li.failed { background:#2a1616; }
li .s { color:var(--dim); white-space:nowrap; }
li.done .s { color:var(--ok); } li.running .s { color:var(--run); } li.failed .s { color:var(--bad); }
pre { background:#0a0a0a; border:1px solid var(--line); border-radius:8px; padding:10px; font-size:12px; max-height:16em; overflow:auto; white-space:pre-wrap; }
.err { color:var(--bad); margin:8px 0; }
img.frame { max-width:200px; float:right; margin:0 0 12px 12px; border-radius:8px; border:1px solid var(--line); }
@media (max-width:600px) { img.frame { float:none; display:block; margin:0 auto 12px; } }
</style>
<h1 id="title">phone write</h1>
<div class="sub" id="sub"></div>
<div class="bar"><i id="bar"></i></div>
<div class="controls">
  <span class="status" id="status">–</span>
  <button id="pause">Pause after this operation</button>
  <button id="resume">Resume</button>
  <button id="stop" class="stop">Stop after this operation</button>
  <button id="start">Continue the run</button>
  <span id="hint" style="color:var(--dim)"></span>
</div>
<div class="err" id="err"></div>
<img class="frame" id="frame" hidden alt="">
<ol id="ops"></ol>
<h2 style="font-size:15px;margin:18px 0 6px">log</h2>
<pre id="log"></pre>
<script>
const $ = id => document.getElementById(id);
let doc = null, scrolledTo = 0;
function fmt(s) { s = Math.round(s); return s >= 60 ? Math.floor(s/60) + 'm ' + (s%60) + 's' : s + 's'; }
function render() {
  if (!doc) return;
  const ops = doc.ops, done = ops.filter(o => o.state === 'done').length;
  $('title').textContent = 'phone write: ' + (doc.proposal || '');
  $('bar').style.width = (100 * done / ops.length) + '%';
  const st = $('status'); st.textContent = doc.status; st.className = 'status ' + doc.status;
  const running = doc.alive && (doc.status === 'running' || doc.status === 'paused');
  $('pause').disabled = !(doc.alive && doc.status === 'running'); $('resume').disabled = !(doc.alive && doc.status === 'paused'); $('stop').disabled = !running;
  $('start').hidden = running || !doc.path; $('start').disabled = !!doc.starting;
  if (!doc.alive && (doc.status === 'running' || doc.status === 'paused')) st.textContent = 'process gone';
  $('err').textContent = doc.error || '';
  const cur = ops.find(o => o.state === 'running');
  const since = cur && doc.updated ? (Date.now() - Date.parse(doc.updated)) / 1000 : 0;
  const spent = ops.reduce((a, o) => a + (o.secs || 0), 0) + since;
  const avg = done ? ops.filter(o => o.state === 'done').reduce((a, o) => a + o.secs, 0) / done : 0;
  const left = avg * (ops.length - done - (cur ? 1 : 0)) + (cur ? Math.max(0, avg - since) : 0);
  $('sub').textContent = done + ' of ' + ops.length + ' operations · ' + fmt(spent) + ' spent' + (running && avg ? ' · about ' + fmt(left) + ' left' : '') + ' · started ' + (doc.started || '').replace('T', ' ');
  const list = $('ops');
  if (list.children.length !== ops.length) {
    list.innerHTML = ops.map(o => '<li><span class="n">' + o.i + '</span><span class="t">' + esc(o.text) + (o.apps.length ? '<small>' + esc(o.apps.join(', ')) + '</small>' : '') + '</span><span class="s"></span></li>').join('');
    scrolledTo = 0;
  }
  ops.forEach((o, k) => {
    const li = list.children[k];
    if (li.className !== o.state) li.className = o.state;
    const txt = o.state === 'done' ? fmt(o.secs) + ' · ' + o.dumps + ' dumps' : o.state === 'running' ? fmt(since) + '…' : o.state === 'failed' ? 'failed after ' + fmt(o.secs) : '';
    const sp = li.lastElementChild; if (sp.textContent !== txt) sp.textContent = txt;
  });
  if (cur && cur.i !== scrolledTo) { scrolledTo = cur.i; list.children[cur.i - 1].scrollIntoView({block: 'center', behavior: 'smooth'}); }
  const last = [...ops].reverse().find(o => o.state === 'done');
  if (last) { $('frame').hidden = false; $('frame').src = '/frame/' + last.i + '?t=' + last.secs; $('frame').onerror = () => { $('frame').hidden = true; }; }
}
function esc(s) { return s.replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
async function poll() {
  try { const r = await fetch('/progress'); doc = r.ok ? await r.json() : null; if (!doc) $('status').textContent = 'no run'; } catch (e) { $('status').textContent = 'server gone'; }
  try { const r = await fetch('/log'); const pre = $('log'); const atEnd = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 4; pre.textContent = await r.text(); if (atEnd) pre.scrollTop = pre.scrollHeight; } catch (e) {}
  render();
}
async function tell(what) { const r = await fetch('/' + what, {method: 'POST'}); const t = await r.text(); $('hint').textContent = what === 'resume' ? '' : what === 'start' ? t : 'takes effect when the current operation finishes'; poll(); }
$('pause').onclick = () => tell('pause'); $('resume').onclick = () => tell('resume'); $('stop').onclick = () => tell('stop'); $('start').onclick = () => tell('start');
poll(); setInterval(poll, 1000);
</script>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body if isinstance(body, bytes) else body.encode())

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            return self.send(200, PAGE, "text/html; charset=utf-8")
        if path == "/progress":
            if not os.path.exists(pw.PROGRESS):
                return self.send(404, "no run")
            with open(pw.PROGRESS) as fh:
                doc = json.load(fh)
            doc["alive"] = alive(doc.get("pid"))
            doc["starting"] = CHILD is not None and CHILD.poll() is None and not doc["alive"]
            return self.send(200, json.dumps(doc), "application/json")
        if path == "/log":
            if not os.path.exists(pw.RUN_LOG):
                return self.send(200, "")
            with open(pw.RUN_LOG, "rb") as fh:
                fh.seek(0, 2)
                fh.seek(max(0, fh.tell() - 12000))
                return self.send(200, fh.read().decode(errors="replace").split("\n", 1)[-1])
        if path.startswith("/frame/"):
            frame = os.path.join(pw.LOG, f"op{int(path[7:]):02}.png")
            if not os.path.exists(frame):
                return self.send(404, "no frame")
            with open(frame, "rb") as fh:
                return self.send(200, fh.read(), "image/png")
        self.send(404, "not found")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/pause", "/stop"):
            with open(pw.CONTROL, "w") as fh:
                fh.write(path[1:])
            return self.send(200, "ok")
        if path == "/resume":
            if os.path.exists(pw.CONTROL):
                os.remove(pw.CONTROL)
            return self.send(200, "ok")
        if path == "/start":
            return self.send(200, start())
        self.send(404, "not found")


CHILD = None


def alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def start() -> str:
    """Run `phone write` again on the proposal of the last run, from the
    driver's record, as a child of this server. One at a time."""
    global CHILD
    if not os.path.exists(pw.PROGRESS):
        return "no earlier run to continue"
    with open(pw.PROGRESS) as fh:
        doc = json.load(fh)
    if alive(doc.get("pid")) or (CHILD is not None and CHILD.poll() is None):
        return "a run is already going"
    if not doc.get("path") or not os.path.exists(doc["path"]):
        return "the last run's proposal file is gone"
    if os.path.exists(pw.CONTROL):
        os.remove(pw.CONTROL)
    cmd = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "phone_write.py"), doc["path"]]
    if doc.get("serial"):
        cmd += ["--serial", doc["serial"]]
    CHILD = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=pw.ph.REPO)
    return f"started: {os.path.basename(doc['path'])}"


def main():
    ap = argparse.ArgumentParser(prog="launchpad-map phone watch", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=7822)
    ap.add_argument("--open", action="store_true", help="also open the page in the default browser")
    args = ap.parse_args()
    os.makedirs(pw.LOG, exist_ok=True)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(url)
    if args.open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
