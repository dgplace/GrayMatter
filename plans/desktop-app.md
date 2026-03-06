# CodeBrain Desktop App — Implementation Plan

## Context

The CodeBrain ingestion pipeline (`ingest.py`) is currently CLI-only. Users must manually run commands per repository and can only watch one repo at a time. A cross-platform desktop app will provide a GUI for managing multiple repositories, running ingestion with live progress, and watching N repos simultaneously with system tray support for background operation.

**Infrastructure unchanged**: The existing containerized PostgreSQL (pgvector) and MCP server from `docker-compose.yml` remain the source of truth for persistence and MCP tooling. The desktop app is a client that connects to the same PostgreSQL instance and calls the same pipeline code — it does not embed or replace those services.

## Framework: PySide6 (Qt for Python)

- LGPL license (no commercial licensing needed unlike PyQt6's GPL)
- Native widgets + system tray on Windows/macOS/Linux
- Qt's signal/slot threading model is ideal for decoupling heavy pipeline work from UI
- Direct access to all existing Python modules — no IPC bridge needed
- Mature packaging via PyInstaller

## Project Structure

```
desktop/
  __init__.py
  __main__.py              # Entry: `python -m desktop`
  app.py                   # QApplication lifecycle, single-instance, config loading
  core/
    __init__.py
    engine.py              # IngestionEngine: wraps ingest.py functions, emits Qt signals
    watcher.py             # MultiRepoWatcher: N-repo watchdog orchestration
    state.py               # AppState: SQLite persistence for repo list, settings
    signals.py             # Shared signal definitions
  ui/
    __init__.py
    main_window.py         # Sidebar nav + stacked views
    repo_panel.py          # Add/remove repos, per-repo cards with status
    ingestion_view.py      # Progress bars, live file log, run controls
    stats_view.py          # Per-repo stats (files, chunks, symbols, languages)
    history_view.py        # Past ingestion_runs table
    settings_dialog.py     # DB, embeddings, classifier config editor
    tray.py                # System tray icon, context menu, notifications
  resources/
    icons/                 # App icons (.png, .ico, .icns)
```

## Architecture

### Engine Layer (`core/engine.py`)

Thin bridge between GUI and existing pipeline. Calls existing functions directly:
- `load_config()` from `ingest.py:48`
- `walk_repo()` from `ingest.py:419`
- `process_file()` from `ingest.py:451` — already self-contained, takes explicit args, returns result dict
- `ensure_schema()` from `ingest.py:218`

Emits Qt signals for progress instead of Rich console output:
- `repo_started(repo_name, total_files)`
- `progress(repo_name, current, total, result_dict)`
- `file_processed(repo_name, file_path, status)`
- `repo_completed(repo_name, stats_dict)`
- `repo_error(repo_name, error_message)`

Heavy work runs on background threads via `ThreadPoolExecutor` (same pattern as `ingest.py:788-842`). Qt signals are thread-safe and deliver to the UI thread automatically.

### Multi-Repo Watcher (`core/watcher.py`)

Extends the single-repo watch pattern (`ingest.py:873-905`) to N repos:
- One `watchdog.Observer` per watched repo
- **Shared** `EmbeddingClient` and `IntentClassifier` (thread-safe, use `httpx.Client`)
- **Per-repo** `ASTChunker` (tree-sitter Parser is stateful/not thread-safe)
- **Per-repo** `ThreadedConnectionPool` (2 connections each)
- Clean lifecycle: stop one repo without affecting others

### State Persistence (`core/state.py`)

Local SQLite DB at platform-appropriate path (`platformdirs`):
- macOS: `~/Library/Application Support/CodeBrain/desktop_state.db`
- Linux: `~/.local/share/CodeBrain/desktop_state.db`
- Windows: `%LOCALAPPDATA%\CodeBrain\desktop_state.db`

Stores:
- Registered repos (path, name, auto_watch flag, last ingestion result)
- App settings overrides (DB url, embedding config, etc.)

Intentionally separate from PostgreSQL — works even when PG is unreachable.

### UI Layout

```
+--------------------------------------------------+
|  CodeBrain Desktop                     [_] [□] [x]|
+--------+-----------------------------------------+
|        |                                          |
| Repos  | [Active view area]                       |
|        |  - Repo cards with watch toggle          |
| Ingest |  - Index/Stop buttons per repo           |
|        |  - Progress bars during ingestion        |
| Stats  |  - Per-repo statistics tables            |
|        |  - Ingestion run history                 |
| History|  - Settings form                         |
|        |                                          |
| Settn. |                                          |
+--------+-----------------------------------------+
|  Watching: 3 repos | Idle: 2 repos               |
+--------------------------------------------------+
```

System tray: app minimizes to tray when watchers are active. Tray menu shows watched repos, provides Show/Stop All/Quit.

## Key Design Decisions

1. **No changes to `ingest.py`**: `process_file()` is already cleanly factored. The engine duplicates only the orchestration logic (walk → thread pool → collect results) with signals instead of Rich output.

2. **PostgreSQL stays containerized**: The desktop app connects to the same PostgreSQL instance from `docker-compose.yml` (default `localhost:5433`). The settings dialog allows configuring the DB URL. The MCP server container continues to serve MCP tools over HTTP — the desktop app does not interact with it directly.

3. **SQLite for app state only**: Repo list, auto-watch preferences, and UI settings are local to the desktop app. All indexed code data (files, chunks, symbols, embeddings) goes to PostgreSQL as before.

4. **One Observer per repo**: Clean lifecycle management and crash isolation. Each repo's watcher can be started/stopped independently.

5. **Cooperative cancellation**: Engine checks a `cancelled` set after each file completes. In-flight files finish, but no new ones start.

## Dependencies

New `requirements-gui.txt`:
```
-r requirements.txt
PySide6>=6.6
platformdirs>=4.0
```

## Implementation Phases

### Phase 1: Core engine (no UI)
1. `desktop/core/state.py` — SQLite repo/settings persistence
2. `desktop/core/engine.py` — `IngestionEngine` with Qt signals wrapping pipeline functions
3. `desktop/core/watcher.py` — `MultiRepoWatcher` managing N watchdog Observers
4. `desktop/core/signals.py` — shared signal type definitions
5. Unit tests for engine, state, and watcher

### Phase 2: Minimal UI shell
6. `desktop/__main__.py` + `desktop/app.py` — app entry point and lifecycle
7. `desktop/ui/main_window.py` — sidebar + stacked widget layout
8. `desktop/ui/repo_panel.py` — add/remove repos, index/watch buttons, status cards
9. `desktop/ui/ingestion_view.py` — progress bars, scrolling file log

### Phase 3: Complete UI
10. `desktop/ui/stats_view.py` — per-repo stats from `codebase_stats` view
11. `desktop/ui/history_view.py` — `ingestion_runs` table display
12. `desktop/ui/settings_dialog.py` — config editor (DB, embeddings, classifier)
13. `desktop/ui/tray.py` — system tray icon, context menu, notifications

### Phase 4: Polish
14. App icons and resources
15. PyInstaller packaging spec
16. Update `ARCHITECTURE.md`, `README.md`, `LOG.md`

## Verification

1. `python -m desktop` launches the window on the current platform
2. Add a repo via the UI → triggers `walk_repo()`, shows file count
3. Click "Index" → progress bar advances, file log scrolls, stats update on completion
4. Toggle "Watch" on two repos → both repos show as watching in tray menu
5. Modify a file in a watched repo → file re-indexes, tray notification appears
6. Close window while watching → app stays in tray, watchers continue
7. Quit from tray → all watchers stop, app exits cleanly

## Files to Modify (existing)

- `requirements.txt` — no changes (PySide6 goes in `requirements-gui.txt`)
- `ARCHITECTURE.md` — add Desktop App section
- `README.md` — add desktop usage instructions
- `LOG.md` — add changelog entry
