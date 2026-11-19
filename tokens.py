import json
import os
from pathlib import Path

config_dir = Path.home() / '.config' / 'figma-cache-mcp'
tokens_path = config_dir / 'tokens.json'
legacy_path = config_dir / 'token'


def load():
    try:
        data = json.loads(tokens_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def save(tokens):
    config_dir.mkdir(parents=True, exist_ok=True)
    tmp = tokens_path.with_suffix('.tmp')
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(tokens, handle, ensure_ascii=False)
    tmp.replace(tokens_path)


def add(name, token):
    tokens = load()
    tokens[name] = token
    save(tokens)


def remove(name):
    tokens = load()
    tokens.pop(name, None)
    save(tokens)


def env_token():
    token = os.environ.get('FIGMA_TOKEN', '').strip()
    if token:
        return token
    try:
        return legacy_path.read_text(encoding='utf-8').strip() or None
    except OSError:
        return None


def resolve(token_name=None, one_time=None):
    if one_time and one_time.strip():
        return one_time.strip()
    if token_name and token_name != 'env':
        return load().get(token_name)
    return env_token()