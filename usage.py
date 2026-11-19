import fcntl
import json
from datetime import datetime, timezone
from pathlib import Path

cache_dir = Path(__file__).resolve().parent / 'cache'


def record(file_key, kind, tier1=0, tier2=0):
    cache_dir.mkdir(exist_ok=True)
    path = cache_dir / 'usage.json'
    with (cache_dir / '.usage.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            usage = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            usage = {}
        entry = usage.setdefault(file_key, {'tier1': 0, 'tier2': 0, 'history': []})
        entry['tier1'] += tier1
        entry['tier2'] += tier2
        entry['history'].append({
            'at': datetime.now(timezone.utc).isoformat(),
            'kind': kind, 'tier1': tier1, 'tier2': tier2,
        })
        entry['history'] = entry['history'][-50:]
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(usage, ensure_ascii=False), encoding='utf-8')
        tmp.replace(path)


def load():
    try:
        return json.loads((cache_dir / 'usage.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}