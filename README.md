# CodeBrain

CodeBrain is a codebase indexing and MCP query system:
- a Python ingestion pipeline indexes repositories into PostgreSQL + pgvector
- a TypeScript MCP server exposes repo-scoped semantic search, symbol lookup, references, and dependency tracing
- an embedded HTTP UI lets users browse per-repo stats and semantic graph edges

## Prerequisites

- Python 3.11+
- Node.js 22+
- PostgreSQL with `pgvector`
- An embedding endpoint compatible with the configured embedding client
- An OpenAI-compatible chat endpoint for classification

The default config in `codebrain.toml` points at local host services (`127.0.0.1`) for ingestion.

## Configuration

Runtime defaults live in:
- `codebrain.toml` for local ingestion
- `schema.sql` for first-time database initialization

Update `codebrain.toml` if your database, embedding service, or classifier endpoint changes.

## Ingest a Repository

Local ingestion is the intended path.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python ingest.py /path/to/repo
python ingest.py /path/to/repo --force
python ingest.py /path/to/repo --watch
```

Notes:
- `--force` ignores the file hash cache and re-indexes everything
- `--watch` re-indexes changed files on save
- `.gitignore` is respected during ingestion

## Run the MCP Server

### Local

```bash
npm install
npm run build
npm start
```

Default local endpoints:

```text
http://127.0.0.1:3001/mcp
http://127.0.0.1:3001/ui
http://127.0.0.1:3001/healthz
```

Legacy stdio mode:

```bash
MCP_TRANSPORT=stdio node dist/index.js
```

### Docker (MCP + UI)

```bash
docker compose build mcp
docker compose up -d mcp
```

The container publishes:
- `http://127.0.0.1:3001/mcp`
- `http://127.0.0.1:3001/ui`
- `http://127.0.0.1:3001/healthz`

## MCP Tooling Notes

- Repo scoping is mandatory for query tools.
- Start with `list_repositories` to discover valid repository names.
- Pass `repo` into tools such as `semantic_search`, `find_symbol`, `find_references`, `trace_dependencies`, `get_file_map`, `get_intent`, and `codebase_stats`.

## Run Tests

Python unit tests:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

TypeScript unit tests:

```bash
npm install
npm test
```

## Typical Workflow

1. Configure `codebrain.toml` for your database, embedding endpoint, and classifier.
2. Run local ingestion against the repository you want indexed.
3. Start the MCP server.
4. Use `list_repositories` to discover indexed repo names.
5. Query tools with an explicit `repo` argument.
6. Open `/ui` to browse per-repo stats and semantic graph edges.

## Notes

- When indexing behavior changes materially, re-run ingestion with `--force`.
- When schema, tool behavior, or architecture changes, update `ARCHITECTURE.md` and `LOG.md` as required by `AGENTS.md`.
