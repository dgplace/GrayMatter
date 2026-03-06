# ARCHITECTURE

## Purpose

CodeBrain is a local-first code intelligence system for indexing source repositories and exposing searchable architectural knowledge over MCP.

It has two primary runtime concerns:
- ingestion: parse code, classify intent, embed content, and persist structured knowledge
- serving: query persisted knowledge through MCP tools and a lightweight HTTP UI

## High-level Components

### 1. Ingestion Pipeline (Python)

Core files:
- `ingest.py`
- `chunker.py`
- `embedder.py`
- `classifier.py`

Responsibilities:
- walk a repository while respecting config excludes and `.gitignore`
- detect language from extension mapping
- parse source with tree-sitter where supported
- split files into semantically meaningful chunks
- classify chunks and files with an OpenAI-compatible chat model
- generate embeddings
- persist files, chunks, symbols, references, and dependencies into PostgreSQL

Design pattern:
- pipeline orchestration with explicit stages
- narrow stages for chunking, embedding, classification, and persistence

### 2. Query Server (TypeScript MCP + HTTP UI)

Core modules:
- `index.ts` (entrypoint + stable utility re-exports)
- `src/server.ts` (transport bootstrap)
- `src/mcp/*` (tool/resource/logging/search formatting/graph algorithms)
- `src/repositories/store.ts` (repo-scoped read model queries)
- `src/web/*` (embedded browser UI and JSON endpoints)

Responsibilities:
- expose MCP resources and tools
- enforce mandatory repository scope for query tools
- run hybrid search (semantic + keyword) within a selected repository
- provide symbol lookup, references, dependency tracing, file map, and intent summaries
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
- `ingestion_runs`

Responsibilities:
- store normalized indexed code metadata
- store vector embeddings for semantic retrieval
- store lexical and structural relationships for exact and dependency-style queries
- support repo-scoped query-time filtering across tools and UI APIs

Design pattern:
- relational core with vector similarity support
- write-heavy ingestion, read-heavy serving

## Data Flow

### Ingestion flow

1. Repository walk starts in `ingest.py`.
2. File paths are filtered by config excludes and Git ignore rules.
3. `chunker.py` parses supported languages with tree-sitter.
4. AST chunks are generated, with language-specific metadata where available.
5. `classifier.py` summarizes files and classifies chunk intent.
6. `embedder.py` generates file and chunk embeddings.
7. `ingest.py` stores normalized records in PostgreSQL.
8. Symbol references and dependency edges are derived and persisted.

### MCP query flow

1. MCP client calls `list_repositories` to discover indexed repos.
2. Client calls repo-scoped tools with a required `repo` argument.
3. For semantic tools, server embeds query text.
4. SQL runs with explicit repository filtering.
5. MCP formatter modules produce text responses.

### UI flow

1. Browser opens `/ui`.
2. UI fetches `/ui/api/repos` to populate the repo selector.
3. UI polls `/ui/api/tool-calls` for live MCP tool invocation counters.
4. UI fetches `/ui/api/repos/:repo/stats` and `/ui/api/repos/:repo/graph`.
5. Client-side rendering displays metrics and a browsable graph projection.

## Core Design Patterns

### Mandatory repo scope at query time

MCP query tools require a `repo` parameter, preventing accidental cross-repo mixing during search, symbol lookup, references, dependency tracing, and file intent workflows.

### Separation of concerns

- `src/server.ts` handles lifecycle and transport wiring.
- `src/mcp/tools.ts` owns tool schemas, validation flow, and orchestration.
- `src/repositories/store.ts` owns repository read-model SQL.
- `src/mcp/formatters.ts` owns textual response formatting.
- `src/web/routes.ts` and `src/web/ui.ts` own HTTP UI concerns.
- ingestion modules remain separate from MCP serving modules.

### Hybrid retrieval

Semantic search combines vector similarity with keyword fallback and result fusion, scoped to a selected repository.

### Refactoring analysis layer

The refactoring tools (`analyze_coupling`, `extract_module_interface`, `find_dependency_cycles`, `find_modularization_seams`) operate on the same indexed data without re-ingestion. They compose SQL queries over the `dependencies`, `symbol_references`, and `symbols` tables to answer structural questions. Graph algorithms (cycle detection) live in `src/mcp/graph.ts` as pure functions operating on in-memory edge lists extracted from the database. This keeps graph logic testable and separated from SQL and MCP concerns.

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
- provide a GUI equivalent of the `ingest.py` CLI for all three platforms
- manage multiple repositories simultaneously (add, remove, index, watch)
- run ingestion workers on background QThreads and report live progress
- watch N repos for file changes using concurrent watchdog Observers
- persist to system tray when windows are closed and watchers are active
- store app-level preferences (repo list, auto-watch) in a local SQLite DB

Design pattern:
- `IngestionEngine` is a thin bridge: it duplicates only the thread orchestration
  from `ingest.py main()` and replaces Rich output with Qt signals; `process_file`,
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
- ingestion run locally against configured services — either via `ingest.py` CLI
  or via the desktop application (`python -m desktop`)

Containerized MCP service publishes HTTP-only endpoints and includes the embedded UI.

## Documentation Maintenance Rules

- Update this document when adding/removing MCP tools, UI endpoints, major query behavior, schema behavior, or deployment topology.
- Keep `LOG.md` to one line per substantive change or commit.
- Keep `AGENTS.md` focused on working rules; keep this file focused on system structure and design.
