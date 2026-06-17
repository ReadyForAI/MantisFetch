# Migration Notice: LarkScout → MantisFetch

The project has been renamed from **LarkScout** to **MantisFetch** (org `ReadyForAI` unchanged).
This is a hard rename with **no backward-compatibility shims** — downstream repos and deployments
must update. HTTP endpoints, ports, API schemas, and on-disk data formats are **unchanged**.

## TL;DR for downstream repos

1. Repo URL: `github.com/ReadyForAI/LarkScout` → `github.com/ReadyForAI/MantisFetch` (GitHub redirects, but update remotes/submodules).
2. Rename every `LARKSCOUT_*` environment variable to `MANTISFETCH_*` (same suffix).
3. Move the data directory `~/.larkscout/docs` → `~/.mantisfetch/docs` (or point `MANTISFETCH_DOCS_DIR` at the old path).
4. Python package / SDK / imports renamed (see table).

## Breaking changes

| Area | Before | After |
| --- | --- | --- |
| Brand string | `LarkScout` | `MantisFetch` |
| GitHub repo | `ReadyForAI/LarkScout` | `ReadyForAI/MantisFetch` |
| Distribution package | `larkscout` | `mantisfetch` |
| Python SDK package | `larkscout-client` (`import larkscout_client`) | `mantisfetch-client` (`import mantisfetch_client`) |
| Entry point | `larkscout_server.py` | `mantisfetch_server.py` |
| Internal packages | `larkscout_browser` / `larkscout_docreader` / `larkscout_common` | `mantisfetch_browser` / `mantisfetch_docreader` / `mantisfetch_common` |
| Env var prefix | `LARKSCOUT_*` (all 51 vars) | `MANTISFETCH_*` |
| Data directory | `~/.larkscout/docs` | `~/.mantisfetch/docs` |
| Skill files | `skills/larkscout-*-SKILL.md` | `skills/mantisfetch-*-SKILL.md` |
| FastAPI title | `"LarkScout"` | `"MantisFetch"` |

### Environment variables

Every variable is a prefix swap, e.g.:

```
LARKSCOUT_DOCS_DIR        → MANTISFETCH_DOCS_DIR
LARKSCOUT_HOST_DOCS_DIR   → MANTISFETCH_HOST_DOCS_DIR
LARKSCOUT_LLM_PROVIDER    → MANTISFETCH_LLM_PROVIDER
LARKSCOUT_LLM_API_KEY     → MANTISFETCH_LLM_API_KEY
LARKSCOUT_API_KEY         → MANTISFETCH_API_KEY
...  (all LARKSCOUT_* → MANTISFETCH_*)
```

Update `.env` files, CI secrets, k8s/compose manifests, and any shell exports.

### Data directory migration

Captured/parsed documents on disk do **not** embed the brand name (manifest.json / doc-index.json
are unchanged), so existing data is portable by moving the folder:

```bash
mv ~/.larkscout ~/.mantisfetch
# or keep data in place and point the service at it:
export MANTISFETCH_DOCS_DIR=~/.larkscout/docs
```

### Docker

- compose service name `larkscout` → `mantisfetch`
- volume mount `/root/.larkscout/docs` → `/root/.mantisfetch/docs`
- all `LARKSCOUT_*` compose env keys → `MANTISFETCH_*`
- run command `python larkscout_server.py` → `python mantisfetch_server.py`

## What did NOT change (no action needed)

- HTTP API: routes (`/web/*`, `/doc/*`, `/health`), request/response schemas, status codes.
- Default port: `9898`.
- On-disk formats: `manifest.json`, `doc-index.json` (v2), section/table layout.
- WebMCP behavior and the browser distill/act contract.

## Action items by consumer

- **BA-Agents** (`github.com/RuiDiFu/BA-Agents`): update the base URL/remote, env var prefix, and
  any `larkscout` package/import references. API calls themselves are unchanged.
- **NodalOS**: no MCP server exists yet; the planned MantisFetch MCP server will use the new name.
- **Deployments / CI**: rotate env var names, update the data-dir mount, repoint git remotes.
