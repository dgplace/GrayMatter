# AGENTS.md

This file defines repository-wide working rules for coding agents.

## Repository Scope

CodeBrain has two runtime concerns:
- a Python ingestion pipeline that parses code, classifies intent, embeds content, and writes normalized records into PostgreSQL + pgvector
- a TypeScript MCP server that serves search, symbol, reference, and dependency tools over the indexed database

CodeBrain is designed to run on both Windows and Unix-like hosts (macOS/Linux) with equivalent operational behavior; helper scripts and endpoint policy changes must preserve cross-platform parity.

Read `README.md` for operational quickstart and `ARCHITECTURE.md` for system design. Keep this file focused on agent behavior and engineering standards.

## Documentation Map

- `README.md`: concise operational quickstart and day-to-day commands
- `AGENTS.md`: repository-wide working rules, engineering standards, and maintenance expectations
- `ARCHITECTURE.md`: detailed architecture, design patterns, data flow, and component boundaries
- `LOG.md`: one-line changelog for each substantive change or commit
- `CLAUDE.md`: symlink alias to `AGENTS.md` for tools that look for a Claude-named instruction file
- `GEMINI.md`: symlink alias to `AGENTS.md` for tools that look for a Gemini-named instruction file

Keep these files focused on distinct purposes.
- Do not duplicate detailed architecture in `README.md`.
- Keep `CLAUDE.md` and `GEMINI.md` as symlinks to `AGENTS.md`, not separate duplicate files.
- When shared guidance changes, update `AGENTS.md`; the symlink aliases should continue to resolve to the same content.

## Engineering Approach

### Think before coding

- If a request has multiple valid interpretations, state assumptions explicitly and choose one before implementation.
- If requirements are ambiguous and a wrong assumption would risk behavior regressions, pause and clarify.
- If a materially simpler solution exists, prefer it over a more abstract design.

### Keep it simple

- Implement only what was requested.
- Avoid speculative abstractions and optional configurability unless there is an active operational need.
- Prefer direct, readable control flow over indirection.

### Make surgical changes

- Touch only files and lines required for the request.
- Match local style and patterns in the surrounding module.
- Do not bundle unrelated refactors or cleanup with feature or bug-fix changes.
- Remove unused imports, variables, and helpers introduced by your change.
- If you discover broader cleanup opportunities, note them separately instead of mixing them into the same edit.

### Respect single sources of truth

- Resolve each operational value once in the layer that owns it (config, ingestion normalization, persistence schema, or MCP response assembly).
- Treat consumer-side fallbacks for missing upstream data as defects to fix at the owning layer.
- Keep behavior consistent across MCP tools; do not patch divergence in one tool while leaving other tools inconsistent.

### Execute against verifiable goals

- Translate each task into concrete checks (tests, command outputs, or observable behavior).
- For bug fixes, prefer reproducing the failure first, then lock the fix with a regression test.
- For feature work, add focused tests that prove the new behavior.
- For multi-step work, keep a short plan and verify each step before moving on.

## Operational Rules

- Treat `docker/docker-compose.yml` as the source of truth for the runtime topology (postgres, mcp, indexer).
- Treat `codebrain.toml` as the source of truth for ingestion defaults shared across host and container runs.
- Run ingestion through the `indexer` container service (`docker compose -f docker/docker-compose.yml --profile indexer run --rm indexer python -m codebrain.ingest ...`). The container ships with all toolchains required for parsing and indexing; bare-metal `python -m codebrain.ingest` is no longer the supported path.
- The MCP server is HTTP-first. Keep HTTP behavior as the default path and only preserve stdio mode when there is an active client need.
- When changing runtime defaults, prefer updating config files and top-level constants rather than scattering literals. Boundary endpoints (`DATABASE_URL`, `EMBED_BASE_URL`, `CLASSIFIER_BASE_URL`) may also be overridden via environment variables for container/CI use.

## Tooling and Workflow

- Use CodeBrain tooling first for repository discovery that benefits from intent, symbol, reference, and dependency context.
- Use `rg` for exact text and filename discovery, validating index-backed findings, or when index coverage is stale or incomplete.
- Keep ingestion and MCP workflow commands aligned with `README.md`; if operational commands change, update docs in the same change.
- The canonical CodeBrain MCP usage guide lives in `src/mcp/resources.ts` as the `codebrain://usage` resource (with a short pointer in `CODEBRAIN_SERVER_INSTRUCTIONS`). When tools are added, removed, renamed, or change their parameter contracts, update `CODEBRAIN_USAGE_TEXT` in the same change so all MCP clients see consistent guidance. Do not create parallel usage docs (e.g., a Claude-only `SKILL.md`) that can drift from this source.

### CodeBrain Self-Discovery Policy

- Treat this repository as a self-hosted discovery environment: when working in CodeBrain, use CodeBrain MCP tools as the primary discovery path.
- Start discovery with `list_repositories`, then scope subsequent MCP tool calls to the correct `repo` value.
- Prefer MCP semantic/symbol/reference/dependency tools for architecture and impact analysis; use `rg` as a precision and verification complement.
- If indexed results appear stale, incomplete, or inconsistent with the working tree, run local re-ingestion and continue with refreshed MCP results.
- Do not bypass stale-index problems with ad-hoc per-tool heuristics; fix freshness at the ingestion/index layer first.

## Engineering Standards

### Code quality requirements
- **Doxygen Headers**: Every source file, public class, and function/method must have a standard Doxygen-style header that states purpose, key behavior, and parameters/return values where applicable.
- **Header Backfill Rule**: When modifying existing code, add missing Doxygen headers for the touched file and all touched functions/methods instead of leaving mixed documentation quality behind.
- **Single Purpose**: Keep functions, methods, and classes focused on one responsibility. Split work when a unit starts combining orchestration, transformation, and persistence concerns.
- **Separation of Concerns**: Keep parsing, persistence, transport, ranking, and presentation logic separated. Avoid mixing MCP formatting logic with ingestion logic or SQL with prompt construction in the same function unless unavoidable.
- **Remove Unused Code**: When refactoring, proactively remove dead code, obsolete fallbacks, old bridging logic, stale config branches, and unused helper functions.
- **Minimal Surface Area**: Prefer the smallest change that solves the real problem, but do not preserve unnecessary complexity just to avoid touching old code.
- **No Hidden Coupling**: Shared behavior should live in explicit helper functions or modules, not be duplicated with small variations across files.
- **Stable Interfaces**: Prefer clear typed inputs/outputs, explicit return values, and deterministic data shapes over implicit side effects.
- **Readable Over Clever**: Favor straightforward control flow and descriptive names over compact but opaque implementations.
- **Fail Clearly**: For operational failures, surface clear errors with enough context to diagnose the problem. Do not silently swallow errors unless there is a deliberate fallback path.

### Cross-cutting maintenance rules
- **Large File Limit**: Source files must not exceed 1000 lines. Split by responsibility before merge; do not add or rely on justification comments.
- **Large Function Limit**: Functions or methods must not exceed 150 lines. Split logic before merge; do not add or rely on justification comments.
- **Handler/Parser/Asset Separation**: Tool handlers, language-specific parsing logic, and HTML/CSS assets must each live in their own files. Do not co-locate those concerns in a single module.
- **No Inline Script Bodies**: Do not embed inline executable code blocks (for example `python -c`, `python - <<'PY'`, Node here-docs, or similar) inside shell scripts. Place executable logic in dedicated versioned source files and invoke those files from shell wrappers.

### Software engineering principles
- Prefer composition over large multi-purpose classes.
- Keep data extraction, storage, and query-time ranking as distinct layers.
- Preserve backward compatibility only when it has a current operational need.
- Keep infrastructure defaults centralized in config files or top-level constants rather than scattered literals.
- Add or update tests for all non-trivial behavior changes; if tests are not added, note the gap and why.
- When adding schema or protocol behavior, make migrations or compatibility handling explicit.

## Testing Expectations

- Keep tests deterministic and repeatable across runs.
- Reuse existing fixtures and sample inputs where possible; add new fixtures only when required.
- Prefer small, behavior-focused tests close to the changed logic.
- For ingestion/classification/ranking bug fixes, include assertions that would fail if the regression returns.
- Run indexing/ingestion verification through the `indexer` container service (`docker compose -f docker/docker-compose.yml --profile indexer run --rm indexer ...`) rather than bare-metal Python.

## Documentation and Maintenance Rules

- Keep `README.md` short and operational. It is not the canonical architecture document.
- Keep `CLAUDE.md` and `GEMINI.md` as lightweight symlink aliases to this file.
- Remove dead setup paths when they are no longer supported. Do not leave old container, proxy, or bridge instructions behind after the code path is retired.
- When removing a workflow, delete its code and config artifacts unless they are still required for a current user.
- Maintain `LOG.md` as a lightweight changelog with one single-line entry per substantive change or commit.
- Maintain `ARCHITECTURE.md` as the detailed architecture and design-pattern reference for this repository.
- When changing system structure, data flow, major tool behavior, or deployment topology, update `ARCHITECTURE.md` in the same change.
- When changing operational behavior, defaults, or developer workflow, update this file if the guidance changes.
