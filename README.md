# Figma Cache MCP

A local [MCP](https://modelcontextprotocol.io) server that caches Figma design data once and then serves it from disk, so an AI agent can read designs without spending any extra Figma API requests. Ships with a web management panel for cache status, quota usage and syncs.

The point: iterate with an agent without burning through a limited monthly Figma request quota.

## Features

- **Whole-file cache**: design tree with path geometry (`geometry=paths`), file name, components and styles
- **Icon SVG cache**: multi-vector icons are exported as **one complete SVG**; requesting any inner vector id still returns the full icon. Content-identical exports are deduplicated to keep request counts low
- **Image fills**: image bytes are downloaded to disk and served by local path. Temporary Figma links are refreshed only when needed, with a 60-second cooldown and `Retry-After` handling
- **Safe cache-miss behaviour**: if a node is not cached, no request is sent to Figma — only an error is returned
- **Quota accounting**: per-tier (Tier 1 / Tier 2) counters, a 50-entry history, and a `--dry-run` mode that shows the cost of an operation before it runs
- **Multiple tokens**: stored in `~/.config/figma-cache-mcp/tokens.json` with mode `600`; values are never returned to the browser
- **Zero-cost browsing**: the panel reads local files only, so checking status never spends quota

## Requirements

- Linux, WSL, or macOS. Native Windows is not supported: the code uses `fcntl` for file locking.
- Python 3.10 or newer
- A Figma personal access token: Figma → Settings → Security → Personal access tokens

## Install

```bash
git clone <REPO_URL> figma-cache-mcp
cd figma-cache-mcp
./setup.sh
```

`setup.sh` creates the `.venv` virtualenv, installs dependencies and marks the shell scripts executable. Manual install:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
chmod +x run-mcp.sh run-panel.sh
```

## Run the panel

```bash
./run-panel.sh              # default: http://127.0.0.1:8765
./run-panel.sh --port 9000
```

The panel binds to `127.0.0.1` by default. Add a token under **Stored tokens** or set the `FIGMA_TOKEN` environment variable.

| Action | What it does | Cost |
| --- | --- | --- |
| Cache whole file | full tree + SVGs + image links | 2× Tier 1, 1× Tier 2 |
| Cache a frame | JSON for that frame only | 1× Tier 1 |
| Retry SVGs | reuses the cached tree; requests only if needed | usually 0 or 1× Tier 1 |
| Retry images | reuses fresh links | usually 0 or 1× Tier 2 |

> Downloading SVG and image files from the Figma CDN does not consume API quota.

## Connect a client (MCP)

The server uses the `stdio` transport: the client launches it, so there is no need to run it by hand.

**Client on the same machine (Linux/macOS):**

```json
{
  "mcpServers": {
    "figma-cache": {
      "command": "/absolute/path/to/figma-cache-mcp/run-mcp.sh"
    }
  }
}
```

**Client on Windows, server inside WSL:**

```json
{
  "mcpServers": {
    "figma-cache": {
      "command": "wsl.exe",
      "args": ["-d", "Ubuntu", "--", "/home/USER/figma-cache-mcp/run-mcp.sh"]
    }
  }
}
```

Where to put it:

- **Claude Desktop**: `Settings → Developer → Edit Config` → `claude_desktop_config.json`
- **Cursor**: `Settings → MCP → Add Server` → `mcp.json`
- **Other clients**: anything that supports MCP over stdio

The token comes from `~/.config/figma-cache-mcp/token` (read by `run-mcp.sh`) or the `FIGMA_TOKEN` environment variable. Without a token the server still works fully offline and serves local images; only image-link refresh is disabled.

## MCP tools

### `get_cached_design(url)`

Takes a Figma **Copy link to selection** URL (must contain `node-id`) and returns that node's JSON from cache: layout (`absoluteBoundingBox`), colors (`fills`/`strokes`), text and typography, component structure and `fillGeometry`. Nodes with a ready SVG are marked `svg_cached: true`.

### `get_cached_svg(file_key, node_id)`

Reads a complete icon SVG from cache. The instance id, its frame, its group, or any inner vector id all return the same complete SVG.

## Quota guide

- Caching a whole file: 2 Tier 1 requests (tree + SVG export) and 1 Tier 2 request (image links)
- Caching one frame: 1 Tier 1 request
- `sync-file.py --dry-run FILE_KEY` reports the request cost without spending quota
- If the ids do not fit in one SVG request, the script stops; pass `--max-image-requests=N` to allow more spend
- On HTTP 429 the `Retry-After` value is respected and the cache is left untouched

## Suggested agent prompt

```text
You have access to a local cached Figma MCP server named "figma-cache". Use it for all Figma design data. NEVER call api.figma.com or the live Figma API yourself - the account has a strict monthly request quota.

1. For any Figma selection URL (must contain node-id), call get_cached_design(url).
2. On a cache miss, do not retry and do not fetch from Figma. Ask the user to sync that file or frame in the cache panel.
3. Nodes with "svg_cached": true have a ready SVG: call get_cached_svg(file_key, node_id). Multi-vector icons return as one complete SVG.
4. For image fills, use status "cached_local" and the local path. Never guess image content.
5. Build the UI from the JSON. For vectors without SVG, use fillGeometry path data if present.
6. When you need another node, ask the user for its "Copy link to selection" URL instead of guessing ids.
```

## Project layout

| File | Role |
| --- | --- |
| `server.py` | MCP server exposing `get_cached_design` and `get_cached_svg` |
| `panel.py` | Local web panel (HTTP on localhost), token management, job runner |
| `sync.py` | Caches one or more specific nodes via `GET /v1/files/:key/nodes` |
| `sync-file.py` | Caches the whole file, icon SVGs and images; supports `--dry-run`, `--svg-only`, `--images-only` |
| `tokens.py` | Token storage with mode `600` |
| `usage.py` | Tier-aware request counters and history |
| `run-mcp.sh` / `run-panel.sh` | Launch the server or panel from `.venv` |
| `setup.sh` | Creates the virtualenv and installs dependencies |
| `cache/` | Local cache (git-ignored) |

## Privacy and security

- Tokens live in `~/.config/figma-cache-mcp/tokens.json` with mode `600` and are never sent to the browser; they are only injected into the sync process
- The panel binds to `127.0.0.1` by default. **Do not expose it on `0.0.0.0`** — it has no authentication
- `/svg/` and `/img/` paths are validated with regexes and parent-directory checks to prevent path traversal
- The `cache/` directory holds your design data; it is listed in `.gitignore` and must not be published

## Contributing

1. Fork the repository
2. Create a branch: `git checkout -b feature/name`
3. Commit your changes
4. Open a pull request

For behavioural changes, please open an issue first describing the quota impact.

## License

Released under the MIT License — see [LICENSE](LICENSE).