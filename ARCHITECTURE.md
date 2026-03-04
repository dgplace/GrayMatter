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
- `src/mcp/*` (tool/resource/logging/search formatting)
- `src/repositories/store.ts` (repo-scoped read model queries)
- `src/web/*` (embedded browser UI and JSON endpoints)

Responsibilities:
- expose MCP resources and tools
- enforce mandatory repository scope for query tools
- run hybrid search (semantic + keyword) within a selected repository
- provide symbol lookup, references, dependency tracing, file map, and intent summaries
- expose repository discovery and stats (`list_repositories`, repo-scoped `codebase_stats`)
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
3. UI fetches `/ui/api/repos/:repo/stats` and `/ui/api/repos/:repo/graph`.
4. Client-side rendering displays metrics and a browsable graph projection.

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

### Explicit metadata over query-time inference

Dependencies, references, and symbols are extracted during ingestion and stored explicitly so query-time work is focused on filtering, ranking, and formatting.

## Operational Topology

Typical deployment:
- PostgreSQL on local or network host
- embedding provider on local or network host
- classifier provider on local or network host
- MCP server exposing `/mcp`, `/ui`, and `/healthz`
- ingestion run locally against configured services

Containerized MCP service publishes HTTP-only endpoints and includes the embedded UI.

## Documentation Maintenance Rules

- Update this document when adding/removing MCP tools, UI endpoints, major query behavior, schema behavior, or deployment topology.
- Keep `LOG.md` to one line per substantive change or commit.
- Keep `AGENTS.md` focused on working rules; keep this file focused on system structure and design.
