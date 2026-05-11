# CodeBrain

CodeBrain is a codebase indexing and MCP query system:
- a Python ingestion pipeline indexes repositories into PostgreSQL + pgvector
- a TypeScript MCP server exposes repo-scoped semantic search, symbol lookup, references, and dependency tracing
- an embedded HTTP UI lets users browse per-repo stats and semantic graph edges

## Prerequisites

- Docker with Compose v2
- An embedding endpoint compatible with the configured embedding client (e.g. Ollama on the host at `:11434`)
- An OpenAI-compatible chat endpoint for classification

Postgres + pgvector, the MCP server, and the indexer toolchain (Node, Python, Git for `.gitignore` filtering, tree-sitter, `scip-typescript`, `scip-python`, `scip-dotnet`, and the base `scip` CLI) all run in containers managed by `docker/docker-compose.yml`.

## Configuration

Runtime defaults live in:
- `docker/docker-compose.yml` for service topology and boundary endpoints
- `codebrain.toml` for ingestion defaults (chunking, language list, exclusions)
- `schema.sql` for first-time database initialization

By default, ingestion includes Markdown (`.md`), TOML (`.toml`), YAML
(`.yml`/`.yaml`), HTML (`.html`), and CSS (`.css`) files, with a non-code
per-file size cap controlled by `ingestion.non_code_max_bytes` in
`codebrain.toml`. HTML/CSS are content-only: they are chunked, embedded, and
searchable, but do not emit symbol graph edges.
Callback/event edge extractors are configured by
`ingestion.callback_extractors_enabled` (for example `emitter_on`,
`dom_add_event_listener`, `http_route`, `event_emit`) so noisy patterns can be
disabled per repo.

Container runs honor three environment overrides for endpoint values: `DATABASE_URL`, `EMBED_BASE_URL`, and `CLASSIFIER_BASE_URL`. All are set in `docker/docker-compose.yml`; override per-run with `-e VAR=value` if needed.

## Ingest a Repository

### Container (default)

Helper scripts:

```bash
./scripts/build.sh
./scripts/index-repo.sh /absolute/path/to/repo --force
./scripts/watch-repo.sh /absolute/path/to/repo
```

```bat
scripts\build.bat
scripts\index-repo.bat C:\absolute\path\to\repo --force
scripts\watch-repo.bat C:\absolute\path\to\repo
```

The repo path argument is optional; if omitted, the scripts index the current
working directory.

`build.sh`/`build.bat` rebuild the Compose images. By default they recreate
only `mcp`; pass `--reset` to recreate both `postgres` and `mcp`, or `--wipe`
to drop the `codebrain_postgres_data` volume before recreating both services.

The helper scripts mount the target repository at `/target` inside the
container so they do not conflict with the CodeBrain source mount at
`/workspace`. They also pass `--repo-name` using the host folder basename, so
indexed repository names remain stable instead of becoming `target`.

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

Module-intent synthesis overlays narrative LLM intents on directories and on the existing
Leiden clusters rebuilt by ingestion. It can be run inline as part of an ingest, or as a
standalone follow-up command.

Inline (single command, recommended for normal use):

```bash
python -m codebrain.ingest <repo-path> --synthesize
```

Standalone (e.g. to refresh narratives without re-ingesting):

```bash
python -m codebrain.synthesize_modules --repo <repo-name>
python -m codebrain.synthesize_modules --repo <repo-name> --mode logical
```

Options for the standalone command:

| Flag | Default | Effect |
|------|---------|--------|
| `--mode` | `all` | `directory`, `logical`, or `all` |
| `--min-files` | `3` | Minimum distinct files for a module to be created |

Logical modules are not re-clustered at synthesis time — they are sourced directly from
the `clusters` and `cluster_members` rows produced during ingestion (Leiden, with Louvain
and connected-components fallbacks). To tune community granularity, set Leiden resolution
in `codebrain.toml` under `[clustering]` and re-ingest.

Synthesis is LLM-driven, so it is silently skipped when `--no-classify` is set. The
desktop app runs synthesis from the repo panel with a deterministic progress bar.

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
- Dependency/impact/flow tools follow `find_*` naming: `find_call_graph`, `find_cycles`, `find_impact`, `find_external_dependencies`, and `find_flows`.

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
