# Plan: Module-Level Intent Synthesis (Repository-Current)

## Current State (2026-03-07)
- `module_intents` is not implemented yet: no table, no synthesis script, no MCP tool, no app endpoint.
- MCP currently includes index management tools: `get_index_size` and `delete_index`.
- The embedded CodeBrain app (`/ui`) already exposes that new index-management capability via:
  - `GET /ui/api/repos/:repo/size`
  - `DELETE /ui/api/repos/:repo`
  - `Index Management` panel in `src/web/ui.ts`

This plan adds module-level intent synthesis and exposes it in both MCP and the embedded CodeBrain app, following the same store/routes/ui pattern used by index management.

## Goal
Add repository-scoped module intent synthesis with two module kinds:
- `directory`: folder-based modules (>= N source files)
- `logical`: cross-folder communities from dependency/reference graph

Then expose results in:
- MCP tool: `get_module_map`
- CodeBrain app (`/ui`): module-intents panel + API route
- Desktop app (PySide6): module-intents panel in the Statistics workflow

## Files To Modify / Create

| File | Change |
|---|---|
| `schema.sql` | Add `module_intents` DDL |
| `ingest.py` | Add `module_intents` create/index statements to `SCHEMA_PATCHES` |
| `src/db.ts` | Add `module_intents` create/index statements to `SCHEMA_PATCHES` |
| `synthesize_modules.py` | New standalone synthesis CLI |
| `src/repositories/store.ts` | Add `ModuleIntent` type + `getModuleIntents()` |
| `src/mcp/tools.ts` | Add `get_module_map` MCP tool |
| `src/web/routes.ts` | Add `GET /ui/api/repos/:repo/modules` endpoint |
| `src/web/ui.ts` | Add module-intents panel and rendering logic |
| `desktop/core/engine.py` | Add DB query helper for module intents |
| `desktop/ui/stats_view.py` | Add module-intents display section for selected repo |
| `tests/web-ui.test.ts` | Assert module panel/API hook is rendered |

## 1. Schema

Add `module_intents`:

```sql
CREATE TABLE IF NOT EXISTS module_intents (
  repo            TEXT NOT NULL,
  module_path     TEXT NOT NULL, -- directory path OR "_logical/<slug>"
  kind            TEXT NOT NULL DEFAULT 'directory', -- 'directory' | 'logical'
  module_name     TEXT,
  summary         TEXT,
  role            TEXT,
  dominant_intent TEXT,
  file_count      INTEGER NOT NULL DEFAULT 0,
  chunk_count     INTEGER NOT NULL DEFAULT 0,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (repo, module_path)
);
CREATE INDEX IF NOT EXISTS idx_module_intents_repo ON module_intents(repo);
CREATE INDEX IF NOT EXISTS idx_module_intents_kind ON module_intents(repo, kind);
```

Apply in `schema.sql`, `ingest.py` patch list, and `src/db.ts` patch list.

## 2. Synthesis CLI (`synthesize_modules.py`)

CLI:

```bash
python synthesize_modules.py --repo <name> [--mode directory|logical|all] [--min-files 3] [--config codebrain.toml]
```

Implementation notes:
- reuse `load_config()` from `ingest.py` (includes `.env/codebrain.toml` merge behavior)
- reuse `IntentClassifier` for summary/role/dominant-intent synthesis
- default mode `all`
- upsert into `module_intents` by `(repo, module_path)`

Directory pass:
- group by parent directory from `files.path`
- include only dirs with `>= min-files`
- aggregate file summaries/intents/chunk counts
- synthesize `summary`, `role`, `dominant_intent`

Logical pass:
- build file graph from `dependencies` + `symbol_references`
- run `networkx.community.greedy_modularity_communities`
- drop communities below `min-files`
- skip communities fully contained in one directory
- synthesize name + summary/role/dominant-intent
- write as `_logical/<slug>`

## 3. MCP Surface

In `src/repositories/store.ts`:
- add `ModuleIntent` type
- add `getModuleIntents(repo, pathPrefix?, kind?)`

In `src/mcp/tools.ts`:
- register `get_module_map` with required `repo` and optional `path_prefix`, `kind`
- enforce repo existence with current `requireRepository()` flow
- format output in grouped sections (`directory`, `logical`)
- return explicit “run synthesize_modules.py first” message when empty

## 4. CodeBrain App (`/ui`) Exposure

Add a module-intents read path to the existing embedded app (which already has index-management controls):

1. `src/web/routes.ts`
- add `GET /ui/api/repos/:repo/modules`
- params:
  - `kind` (`directory|logical|all`, default `all`)
  - `path_prefix` (optional)
- use `repositoryExists()` guard + `getModuleIntents()`

2. `src/web/ui.ts`
- add a new sidebar panel: `Module Intents`
- fetch modules on repo selection and refresh
- render grouped directory/logical cards:
  - module name/path
  - role + dominant intent
  - file/chunk counts
  - summary
- preserve existing `Index Management` behavior unchanged

## 5. Desktop App Exposure (`python -m desktop`)

Expose module intents in the existing desktop stats flow without changing watcher/ingestion behavior:

1. `desktop/core/engine.py`
- add `get_module_intents(repo_name: str, kind: str = "all", path_prefix: str = "") -> list[dict]`
- query `module_intents` with repo/kind/path filtering, ordered by `kind, module_path`
- return empty list on DB errors to match current desktop defensive behavior

2. `desktop/ui/stats_view.py`
- add a "Module Intents" group below current summary/language blocks
- add a `kind` selector (`all`, `directory`, `logical`) and refresh button reuse
- render each module with:
  - module name/path
  - role + dominant intent
  - file/chunk counts
  - summary text
- refresh module-intent data on:
  - repo selection change
  - manual refresh click
  - `engine.repo_completed` for the selected repo

## 6. Verification

1. Schema patching:
- run `python ingest.py /path/to/repo --force` once
- confirm `module_intents` exists

2. Synthesis:
- `python synthesize_modules.py --repo <repo> --mode all`
- verify rows:
  - `SELECT * FROM module_intents WHERE repo='<repo>' ORDER BY kind, module_path;`

3. MCP:
- call `get_module_map` with `repo=<repo>, kind=all`
- verify grouped directory/logical output

4. Embedded app:
- run server, open `/ui`
- select repo and verify module panel loads
- verify `/ui/api/repos/:repo/modules` returns data

5. Desktop app:
- run `python -m desktop`
- open Statistics for an indexed repo
- verify module intents render and `kind` filter switches between `all/directory/logical`
- run ingestion for the selected repo and confirm the panel auto-refreshes on completion

6. Tests/build:
- `npm test`
- `node node_modules/typescript/lib/tsc.js --noEmit`
- `.venv/bin/python -m pytest -q`

Note: desktop UI currently has limited automated coverage in this repo; manual verification remains required for the new desktop module-intents view.
