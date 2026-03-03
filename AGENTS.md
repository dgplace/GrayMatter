# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

CodeBrain is a local codebase intelligence system with two independent components:

1. **Python ingestion pipeline** — walks a codebase, parses it with tree-sitter, embeds chunks via Ollama, classifies intent via an OpenAI-compatible LLM proxy, and stores everything in PostgreSQL + pgvector.
2. **TypeScript MCP server** (`index.ts`) — a stdio MCP server that exposes semantic search, symbol lookup, dependency tracing, and stats queries over the database.

## Commands

### Python pipeline (ingestion)
```bash
pip install -r requirements.txt

# Ingest a codebase
python ingest.py /path/to/repo

# Force full re-index (ignore hash cache)
python ingest.py /path/to/repo --force

# Watch mode (auto re-index on file save)
python ingest.py /path/to/repo --watch

# Override worker count
python ingest.py /path/to/repo --workers 8
```

### MCP server (TypeScript)
```bash
npm install
npm run build      # tsc → dist/
npm start          # node dist/index.js
npm run dev        # tsx index.ts (no build needed)
```

### Database setup (first time)
```bash
docker run -d --name codebrain-db \
  -e POSTGRES_DB=codebrain -e POSTGRES_USER=codebrain \
  -e POSTGRES_PASSWORD=codebrain_local \
  -p 5433:5432 pgvector/pgvector:pg16

psql "postgresql://codebrain:codebrain_local@localhost:5433/codebrain" -f schema.sql
```

## Architecture

### Data flow
```
Codebase files
  → chunker.py (tree-sitter AST chunking)
  → embedder.py (Ollama /api/embed → 768-dim vectors)
  → classifier.py (LLM proxy /v1/chat/completions → intent + summary)
  → PostgreSQL via ingest.py
  → MCP server (index.ts) queries DB at runtime
```

### Key files
| File | Role |
|------|------|
| `ingest.py` | CLI entrypoint; orchestrates the pipeline |
| `chunker.py` | AST-aware chunking via tree-sitter; falls back to line-based for unsupported languages |
| `embedder.py` | Wraps Ollama `/api/embed`; all embeddings are 768-dim (nomic-embed-text) |
| `classifier.py` | Wraps OpenAI-compatible `/v1/chat/completions` for intent classification and file summarization |
| `index.ts` | MCP server; re-embeds queries at runtime to do cosine similarity search |
| `schema.sql` | Full DB schema + stored functions (`search_code`, `find_symbol`, `trace_dependencies`) |
| `codebrain.toml` | Single config file for all components |

### Configuration (`codebrain.toml`)
- `[embeddings]` — Ollama model + URL (used by `embedder.py` and `index.ts`)
- `[classifier]` — OpenAI-compatible model + `base_url` (used by `classifier.py`); currently points to `http://localhost:3000`
- `[database]` — PostgreSQL connection string (port 5433)
- `[ingestion]` — chunk size, overlap, worker count, exclude patterns

### LLM endpoints (split)
- **Embeddings**: Ollama at `http://localhost:11434` via `/api/embed` — not available on the local proxy
- **Text generation**: OpenAI-compatible proxy at `http://localhost:3000` via `/v1/chat/completions`

### Database schema
- `files` — one row per source file; includes file-level embedding, LLM summary, and architectural role
- `code_chunks` — AST-aware chunks with embeddings, intent classification, and symbol metadata
- `symbols` — extracted functions/classes/types with qualified names and embeddings
- `dependencies` — directed import/call graph edges between files and symbols
- `ingestion_runs` — audit log of pipeline runs

### MCP tools exposed
`semantic_search`, `find_symbol`, `trace_dependencies`, `get_file_map`, `get_intent`, `codebase_stats`

### tsconfig note
`tsconfig.json` sets `rootDir: "src"` but `index.ts` lives at the repo root. If building with `tsc`, either move `index.ts` into `src/` or change `rootDir` to `"."`.

## Adding a Claude Desktop / Claude Code MCP connection
```json
// claude_desktop_config.json
{
  "mcpServers": {
    "codebrain": {
      "command": "node",
      "args": ["/absolute/path/to/GrayMatter/dist/index.js"],
      "env": { "DATABASE_URL": "postgresql://codebrain:codebrain_local@localhost:5433/codebrain" }
    }
  }
}
```
```bash
# Claude Code
claude mcp add codebrain --transport stdio -- node /absolute/path/to/GrayMatter/dist/index.js
```
