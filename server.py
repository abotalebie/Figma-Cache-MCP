import fcntl
import json
import os
import re
from pathlib import Path
from time import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from mcp.server.fastmcp import FastMCP

mcp = FastMCP('figma-cache')
cache_dir = Path(__file__).resolve().parent / 'cache'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def put_json(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data), encoding='utf-8')
    tmp.replace(path)


def fill_status(path, refs):
    try:
        urls = json.loads(path.read_text(encoding='utf-8'))['meta']['images']
        if not isinstance(urls, dict):
            urls = {}
        age = time() - path.stat().st_mtime
    except (OSError, ValueError, KeyError, TypeError):
        urls, age = {}, float('inf')
    fills = {}
    local_dir = cache_dir / 'images' / path.stem
    for ref in sorted(refs):
        local = next(local_dir.glob(f'{quote(ref, safe="")}.*'), None)
        if local is not None:
            fills[ref] = {'status': 'cached_local', 'path': str(local)}
            continue
        url = urls.get(ref)
        if not isinstance(url, str):
            fills[ref] = {'status': 'not_cached'}
            continue
        try:
            parsed = urlsplit(url)
            expires = parse_qs(parsed.query).get('Expires', [''])[0]
            if parsed.scheme != 'https' or not parsed.hostname:
                raise ValueError('Invalid image URL')
            expired = int(expires) <= time() + 30 if expires.isdecimal() else age >= 3600
            fills[ref] = {'status': 'expired' if expires.isdecimal() else 'stale'} if expired else {'status': 'cached', 'url': url}
        except ValueError:
            fills[ref] = {'status': 'invalid_url'}
    return fills


def get_image_fills(file_key, refs):
    directory = cache_dir / 'image-fills'
    path = directory / f'{file_key}.json'
    fills = fill_status(path, refs)
    if all(item['status'] in ('cached', 'cached_local') for item in fills.values()):
        return fills, 'cache_hit'
    token = os.environ.get('FIGMA_TOKEN')
    if not token:
        return fills, 'missing_figma_token'
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / '.refresh.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            fills = fill_status(path, refs)
            if all(item['status'] in ('cached', 'cached_local') for item in fills.values()):
                return fills, 'cache_hit'
            budget = directory / '.next-request.json'
            now = time()
            if budget.exists() and json.loads(budget.read_text()) > now:
                return fills, 'cooldown'
            put_json(budget, now + 60)
            req = Request(f'https://api.figma.com/v1/files/{file_key}/images',
                          headers={'X-Figma-Token': token})
            try:
                with build_opener(NoRedirect()).open(req, timeout=10) as response:
                    data = json.load(response)
                if not isinstance(data['meta']['images'], dict):
                    return fills, 'invalid_api_response'
                put_json(path, data)
            except HTTPError as error:
                if error.code == 429:
                    delay = error.headers.get('Retry-After', '').strip()
                    put_json(budget, time() + (max(60, int(delay)) if delay.isdecimal() else 300))
                return fills, f'http_{error.code}'
            except (URLError, TimeoutError):
                return fills, 'network_error'
            return fill_status(path, refs), 'refreshed'
    except (OSError, ValueError, KeyError, TypeError):
        return fills, 'cache_or_response_error'


@mcp.tool()
def get_cached_svg(file_key: str, node_id: str) -> dict:
    """Read a cached SVG export for a vector node (or its icon group). Never sends API requests."""
    if not re.fullmatch(r'[A-Za-z0-9]+', file_key):
        return {'error': 'Invalid file key.'}
    node_id = node_id.strip().replace('-', ':')
    if not re.fullmatch(r'[A-Za-z0-9:;]+', node_id):
        return {'error': 'Invalid node id.'}
    svg_dir = cache_dir / 'svg' / quote(file_key, safe='')
    try:
        manifest = json.loads((svg_dir / 'manifest.json').read_text(encoding='utf-8'))['map']
    except (OSError, ValueError, KeyError):
        return {'error': 'No SVG cache for this file. Run sync-file.py first.', 'node_id': node_id}
    candidates = [node_id]
    if ';' in node_id:
        candidates.append(node_id.rsplit(';', 1)[1])
    for candidate in candidates:
        name = manifest.get(candidate)
        if not isinstance(name, str):
            continue
        try:
            svg = (svg_dir / name).read_text(encoding='utf-8')
        except OSError:
            continue
        return {'node_id': node_id, 'exported_node_id': candidate, 'svg': svg}
    return {'error': 'SVG not cached for this node. It may be in the missing list of the manifest; fillGeometry paths are available via get_cached_design.', 'node_id': node_id}


@mcp.tool()
def get_cached_design(url: str) -> dict:
    """Read cached Figma design JSON by selection URL. Refreshes image-fill links when necessary and enabled; for vector SVGs use get_cached_svg."""
    try:
        link = urlsplit(url.strip())
        trusted = link.scheme == 'https' and link.hostname in {'figma.com', 'www.figma.com'}
    except ValueError:
        trusted = False
    if not trusted:
        return {'error': 'Provide an HTTPS Figma selection link.'}
    match = re.match(r'^/(?:design|file)/([A-Za-z0-9]+)(?:/|$)', link.path)
    ids = parse_qs(link.query).get('node-id', [])
    if not match or len(ids) != 1 or not re.fullmatch(r'[0-9]+[-:][0-9]+', ids[0]):
        return {'error': 'Use Copy link to selection in Figma. The link must contain node-id.'}

    file_key = match[1]
    node_id = ids[0].replace('-', ':')
    direct = cache_dir / f'{file_key}__{quote(node_id, safe="")}.json'
    best = None
    for path in dict.fromkeys([direct, *sorted(cache_dir.glob(f'{file_key}__*.json'))]):
        try:
            cached = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        document = (cached.get('node') or {}).get('document')
        stack = [document] if document else []
        while stack:
            node = stack.pop()
            if node.get('id') == node_id:
                candidate = {'synced_at': cached.get('synced_at'), 'node': node}
                if best is None or (candidate['synced_at'] or '') > (best['synced_at'] or ''):
                    best = candidate
                break
            stack.extend(node.get('children', []))
    if best is None:
        return {'error': 'Cache miss. Sync this node or a parent manually. No request sent.', 'node_id': node_id}

    try:
        svg_map = json.loads((cache_dir / 'svg' / quote(file_key, safe='') / 'manifest.json').read_text(encoding='utf-8'))['map']
    except (OSError, ValueError, KeyError):
        svg_map = {}
    if svg_map:
        pending = [best['node']]
        while pending:
            node = pending.pop()
            child_id = node.get('id', '')
            if child_id in svg_map or (';' in child_id and child_id.rsplit(';', 1)[1] in svg_map):
                node['svg_cached'] = True
            pending.extend(node.get('children', []))

    refs = set()
    pending = [best['node']]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            ref = value.get('imageRef')
            if isinstance(ref, str) and ref:
                refs.add(ref)
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)

    if refs:
        best['image_fills'], best['image_refresh'] = get_image_fills(file_key, refs)
    return best


if __name__ == '__main__':
    mcp.run(transport='stdio')
