import hashlib
import http.client
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from urllib.request import Request, urlopen

import usage

VECTOR_TYPES = {'VECTOR', 'BOOLEAN_OPERATION', 'STAR', 'LINE', 'ELLIPSE', 'REGULAR_POLYGON'}
ICON_ROOT_TYPES = VECTOR_TYPES | {'GROUP', 'FRAME', 'INSTANCE', 'COMPONENT'}
ALLOWED_SUBTREE_TYPES = ICON_ROOT_TYPES | {'RECTANGLE'}
MAX_URL_LENGTH = 7500

cache_dir = Path(__file__).resolve().parent / 'cache'


def api_get(url, token, on_request=None):
    request = Request(url, headers={'X-Figma-Token': token})
    try:
        with urlopen(request, timeout=60) as response:
            if on_request:
                on_request()
            return json.load(response)
    except HTTPError as error:
        if on_request:
            on_request()
        if error.code == 429:
            retry_after = error.headers.get('Retry-After', 'not provided')
            raise SystemExit(f'Figma rate limit (429). Retry-After: {retry_after}.')
        raise SystemExit(f'Figma returned HTTP {error.code}.')
    except (URLError, TimeoutError) as error:
        raise SystemExit(f'Network error contacting Figma: {error}')


def with_retry(fn, attempts=3):
    for attempt in range(attempts):
        try:
            return fn()
        except (OSError, ValueError, http.client.HTTPException):
            if attempt == attempts - 1:
                raise
            time.sleep(1 + attempt)


def fetch_bytes(url, timeout):
    def once():
        with urlopen(Request(url), timeout=timeout) as response:
            return response.read()
    return with_retry(once)


def fetched_fresh(fetched_at):
    if not fetched_at:
        return False
    try:
        return time.time() - datetime.fromisoformat(fetched_at).timestamp() < 14 * 86400
    except ValueError:
        return False


def url_fresh(url, fetched_at=None):
    if not isinstance(url, str) or not url.startswith('https://'):
        return False
    expires = parse_qs(urlsplit(url).query).get('Expires', [''])[0]
    if expires.isdecimal():
        return int(expires) > time.time() + 60
    return fetched_fresh(fetched_at)


def response_images(result):
    images = result.get('images')
    if not isinstance(images, dict):
        images = (result.get('meta') or {}).get('images')
    return images if isinstance(images, dict) else {}


def iter_nodes(root):
    stack = [root]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            yield node
            stack.extend(node.get('children', []))


def contains_vector(node):
    return any(n.get('type') in VECTOR_TYPES for n in iter_nodes(node))


def is_pure_vector(node):
    return all(
        n.get('type') in ALLOWED_SUBTREE_TYPES and n.get('visible') is not False
        for n in iter_nodes(node)
    )


def collect_exports(document, all_ids=None):
    if all_ids is None:
        all_ids = {n.get('id') for n in iter_nodes(document)}
    exports = {}

    def canonical(node_id):
        if ';' in node_id:
            tail = node_id.rsplit(';', 1)[1]
            if tail in all_ids:
                return tail
        return node_id

    def walk(node):
        if node.get('visible') is False:
            return
        if node.get('type') in ICON_ROOT_TYPES and contains_vector(node) and is_pure_vector(node):
            export_id = canonical(node['id'])
            covered = [n['id'] for n in iter_nodes(node)]
            exports.setdefault(export_id, []).extend(covered)
            return
        for child in node.get('children', []):
            walk(child)

    walk(document)
    return exports


IDENTITY_KEYS = {'id', 'name', 'absoluteBoundingBox', 'absoluteRenderBounds', 'relativeTransform',
                 'componentId', 'overrides', 'componentProperties', 'scrollBehavior'}


def strip_identity(value):
    if isinstance(value, dict):
        return {k: ('FRAME' if k == 'type' and v in ('INSTANCE', 'COMPONENT') else strip_identity(v))
                for k, v in value.items() if k not in IDENTITY_KEYS}
    if isinstance(value, list):
        return [strip_identity(item) for item in value]
    return value


def dedup_exports(exports, node_index):
    groups = {}
    for export_id in exports:
        node = node_index.get(export_id)
        key = hashlib.md5(json.dumps(strip_identity(node), sort_keys=True).encode()).hexdigest() if node else export_id
        groups.setdefault(key, []).append(export_id)
    merged = {}
    for members in groups.values():
        rep = min(members, key=lambda i: (
            node_index.get(i, {}).get('type') != 'COMPONENT', ';' in i, len(i), i))
        covered = list(members)
        for member in members:
            covered.extend(exports[member])
        merged[rep] = covered
    return merged


def chunk_ids(ids, file_key):
    base = f'https://api.figma.com/v1/images/{quote(file_key, safe="")}?format=svg&ids='
    limit = MAX_URL_LENGTH - len(base)
    chunks, current, length = [], [], 0
    for node_id in ids:
        extra = len(quote(node_id, safe='')) + 1
        if current and length + extra > limit:
            chunks.append(current)
            current, length = [], 0
        current.append(node_id)
        length += extra
    if current:
        chunks.append(current)
    return chunks


def load_cached_documents(file_key):
    documents = []
    for path in sorted(cache_dir.glob(f'{quote(file_key, safe="")}__*.json')):
        try:
            cached = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        document = (cached.get('node') or {}).get('document')
        if document:
            documents.append(document)
    return documents


def download_svg(item):
    node_id, url, directory = item
    name = f'{quote(node_id, safe="")}.svg'
    try:
        if not url.startswith('https://'):
            return node_id, 'invalid_url'
        content = fetch_bytes(url, 30)
        if b'<svg' not in content[:2000]:
            return node_id, 'not_svg'
        tmp = directory / (name + '.tmp')
        tmp.write_bytes(content)
        tmp.replace(directory / name)
        return node_id, name
    except (OSError, ValueError, http.client.HTTPException):
        return node_id, 'download_failed'


def collect_image_refs(document):
    refs = set()
    for node in iter_nodes(document):
        for fill in node.get('fills', []):
            if isinstance(fill, dict) and fill.get('type') == 'IMAGE' and isinstance(fill.get('imageRef'), str):
                refs.add(fill['imageRef'])
    return sorted(refs)


def sniff_extension(content):
    if content.startswith(b'\x89PNG'):
        return '.png'
    if content.startswith(b'\xff\xd8'):
        return '.jpg'
    if content.startswith(b'GIF8'):
        return '.gif'
    if content[:4] == b'RIFF' and content[8:12] == b'WEBP':
        return '.webp'
    if b'<svg' in content[:2000]:
        return '.svg'
    return '.bin'


def download_image(item):
    ref, url, directory = item
    try:
        if not url.startswith('https://'):
            return ref, 'invalid_url'
        content = fetch_bytes(url, 60)
        ext = Path(urlsplit(url).path).suffix.lower()
        if ext not in ('.png', '.jpg', '.jpeg', '.svg', '.webp', '.gif'):
            ext = sniff_extension(content)
        name = quote(ref, safe='') + ext
        tmp = directory / (name + '.tmp')
        tmp.write_bytes(content)
        tmp.replace(directory / name)
        return ref, name
    except (OSError, ValueError, http.client.HTTPException):
        return ref, 'download_failed'


def sync_image_fills(file_key, key_q, document, token, reuse=False):
    refs = collect_image_refs(document)
    if not refs:
        print('No image fills in this file.')
        return
    fills_dir = cache_dir / 'image-fills'
    fills_path = fills_dir / f'{key_q}.json'
    links = {}
    if reuse:
        try:
            links = json.loads(fills_path.read_text(encoding='utf-8'))['meta']['images']
        except (OSError, ValueError, KeyError, TypeError):
            links = {}
    urls = {ref: links[ref] for ref in refs if url_fresh(links.get(ref))}
    need = [ref for ref in refs if ref not in urls]
    if need:
        print(f'Request: GET image fill links for {len(need)} refs (Tier 2)...')
        result = api_get(f'https://api.figma.com/v1/files/{key_q}/images', token,
                         lambda: usage.record(file_key, 'image-fills', tier2=1))
        images = (result.get('meta') or {}).get('images') or {}
        urls.update({ref: url for ref, url in images.items() if isinstance(url, str)})
    else:
        print(f'Reusing {len(urls)} cached image links (no API request).')
    fills_dir.mkdir(parents=True, exist_ok=True)
    tmp = fills_path.with_suffix('.tmp')
    tmp.write_text(json.dumps({'meta': {'images': urls}}, ensure_ascii=False), encoding='utf-8')
    tmp.replace(fills_path)
    img_dir = cache_dir / 'images' / key_q
    img_dir.mkdir(parents=True, exist_ok=True)
    existing = {p.stem for p in img_dir.iterdir() if p.is_file() and not p.name.endswith('.tmp')}
    already = [ref for ref in refs if ref in existing]
    todo = [ref for ref in refs if ref in urls and ref not in existing]
    failed = [ref for ref in refs if ref not in urls and ref not in existing]
    print(f'Image fills: {len(already)} already local, {len(todo)} to download, {len(failed)} without link')
    jobs = [(ref, urls[ref], img_dir) for ref in todo]
    succeeded = 0
    total = len(jobs)
    with ThreadPoolExecutor(max_workers=8) as pool:
        for done, (ref, outcome) in enumerate(pool.map(download_image, jobs), 1):
            if outcome in ('invalid_url', 'download_failed'):
                failed.append(ref)
            else:
                succeeded += 1
            if done % 25 == 0 or done == total:
                print(f'  images progress: {done}/{total}, failed: {len(failed)}', flush=True)
    print(f'Image fills: {succeeded} downloaded, {len(failed)} failed, {len(already)} already local')


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    dry_run = '--dry-run' in sys.argv
    svg_only = '--svg-only' in sys.argv
    images_only = '--images-only' in sys.argv
    if svg_only and images_only:
        raise SystemExit('Use only one of --svg-only / --images-only.')
    max_image_requests = 1
    for arg in sys.argv[1:]:
        if arg.startswith('--max-image-requests='):
            max_image_requests = int(arg.split('=', 1)[1])
    if len(args) != 1:
        raise SystemExit('Usage: python sync-file.py FILE_KEY [--dry-run] [--svg-only] [--images-only] [--max-image-requests=N]')
    file_key = args[0]
    key_q = quote(file_key, safe='')

    if dry_run:
        documents = load_cached_documents(file_key)
        if not documents:
            raise SystemExit('No cached data for this file key; dry-run needs an existing cache.')
        all_ids = set()
        node_index = {}
        for document in documents:
            for node in iter_nodes(document):
                all_ids.add(node.get('id'))
                node_index.setdefault(node['id'], node)
        exports = {}
        for document in documents:
            for export_id, covered in collect_exports(document, all_ids).items():
                exports.setdefault(export_id, []).extend(covered)
        raw_count = len(exports)
        exports = dedup_exports(exports, node_index)
        chunks = chunk_ids(sorted(exports), file_key)
        print(f'[dry-run] vector nodes covered: {sum(len(v) for v in exports.values())}')
        print(f'[dry-run] export ids: {raw_count} -> {len(exports)} after content dedup')
        print(f'[dry-run] image requests needed: {len(chunks)} (allowed: {max_image_requests})')
        refs = set()
        for document in documents:
            refs.update(collect_image_refs(document))
        print(f'[dry-run] image fills: {len(refs)} refs (1 Tier-2 request + byte downloads)')
        return

    token = os.environ.get('FIGMA_TOKEN')
    if not token:
        raise SystemExit('Set FIGMA_TOKEN first.')

    cache_dir.mkdir(exist_ok=True)
    tree_path = cache_dir / f'{key_q}__0%3A0.json'
    synced_at = datetime.now(timezone.utc).isoformat()
    if svg_only or images_only:
        try:
            document = json.loads(tree_path.read_text(encoding='utf-8'))['node']['document']
        except (OSError, ValueError, KeyError, TypeError):
            raise SystemExit('No cached full tree for this file; run a full sync first.')
        print('Using cached full tree (no API request).')
    else:
        print('Request: GET full file tree (Tier 1)...')
        data = api_get(f'https://api.figma.com/v1/files/{key_q}?geometry=paths', token,
                       lambda: usage.record(file_key, 'full-tree', tier1=1))
        document = data.get('document')
        if not document:
            raise SystemExit('Unexpected API response: no document.')
        tmp = tree_path.with_suffix('.tmp')
        tmp.write_text(json.dumps({
            'synced_at': synced_at,
            'name': data.get('name'),
            'node': {'document': document},
            'components': data.get('components', {}),
            'styles': data.get('styles', {}),
        }, ensure_ascii=False), encoding='utf-8')
        tmp.replace(tree_path)
        print(f'Cached full tree -> {tree_path.name}')

    if images_only:
        sync_image_fills(file_key, key_q, document, token, reuse=True)
        print('Done.')
        return

    exports = collect_exports(document)
    node_index = {}
    for node in iter_nodes(document):
        node_index.setdefault(node['id'], node)
    exports = dedup_exports(exports, node_index)
    chunks = chunk_ids(sorted(exports), file_key)
    print(f'Vector nodes: {sum(len(v) for v in exports.values())}, export ids: {len(exports)}, image requests needed: {len(chunks)}')
    if len(chunks) > max_image_requests:
        raise SystemExit(
            f'IDs do not fit in {max_image_requests} image request(s). '
            'Tree is cached; rerun with --max-image-requests=N to spend more quota, '
            'or use --dry-run to inspect.')

    svg_dir = cache_dir / 'svg' / key_q
    svg_dir.mkdir(parents=True, exist_ok=True)
    manifest = {'synced_at': synced_at, 'map': {}, 'missing': []}
    manifest_path = svg_dir / 'manifest.json'
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass

    links_path = svg_dir / 'links.json'
    try:
        links_doc = json.loads(links_path.read_text(encoding='utf-8'))
        links = links_doc.get('urls', {})
        links_fetched_at = links_doc.get('fetched_at')
    except (OSError, ValueError, AttributeError):
        links, links_fetched_at = {}, None

    for index, chunk in enumerate(chunks, 1):
        urls = {node_id: links[node_id] for node_id in chunk
                if url_fresh(links.get(node_id), links_fetched_at)}
        nulls = [node_id for node_id in chunk
                 if node_id in links and links[node_id] is None and fetched_fresh(links_fetched_at)]
        need = [node_id for node_id in chunk if node_id not in urls and node_id not in nulls]
        if nulls:
            print(f'  {len(nulls)} ids Figma cannot render (cached null result), not re-requesting')
        if need:
            print(f'Request: GET svg export for {len(need)} ids (Tier 1, {index}/{len(chunks)})...')
            query = urlencode({'ids': ','.join(need), 'format': 'svg'})
            result = api_get(f'https://api.figma.com/v1/images/{key_q}?{query}', token,
                             lambda: usage.record(file_key, 'svg-export', tier1=1))
            images = response_images(result)
            for node_id, value in images.items():
                if isinstance(value, str):
                    urls[node_id] = value
                    links[node_id] = value
                elif value is None:
                    links[node_id] = None
            if not any(node_id in urls for node_id in need):
                (svg_dir / 'last-response.json').write_text(
                    json.dumps(result, ensure_ascii=False)[:200000], encoding='utf-8')
                print('  WARNING: API returned no usable SVG URLs; raw response saved to last-response.json')
                print('  response head:', json.dumps(result, ensure_ascii=False)[:300])
            tmp = links_path.with_suffix('.tmp')
            tmp.write_text(json.dumps({'fetched_at': synced_at, 'urls': links}, ensure_ascii=False), encoding='utf-8')
            tmp.replace(links_path)
        else:
            print(f'Reusing {len(urls)} cached SVG links (no API request).')
        cached = [node_id for node_id in chunk
                  if node_id in urls and manifest['map'].get(node_id)
                  and (svg_dir / manifest['map'][node_id]).is_file()]
        jobs = [(node_id, urls[node_id], svg_dir) for node_id in chunk
                if node_id in urls and node_id not in cached]
        manifest['missing'].extend(node_id for node_id in chunk if node_id not in urls)
        if cached:
            print(f'  {len(cached)} svg files already cached, skipped')
        total = len(jobs)
        with ThreadPoolExecutor(max_workers=8) as pool:
            for done, (node_id, outcome) in enumerate(pool.map(download_svg, jobs), 1):
                if outcome.endswith('.svg'):
                    for covered in exports[node_id]:
                        manifest['map'][covered] = outcome
                    manifest['map'][node_id] = outcome
                else:
                    manifest['missing'].append(f'{node_id}:{outcome}')
                if done % 25 == 0 or done == total:
                    print(f'  svg progress: {done}/{total}', flush=True)
        manifest['synced_at'] = synced_at
        manifest['missing'] = sorted(set(manifest['missing']) - set(manifest['map']))
        tmp = manifest_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
        tmp.replace(manifest_path)
        print(f'  saved {len(jobs)} svg downloads, manifest updated')

    print(f'SVGs mapped: {len(manifest["map"])}, missing: {len(manifest["missing"])}')
    if manifest['missing']:
        print('Missing ids (geometry paths are cached as fallback):')
        for item in manifest['missing'][:20]:
            print(f'  {item}')

    sync_image_fills(file_key, key_q, document, token, reuse=True)
    print('Done.')


if __name__ == '__main__':
    main()