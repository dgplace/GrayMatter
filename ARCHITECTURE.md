# ARCHITECTURE

## Purpose

CodeBrain is a local-first code intelligence system for indexing source repositories and exposing searchable architectural knowledge over MCP.

It has two primary runtime concerns:
- ingestion: parse code, classify intent, embed content, and persist structured knowledge
- serving: query the persisted knowledge through MCP tools for discovery and navigation

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
- each stage has a narrow concern: chunking, embedding, classification, persistence

### 2. Query Server (TypeScript MCP)

Core file:
- `index.ts`

Responsibilities:
- expose MCP resources and tools
- run semantic and hybrid search over indexed content
- provide symbol lookup, file mapping, intent summaries, dependency tracing, and repository stats
- format query results for MCP clients

Design pattern:
- thin transport layer over a database-backed query service
- tool handlers are request adapters around SQL-backed lookups and ranking logic

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

Design pattern:
- relational core with vector search support
- write-heavy ingestion, read-heavy query serving

## Data Flow

### Ingestion flow

1. Repository walk starts in `ingest.py`.
2. File paths are filtered by config excludes and Git ignore rules.
3. `chunker.py` parses supported languages with tree-sitter.
4. AST chunks are generated, with language-specific metadata where available.
5. `classifier.py` summarizes files and classifies chunk intent.
6. `embedder.py` generates file and chunk embeddings.
7. `ingest.py` stores normalized records in PostgreSQL.
8. Additional symbol references and dependency edges are derived during ingestion.

### Query flow

1. MCP client calls a tool on `index.ts`.
2. If needed, the server embeds the incoming query.
3. SQL queries run against the indexed database.
4. Result ranking and formatting happen in the MCP layer.
5. Structured text responses are returned to the client.

## Core Design Patterns

### Separation of concerns

- `chunker.py` should only understand source structure and extraction.
- `embedder.py` should only talk to embedding providers.
- `classifier.py` should only talk to text-generation/classification providers.
- `ingest.py` should orchestrate and persist, not own language-specific parsing logic.
- `index.ts` should query and present, not reimplement ingestion behavior.

### Hybrid retrieval

The query server uses hybrid retrieval:
- semantic vector search for conceptual matches
- keyword/symbol matching for exact or sparse terminology

This avoids relying on either pure embeddings or pure text matching alone.

### Progressive precision

The intended lookup strategy is:
- use exact or symbol-oriented tools when identifiers are known
- use hybrid semantic search when only the concept is known
- use dependency and reference tools once the target symbol/file is identified

### Language-specific extraction on top of generic infrastructure

The system uses a generic ingestion/query framework, but extraction quality is language-dependent.
Language-specific improvements should be added inside the extraction layer rather than by scattering special cases across the entire system.

### Explicit metadata over inference at query time

Whenever possible, useful relationships should be extracted during ingestion and stored explicitly.
Query-time reconstruction should be limited to ranking, disambiguation, and formatting.

## Current Architectural Boundaries

### What belongs in ingestion

- AST parsing
- symbol extraction
- reference extraction
- dependency edge generation
- file/chunk embeddings
- chunk/file classification

### What belongs in MCP query handling

- search orchestration
- ranking and result fusion
- exact-match fallbacks
- human-readable formatting
- transport/session handling

### What should not be mixed

- transport code should not contain parsing logic
- classifier prompt logic should not contain database concerns
- schema evolution should not be hidden inside unrelated ranking code

## Swift-specific Notes

Swift is an important target language for this repository.
The current architecture supports:
- top-level symbol extraction
- extension-aware symbol metadata
- nested member symbol indexing
- Swift service-style dependency edges from typed properties, initializer injection, and member-call usage

This is still lighter than full semantic analysis. It is designed as a practical indexing layer, not a compiler-grade reference resolver.

## Operational Topology

The system can run split across machines.
Typical deployment:
- PostgreSQL on a network host
- embedding provider on a network host
- classifier on a network host
- MCP server on a network host
- ingestion run locally against remote services

This means config defaults and host allowlists matter operationally and must be updated when topology changes.

## Documentation Maintenance Rules

- Update this document when adding new tables, new MCP tools, new major extraction logic, or deployment topology changes.
- Keep `LOG.md` to one line per substantive change or commit.
- Keep `AGENTS.md` focused on working rules; keep this file focused on system structure and design.
