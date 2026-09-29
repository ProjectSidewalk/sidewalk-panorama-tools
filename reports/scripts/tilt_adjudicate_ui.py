"""Local web page for judging endpoint C's sheets (tilt_adjudicate.py), one or more batches at a time.

    python reports/scripts/tilt_adjudicate_ui.py --judge jon \\
        --out reports/data/2026-09-29-tilt-adjudication-jm --out reports/data/2026-09-30-tilt-adjudication-jm-b2
    # then open http://127.0.0.1:8765/

Every answer is written by the tool's own `record` subcommand (so its blind check, which refuses to run
while a key file sits outside sealed/, still gates each write), and each batch is opened only after the
tool's `next` passes that check. Besides the sheet images, the page reads only each batch's tasks.json (label
type and tags, the text already on the sheet), draw.json's |T| threshold, and the judge's own verdict file.
It never reads sealed/. Loopback only.
"""
import argparse
import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tilt_adjudicate as ta  # noqa: E402

TOOL = [sys.executable, os.path.join(HERE, 'tilt_adjudicate.py')]


class Batch:
    def __init__(self, out, judge):
        self.out, self.judge, self.name = out, judge, os.path.basename(os.path.normpath(out))
        with open(os.path.join(out, 'tasks.json'), encoding='utf-8') as f:
            self.tasks = {t: {'label_type': v['label_type'], 'tags': json.loads(v.get('tags') or '[]')}
                          for t, v in json.load(f).items()}
        self.order = sorted(self.tasks)                  # the order `next` walks
        try:
            with open(os.path.join(out, 'draw.json'), encoding='utf-8') as f:
                self.min_abs_t = json.load(f).get('min_abs_t_deg', ta.MIN_ABS_T_DEG)
        except OSError:
            self.min_abs_t = None

    def tool(self, cmd, *extra):
        p = subprocess.run(TOOL + [cmd, '--out', self.out, '--judge', self.judge] + list(extra),
                           capture_output=True, text=True)
        return p.returncode, (p.stderr or p.stdout).strip()

    def state(self):
        code, msg = self.tool('next')                    # the blind check, before anything is served
        if code != 0:
            return {'name': self.name, 'error': msg}
        return {'name': self.name, 'min_abs_t': self.min_abs_t, 'order': self.order, 'tasks': self.tasks,
                'verdicts': ta.load_verdicts(self.out, self.judge),
                'comments': ta.load_comments(self.out, self.judge)}


PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>Tilt adjudication</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#fafaf9;--fg:#1c1917;--muted:#78716c;--btn:#e7e5e4;--accent:#2563eb;--card:#fff;--line:#e7e5e4;--hl:#fef3c7}
@media (prefers-color-scheme:dark){:root{--bg:#1c1917;--fg:#f5f5f4;--muted:#a8a29e;--btn:#44403c;--accent:#60a5fa;--card:#292524;--line:#44403c;--hl:#422006}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
header{display:flex;gap:12px;align-items:center;padding:10px 16px;flex-wrap:wrap}
header b{font-size:16px}
.tab{font:600 14px system-ui;padding:6px 12px;border-radius:8px;background:var(--btn);color:var(--fg);border:0;cursor:pointer}
.tab.on{background:var(--accent);color:#fff}
main{padding:0 16px 16px;max-width:1500px;margin:0 auto}
.strip{display:flex;gap:3px;flex-wrap:wrap;justify-content:center;margin:0 0 8px}
.dot{font:600 11px system-ui;min-width:30px;height:24px;padding:0 3px;border-radius:5px;border:0;background:var(--btn);color:var(--muted);cursor:pointer}
.dot.done{background:var(--accent);color:#fff} .dot.cur{outline:3px solid var(--fg)} .dot.note{box-shadow:inset 0 -3px 0 #f59e0b}
.nav{display:flex;gap:8px;align-items:center;justify-content:center;flex-wrap:wrap;margin:4px 0 10px}
.pos{font-weight:600;text-align:center}
.task{display:flex;gap:16px;flex-wrap:wrap;align-items:flex-start;margin-bottom:10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px}
.find{flex:2 1 420px} .rules{flex:1 1 320px;font-size:14px}
.k{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.find h2{margin:2px 0 4px;font-size:24px} .find p{margin:0 0 6px}
.chips{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.chip{background:var(--hl);border-radius:999px;padding:2px 10px;font-weight:600}
.small{font-size:13px;color:var(--muted)}
.rules ol{margin:4px 0 0;padding-left:20px} .rules li{margin:2px 0}
.banner{background:var(--hl);border-radius:8px;padding:6px 12px;margin-bottom:8px;font-weight:600;text-align:center}
.sheet{text-align:center} img{max-width:100%;max-height:calc(100vh - 380px);min-height:220px;border-radius:4px}
.q{margin:8px 0;font-size:17px;text-align:center}
.btns{display:flex;gap:8px;justify-content:center;flex-wrap:wrap}
.btns button{font:600 18px system-ui;padding:10px 22px;border:0;border-radius:8px;background:var(--btn);color:var(--fg);cursor:pointer}
.btns button.tie{font-size:15px;padding:10px 14px}
.btns button:hover{outline:2px solid var(--accent)} .btns button.sel{outline:3px solid var(--accent)}
button.link{font:14px system-ui;padding:6px 10px;background:none;border:0;color:var(--accent);cursor:pointer}
button:disabled{opacity:.4;cursor:default}
.comment{max-width:900px;margin:10px auto 0;display:flex;gap:8px;align-items:flex-start}
textarea{flex:1;min-height:44px;font:14px system-ui;padding:6px 8px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg)}
#msg{color:var(--muted);min-height:1.4em;text-align:center;margin-top:6px}
</style></head><body>
<header><b>Tilt adjudication (#158)</b><span id="tabs"></span></header>
<main><div id="view"></div><div id="msg"></div></main>
<script>
const WHAT={
 CurbRamp:['a curb ramp','The ramp itself: the sloped section that joins the sidewalk to the street.'],
 NoCurbRamp:['a missing curb ramp','The spot that should have a curb ramp but does not: usually a raised curb edge where people would cross.'],
 SurfaceProblem:['a surface problem','The damaged or uneven walking surface itself.'],
 Obstacle:['an obstacle in the path','The object that blocks the walking path.']};
const CHOICES=['A','B','C','A=B','A=C','B=C','none'];
const KEYS={a:'A',b:'B',c:'C','1':'A=B','2':'A=C','3':'B=C',n:'none'};
let B=[], bi=0, i=0, busy=false;
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const label=c=>c==='none'?'None':c;
const cur=()=>B[bi];
async function load(){B=await (await fetch('/all')).json();
 bi=Math.max(0,B.findIndex(b=>!b.error&&b.order.some(t=>!(t in b.verdicts))));
 const b=cur(); if(b.error){render();return}
 const u=b.order.findIndex(t=>!(t in b.verdicts)); i=u<0?0:u; render()}
function nextWhere(pred){const b=cur(),n=b.order.length;for(let k=1;k<=n;k++){const j=(i+k)%n;if(pred(b.order[j]))return j}return -1}
function go(j){const b=cur();if(!b||b.error||j<0||j>=b.order.length)return;i=j;document.getElementById('msg').textContent='';render()}
function tab(k){bi=k;const b=cur();if(b.error){render();return}const u=b.order.findIndex(t=>!(t in b.verdicts));i=u<0?0:u;render()}
function render(){
 document.getElementById('tabs').innerHTML=B.map((b,k)=>'<button class="tab'+(k===bi?' on':'')+'" onclick="tab('+k+')">Batch '+(k+1)+
   (b.min_abs_t!=null?' (tilt &ge; '+b.min_abs_t+'&deg;)':'')+(b.error?' - error':' - '+Object.keys(b.verdicts).length+'/'+b.order.length)+'</button>').join(' ');
 const v=document.getElementById('view'), b=cur();
 if(b.error){v.innerHTML='<p>'+esc(b.error)+'</p>';return}
 const n=b.order.length, done=Object.keys(b.verdicts).length, t=b.order[i], task=b.tasks[t], prev=b.verdicts[t];
 const w=WHAT[task.label_type]||[task.label_type,''];
 const strip='<div class="strip">'+b.order.map((tk,j)=>{const a=b.verdicts[tk];
   return '<button class="dot'+(a?' done':'')+(j===i?' cur':'')+(b.comments[tk]?' note':'')+'" title="Sheet '+(j+1)+(a?': '+label(a):': not answered')+(b.comments[tk]?' (note)':'')+'" onclick="go('+j+')">'+(a?(a==='none'?'&ndash;':a):(j+1))+'</button>'}).join('')+'</div>';
 const nu=nextWhere(tk=>!(tk in b.verdicts)), nn=nextWhere(tk=>b.verdicts[tk]==='none');
 const nav='<div class="nav"><button class="link" onclick="go(i-1)"'+(i?'':' disabled')+'>&larr; Prev</button>'+
  '<span class="pos">Sheet '+(i+1)+' of '+n+' &middot; '+(prev?'your answer: '+label(prev):'not answered')+'</span>'+
  '<button class="link" onclick="go(i+1)"'+(i<n-1?'':' disabled')+'>Next &rarr;</button>'+
  (nu>=0&&nu!==i?'<button class="link" onclick="go('+nu+')">Next unanswered ('+(nu+1)+')</button>':'')+
  (nn>=0&&nn!==i?'<button class="link" onclick="go('+nn+')">Next None ('+(nn+1)+')</button>':'')+'</div>';
 const tags=(task.tags||[]).length?'<div class="chips"><span class="small">Labeller\'s tags:</span>'+task.tags.map(x=>'<span class="chip">'+esc(x)+'</span>').join('')+'</div><div class="small">Tags describe the label. Not every tag is a visible object (e.g. "points into traffic"), so use the ones that name a thing to help find it.</div>'
  :'<div class="small">No tags on this label.</div>';
 const banner=done===n?'<div class="banner">All '+n+' in this batch answered. You can still review and change any answer. Stay blind until Claude has committed them.</div>':'';
 v.innerHTML=banner+strip+nav+
 '<div class="task"><div class="card find"><div class="k">Find</div><h2>'+esc(w[0])+'</h2><p>'+esc(w[1])+'</p>'+tags+'</div>'+
 '<div class="card rules"><b>How to decide</b><ol>'+
 '<li>The three panels show the same spot cut at different heights, each ring at its panel\'s centre.</li>'+
 '<li>Pick the ring <b>closest to where you would have marked '+esc(w[0])+'</b>. It need not land exactly on it.</li>'+
 '<li>Two rings about equally close, and closer than the third: pick the <b>tie</b> (A=B, A=C, B=C).</li>'+
 '<li><b>None</b> only if all three rings are clearly off it.</li>'+
 '<li>Judge only where each ring falls on the feature, not which panel looks "original".</li></ol>'+
 '<div class="small" style="margin-top:6px">Keys: A B C &middot; 1 = A=B, 2 = A=C, 3 = B=C &middot; N = None &middot; &larr; &rarr; move. Answering moves on.</div></div></div>'+
 '<div class="sheet"><img src="/sheet/'+bi+'/'+t+'.jpg" alt="three panels A, B, C"></div>'+
 '<p class="q">Which ring is closest to <b>'+esc(w[0])+'</b>?</p>'+
 '<div class="btns">'+CHOICES.map(c=>'<button class="'+(c.includes('=')?'tie ':'')+(prev===c?'sel':'')+'" onclick="rec(\''+c+'\')">'+(c==='none'?'None (N)':c)+'</button>').join('')+'</div>'+
 '<div class="comment"><textarea id="note" placeholder="Optional note on this sheet (saved with your answer)">'+esc(b.comments[t]||'')+'</textarea>'+
 '<button class="link" onclick="saveNote()"'+(prev?'':' disabled title="answer the sheet first"')+'>Save note</button></div>';}
async function post(t,c,note){const r=await (await fetch('/record',{method:'POST',body:JSON.stringify({batch:bi,token:t,choice:c,comment:note})})).json();
 if(r.ok){cur().verdicts=r.verdicts;cur().comments=r.comments}return r}
async function rec(c){const b=cur();if(!b||b.error||busy)return;busy=true;const t=b.order[i],j=i,note=document.getElementById('note').value;
 try{const r=await post(t,c,note);
  document.getElementById('msg').textContent=r.ok?('Recorded '+label(c)+' for sheet '+(j+1)+(note.trim()?' with your note':'')+'.'):('Error: '+r.error);
  if(r.ok&&i===j&&j<b.order.length-1)i=j+1;render()}
 finally{busy=false}}
async function saveNote(){const b=cur(),t=b.order[i];if(!b.verdicts[t]||busy)return;busy=true;
 try{const r=await post(t,b.verdicts[t],document.getElementById('note').value);
  document.getElementById('msg').textContent=r.ok?'Note saved for sheet '+(i+1)+'.':'Error: '+r.error;render()}
 finally{busy=false}}
document.addEventListener('keydown',e=>{if(e.ctrlKey||e.metaKey||e.altKey||e.target.tagName==='TEXTAREA')return;
 const k=e.key.toLowerCase();if(KEYS[k])rec(KEYS[k]);else if(e.key==='ArrowLeft')go(i-1);else if(e.key==='ArrowRight')go(i+1)});
load();
</script></body></html>"""


def make_handler(batches):
    class H(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype='application/json'):
            data = body if isinstance(body, bytes) else body.encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == '/':
                return self._send(200, PAGE, 'text/html; charset=utf-8')
            if path == '/all':
                return self._send(200, json.dumps([b.state() for b in batches]))
            parts = path.split('/')
            if len(parts) == 4 and parts[1] == 'sheet' and parts[2].isdigit() and int(parts[2]) < len(batches):
                b, name = batches[int(parts[2])], parts[3]
                if name.endswith('.jpg') and name[:-4] in b.tasks:
                    with open(os.path.join(b.out, 'sheets', name), 'rb') as fh:
                        return self._send(200, fh.read(), 'image/jpeg')
            self._send(404, 'no', 'text/plain')

        def do_POST(self):
            if urlparse(self.path).path != '/record':
                return self._send(404, 'no', 'text/plain')
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
            k, token, choice = body.get('batch'), str(body.get('token', '')), str(body.get('choice', ''))
            if not (isinstance(k, int) and 0 <= k < len(batches)) or token not in batches[k].tasks \
                    or choice not in ta.CHOICES:
                return self._send(400, json.dumps({'ok': False, 'error': 'bad input'}))
            b = batches[k]
            extra = [token, choice]
            note = str(body.get('comment') or '').strip()
            if note:
                extra += ['--comment', note]
            code, msg = b.tool('record', *extra)
            self._send(200, json.dumps({'ok': code == 0, 'error': msg,
                                        'verdicts': ta.load_verdicts(b.out, b.judge),
                                        'comments': ta.load_comments(b.out, b.judge)}))

        def log_message(self, *a):
            pass
    return H


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--out', action='append', required=True, help='an adjudication folder; repeat for batches')
    ap.add_argument('--judge', required=True, type=ta.normalise_judge)
    ap.add_argument('--port', type=int, default=8765)
    args = ap.parse_args(argv)
    batches = [Batch(o, args.judge) for o in args.out]
    print('Tilt adjudication: http://127.0.0.1:%d/  (%s)' % (args.port, ', '.join(b.name for b in batches)), flush=True)
    ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(batches)).serve_forever()


if __name__ == '__main__':
    main()
