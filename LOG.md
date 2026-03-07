# LOG

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
