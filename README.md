# CodeBrain — Local Codebase Intelligence System

A fully local system that builds a semantic map of your codebase: vector embeddings, intent classification, dependency graphs, and an MCP server so any AI tool you use shares persistent understanding of your code.

---

## What You're Building

A pipeline that ingests a codebase — every file, function, class, and module — embeds it semantically, classifies intent and architectural role, maps dependencies, and stores everything in a local PostgreSQL database with pgvector. Then a local MCP server lets Claude Desktop, Claude Code, Cursor, or any MCP client query your codebase by meaning, not just grep.

### Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     Your Codebase                       │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│              Ingestion Pipeline (Python)                 │
│                                                         │
│  1. File walker + language-aware chunker                 │
│  2. AST parser (tree-sitter) → functions, classes, etc  │
│  3. Local embeddings (LM Studio / Qwen3-Embedding)       │
│  4. Intent classifier (OpenAI-compatible LLM proxy)     │
│  5. Dependency graph extraction                         │
│  6. Store everything in PostgreSQL + pgvector            │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│           PostgreSQL + pgvector (local)                  │
│                                                         │
│  Tables:                                                │
│  • code_chunks — raw code + embeddings + metadata       │
│  • symbols — functions, classes, variables               │
│  • dependencies — import/call graph edges                │
│  • files — file-level summaries + role classification    │
│  • ingestion_runs — audit log of pipeline runs           │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│              MCP Server (local, stdio)                   │
│                                                         │
│  Tools exposed:                                         │
│  • semantic_search — find code by meaning               │
│  • find_symbol — locate functions/classes/types          │
│  • trace_dependencies — follow import/call chains       │
│  • get_file_map — architectural overview of a path      │
│  • get_intent — what is this code trying to do?         │
│  • codebase_stats — overview metrics                    │
└─────────────────────────────────────────────────────────┘
```

---

## Prerequisites

- **Docker + Docker Compose** — runs PostgreSQL + pgvector
- **Ollama** running locally on port 11434 with `nomic-embed-text`
- **Python 3.11+** — ingestion pipeline
- **Node.js 22+** — MCP server
- **An OpenAI-compatible LLM proxy** at `http://localhost:3000` — used for intent classification and file summarization (e.g. LiteLLM, Ollama's OpenAI-compat endpoint, or a hosted API)

---

## Step 1: Start the Infrastructure

Make sure Ollama is running locally and has the embedding model:

```bash
ollama pull nomic-embed-text
```

Then start the database:

```bash
docker compose up -d
```

This starts **PostgreSQL + pgvector** on port 5433. The schema is applied automatically from `schema.sql` on first run.

If you are upgrading from the temporary 1024-dim schema, reset the database schema before re-indexing so the vector columns and indexes are recreated at 768 dimensions.

---

## Step 2: Configure the LLM Proxy

The classifier uses an OpenAI-compatible endpoint. In the default local setup, this repo points at LM Studio on `http://localhost:11435`.

The model and URL are set in `codebrain.toml`:

```toml
[classifier]
model = "qwen2.5-coder-7b-instruct"
base_url = "http://localhost:11435"
```

---

## Step 3: Ingest Your Codebase

```bash
pip install -r requirements.txt

# Ingest a repository
python ingest.py /path/to/your/repo

# Options:
#   --force      Re-index all files regardless of hash cache
#   --watch      Re-index automatically on file changes (uses watchdog)
#   --workers N  Override parallel worker count (default: 4 from config)
#   --config     Path to config file (default: codebrain.toml)
```

The ingestion pipeline:
1. Walks every file, skipping patterns defined in `codebrain.toml` under `[ingestion].exclude`
2. Parses supported languages with tree-sitter to extract symbols (functions, classes, interfaces, types)
3. Chunks files intelligently — respecting AST boundaries so you don't split a function in half
4. Embeds each chunk via the configured embedding endpoint
5. Classifies each chunk's intent and generates a plain-English summary via the LLM proxy
6. Extracts imports/dependencies from the AST
7. Stores everything in PostgreSQL

---

## Step 4: Build the MCP Server

```bash
npm install
npm run build   # tsc → dist/
```

The server lives at `index.ts` in the repo root and compiles to `dist/index.js`.

### Connect to Claude Desktop

Add to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json` on Mac):

```json
{
  "mcpServers": {
    "codebrain": {
      "command": "node",
      "args": ["/absolute/path/to/GrayMatter/dist/index.js"],
      "env": {
        "DATABASE_URL": "postgresql://codebrain:codebrain_local@localhost:5433/codebrain"
      }
    }
  }
}
```

### Connect to Claude Code

```bash
claude mcp add codebrain \
  --transport stdio \
  -- node /absolute/path/to/GrayMatter/dist/index.js
```

The MCP server reads these environment variables (all have sensible defaults):

| Variable | Default |
|----------|---------|
| `DATABASE_URL` | `postgresql://codebrain:codebrain_local@localhost:5433/codebrain` |
| `EMBED_API_STYLE` | `ollama` |
| `EMBED_BASE_URL` | `http://localhost:11434` |
| `EMBED_MODEL` | `nomic-embed-text` |
| `EMBED_DIMENSIONS` | `768` |
| `EMBED_API_KEY` | unset |

---

## Step 5: Use It

Ask your AI naturally:

| Prompt | Tool Used |
|--------|-----------|
| "How does authentication work in this codebase?" | `semantic_search` |
| "Where is the User class defined?" | `find_symbol` |
| "What depends on the database module?" | `trace_dependencies` |
| "Give me an architectural overview of src/api/" | `get_file_map` |
| "What is src/utils/transform.ts trying to do?" | `get_intent` |
| "How big is this codebase?" | `codebase_stats` |

---

## Re-indexing

```bash
# Full re-index (ignores hash cache)
python ingest.py /path/to/repo --force

# Incremental (only changed files since last run)
python ingest.py /path/to/repo

# Watch mode — auto re-index on save
python ingest.py /path/to/repo --watch
```

---

## Configuration

All config lives in `codebrain.toml`. When running the ingestor inside Docker, `codebrain.docker.toml` is mounted instead — the only difference is the database URL uses the `postgres` service name and LM Studio is reached via `host.docker.internal`.

```toml
[database]
url = "postgresql://codebrain:codebrain_local@localhost:5433/codebrain"

[embeddings]
model = "nomic-embed-text"           # Ollama model name
dimensions = 768                     # Must match model output
api_style = "ollama"                 # "openai" for LM Studio, "ollama" for /api/embed
base_url = "http://localhost:11434"

[classifier]
model = "qwen2.5-coder-7b-instruct"     # Model name exposed by LM Studio
base_url = "http://localhost:11435"      # OpenAI-compatible endpoint

[ingestion]
chunk_size = 512                    # Max tokens per chunk
overlap = 64                        # Token overlap between chunks
workers = 4                         # Parallel processing
exclude = ["node_modules", ".git", "dist", "__pycache__", "*.lock"]

[languages]
supported = [
    "python", "typescript", "javascript", "tsx", "jsx",
    "rust", "go", "java", "c", "cpp", "ruby", "php",
    "swift", "kotlin", "scala", "zig", "elixir"
]
```

---

## Running the Ingestor via Docker

Instead of running Python locally you can use the pre-built ingestor container. Uncomment and set the repo volume in `docker-compose.yml`, then:

```bash
# Build images
docker compose build

# Run ingestor against a mounted repo
docker compose run --rm --profile tools ingestor /repos/myrepo

# With flags
docker compose run --rm --profile tools ingestor /repos/myrepo --force --workers 8
```

---

## How It Works Under the Hood

### Chunking Strategy

Naive chunking (split every N lines) destroys code semantics. CodeBrain uses AST-aware chunking:

1. Parse the file with tree-sitter to get the syntax tree
2. Extract top-level symbols (functions, classes, interfaces) as natural chunk boundaries
3. Each symbol becomes its own chunk with full context (docstring, decorators, type signatures)
4. Large symbols (200+ lines) get sub-chunked at logical breaks (method boundaries within a class)
5. "Glue" code between symbols (imports, constants, module-level logic) gets its own chunk

### Intent Classification

Each chunk gets classified by the LLM into categories:

- **data-model** — defines schemas, types, database models
- **business-logic** — core domain logic, algorithms
- **api-endpoint** — HTTP handlers, route definitions
- **utility** — helper functions, formatters, validators
- **configuration** — env vars, settings, constants
- **test** — test cases, fixtures, mocks
- **infrastructure** — database connections, caching, queuing
- **ui-component** — frontend components, templates
- **integration** — third-party API clients, SDK wrappers
- **orchestration** — pipelines, workflows, job scheduling

### Dependency Graph

The AST parser extracts:
- Import statements → which files depend on which
- Function calls → which symbols call which
- Type references → which types are used where

This gets stored as a directed graph in the `dependencies` table, enabling queries like "what would break if I changed this function?" and "trace the full call chain from the API endpoint to the database."

### Semantic Search

When you ask "how does auth work?", the MCP server:
1. Embeds your question with the same configured embedding model
2. Runs a cosine similarity search across all stored chunks
3. Optionally filters by intent category, file path, or symbol type
4. Returns ranked results with surrounding context

---

## Resource Usage

| Component | Approx. RAM/VRAM |
|-----------|-----------------|
| PostgreSQL + pgvector | ~100 MB + disk proportional to codebase |
| Ollama + nomic-embed-text | ~500 MB RAM |
| LLM proxy / classifier model | depends on your setup |
| MCP Server | negligible |

Initial indexing of a 100K LOC codebase takes roughly 10–20 minutes depending on hardware. Incremental updates are near-instant.
