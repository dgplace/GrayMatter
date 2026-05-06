# Spike: SCIP indexer integration & distribution strategy

Deliverable for CODEBRAIN-8. This memo captures the recommendations for how SCIP-based reference resolution is integrated into CodeBrain and distributed to users.

## Indexers in scope for v1

Aligned with CodeBrain's supported language list:

| Language       | Indexer       | Status        | Notes |
|---------------|---------------|---------------|-------|
| TypeScript/JS | scip-typescript | **PoC done** | Requires `node_modules` in target repo for full type resolution. |
| Python        | scip-python   | Pending integration | Confidence drops on untyped/dynamic call sites. |
| Java          | scip-java     | Pending integration | Needs Maven/Gradle build context; auto-detect. |
| C / C++       | scip-clang    | Pending integration | Needs `compile_commands.json`; degrade if absent. |
| C#            | scip-dotnet   | Pending integration | Needs `.csproj` / `.sln`. |
| Swift         | _none_        | Heuristic only | No first-party SCIP; tree-sitter + heuristic resolver. |
| HTML / CSS    | _n/a_         | Content-only  | Embedded and searchable; no resolver participation. |

## Packaging and distribution: container-only

**Decision:** all indexers ship inside a single `codebrain-indexer` container image, built by `Dockerfile.indexer` and run via `docker compose --profile indexer`.

Rationale:

- **Reproducibility.** Each SCIP indexer has different runtime needs (Node, Python, JVM, native C++). Pinning all of these on every host machine is brittle. Pinning them in a single image is a one-line build-arg per indexer.
- **No "indexer missing" case.** All indexers are always present in the image. Graceful fallback applies only to *repo conditions* the indexer can't satisfy (no `compile_commands.json`, no `pom.xml`, no `tsconfig.json`).
- **Onboarding.** A fresh clone needs Docker + Compose; nothing else. No host Python venv, no host npm, no per-language toolchain installs.
- **Image size is not a concern** per project decision (2026-05-06).

Per-language images were considered and rejected: combinatorial CI complexity, more orchestration code in ingest.py, with negligible upside given image bloat is a non-concern.

## Distribution mechanics

- `Dockerfile.indexer` pins indexer versions via `ARG` build args (`SCIP_TYPESCRIPT_VERSION`, `SCIP_CLI_VERSION`, etc.).
- The `scip` reader CLI is downloaded from the GitHub `scip-code/scip` releases (note: name collision with the SCIP optimization solver in Homebrew — do not use `brew install scip`).
- Boundary endpoints come from environment variables: `DATABASE_URL` (compose network), `EMBED_BASE_URL` (`host.docker.internal`).
- The `indexer` profile keeps the service from auto-starting with plain `docker compose up`.

## Integration design (for KG Phase 1 follow-ons)

The work decomposes cleanly into the existing `Resolution` module. Recommended flow per ingestion run:

1. **Pre-parse step.** For each language present in the repo with a SCIP indexer, run that indexer inside the container and emit `index.scip` to a working directory (e.g. `.codebrain/scip/{language}.scip`). Skip languages whose preconditions aren't met (no tsconfig, no compile_commands, etc.).
2. **Load.** Parse all `index.scip` files at the start of ingestion. Build an in-memory index keyed by `(relative_path, line_range)` returning the SCIP symbol ID + role (definition vs reference).
3. **Resolver stage** (CODEBRAIN-16). Sits between parsing and persistence. For each tree-sitter symbol/reference, look up the matching SCIP record and attach `target_symbol_id` with `resolution_confidence = 1.0`. Unmatched edges fall through to heuristic resolution with confidence < 1.0.
4. **Persist.** New columns on `symbol_references` (CODEBRAIN-15): `target_symbol_id`, `resolution_confidence`, `reference_kind_v2`. Additive migration, reversible.
5. **Query.** `find_references` (CODEBRAIN-19) prefers `target_symbol_id` matches; falls back to name matching only for languages/repos that produced no SCIP edges.

SCIP symbol IDs are stable strings of the form
`scip-typescript npm <package> <version> <relative-path>/<symbol>.`
They embed package + version, which gives free cross-package edges as a side effect.

## Onboarding impact

- **Desktop app.** No change to its own runtime — desktop remains a host-Python GUI client. But ingestion launched by the desktop app must shell out to `docker compose run --rm indexer python ingest.py ...` instead of invoking ingest.py in-process. This is a follow-up for desktop integration, separate from KG Phase 1.
- **Dev workflow.** `requirements.txt` is no longer expected to be installed on the host. Tests that exercise ingest.py either run inside the indexer container or rely on the host Python venv — both remain supported, but the runtime path is the container.

## Recommended sequencing

KG Phase 1 issues, in dependency order:

1. CODEBRAIN-15 — schema (additive, reversible)
2. CODEBRAIN-16 — resolver pipeline stage (language-agnostic)
3. CODEBRAIN-17 — scip-typescript wiring (first concrete language)
4. CODEBRAIN-19 — find_references update (consumes new columns)
5. CODEBRAIN-18 — heuristic fallback resolver (Swift + degraded paths)
6. CODEBRAIN-46–49 — remaining per-language SCIP integrations
7. CODEBRAIN-50 — Swift-specific notes
8. CODEBRAIN-51 — HTML/CSS content-only documentation

Acceptance criteria for these issues should be updated to reflect the container-only distribution: there is no "indexer missing" case; the only graceful skip is on repo precondition failures.
