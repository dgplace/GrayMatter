# LOG

2026-05-07 Added AGENTS.md cross-cutting maintenance limits: justification-or-split rules for files over 1000 lines and functions over 150 lines, plus mandatory file separation for tool handlers, language-specific parsers, and HTML/CSS assets.
2026-05-07 Clarified README test setup to require the repo virtualenv for Python tests instead of assuming the host `python3` environment includes `pytest`.
2026-05-07 Implemented CODEBRAIN-15 additive symbol_references schema/bootstrap migration: added resolved-target metadata columns plus reverse-lookup indexes in schema.sql, ingest.py, and MCP startup tests/docs while keeping existing reference queries unchanged.
2026-05-07 Recorded CODEBRAIN-13 spike findings in docs/spike-incremental-resolved-edges.md: web-backed incremental-edge strategy with required symbol-reference indexing/schema additions and <1M LoC guardrails for selective incoming-edge refresh in watch mode.
2026-05-06 Recorded CODEBRAIN-14 spike outcome in LOG: web-research-only assessment of AGE vs recursive CTE for <1M LoC scope; recommendation NO-GO on Apache AGE, keep recursive CTEs as canonical traversal path; Phase 7 (CODEBRAIN-44, CODEBRAIN-45) stays in Backlog.
2026-05-06 Recorded CODEBRAIN-12 spike findings (embedding+cochange naming variant) in docs/spike-cluster-naming-signals.md; implementation deferred.
2026-05-06 Recorded CODEBRAIN-11 literature-only spike findings in docs/spike-symbol-vs-file-granularity.md: default to symbol-level clustering under 1M LoC with node/edge guardrails and auto fallback.
2026-05-06 Recorded CODEBRAIN-9 spike findings in docs/spike-python-resolution-confidence.md: scip-python benchmark on pallets/flask, recommended confidence thresholds (find_references 0.55, impact_of 0.75 with 0.55-0.74 possible-impact band), and UX proposal for surfacing confidence in MCP responses.
2026-05-06 Added containerized indexer service (Dockerfile.indexer + compose profile) bundling Node, Python, tree-sitter, and scip-typescript; ingest.py now honors DATABASE_URL and EMBED_BASE_URL env overrides; AGENTS.md and README.md updated to make container-based ingestion the canonical path.
2026-03-04 Added repository engineering standards to AGENTS.md and created LOG.md plus ARCHITECTURE.md documentation scaffolding.
2026-03-04 Trimmed README.md to a quickstart and updated AGENTS.md to map all repository Markdown docs by purpose.
2026-03-04 Removed the documentation map from README.md so document ownership guidance lives only in AGENTS.md.
2026-03-04 Consolidated shared guidance into AGENTS.md, reduced CLAUDE.md and GEMINI.md to client-only notes, and removed obsolete Docker ingestor files.
2026-03-04 Restored CLAUDE.md and GEMINI.md as symlinks to AGENTS.md and corrected the documentation map to reflect the alias setup.
2026-03-05 Added baseline Python and TypeScript unit tests plus test runner setup to support safe refactoring.
2026-03-05 Refactored MCP server into src modules, enforced mandatory repo-scoped query tools, added list_repositories plus repo-scoped stats, and shipped an embedded /ui semantic graph browser.
2026-03-05 Fixed ingestion status accounting to count worker errors correctly, added a --debug flag for per-file failures, and surfaced error samples in the CLI summary.
2026-03-05 Switched local ingestion defaults to 127.0.0.1 endpoints and improved embedding transport errors to include endpoint/model context for timeout diagnosis.
2026-03-05 Added live `/ui/api/tool-calls` counters and a real-time UI panel showing per-function MCP tool invocation totals.
2026-03-05 Added full C# ingestion support with `.cs` mapping, tree-sitter-c-sharp parsing, namespace-aware symbol extraction, and C# dependency parsing/tests.
2026-03-05 Added refactoring analysis MCP tools: analyze_coupling, extract_module_interface, find_dependency_cycles, find_modularization_seams; enhanced trace_dependencies with summary mode; added graph.ts cycle detection module and performance indexes.
2026-03-07 Added cross-platform desktop application (desktop/ package, PySide6): multi-repo management, live ingestion progress, concurrent file watching with system tray, stats/history views, settings dialog; adds requirements-gui.txt.
2026-03-07 Added explicit classifier fallback reporting across CLI and desktop flows: per-file warnings, summary fallback counts, and tests covering warning propagation.
2026-03-07 Finished desktop re-index action wiring: added RepoPanel handling for RepoCard `Re-index` to launch force ingestion (equivalent to `ingest.py --force`).
2026-03-07 Improved desktop startup dependency error handling to show a clear `pip install -r requirements-gui.txt` fix when `PySide6` is missing.
2026-03-07 Rewrote module intent synthesis: class-level weighted graph (Louvain with tunable resolution), hub dampening, recursive splitting, narrative domain-specific intents; added member_symbols column and [synthesis] config.
2026-03-07 Added --machine flag to synthesize_modules.py for deterministic desktop progress; added synthesis docs to README.
2026-03-08 Added resolution input to desktop synthesis; fixed MCP server for Gemini compatibility (restored Streamable HTTP, removed Zod defaults, and allowed 0.0.0.0 binding).
2026-03-08 Fixed ingestion to prune stale files from the database; added on_created, on_deleted, and on_moved handlers to watch mode for real-time index synchronization. Updated desktop UI to show pruning and deletion events in the ingestion log via new Qt signals. Updated file watcher to rigorously ignore .git and other excluded paths in all event handlers.
2026-03-08 Added top-level MCP server instructions and usage guidance telling agents to use CodeBrain first for structured repo discovery and `rg` as the fallback/complement for exact or stale-index cases.
2026-03-08 Broadened MCP guidance to recommend fast local text or filename search tools such as `rg`, instead of assuming every client environment exposes `rg`.
2026-03-08 Added a top-level executable `desktop.py` launcher that re-runs `.venv/bin/python -m desktop`.
2026-05-06 Updated plane.json to point at the CodeBrain Plane project and refreshed estimate_points IDs from project calibration items (1, 2, 3, 5, 8, 13), then verified mappings against live Plane items.
2026-05-06 Updated AGENTS.md with transferable engineering guidance from CruiseReport: think-before-coding rules, surgical change discipline, single-source-of-truth expectations, verifiable-goal/testing workflow, and CodeBrain-specific tooling guidance.
2026-05-06 Switched local runtime defaults from remote Apple Pi hosts to local Docker/host endpoints: updated .env/codebrain.toml, MCP config defaults, docker-compose host allowlist, and Dockerfile.mcp runtime ENV defaults for local container execution.
2026-05-06 Clarified AGENTS.md self-discovery policy: when coding CodeBrain, agents should use CodeBrain MCP tools as the primary repo discovery path, re-ingest when index data is stale, and use rg as a precision/validation complement.
