# AGENTS.md

This file defines repository-wide working rules for coding agents.

## Repository Scope

CodeBrain has two runtime concerns:
- a Python ingestion pipeline that parses code, classifies intent, embeds content, and writes normalized records into PostgreSQL + pgvector
- a TypeScript MCP server that serves search, symbol, reference, and dependency tools over the indexed database

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

## Operational Rules

- Treat `codebrain.toml` as the source of truth for local ingestion defaults.
- Treat `docker-compose.yml` as the source of truth for the containerized MCP topology.
- Prefer local ingestion (`python ingest.py ...`) unless the user explicitly asks for a containerized ingestion path.
- The MCP server is HTTP-first. Keep HTTP behavior as the default path and only preserve stdio mode when there is an active client need.
- When changing runtime defaults, prefer updating config files and top-level constants rather than scattering literals.

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

### Software engineering principles
- Prefer composition over large multi-purpose classes.
- Keep data extraction, storage, and query-time ranking as distinct layers.
- Preserve backward compatibility only when it has a current operational need.
- Keep infrastructure defaults centralized in config files or top-level constants rather than scattered literals.
- Add or update tests when behavior changes materially; if tests are not added, note the gap and why.
- When adding schema or protocol behavior, make migrations or compatibility handling explicit.

## Documentation and Maintenance Rules

- Keep `README.md` short and operational. It is not the canonical architecture document.
- Keep `CLAUDE.md` and `GEMINI.md` as lightweight symlink aliases to this file.
- Remove dead setup paths when they are no longer supported. Do not leave old container, proxy, or bridge instructions behind after the code path is retired.
- When removing a workflow, delete its code and config artifacts unless they are still required for a current user.
- Maintain `LOG.md` as a lightweight changelog with one single-line entry per substantive change or commit.
- Maintain `ARCHITECTURE.md` as the detailed architecture and design-pattern reference for this repository.
- When changing system structure, data flow, major tool behavior, or deployment topology, update `ARCHITECTURE.md` in the same change.
- When changing operational behavior, defaults, or developer workflow, update this file if the guidance changes.
