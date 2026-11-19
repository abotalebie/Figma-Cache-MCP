import argparse
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import tokens
import usage

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / 'cache'
VECTOR_TYPES = {'VECTOR', 'BOOLEAN_OPERATION', 'STAR', 'LINE', 'ELLIPSE', 'REGULAR_POLYGON'}
KEY_RE = re.compile(r'^[A-Za-z0-9]+$')
NODE_RE = re.compile(r'^[0-9]+:[0-9]+$')
ASSET_RE = re.compile(r'^[A-Za-z0-9%.;_-]+\.(svg|png|jpg|jpeg|gif|webp|bin)$')

JOBS = {}
JOB_LOCK = threading.Lock()


def detect_wsl_distro():
    try:
        if 'microsoft' not in Path('/proc/version').read_text().lower():
            return None
    except OSError:
        return None
    name = os.environ.get('WSL_DISTRO_NAME')
    if name:
        return name
    try:
        out = subprocess.run(['wsl.exe', '-l', '-q'], capture_output=True, timeout=5).stdout
        lines = [line.strip() for line in out.decode('utf-16-le', errors='ignore').splitlines() if line.strip()]
        if lines:
            return lines[0]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return 'Ubuntu'


WSL_DISTRO = detect_wsl_distro()

WSL_BLOCK = '''<h3>Windows client (WSL)</h3>
<p>If the client runs on Windows while this server runs inside WSL, use this configuration instead of the one above:</p>
<pre class="code" id="cfg-win">{
  "mcpServers": {
    "figma-cache": {
      "command": "wsl.exe",
      "args": ["-d", "{{DISTRO}}", "--", "{{ROOT}}/run-mcp.sh"]
    }
  }
}</pre>
<button class="ghost copy" onclick="copyText('cfg-win')">Copy Windows config</button>
<ul>
<li><b>Claude Desktop (Windows):</b> <code>%APPDATA%\\Claude\\claude_desktop_config.json</code></li>
<li><b>Cursor (Windows):</b> <code>%USERPROFILE%\\.cursor\\mcp.json</code></li>
</ul>
<p>The <code>{{DISTRO}}</code> name must match the output of <code>wsl -l -q</code> in PowerShell; if it does not, fix the <code>args</code> array.</p>'''


def iter_nodes(root):
    stack = [root]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            yield node
            stack.extend(node.get('children', []))


def parse_input(text):
    text = text.strip()
    if text.startswith('http'):
        link = urlsplit(text)
        if link.scheme != 'https' or link.hostname not in ('figma.com', 'www.figma.com'):
            return None, None
        match = re.match(r'^/(?:design|file)/([A-Za-z0-9]+)(?:/|$)', link.path)
        if not match:
            return None, None
        ids = parse_qs(link.query).get('node-id', [])
        node_id = ids[0].replace('-', ':') if len(ids) == 1 else None
        return match[1], node_id
    if KEY_RE.fullmatch(text):
        return text, None
    return None, None


def build_status():
    files = {}
    for path in sorted(CACHE.glob('*__*.json')):
        if '__' not in path.stem:
            continue
        key_q, node_q = path.stem.split('__', 1)
        try:
            cached = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        document = (cached.get('node') or {}).get('document')
        if not document:
            continue
        key, node_id = unquote(key_q), unquote(node_q)
        entry = files.setdefault(key, {
            'file_key': key, 'name': None, 'full': None, 'frames': [],
            'svg': None, 'images': None, 'usage': None,
        })
        nodes = list(iter_nodes(document))
        info = {
            'node_id': node_id,
            'name': cached.get('name') or document.get('name'),
            'synced_at': cached.get('synced_at'),
            'nodes': len(nodes),
            'vectors': sum(1 for n in nodes if n.get('type') in VECTOR_TYPES),
        }
        if node_id == '0:0':
            refs = set()
            for n in nodes:
                for fill in n.get('fills', []):
                    if isinstance(fill, dict) and fill.get('type') == 'IMAGE' and isinstance(fill.get('imageRef'), str):
                        refs.add(fill['imageRef'])
            info['image_refs'] = sorted(refs)
            entry['full'] = info
            entry['name'] = info['name']
        else:
            entry['frames'].append(info)
            entry['name'] = entry['name'] or info['name']

    usage_data = usage.load()
    for key, entry in files.items():
        manifest_path = CACHE / 'svg' / key / 'manifest.json'
        try:
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            names = sorted(set(manifest.get('map', {}).values()))
            entry['svg'] = {
                'synced_at': manifest.get('synced_at'),
                'icons': len(names),
                'covered_nodes': len(manifest.get('map', {})),
                'missing': manifest.get('missing', []),
                'preview': names[:24],
            }
        except (OSError, ValueError):
            pass
        img_dir = CACHE / 'images' / key
        local = sorted(p.name for p in img_dir.iterdir() if p.is_file() and not p.name.endswith('.tmp')) if img_dir.is_dir() else []
        try:
            links = json.loads((CACHE / 'image-fills' / f'{key}.json').read_text(encoding='utf-8'))['meta']['images']
            link_count = len(links) if isinstance(links, dict) else 0
        except (OSError, ValueError, KeyError, TypeError):
            link_count = 0
        missing_refs = None
        if entry['full']:
            local_refs = {Path(n).stem for n in local}
            missing_refs = [r for r in entry['full']['image_refs'] if r not in local_refs]
        entry['images'] = {'local': len(local), 'links': link_count, 'missing_refs': missing_refs}
        entry['usage'] = usage_data.get(key, {'tier1': 0, 'tier2': 0, 'history': []})
    return {'token_set': bool(tokens.resolve()),
            'files': sorted(files.values(), key=lambda f: (f['name'] or f['file_key']).lower())}


def start_job(cmd, token):
    job = {'id': datetime.now(timezone.utc).strftime('%H%M%S%f'), 'lines': [], 'done': False, 'code': None,
           'cmd': ' '.join(cmd[1:])}
    JOBS[job['id']] = job

    def run():
        try:
            env = os.environ.copy()
            env['FIGMA_TOKEN'] = token
            env['PYTHONUNBUFFERED'] = '1'
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, cwd=ROOT, env=env)
            for line in proc.stdout:
                job['lines'].append(line.rstrip())
            proc.wait()
            job['code'] = proc.returncode
        except OSError as error:
            job['lines'].append(str(error))
            job['code'] = -1
        finally:
            job['done'] = True

    threading.Thread(target=run, daemon=True).start()
    return job


PAGE = '''<!DOCTYPE html>
<html lang="en" dir="ltr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Figma Cache Panel</title>
<style>
:root { --bg:#f4f5f7; --card:#fff; --line:#e3e5e8; --txt:#1f2430; --mut:#6b7280; --acc:#2563eb; --ok:#16a34a; --warn:#d97706; --err:#dc2626; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--txt); font:14px/1.7 -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
.wrap { max-width:1000px; margin:0 auto; padding:24px 16px 60px; }
h1 { font-size:20px; margin:0 0 4px; }
.sub { color:var(--mut); margin:0 0 20px; font-size:12px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; margin-bottom:14px; }
.banner { border-radius:8px; padding:10px 14px; margin-bottom:14px; font-size:13px; }
.banner.err { background:#fef2f2; color:var(--err); border:1px solid #fecaca; }
label { font-size:12px; color:var(--mut); display:block; margin-bottom:4px; }
input,select { width:100%; padding:8px 10px; border:1px solid var(--line); border-radius:8px; font:inherit; background:#fff; }
.row { display:flex; gap:10px; flex-wrap:wrap; align-items:flex-end; }
.row > div { flex:1; min-width:180px; }
button { padding:8px 18px; border:0; border-radius:8px; background:var(--acc); color:#fff; font:inherit; cursor:pointer; }
button:hover { filter:brightness(1.1); }
button.ghost { background:#eef1f5; color:var(--txt); }
button:disabled { opacity:.5; cursor:default; }
.cost { font-size:11px; color:var(--mut); margin-top:8px; }
pre#log { background:#0f172a; color:#d7e0ee; border-radius:8px; padding:12px; font:12px/1.8 Consolas,monospace; max-height:260px; overflow:auto; white-space:pre-wrap; display:none; }
pre.code { background:#f8fafc; border:1px solid var(--line); border-radius:8px; padding:12px; font:12px/1.7 Consolas,monospace; overflow:auto; white-space:pre-wrap; margin:8px 0; }
.copy { padding:3px 14px; font-size:12px; }
.guide h3 { font-size:13px; margin:14px 0 4px; }
.guide p, .guide li { font-size:12.5px; color:var(--mut); }
.guide code { display:inline-block; font:11px Consolas,monospace; background:#f1f5f9; border-radius:4px; padding:0 5px; }
.file h2 { font-size:16px; margin:0; display:inline; }
.key { font:11px Consolas,monospace; color:var(--mut); display:inline-block; margin-left:8px; }
.badge { display:inline-block; font-size:11px; border-radius:20px; padding:1px 10px; margin-left:6px; }
.badge.full { background:#ecfdf5; color:var(--ok); }
.badge.part { background:#fffbeb; color:var(--warn); }
.stats { display:flex; gap:18px; flex-wrap:wrap; margin:10px 0; font-size:12px; color:var(--mut); }
.stats b { color:var(--txt); font-size:14px; display:block; }
.stats .bad b { color:var(--err); }
.icons { display:flex; flex-wrap:wrap; gap:6px; margin:10px 0; }
.icons img { width:34px; height:34px; object-fit:contain; background:#f8fafc; border:1px solid var(--line); border-radius:6px; padding:3px; }
details { font-size:12px; color:var(--mut); margin-top:6px; }
summary { cursor:pointer; }
.missing { font:11px Consolas,monospace; max-height:140px; overflow:auto; background:#f8fafc; border-radius:6px; padding:8px; margin-top:6px; }
.actions { margin-top:10px; display:flex; gap:8px; flex-wrap:wrap; }
.meta { font-size:11px; color:var(--mut); }
#empty { color:var(--mut); text-align:center; padding:30px; }
</style>
</head>
<body>
<div class="wrap">
<h1>Figma Cache Panel</h1>
<p class="sub">Cached file status, quota usage and new syncs. Viewing status never spends a request.</p>
<div id="token-warn" class="banner err" style="display:none">No token available. Add one under "Stored tokens" or set <code>FIGMA_TOKEN</code>.</div>

<div class="card guide">
<details><summary>Quick start &mdash; three steps</summary>
<h3>1. Install</h3>
<p>Run <code>./setup.sh</code> in the project root. It creates the <code>.venv</code> virtualenv and installs dependencies.</p>
<h3>2. Figma token</h3>
<p>Create a personal access token in Figma &rarr; Settings &rarr; Security, then store it under "Stored tokens" below (or set the <code>FIGMA_TOKEN</code> environment variable).</p>
<h3>3. Cache</h3>
<p>Paste a file key or a full Figma link and press "Cache". To cache a single frame, switch Mode to "Single frame (node-id)".</p>
</details>
</div>

<div class="card">
<div class="row">
<div style="flex:3"><label>File key or full Figma link</label><input id="inp" placeholder="e.g. E0bndiNiGIH71h0wGGH2cq or https://www.figma.com/design/..."></div>
<div><label>Mode</label><select id="mode"><option value="file">Whole file</option><option value="frame">Single frame (node-id)</option></select></div>
<div id="node-box" style="display:none"><label>node-id</label><input id="node" placeholder="437:649"></div>
<div style="flex:0"><button id="go" onclick="startSync()">Cache</button></div>
</div>
<div class="row" style="margin-top:10px">
<div><label>Token</label><select id="token-src" onchange="tokenSrcChange()"></select></div>
<div id="once-box" style="display:none;flex:2"><label>One-time token (not stored)</label><input id="token-once" type="password" autocomplete="off" placeholder="figd_..."></div>
</div>
<div class="cost" id="cost">Whole file: 2 Tier 1 requests (tree + SVGs) and 1 Tier 2 request (image links). Downloading assets costs no API quota.</div>
</div>

<div class="card">
<details><summary>Stored tokens</summary>
<div class="row" style="margin-top:10px">
<div><label>Name</label><input id="tname" placeholder="e.g. Personal account"></div>
<div style="flex:2"><label>Token</label><input id="tval" type="password" autocomplete="off" placeholder="figd_..."></div>
<div style="flex:0"><button onclick="addToken()">Save</button></div>
</div>
<div id="token-list" style="margin-top:10px"></div>
<p class="cost">Tokens are stored in <code>~/.config/figma-cache-mcp/tokens.json</code> with mode 600. Token values are never sent back to the browser and are only injected into the sync process at run time.</p>
</details>
</div>

<div class="card guide">
<details><summary>Connecting an agent (MCP) + suggested prompt</summary>

<h3>1. Register the MCP server in your client</h3>
<p>The server uses the stdio transport; the client launches it, so you never start it by hand. Add this to your client's MCP configuration:</p>
<pre class="code" id="cfg">{
  "mcpServers": {
    "figma-cache": {
      "command": "{{ROOT}}/run-mcp.sh"
    }
  }
}</pre>
<button class="ghost copy" onclick="copyText('cfg')">Copy config</button>
<ul>
<li><b>Claude Desktop:</b> Settings &rarr; Developer &rarr; Edit Config &rarr; <code>claude_desktop_config.json</code></li>
<li><b>Cursor:</b> Settings &rarr; MCP &rarr; Add Server (<code>mcp.json</code>)</li>
<li><b>Qoder:</b> MCP settings &rarr; add a server with the command above</li>
<li><b>Other clients:</b> any client that supports MCP over stdio; command <code>{{ROOT}}/run-mcp.sh</code></li>
</ul>
<p>The script reads the token from <code>~/.config/figma-cache-mcp/token</code> (only needed to auto-refresh Tier 2 image links). Without a token the server still works fully offline and serves local images.</p>
{{WSL_BLOCK}}
<h3>2. Tools</h3>
<ul>
<li><code>get_cached_design(url)</code> &mdash; pass a Figma selection link (must contain node-id); returns that node's JSON from the local cache. On a cache miss it never calls Figma.</li>
<li><code>get_cached_svg(file_key, node_id)</code> &mdash; reads a complete icon SVG from cache; using the instance id or any inner vector id returns the full group SVG.</li>
</ul>

<h3>3. Suggested agent prompt</h3>
<p>Give this text at the start of the conversation or in the project's instructions/rules:</p>
<pre class="code" id="prompt">You have access to a local cached Figma MCP server named "figma-cache". Use it for all Figma design data. NEVER call api.figma.com or any live Figma API yourself - the account has a strict monthly request quota managed by the user.

Workflow:
1. For any Figma selection URL (it must contain node-id), call get_cached_design(url). It returns the node's design JSON from local cache: layout (absoluteBoundingBox), colors (fills/strokes), text (TEXT nodes), typography, and component structure.
2. If the result is a cache miss, do not retry and do not fetch from Figma. Tell the user to sync the file or frame in the cache panel, then continue with other work.
3. Nodes marked "svg_cached": true have a ready-made SVG. Call get_cached_svg(file_key, node_id) to get it - file_key is the key from the Figma URL. Icons made of multiple vectors are exported as ONE complete SVG: requesting the icon's frame/instance id or any child vector id returns the whole icon. Use these SVGs directly instead of recreating icons by hand.
4. Image fills: inspect "image_fills" in the response. Status "cached_local" means the image bytes are on disk at "path" - use that local file. Status "cached" provides a temporary URL. For "expired"/"not_cached", ask the user to refresh; never guess image content.
5. Implement the UI from the JSON data. For vectors without a cached SVG, use fillGeometry path data if present; otherwise ask the user to sync SVGs.
6. When you need a different node, ask the user for its "Copy link to selection" URL rather than guessing node ids.</pre>
<button class="ghost copy" onclick="copyText('prompt')">Copy prompt</button>
</details>
</div>

<pre id="log"></pre>
<div class="row" style="margin-bottom:14px"><div style="flex:0"><button class="ghost" onclick="loadStatus()">Reload status</button></div></div>
<div id="files"></div>
<div id="empty" style="display:none">Nothing is cached yet.</div>
</div>
<script>
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'\u0026':'\u0026amp;','\u003c':'\u0026lt;','\u003e':'\u0026gt;','"':'\u0026quot;',"'":'\u0026#39;'}[c]));
const fmtDate = s => s ? new Date(s).toLocaleString('en-US') : '—';
let jobTimer = null;

async function copyText(id) {
  const text = document.getElementById(id).textContent;
  try { await navigator.clipboard.writeText(text); }
  catch { const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove(); }
}

document.getElementById('mode').onchange = e => {
  const frame = e.target.value === 'frame';
  document.getElementById('node-box').style.display = frame ? '' : 'none';
  document.getElementById('cost').textContent = frame
    ? 'Single frame: 1 Tier 1 request. Only that frame\\u2019s JSON is cached (no SVGs).'
    : 'Whole file: 2 Tier 1 requests (tree + SVGs) and 1 Tier 2 request (image links). Downloading assets costs no API quota.';
};

async function loadStatus() {
  const r = await fetch('/api/status'); const data = await r.json();
  document.getElementById('token-warn').style.display = data.token_set ? 'none' : '';
  const box = document.getElementById('files');
  document.getElementById('empty').style.display = data.files.length ? 'none' : '';
  box.innerHTML = data.files.map(f => {
    const last = f.full ? f.full.synced_at : (f.frames.map(x=>x.synced_at).sort().pop());
    const svg = f.svg, img = f.images, u = f.usage;
    const missingSvg = svg && svg.missing.length ? `<details><summary>Uncached SVGs (${svg.missing.length})</summary><div class="missing">${svg.missing.map(esc).join('<br>')}</div></details>` : '';
    const missingImg = img.missing_refs && img.missing_refs.length ? `<details><summary>Images not downloaded (${img.missing_refs.length})</summary><div class="missing">${img.missing_refs.map(esc).join('<br>')}</div></details>` : '';
    const frames = !f.full && f.frames.length ? `<details><summary>Cached frames (${f.frames.length}) &mdash; whole file not cached</summary><div class="missing">${f.frames.map(x=>esc(x.node_id)+'  '+esc(x.name||'')+'  '+fmtDate(x.synced_at)).join('<br>')}</div></details>` : '';
    const history = u.history.length ? `<details><summary>Usage history (${u.history.length})</summary><div class="missing">${u.history.slice().reverse().map(h=>fmtDate(h.at)+'  '+esc(h.kind)+'  T1:'+h.tier1+' T2:'+h.tier2).join('<br>')}</div></details>` : '';
    const icons = svg && svg.preview.length ? `<div class="icons">${svg.preview.map(n=>`<img loading="lazy" title="${esc(n)}" src="/svg/${esc(f.file_key)}/${esc(n)}">`).join('')}</div>` : '';
    return `<div class="card file">
      <h2>${esc(f.name || f.file_key)}</h2><span class="key">${esc(f.file_key)}</span>
      <span class="badge ${f.full?'full':'part'}">${f.full?'Full':'Frames only'}</span>
      <div class="meta">Last updated: ${fmtDate(last)}${svg? ' | SVG: '+fmtDate(svg.synced_at):''}</div>
      <div class="stats">
        <span><b>${f.full? f.full.nodes : f.frames.reduce((a,x)=>a+x.nodes,0)}</b>nodes cached</span>
        <span><b>${f.full? f.full.vectors : f.frames.reduce((a,x)=>a+x.vectors,0)}</b>vectors</span>
        <span><b>${svg? svg.icons : 0}</b>SVG icons</span>
        <span class="${svg && svg.missing.length?'bad':''}"><b>${svg? svg.missing.length : '—'}</b>SVGs missing</span>
        <span><b>${img.local}${f.full && f.full.image_refs.length? ' / '+f.full.image_refs.length:''}</b>local images</span>
        <span><b>${u.tier1}</b>Tier 1 requests</span>
        <span><b>${u.tier2}</b>Tier 2 requests</span>
      </div>
      ${icons}${missingSvg}${missingImg}${frames}${history}
      <div class="actions">
        <button onclick="syncFile('${esc(f.file_key)}')">Refresh whole file</button>
        <button class="ghost" title="No tree re-download; at most 1 Tier 1 request" onclick="startSync('${esc(f.file_key)}','svg','')">Retry SVGs</button>
        <button class="ghost" title="Fresh links are reused; usually no API request" onclick="startSync('${esc(f.file_key)}','images','')">Retry images</button>
        <button class="ghost" onclick="syncFrame('${esc(f.file_key)}')">Cache a frame</button>
      </div>
    </div>`;
  }).join('');
}

async function loadTokens() {
  const t = await (await fetch('/api/tokens')).json();
  const sel = document.getElementById('token-src');
  const prev = sel.value;
  sel.innerHTML = '';
  if (t.env_available) sel.add(new Option('Environment / default file token', 'env'));
  t.names.forEach(n => sel.add(new Option('Stored: ' + n, n)));
  sel.add(new Option('One-time...', 'onetime'));
  if ([...sel.options].some(o => o.value === prev)) sel.value = prev;
  tokenSrcChange();
  document.getElementById('token-list').innerHTML = t.names.length
    ? t.names.map(n => `<div class="meta" style="display:flex;justify-content:space-between;align-items:center;margin-top:4px"><span>${esc(n)}</span><button class="ghost" style="padding:2px 12px" data-name="${esc(n)}" onclick="delToken(this.dataset.name)">Delete</button></div>`).join('')
    : '<span class="meta">No stored tokens.</span>';
}
function tokenSrcChange() {
  document.getElementById('once-box').style.display = document.getElementById('token-src').value === 'onetime' ? '' : 'none';
}
async function addToken() {
  const name = document.getElementById('tname').value, token = document.getElementById('tval').value;
  const r = await fetch('/api/tokens', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({name, token})});
  const d = await r.json();
  if (!r.ok) { alert(d.error); return; }
  document.getElementById('tname').value = ''; document.getElementById('tval').value = '';
  loadTokens(); loadStatus();
}
async function delToken(name) {
  if (!confirm('Delete token "' + name + '"?')) return;
  await fetch('/api/tokens/delete', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({name})});
  loadTokens(); loadStatus();
}

async function startSync(input, mode, nodeId) {
  input = input ?? document.getElementById('inp').value;
  mode = mode ?? document.getElementById('mode').value;
  nodeId = nodeId ?? document.getElementById('node').value;
  const src = document.getElementById('token-src').value;
  const body = {input, mode, node_id: nodeId};
  if (src === 'onetime') body.token = document.getElementById('token-once').value;
  else body.token_name = src;
  const r = await fetch('/api/sync', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  const data = await r.json();
  const log = document.getElementById('log');
  log.style.display = 'block';
  if (!r.ok) { log.textContent = 'Error: ' + (data.error || r.status); return; }
  document.getElementById('token-once').value = '';
  log.textContent = '$ ' + data.cmd + '\\n';
  document.getElementById('go').disabled = true;
  clearInterval(jobTimer);
  jobTimer = setInterval(async () => {
    const j = await (await fetch('/api/job?id=' + data.id)).json();
    log.textContent = '$ ' + j.cmd + '\\n' + j.lines.join('\\n');
    log.scrollTop = log.scrollHeight;
    if (j.done) {
      clearInterval(jobTimer);
      log.textContent += '\\n[exit ' + j.code + ']';
      document.getElementById('go').disabled = false;
      loadStatus();
    }
  }, 1000);
}
const syncFile = key => startSync(key, 'file', '');
const syncFrame = key => { const n = prompt('Frame node-id (e.g. 437:649):'); if (n) startSync(key, 'frame', n); };
loadTokens();
loadStatus();
</script>
</body>
</html>'''


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parts = urlsplit(self.path)
        path = parts.path
        if path == '/':
            wsl = WSL_BLOCK.replace('{{DISTRO}}', WSL_DISTRO).replace('{{ROOT}}', str(ROOT)) if WSL_DISTRO else ''
            body = PAGE.replace('{{ROOT}}', str(ROOT)).replace('{{WSL_BLOCK}}', wsl).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == '/api/status':
            self.send_json(build_status())
        elif path == '/api/tokens':
            self.send_json({'names': sorted(tokens.load()), 'env_available': bool(tokens.env_token())})
        elif path == '/api/job':
            job_id = parse_qs(parts.query).get('id', [''])[0]
            job = JOBS.get(job_id)
            if job is None:
                self.send_json({'error': 'Job not found.'}, 404)
            else:
                self.send_json(job)
        elif path.startswith('/svg/') or path.startswith('/img/'):
            self.serve_asset(path)
        else:
            self.send_json({'error': 'Path not found.'}, 404)

    def serve_asset(self, path):
        segments = path.strip('/').split('/')
        if len(segments) != 3:
            self.send_json({'error': 'File not found.'}, 404)
            return
        kind, key, name = segments
        if not KEY_RE.fullmatch(key) or not ASSET_RE.fullmatch(name) or '..' in name:
            self.send_json({'error': 'File not found.'}, 404)
            return
        directory = CACHE / ('svg' if kind == 'svg' else 'images') / key
        target = directory / name
        if target.parent != directory or not target.is_file():
            self.send_json({'error': 'File not found.'}, 404)
            return
        content = target.read_bytes()
        ctype = 'image/svg+xml' if name.endswith('.svg') else (mimetypes.guess_type(name)[0] or 'application/octet-stream')
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'max-age=3600')
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self):
        path = urlsplit(self.path).path
        if path == '/api/tokens':
            self.manage_token(add=True)
            return
        if path == '/api/tokens/delete':
            self.manage_token(add=False)
            return
        if path != '/api/sync':
            self.send_json({'error': 'Path not found.'}, 404)
            return
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length))
        except (ValueError, TypeError):
            self.send_json({'error': 'Request body is not valid JSON.'}, 400)
            return
        token = tokens.resolve(body.get('token_name'), body.get('token'))
        if not token:
            self.send_json({'error': 'No token available: pick a stored token, enter a one-time token, or set FIGMA_TOKEN.'}, 400)
            return
        key, link_node = parse_input(str(body.get('input', '')))
        if not key:
            self.send_json({'error': 'Enter a valid Figma file key or link.'}, 400)
            return
        mode = body.get('mode', 'file')
        if mode == 'frame':
            node_id = str(body.get('node_id') or link_node or '').strip().replace('-', ':')
            if not NODE_RE.fullmatch(node_id):
                self.send_json({'error': 'Caching a frame needs a valid node-id (e.g. 437:649).'}, 400)
                return
            cmd = [sys.executable, str(ROOT / 'sync.py'), key, node_id]
        elif mode == 'file':
            cmd = [sys.executable, str(ROOT / 'sync-file.py'), key]
        elif mode == 'svg':
            cmd = [sys.executable, str(ROOT / 'sync-file.py'), key, '--svg-only']
        elif mode == 'images':
            cmd = [sys.executable, str(ROOT / 'sync-file.py'), key, '--images-only']
        else:
            self.send_json({'error': 'Invalid sync mode.'}, 400)
            return
        with JOB_LOCK:
            if any(not j['done'] for j in JOBS.values()):
                self.send_json({'error': 'A cache job is already running; wait for it to finish.'}, 409)
                return
            job = start_job(cmd, token)
        self.send_json({'id': job['id'], 'cmd': job['cmd']})

    def manage_token(self, add):
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length))
        except (ValueError, TypeError):
            self.send_json({'error': 'Request body is not valid JSON.'}, 400)
            return
        name = str(body.get('name', '')).strip()
        if not name or len(name) > 64 or any(c < ' ' for c in name):
            self.send_json({'error': 'Token name must be 1 to 64 characters.'}, 400)
            return
        if add:
            token = str(body.get('token', '')).strip()
            if not token or any(c.isspace() for c in token):
                self.send_json({'error': 'Invalid token value.'}, 400)
                return
            tokens.add(name, token)
        else:
            tokens.remove(name)
        self.send_json({'names': sorted(tokens.load()), 'env_available': bool(tokens.env_token())})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--host', default='127.0.0.1')
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f'Figma cache panel: http://{args.host}:{args.port}')
    server.serve_forever()


if __name__ == '__main__':
    main()