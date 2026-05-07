# CodeBrain

CodeBrain is a codebase indexing and MCP query system:
- a Python ingestion pipeline indexes repositories into PostgreSQL + pgvector
- a TypeScript MCP server exposes repo-scoped semantic search, symbol lookup, references, and dependency tracing
- an embedded HTTP UI lets users browse per-repo stats and semantic graph edges

## Prerequisites

- Docker with Compose v2
- An embedding endpoint compatible with the configured embedding client (e.g. Ollama on the host at `:11434`)
- An OpenAI-compatible chat endpoint for classification

Postgres + pgvector, the MCP server, and the indexer toolchain (Node, Python, Git for `.gitignore` filtering, tree-sitter, `scip-typescript`, `scip-python`, and the base `scip` CLI) all run in containers managed by `docker/docker-compose.yml`.

## Configuration

Runtime defaults live in:
- `docker/docker-compose.yml` for service topology and boundary endpoints
- `codebrain.toml` for ingestion defaults (chunking, language list, exclusions)
- `schema.sql` for first-time database initialization

Container runs honor three environment overrides for endpoint values: `DATABASE_URL`, `EMBED_BASE_URL`, and `CLASSIFIER_BASE_URL`. All are set in `docker/docker-compose.yml`; override per-run with `-e VAR=value` if needed.

## Ingest a Repository

### Container (default)

Helper scripts:

```bash
./scripts/build.sh
./scripts/index-repo.sh /absolute/path/to/repo --force
./scripts/watch-repo.sh /absolute/path/to/repo
```

The repo path argument is optional; if omitted, the scripts index the current
working directory.

The helper scripts mount the target repository at `/target` inside the
container so they do not conflict with the CodeBrain source mount at
`/workspace`.

Equivalent raw Docker commands:

```bash
docker compose -f docker/docker-compose.yml up -d postgres
docker compose -f docker/docker-compose.yml --profile indexer build indexer

docker compose -f docker/docker-compose.yml --profile indexer run --rm indexer python -m codebrain.ingest /workspace
docker compose -f docker/docker-compose.yml --profile indexer run --rm indexer python -m codebrain.ingest /workspace --force
docker compose -f docker/docker-compose.yml --profile indexer run --rm indexer python -m codebrain.ingest /workspace --watch
```

The repo root is mounted at `/workspace` inside the container. To index a different path, mount it: `-v /other/repo:/workspace`.

Notes:
- `--force` ignores the file hash cache and re-indexes everything
- `--watch` re-indexes changed files on save
- `.gitignore` is respected during ingestion
- The `indexer` profile keeps the service from auto-starting with plain `docker compose up`

### Desktop Application (Windows / macOS / Linux)

```bash
pip install -r requirements-gui.txt
python -m desktop
```

The desktop app provides:
- A GUI for all `codebrain/ingest.py` options (force, no-classify, worker count)
- Multi-repo management — add, remove, and index any number of repos
- Concurrent file watching across multiple repos simultaneously
- Live progress bars and a scrolling file log during ingestion
- Per-repo statistics and ingestion history views
- Settings dialog for database, embedding, and classifier configuration
- System tray integration — close the window while watchers continue running

## Synthesize Module Intents

After ingestion, run synthesis to identify logical modules and generate domain-specific intents:

```bash
python -m codebrain.synthesize_modules --repo <repo-name>
python -m codebrain.synthesize_modules --repo <repo-name> --mode logical --resolution 2.5
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
docker compose -f docker/docker-compose.yml build mcp
docker compose -f docker/docker-compose.yml up -d mcp
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

Use the repo virtualenv for Python tests. Do not assume your system `python3`
has `pytest` installed.

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
