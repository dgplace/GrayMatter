# CodeBrain

CodeBrain is a codebase indexing and MCP query system:
- a Python ingestion pipeline indexes repositories into PostgreSQL + pgvector
- a TypeScript MCP server exposes repo-scoped semantic search, symbol lookup, references, and dependency tracing
- an embedded HTTP UI lets users browse per-repo stats and raw index tables

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
Compose host-exposed ports are bound to `127.0.0.1`. Runtime services use an `internal: true` network, and helper scripts route embedding/classifier traffic through fixed in-stack proxy services (`embed_proxy`, `classifier_proxy`). Localhost/loopback URLs are normalized to `host.docker.internal`; non-local targets are allowed only when they exactly match the configured embedding/classifier endpoint values.

Embedding/classifier transport resilience is configurable in `.env/codebrain.toml`:
- `[embeddings]`: `request_timeout_seconds`, `max_retries`, `retry_backoff_seconds`, `batch_size`
- `[classifier]`: `request_timeout_seconds`, `max_retries`, `retry_backoff_seconds`

For Windows host-model runs (`host.docker.internal`), if Ollama is timing out under load, lower `ingestion.workers` and/or `embeddings.batch_size`.

## Ingest a Repository

### Container (default)

Helper scripts:

```bash
./scripts/build.sh
./scripts/index-repo.sh /absolute/path/to/repo --force
./scripts/index-repo.sh /absolute/path/to/repo --database-url postgresql://codebrain:codebrain_local@10.0.0.25:5432/codebrain --force
./scripts/index-repo.sh /absolute/path/to/repo --database-url postgresql://codebrain:codebrain_local@applepi3:5432/codebrain --add-host applepi3:192.168.0.151 --force
./scripts/watch-repo.sh /absolute/path/to/repo
```

```bat
scripts\build.bat
scripts\index-repo.bat C:\absolute\path\to\repo --force
scripts\index-repo.bat C:\absolute\path\to\repo --database-url postgresql://codebrain:codebrain_local@10.0.0.25:5432/codebrain --force
scripts\index-repo.bat C:\absolute\path\to\repo --database-url postgresql://codebrain:codebrain_local@applepi3:5432/codebrain --add-host applepi3:192.168.0.151 --force
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
Use `--database-url <postgres-dsn>` to index into a remote CodeBrain PostgreSQL
instance for a single run; when omitted, the scripts keep using the local
Compose `postgres` container default.
If the database hostname is not resolvable from inside Docker, add
`--add-host <host>:<ip>` (repeatable) so the indexer container can resolve it.
If remote DB connects fail with `Network is unreachable`, rebuild/recreate the
indexer service after pulling latest changes so it joins the host-access
network (`scripts/build.sh` or `scripts\build.bat`).

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

Rebuild clustering + logical modules without re-indexing files:

```bash
python -m codebrain.recluster --repo-name <repo-name> --resolution-multiplier 2.0
./scripts/recluster-repo.sh /absolute/path/to/repo
```

```bat
python -m codebrain.recluster --repo-name <repo-name> --resolution-multiplier 2.0
scripts\recluster-repo.bat C:\absolute\path\to\repo
```

`recluster` re-materializes `clusters` / `cluster_members` and (by default)
refreshes `module_intents.kind='logical'`. It does not parse/chunk/embed source
files again.

Options for the standalone command:

| Flag | Default | Effect |
|------|---------|--------|
| `--mode` | `all` | `directory`, `logical`, or `all` |
| `--min-files` | `1` | Minimum distinct files for a module to be created |

Logical modules are not re-clustered at synthesis time — they are sourced directly from
the `clusters` and `cluster_members` rows produced during ingestion (Leiden, with Louvain
and connected-components fallbacks). To tune community granularity, set Leiden resolution
in `codebrain.toml` under `[clustering]` and re-ingest.
The indexer runtime includes local Leiden dependencies (`igraph` + `leidenalg`) so
cluster materialization stays in-process and does not require external network calls.

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

The `/ui` Index management panel can start an index run for the selected
repository. Enter the absolute local repository path, optionally choose the
folder for client-side name validation, and the server launches the equivalent
indexer container run with `--workers 2` while streaming terminal output in the
dialog. In Docker mode, the `mcp` service uses the host Docker socket and a
read-only CodeBrain source mount so host paths such as `/Users/.../Repo` can be
mounted into the sibling `indexer` run. Rebuild through `scripts/build.sh` or
`scripts\build.bat` so the embedding/classifier proxy sidecars are recreated
with the endpoints from `.env/codebrain.toml`.

Legacy stdio mode:

```bash
MCP_TRANSPORT=stdio node dist/index.js
```

### Docker (MCP + UI)

```bash
docker compose -f docker/docker-compose.yml build mcp
docker compose -f docker/docker-compose.yml --profile tools up -d mcp_frontdoor
```

The frontdoor sidecar publishes:
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
6. Open `/ui` to browse per-repo stats and raw index tables.

## Notes

- When indexing behavior changes materially, re-run ingestion with `--force`.
- When schema, tool behavior, or architecture changes, update `ARCHITECTURE.md` and `LOG.md` as required by `AGENTS.md`.
