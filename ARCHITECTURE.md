# ARCHITECTURE

## Purpose

CodeBrain is a local-first code intelligence system for indexing source repositories and exposing searchable architectural knowledge over MCP.

It has two primary runtime concerns:
- ingestion: parse code, classify intent, embed content, and persist structured knowledge
- serving: query persisted knowledge through MCP tools and a lightweight HTTP UI

## High-level Components

### 1. Ingestion Pipeline (Python)

Core files:
- `codebrain/ingest.py`
- `codebrain/ingestion/runtime.py`
- `codebrain/ingestion/schema.py`
- `codebrain/ingestion/relationships.py`
- `codebrain/ingestion/dependencies.py`
- `codebrain/ingestion/clusters.py`
- `codebrain/ingestion/flows.py`
- `codebrain/chunker.py`
- `resolver.py`
- `codebrain/embedder.py`
- `codebrain/classifier.py`

Responsibilities:
- walk a repository while respecting config excludes and `.gitignore`
- detect language from extension mapping
- parse source with tree-sitter where supported
- split files into semantically meaningful chunks
- resolve lexical references into a uniform resolver record shape before persistence
- classify chunks and files with an OpenAI-compatible chat model
- generate embeddings
- persist files, chunks, symbols, references, and dependencies into PostgreSQL

Design pattern:
- thin entrypoint facade (`codebrain/ingest.py`) delegating to focused ingestion submodules
- narrow stages for chunking, embedding, classification, and persistence

### 2. Query Server (TypeScript MCP + HTTP UI)

Core modules:
- `index.ts` (entrypoint + stable utility re-exports)
- `src/server.ts` (transport bootstrap)
- `src/mcp/*` (tool/resource/logging/search formatting/graph algorithms)
- `src/repositories/store.ts` (repo-scoped read model queries)
- `src/web/routes.ts` + `src/web/ui.ts` (HTTP route registration and HTML shell for `/ui`)
- `src/web/assets/styles.css` + `src/web/assets/app.ts` (browser-side neon-on-light design tokens and the 3D WebGL graph renderer based on `3d-force-graph` + Three.js with directional arrows and rotate/zoom/pan; bundled to `dist/src/web/assets/app.js` by `scripts/build-ui.mjs` via esbuild (minified) and served from `/ui/assets/*`)

Responsibilities:
- expose MCP resources and tools
- enforce mandatory repository scope for query tools
- run hybrid search (semantic + keyword) within a selected repository
- provide symbol lookup, references, dependency tracing, file map, and intent summaries
- preserve language-aware reference traversal in dependency tracing so same-named symbols across languages do not create phantom edges
- support implementation traversal for both `implements` and `extends`, including unresolved external base names
- provide domain-level cluster navigation via `clusters` and `cluster_members`
- provide execution-flow membership lookup via `find_flows` (by symbol or by flow)
- provide node-level prose context via `describe_node(kind,id)` over linked `doc_links`
- expose repository discovery and stats (`list_repositories`, repo-scoped `codebase_stats`)
- provide refactoring analysis: coupling metrics, module interface extraction, cycle detection, and modularization seam planning
- host `/ui` for semantic graph browsing and per-repo stats

Design pattern:
- transport layer (`src/server.ts`) delegates to tool and route modules
- data access centralized in repo-store queries, presentation in formatter/UI modules
- shared utilities (embedding, DB, logging) isolated from tool handlers

### 3. Persistence Layer (PostgreSQL + pgvector)

Core file:
- `schema.sql`

Primary tables:
- `files`
- `code_chunks`
- `symbols`
- `symbol_references`
- `dependencies`
- `clusters`
- `cluster_members`
- `flows`
- `flow_members`
- `doc_links`
- `ingestion_diagnostics`
- `ingestion_runs`

Responsibilities:
- store normalized indexed code metadata
- store vector embeddings for semantic retrieval
- store lexical and structural relationships for exact and dependency-style queries
- preserve lexical references alongside optional resolved symbol targets, confidence, and resolver metadata for future exact-reference upgrades
- persist callback-framework missing-extractor diagnostics so coverage gaps are queryable per repository
- support repo-scoped query-time filtering across tools and UI APIs

Design pattern:
- relational core with vector similarity support
- write-heavy ingestion, read-heavy serving

## Data Flow

### Ingestion flow

1. Repository walk starts in `codebrain/ingest.py`, delegated to `codebrain/ingestion/runtime.py`.
2. File paths are filtered by config excludes and Git ignore rules.
3. `codebrain/chunker.py` parses supported languages with tree-sitter.
4. AST chunks are generated, with language-specific metadata where available.
5. `codebrain/classifier.py` summarizes files and classifies chunk intent.
6. `codebrain/embedder.py` generates file and chunk embeddings.
7. `resolver.py` turns chunk-level lexical references into resolver records with `target_symbol_id`, `resolution_confidence`, `resolution_method`, and `reference_kind_v2` when possible.
8. HTML/CSS are content-only inputs: they still produce chunks/embeddings for search, but ingestion skips symbol-relationship and symbol-reference persistence for those files.
9. Exact resolution is strategy-driven: `scip-typescript` runs for TypeScript-family repos that have both `tsconfig.json` and installed `node_modules`, `scip-python` runs for repositories with recognizable Python project markers and a compatible runtime, and `scip-dotnet` runs for C# repositories where `.sln` or `.csproj` markers are detected. Each strategy joins SCIP occurrence ranges back to `symbols` rows by repo-relative file path plus declaration line range, and unresolved or ambiguous sites fall back cleanly to heuristic name resolution with explicit confidence scores.
10. Callback/event extraction can emit `reference_kind_v2='callback_register'` or `reference_kind_v2='event_emit'` for configured patterns (for example emitter `.on`, DOM `addEventListener`, HTTP route registrations) via `ingestion.callback_extractors_enabled`.
11. Heuristic fallback resolution is language-family scoped to prevent cross-language collisions (for example, TypeScript references do not co-resolve to same-named Python symbols). Node-family files (`typescript|tsx|javascript|jsx`) share one compatibility bucket; other languages resolve by exact language match.
12. During multi-worker full ingest, unresolved reference rows are persisted first and then refreshed in one serial repo-wide resolution pass after all symbols are stable so exact strategies can target the final `symbols` ids.
13. `codebrain/ingest.py` + `codebrain/ingestion/*` store normalized records in PostgreSQL.
14. After each ingest run, dependency cycles are materialized and callback-framework diagnostics are rebuilt from dependency/reference evidence (for example `missing_extractor` gaps keyed by framework and affected file count).
15. Clustering persists semantic `clusters` + `cluster_members`; ingestion first tries NetworkX Leiden dispatch, then a local `igraph` + `leidenalg` Leiden path, then falls back to Louvain and finally connected-components so cluster materialization cannot abort the run.
16. A flow materialization pass computes call-style weakly connected symbol groups from resolved call/service edges, assigns deterministic `flow_key` ids, and persists `flows` + `flow_members` for symbol-to-flow and flow-to-symbol queries.
17. Watch-mode single-file updates use the same resolver stage to resolve the changed file immediately and re-resolve only inbound refs that previously targeted symbols defined in the changed file, while surfacing warning-only guardrails for large fan-out.

### MCP query flow

1. MCP client calls `list_repositories` to discover indexed repos.
2. Client calls repo-scoped tools with a required `repo` argument.
3. For semantic tools, server embeds query text.
4. SQL runs with explicit repository filtering.
5. MCP formatter modules produce text responses.

### UI flow

1. Browser opens `/ui` and loads `/ui/assets/styles.css` (design tokens) plus `/ui/assets/app.js` (3D WebGL renderer + panel logic, bundled by esbuild).
2. UI fetches `/ui/api/repos` to populate the repo selector.
3. UI polls `/ui/api/tool-calls` for live MCP tool invocation counters.
4. UI fetches `/ui/api/repos/:repo/stats`, `/ui/api/repos/:repo/graph`, `/ui/api/repos/:repo/modules`, and `/ui/api/repos/:repo/size`.
5. Client feeds the graph payload to `3d-force-graph` (Three.js) and renders nodes + directional arrow links coloured by `reference_kind`/`dependency_kind`; rotate, zoom, pan, and a floating colour-swatch legend are wired client-side.

## Core Design Patterns

### Mandatory repo scope at query time

MCP query tools require a `repo` parameter, preventing accidental cross-repo mixing during search, symbol lookup, references, dependency tracing, and file intent workflows.

### Separation of concerns

- `src/server.ts` handles lifecycle and transport wiring.
- `src/mcp/tools.ts` is the composition entrypoint for MCP tool registration.
- `src/mcp/tooling/*.ts` owns grouped tool schemas/handlers plus shared repo-validation and normalization helpers.
- `src/repositories/store.ts` owns repository read-model SQL.
- `src/mcp/formatters.ts` owns textual response formatting.
- `src/web/routes.ts` owns HTTP UI route registration and static asset serving for `/ui/assets/*`; `src/web/ui.ts` owns the HTML shell; `src/web/assets/{styles.css,app.ts}` own all browser-side styling and rendering.
- ingestion modules remain separate from MCP serving modules.

### Hybrid retrieval

Semantic search combines vector similarity with keyword fallback and result fusion, scoped to a selected repository.

### Refactoring analysis layer

The refactoring tools (`analyze_coupling`, `extract_module_interface`, `find_cycles`, `find_modularization_seams`) operate on the same indexed data without re-ingestion. They compose SQL queries over the `dependencies`, `symbol_references`, `symbols`, and `dependency_cycles` materialization table to answer structural questions. Cycle analysis is served from persisted SCC snapshots to keep query-time behavior consistent across callers and avoid duplicate implementations.

### Module intent synthesis

`codebrain/synthesize_modules.py` overlays domain-specific narrative intents on
directories and on the existing coupling-based clusters. It runs either inline as the
final stage of `codebrain.ingest --synthesize`, or as a standalone command for refreshing
narratives without re-ingesting. Two module kinds are produced:

**Directory modules** (`kind='directory'`): one per directory with enough files. The LLM
receives file summaries and chunk-level `intent_detail` to produce a narrative intent
describing what the directory accomplishes in the application.

**Logical modules** (`kind='logical'`): cross-directory communities sourced from the
`clusters` / `cluster_members` rows produced during ingestion (see *Cluster
materialization* in this document). Synthesis does not run a second clustering pass — it
filters existing symbol-granularity clusters down to those that span ≥2 directories and
cover at least `--min-files` distinct files, then asks the LLM to author a narrative
`module_name`, `summary`, and `dominant_intent` for each survivor. When a repository has
no symbol clusters, file-granularity clusters are used instead. Each `module_intents` row
records its source `cluster_id` so the two surfaces can be joined.

Because ingestion already runs Leiden (with Louvain and connected-components fallbacks)
on the resolved symbol-coupling graph, logical-module quality is improved transitively
when clustering is improved. Tune Leiden resolution under `codebrain.toml`
`[clustering] resolution` and re-ingest; synthesis itself has no community-detection
knobs.

The `member_symbols` column stores the class/type names (or file basenames, in the
file-cluster fallback) for each logical module so `get_module_map` can render them
without re-joining.

### Explicit metadata over query-time inference

Dependencies, references, and symbols are extracted during ingestion and stored explicitly so query-time work is focused on filtering, ranking, and formatting.

### 4. Desktop Application (Python + PySide6)

Entry point:
- `python -m desktop` from the project root

Core files:
- `desktop/__main__.py` — QApplication entry point
- `desktop/app.py` — lifecycle coordinator (CodeBrainApp)
- `desktop/core/engine.py` — IngestionEngine: wraps pipeline functions, emits Qt signals
- `desktop/core/watcher.py` — MultiRepoWatcher: N-repo watchdog observer management
- `desktop/core/state.py` — AppState: local SQLite persistence for repo list and settings

UI files (`desktop/ui/`):
- `main_window.py` — sidebar navigation + stacked view layout
- `repo_panel.py` — add/remove repos, index and watch controls, status cards
- `ingestion_view.py` — live progress bars and file log per active ingestion
- `stats_view.py` — per-repo aggregate stats from PostgreSQL
- `history_view.py` — ingestion_runs table display
- `settings_dialog.py` — database, embeddings, and classifier config editor
- `tray.py` — system tray icon, context menu, balloon notifications

Responsibilities:
- provide a GUI equivalent of the `codebrain/ingest.py` CLI for all three platforms
- manage multiple repositories simultaneously (add, remove, index, watch)
- run ingestion workers on background QThreads and report live progress
- watch N repos for file changes using concurrent watchdog Observers
- persist to system tray when windows are closed and watchers are active
- store app-level preferences (repo list, auto-watch) in a local SQLite DB

Design pattern:
- `IngestionEngine` is a thin bridge: it duplicates only the thread orchestration
  from `codebrain/ingest.py main()` and replaces Rich output with Qt signals; `process_file`,
  `walk_repo`, and `ensure_schema` are called directly with no modification.
- Qt signal/slot cross-thread delivery handles safe UI updates from worker threads.
- `MultiRepoWatcher` gives each repo its own watchdog Observer and connection pool
  for clean lifecycle isolation; `EmbeddingClient` and `IntentClassifier` are shared
  across repos since they are thread-safe.
- `AppState` (SQLite) stores only desktop UI state; all indexed code data remains
  in the containerized PostgreSQL instance.

## Operational Topology

Typical deployment:
- PostgreSQL on local or network host
- embedding provider on local or network host
- classifier provider on local or network host
- MCP server exposing `/mcp`, `/ui`, and `/healthz`
- ingestion run locally against configured services — either via `codebrain/ingest.py` CLI
  or via the desktop application (`python -m desktop`)

Containerized MCP service publishes HTTP-only endpoints and includes the embedded UI.

## Documentation Maintenance Rules

- Update this document when adding/removing MCP tools, UI endpoints, major query behavior, schema behavior, or deployment topology.
- Keep `LOG.md` to one line per substantive change or commit.
- Keep `AGENTS.md` focused on working rules; keep this file focused on system structure and design.
