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

### CLI

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

### Desktop Application (Windows / macOS / Linux)

```bash
pip install -r requirements-gui.txt
python -m desktop
```

The desktop app provides:
- A GUI for all `ingest.py` options (force, no-classify, worker count)
- Multi-repo management — add, remove, and index any number of repos
- Concurrent file watching across multiple repos simultaneously
- Live progress bars and a scrolling file log during ingestion
- Per-repo statistics and ingestion history views
- Settings dialog for database, embedding, and classifier configuration
- System tray integration — close the window while watchers continue running

## Synthesize Module Intents

After ingestion, run synthesis to identify logical modules and generate domain-specific intents:

```bash
python synthesize_modules.py --repo <repo-name>
python synthesize_modules.py --repo <repo-name> --mode logical --resolution 2.5
```

Options:

| Flag | Default | Effect |
|------|---------|--------|
| `--mode` | `all` | `directory`, `logical`, or `all` |
| `--min-files` | `3` | Minimum files for a module to be created |
| `--resolution` | `1.5` | Louvain resolution. **Higher = smaller, more focused modules**. Lower = broader groupings. |
| `--max-community-size` | `20` | Modules exceeding this are recursively split |
| `--hub-percentile` | `90.0` | Degree percentile above which nodes are dampened to prevent utility classes from merging unrelated clusters |

These can also be set in `codebrain.toml` under `[synthesis]`.

The desktop app runs synthesis from the repo panel with a deterministic progress bar.

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
