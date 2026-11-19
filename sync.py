import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import usage

if len(sys.argv) < 3:
    raise SystemExit("Usage: python sync.py FILE_KEY NODE_ID [NODE_ID ...]")

file_key, *node_ids = sys.argv[1:]
node_ids = [node.replace("-", ":") for node in node_ids]
token = os.environ.get("FIGMA_TOKEN")
if not token:
    raise SystemExit("Set FIGMA_TOKEN first.")

query = urlencode({"ids": ",".join(node_ids)})
url = f"https://api.figma.com/v1/files/{quote(file_key, safe='')}/nodes?{query}"
request = Request(url, headers={"X-Figma-Token": token})

try:
    with urlopen(request, timeout=30) as response:
        usage.record(file_key, "nodes", tier1=1)
        nodes = json.load(response)["nodes"]
except HTTPError as error:
    usage.record(file_key, "nodes", tier1=1)
    if error.code == 429:
        retry_after = error.headers.get("Retry-After", "not provided")
        raise SystemExit(f"Figma rate limit (429). Retry-After: {retry_after}. Cache unchanged.")
    raise SystemExit(f"Figma returned HTTP {error.code}. Cache unchanged.")

cache_dir = Path(__file__).resolve().parent / "cache"
cache_dir.mkdir(exist_ok=True)
synced_at = datetime.now(timezone.utc).isoformat()

for node_id in node_ids:
    node = nodes.get(node_id)
    if node is None:
        print(f"Node not found: {node_id}; previous cache kept.")
        continue
    path = cache_dir / f"{quote(file_key, safe='')}__{quote(node_id, safe='')}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"synced_at": synced_at, "node": node}, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(path)
    print(f"Cached {node_id}")
