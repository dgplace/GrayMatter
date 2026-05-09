"""
@file tests/test_ingest.py
@brief Unit tests for ingestion-side helper functions.
"""

from pathlib import Path

import pytest

from codebrain import ingest


class _FakeCursor:
    """@brief Minimal cursor stub for process_file warning-path tests."""

    def __init__(self) -> None:
        self._pending_fetch: tuple | None = None

    def execute(self, query: str, params=None) -> None:
        """@brief Record a deterministic fetch response for known SQL patterns.

        @param query SQL statement text.
        @param params SQL parameters (unused).
        """
        normalized = " ".join(query.strip().lower().split())
        if normalized.startswith("select id, hash from files"):
            self._pending_fetch = None
        elif normalized.startswith("insert into files"):
            self._pending_fetch = (1,)
        else:
            self._pending_fetch = None

    def fetchone(self):
        """@brief Return the prepared fetch payload."""
        return self._pending_fetch


class _FakeConn:
    """@brief Minimal psycopg2 connection stub for process_file warning-path tests."""

    def __init__(self) -> None:
        self._cursor = _FakeCursor()

    def cursor(self) -> _FakeCursor:
        """@brief Return a reusable fake cursor."""
        return self._cursor

    def commit(self) -> None:
        """@brief No-op commit for the fake connection."""
        return None

    def rollback(self) -> None:
        """@brief No-op rollback for the fake connection."""
        return None


class _FakePool:
    """@brief Minimal connection pool stub for process_file warning-path tests."""

    def __init__(self) -> None:
        self._conn = _FakeConn()

    def getconn(self) -> _FakeConn:
        """@brief Return a fake connection."""
        return self._conn

    def putconn(self, conn: _FakeConn) -> None:
        """@brief No-op pool return method.

        @param conn Connection being returned.
        """
        return None


class _FakeEmbedder:
    """@brief Minimal embedding client stub returning deterministic vectors."""

    def embed(self, text: str) -> list[float]:
        """@brief Return a single deterministic embedding vector.

        @param text Input text to embed.
        @return Dummy vector for test assertions.
        """
        return [0.0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """@brief Return one deterministic vector per input text.

        @param texts Batch of embedding inputs.
        @return Dummy vectors for test assertions.
        """
        return [[0.0] for _ in texts]


class _FakeClassifier:
    """@brief Minimal classifier stub that emits one warning and fallback values."""

    def analyze_file(self, file_path: str, code: str, language: str, on_warning=None) -> tuple[str, str]:
        """@brief Emit a warning and return fallback summary/role values.

        @param file_path Relative file path.
        @param code File contents.
        @param language File language label.
        @param on_warning Optional warning callback.
        @return Empty summary and unknown role.
        """
        if on_warning:
            on_warning(f"Classifier file analysis fallback for {file_path}: test")
        return "", "unknown"

    def classify_chunks_batch(
        self,
        chunks: list[dict],
        language: str,
        file_path: str,
        on_warning=None,
    ) -> list[tuple[str, str]]:
        """@brief Return fallback chunk classifications.

        @param chunks Chunk dictionaries.
        @param language File language label.
        @param file_path Relative file path.
        @param on_warning Optional warning callback.
        @return Utility fallback classifications matching chunk count.
        """
        return [("utility", "")] * len(chunks)


class _FakeChunker:
    """@brief Minimal chunker stub that emits no chunks."""

    def chunk_file(self, content: str, language: str, rel_path: str) -> list[dict]:
        """@brief Return no chunks to keep the test path focused on warnings.

        @param content File contents.
        @param language File language label.
        @param rel_path Relative file path.
        @return Empty chunk list.
        """
        return []

    def extract_dependencies(self, content: str, language: str, rel_path: str) -> list[dict]:
        """@brief Return no dependencies.

        @param content File contents.
        @param language File language label.
        @param rel_path Relative file path.
        @return Empty dependency list.
        """
        return []


def test_clean_swift_type_strips_optionals_generics_and_modules() -> None:
    """@brief Verify Swift type cleanup normalizes decorated type names."""
    assert ingest._clean_swift_type("App.TrackService<Dependency>?") == "TrackService"


def test_filter_gitignored_paths_preserves_input_when_git_root_is_unknown(monkeypatch) -> None:
    """@brief Verify Git filtering becomes a no-op when the repository root is unavailable."""
    paths = [Path("/repo/a.py"), Path("/repo/b.py")]
    monkeypatch.setattr(ingest, "get_git_root", lambda repo_root: None)

    assert ingest.filter_gitignored_paths(paths, Path("/repo")) == paths


def test_filter_gitignored_paths_removes_reported_ignored_files(monkeypatch, tmp_path) -> None:
    """@brief Verify Git-reported ignored files are removed while preserving order."""
    kept = tmp_path / "kept.py"
    ignored = tmp_path / "ignored.py"
    paths = [kept, ignored]

    class _Result:
        """@brief Minimal subprocess result stub for Git ignore tests."""

        returncode = 0
        stdout = b"ignored.py\0"

    monkeypatch.setattr(ingest, "get_git_root", lambda repo_root: tmp_path)
    monkeypatch.setattr(ingest.subprocess, "run", lambda *args, **kwargs: _Result())

    assert ingest.filter_gitignored_paths(paths, tmp_path) == [kept]


def test_normalize_result_status_maps_error_variants_to_errors() -> None:
    """@brief Verify worker result status values are normalized to summary keys."""
    assert ingest.normalize_result_status("indexed") == "indexed"
    assert ingest.normalize_result_status("skipped") == "skipped"
    assert ingest.normalize_result_status("error") == "errors"
    assert ingest.normalize_result_status("errors") == "errors"
    assert ingest.normalize_result_status("unexpected-value") == "errors"
    assert ingest.normalize_result_status(None) == "errors"


def test_resolve_repo_name_prefers_override_and_rejects_path_values() -> None:
    """@brief Verify repo naming uses override when valid and rejects path-like values."""
    repo_root = Path("/tmp/CodeBrain")

    assert ingest.resolve_repo_name(repo_root, None) == "CodeBrain"
    assert ingest.resolve_repo_name(repo_root, "custom-repo") == "custom-repo"
    assert ingest.resolve_repo_name(repo_root, "   ") == "CodeBrain"

    with pytest.raises(ingest.click.BadParameter):
        ingest.resolve_repo_name(repo_root, "nested/name")


def test_forced_non_code_intent_maps_markdown_and_config_languages() -> None:
    """@brief Verify non-code language labels map to deterministic ingestion intents."""
    assert ingest.forced_non_code_intent("markdown") == "documentation"
    assert ingest.forced_non_code_intent("toml") == "configuration"
    assert ingest.forced_non_code_intent("yaml") == "configuration"
    assert ingest.forced_non_code_intent("python") is None
    assert ingest.forced_non_code_intent(None) is None


def test_is_readme_doc_source_matches_readmes_and_top_level_markdown() -> None:
    """@brief Verify readme-source tagging for README files and repo-root markdown docs."""
    assert ingest._is_readme_doc_source("markdown", "README.md")
    assert ingest._is_readme_doc_source("markdown", "docs/README.MD")
    assert ingest._is_readme_doc_source("markdown", "ARCHITECTURE.md")
    assert not ingest._is_readme_doc_source("markdown", "docs/guide.md")
    assert not ingest._is_readme_doc_source("python", "README.md")


def test_persist_doc_links_embeds_and_inserts_rows() -> None:
    """@brief Verify doc_links persistence batches embeddings and inserts one row per payload."""

    class _DocLinkCursor:
        """@brief Cursor stub that records executed SQL statements."""

        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple | None]] = []

        def execute(self, query: str, params=None) -> None:
            self.calls.append((" ".join(query.strip().split()), params))

    cursor = _DocLinkCursor()
    rows = [
        {
            "source": "docstring",
            "target_kind": "symbol",
            "target_id": 17,
            "content": "Returns hydrated session data.",
        },
        {
            "source": "readme",
            "target_kind": "file",
            "target_id": 9,
            "content": "# Quickstart\nRun indexer first.",
        },
    ]

    inserted = ingest._persist_doc_links(
        cur=cursor,
        embedder=_FakeEmbedder(),
        repo_name="CodeBrain",
        rel_path="README.md",
        source_file_id=9,
        rows=rows,
    )
    assert inserted == 2

    insert_calls = [
        params
        for query, params in cursor.calls
        if query.lower().startswith("insert into doc_links")
    ]
    assert len(insert_calls) == 2
    assert insert_calls[0] == (
        "CodeBrain",
        9,
        "docstring",
        "README.md",
        "symbol",
        17,
        "Returns hydrated session data.",
        [0.0],
    )
    assert insert_calls[1] == (
        "CodeBrain",
        9,
        "readme",
        "README.md",
        "file",
        9,
        "# Quickstart\nRun indexer first.",
        [0.0],
    )


def test_cluster_modularity_contribution_prefers_connected_communities() -> None:
    """@brief Verify coherent communities score better than weakly connected singleton groups."""
    graph = ingest.nx.Graph()
    graph.add_edge(1, 2, weight=3.0)
    graph.add_edge(2, 3, weight=1.0)
    graph.add_edge(1, 3, weight=1.0)
    graph.add_edge(3, 4, weight=0.2)

    connected_contribution = ingest._cluster_modularity_contribution(graph, {1, 2, 3})
    singleton_contribution = ingest._cluster_modularity_contribution(graph, {4})
    assert connected_contribution > singleton_contribution


def test_build_cluster_embedding_input_truncates_long_payload() -> None:
    """@brief Verify cluster embedding text is bounded to the configured payload cap."""
    text = ingest._build_cluster_embedding_input(
        name="Ingestion Pipeline",
        summary="x" * (ingest.CLUSTER_SUMMARY_MAX_CHARS * 2),
        members=["member-a", "member-b"],
        granularity="symbol",
    )
    assert len(text) == ingest.CLUSTER_SUMMARY_MAX_CHARS
    assert text.startswith("cluster:Ingestion Pipeline")


def test_parse_cluster_profile_returns_fallback_on_parse_failure() -> None:
    """@brief Verify malformed cluster profile output falls back to deterministic defaults."""

    class _BrokenClassifier:
        """@brief Minimal classifier stub that always fails JSON parsing."""

        def _generate(self, prompt: str, max_tokens: int = 280) -> str:
            return "not-json"

        def _parse_json(self, raw: str):
            raise ValueError("bad json")

    name, summary = ingest._parse_cluster_profile(
        classifier=_BrokenClassifier(),
        prompt="irrelevant",
        fallback_name="Fallback Cluster",
        fallback_summary="Fallback summary",
        no_classify=False,
    )
    assert name == "Fallback Cluster"
    assert summary == "Fallback summary"


def test_detect_communities_falls_back_to_louvain_when_leiden_backend_is_unavailable(monkeypatch) -> None:
    """@brief Verify community detection uses Louvain when Leiden backend support is missing."""
    graph = ingest.nx.Graph()
    graph.add_edge(1, 2, weight=1.0)
    graph.add_node(3)

    def _raise_unavailable(*args, **kwargs):
        raise NotImplementedError("backend missing")

    monkeypatch.setattr(ingest.nx.community, "leiden_communities", _raise_unavailable)
    monkeypatch.setattr(
        ingest.nx.community,
        "louvain_communities",
        lambda *args, **kwargs: [{1, 2}, {3}],
    )

    communities, algorithm = ingest._detect_communities(graph, resolution=1.0)

    assert algorithm == "louvain"
    assert communities == [{1, 2}, {3}]


def test_detect_communities_falls_back_to_connected_components_when_community_apis_unavailable(monkeypatch) -> None:
    """@brief Verify final cluster fallback uses connected components when Leiden/Louvain are unavailable."""
    graph = ingest.nx.Graph()
    graph.add_edge(1, 2, weight=1.0)
    graph.add_node(3)

    def _raise_unavailable(*args, **kwargs):
        raise NotImplementedError("backend missing")

    monkeypatch.setattr(ingest.nx.community, "leiden_communities", _raise_unavailable)
    monkeypatch.setattr(ingest.nx.community, "louvain_communities", _raise_unavailable)

    communities, algorithm = ingest._detect_communities(graph, resolution=1.0)

    assert algorithm == "connected_components"
    assert {frozenset(group) for group in communities} == {frozenset({1, 2}), frozenset({3})}


def test_walk_repo_includes_markdown_toml_and_yaml_extensions(tmp_path) -> None:
    """@brief Verify walk_repo honors doc/config extension mappings in language config."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "README.md").write_text("# docs\n", encoding="utf-8")
    (repo_root / "codebrain.toml").write_text("x = 1\n", encoding="utf-8")
    (repo_root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (repo_root / "notes.txt").write_text("ignore\n", encoding="utf-8")

    config = {
        "ingestion": {"exclude": []},
        "languages": {
            "extensions": {
                "md": "markdown",
                "toml": "toml",
                "yml": "yaml",
            }
        },
    }

    files = ingest.walk_repo(repo_root, config)
    rel_paths = sorted(path.relative_to(repo_root).as_posix() for path in files)
    assert rel_paths == ["README.md", "codebrain.toml", "docker-compose.yml"]


def test_walk_repo_excludes_claude_directory_when_configured(tmp_path) -> None:
    """@brief Verify `.claude` worktree content is excluded by ingestion config patterns."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "main.py").write_text("print('ok')\n", encoding="utf-8")
    claude_dir = repo_root / ".claude" / "worktrees" / "demo"
    claude_dir.mkdir(parents=True)
    (claude_dir / "copy.py").write_text("print('shadow')\n", encoding="utf-8")

    config = {
        "ingestion": {"exclude": [".claude"]},
        "languages": {"extensions": {"py": "python"}},
    }

    files = ingest.walk_repo(repo_root, config)
    rel_paths = sorted(path.relative_to(repo_root).as_posix() for path in files)
    assert rel_paths == ["main.py"]


def test_extract_swift_service_edges_captures_typed_members_and_usage() -> None:
    """@brief Verify Swift service edges are derived from properties, init injection, and method calls."""
    content = "\n".join(
        [
            "final class TrackCoordinator {",
            "    private let trackService: TrackService",
            "    private let logger: Logger",
            "    init(trackService: TrackService) {}",
            "",
            "    func refreshTrack() {",
            "        trackService.reload()",
            "    }",
            "}",
        ]
    )
    chunks = [
        {
            "start_line": 1,
            "end_line": 9,
            "symbol_name": "TrackCoordinator",
            "symbol_type": "class",
        },
        {
            "start_line": 4,
            "end_line": 4,
            "symbol_name": "init",
            "symbol_type": "method",
            "parent_symbol": "TrackCoordinator",
        },
        {
            "start_line": 6,
            "end_line": 8,
            "symbol_name": "refreshTrack",
            "symbol_type": "method",
            "parent_symbol": "TrackCoordinator",
        },
    ]

    edges = ingest.extract_swift_service_edges(content, chunks)

    assert edges == [
        {
            "source_symbol_name": "TrackCoordinator",
            "target_name": "TrackService",
            "kind": "type_reference",
            "line_no": 2,
        },
        {
            "source_symbol_name": "TrackCoordinator",
            "target_name": "TrackService",
            "kind": "injection",
            "line_no": 4,
        },
        {
            "source_symbol_name": "TrackCoordinator",
            "target_name": "TrackService",
            "kind": "service_usage",
            "line_no": 7,
        },
    ]


def test_extract_symbol_references_deduplicates_and_skips_stopwords() -> None:
    """@brief Verify lexical reference extraction avoids duplicates and ignored keywords."""
    references = ingest.extract_symbol_references(
        [
            {
                "content": "\n".join(
                    [
                        "PhotoService()",
                        "photoStore.load(); photoStore.load()",
                        "if helper() { }",
                    ]
                ),
                "start_line": 10,
                "end_line": 12,
                "symbol_name": "refreshPhotos",
            }
        ]
    )

    assert references == [
        {
            "chunk_index": 0,
            "source_symbol_name": "refreshPhotos",
            "target_name": "PhotoService",
            "reference_kind": "type_reference",
            "line_no": 10,
        },
        {
            "chunk_index": 0,
            "source_symbol_name": "refreshPhotos",
            "target_name": "load",
            "reference_kind": "member_call",
            "line_no": 11,
        },
        {
            "chunk_index": 0,
            "source_symbol_name": "refreshPhotos",
            "target_name": "helper",
            "reference_kind": "call",
            "line_no": 12,
        },
    ]


def test_extract_symbol_relationships_parses_inheritance_edges_by_language() -> None:
    """@brief Verify inheritance extraction emits extends/implements/mixin edges."""
    python_edges = ingest.extract_symbol_relationships(
        [
            {
                "symbol_name": "PhotoController",
                "symbol_type": "class",
                "signature": "class PhotoController(BaseController, LoggingMixin):",
                "start_line": 7,
            }
        ],
        "python",
    )
    assert python_edges == [
        {
            "source_symbol_name": "PhotoController",
            "relationship_kind": "extends",
            "target_name": "BaseController",
            "external_module": None,
            "line_no": 7,
        },
        {
            "source_symbol_name": "PhotoController",
            "relationship_kind": "mixin",
            "target_name": "LoggingMixin",
            "external_module": None,
            "line_no": 7,
        },
    ]

    ts_edges = ingest.extract_symbol_relationships(
        [
            {
                "symbol_name": "PhotoService",
                "symbol_type": "class",
                "signature": "export class PhotoService extends BaseService implements Cacheable, Disposable {",
                "start_line": 3,
            }
        ],
        "typescript",
    )
    assert ts_edges == [
        {
            "source_symbol_name": "PhotoService",
            "relationship_kind": "extends",
            "target_name": "BaseService",
            "external_module": None,
            "line_no": 3,
        },
        {
            "source_symbol_name": "PhotoService",
            "relationship_kind": "implements",
            "target_name": "Cacheable",
            "external_module": None,
            "line_no": 3,
        },
        {
            "source_symbol_name": "PhotoService",
            "relationship_kind": "implements",
            "target_name": "Disposable",
            "external_module": None,
            "line_no": 3,
        },
    ]

    csharp_edges = ingest.extract_symbol_relationships(
        [
            {
                "symbol_name": "PhotoStore",
                "symbol_type": "class",
                "signature": "public class PhotoStore : Data.StoreBase, IPhotoStore, IDisposable {",
                "start_line": 11,
            }
        ],
        "csharp",
    )
    assert csharp_edges == [
        {
            "source_symbol_name": "PhotoStore",
            "relationship_kind": "extends",
            "target_name": "StoreBase",
            "external_module": "Data",
            "line_no": 11,
        },
        {
            "source_symbol_name": "PhotoStore",
            "relationship_kind": "implements",
            "target_name": "IPhotoStore",
            "external_module": None,
            "line_no": 11,
        },
        {
            "source_symbol_name": "PhotoStore",
            "relationship_kind": "implements",
            "target_name": "IDisposable",
            "external_module": None,
            "line_no": 11,
        },
    ]


def test_schema_patches_add_resolved_reference_columns_and_indexes() -> None:
    """@brief Verify ingestion schema patches cover CODEBRAIN-15 additive columns and indexes."""
    patch_blob = "\n".join(ingest.SCHEMA_PATCHES)

    assert "ADD COLUMN IF NOT EXISTS target_symbol_id INTEGER REFERENCES symbols(id) ON DELETE SET NULL" in patch_blob
    assert "ADD COLUMN IF NOT EXISTS resolution_confidence REAL" in patch_blob
    assert "ADD COLUMN IF NOT EXISTS resolution_method TEXT" in patch_blob
    assert "ADD COLUMN IF NOT EXISTS reference_kind_v2 TEXT" in patch_blob
    assert "UPDATE symbol_references" in patch_blob
    assert "SET reference_kind_v2 = reference_kind" in patch_blob
    assert "WHERE reference_kind_v2 IS NULL" in patch_blob
    assert "CREATE INDEX IF NOT EXISTS idx_symbol_refs_target_symbol" in patch_blob
    assert "CREATE INDEX IF NOT EXISTS idx_symbol_refs_target_name_kind" in patch_blob
    assert "CREATE INDEX IF NOT EXISTS idx_symbols_file_primary_name" in patch_blob
    assert "CREATE TABLE IF NOT EXISTS symbol_relationships" in patch_blob
    assert "REFERENCES symbols(id) ON DELETE CASCADE" in patch_blob
    assert "ADD COLUMN IF NOT EXISTS target_symbol_id INTEGER REFERENCES symbols(id) ON DELETE SET NULL" in patch_blob
    assert "CREATE INDEX IF NOT EXISTS idx_symbol_rels_source_symbol" in patch_blob
    assert "CREATE INDEX IF NOT EXISTS idx_symbol_rels_target_symbol" in patch_blob
    assert "ADD COLUMN IF NOT EXISTS imported_symbol_id INTEGER REFERENCES symbols(id) ON DELETE SET NULL" in patch_blob
    assert "ADD COLUMN IF NOT EXISTS imported_name TEXT" in patch_blob
    assert "ADD COLUMN IF NOT EXISTS local_alias TEXT" in patch_blob
    assert "ADD COLUMN IF NOT EXISTS is_external BOOLEAN" in patch_blob
    assert "ADD COLUMN IF NOT EXISTS external_version TEXT" in patch_blob
    assert "CREATE INDEX IF NOT EXISTS idx_deps_target_symbol" in patch_blob
    assert "CREATE INDEX IF NOT EXISTS idx_deps_reverse_lookup" in patch_blob
    assert "CREATE INDEX IF NOT EXISTS idx_symbol_refs_reverse_lookup" in patch_blob
    assert "CREATE INDEX IF NOT EXISTS idx_symbol_rels_reverse_lookup" in patch_blob
    assert "CREATE TABLE IF NOT EXISTS dependency_cycles" in patch_blob
    assert "CREATE INDEX IF NOT EXISTS idx_dependency_cycles_repo" in patch_blob
    assert "CREATE TABLE IF NOT EXISTS clusters" in patch_blob
    assert "cluster_key TEXT NOT NULL" in patch_blob
    assert "modularity REAL NOT NULL DEFAULT 0" in patch_blob
    assert "embedding vector(768)" in patch_blob
    assert "ALTER TABLE clusters" in patch_blob
    assert "granularity TEXT NOT NULL CHECK (granularity IN ('symbol', 'file'))" in patch_blob
    assert "CREATE INDEX IF NOT EXISTS idx_clusters_embedding" in patch_blob
    assert "CREATE TABLE IF NOT EXISTS cluster_members" in patch_blob
    assert "cluster_id INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE" in patch_blob
    assert "symbol_id INTEGER REFERENCES symbols(id) ON DELETE CASCADE" in patch_blob
    assert "file_id INTEGER REFERENCES files(id) ON DELETE CASCADE" in patch_blob
    assert "CREATE UNIQUE INDEX IF NOT EXISTS idx_cluster_members_symbol_unique" in patch_blob
    assert "CREATE UNIQUE INDEX IF NOT EXISTS idx_cluster_members_file_unique" in patch_blob
    assert "CREATE TABLE IF NOT EXISTS doc_links" in patch_blob
    assert "embedding vector(768) NOT NULL" in patch_blob
    assert "CREATE INDEX IF NOT EXISTS idx_doc_links_target" in patch_blob
    assert "CREATE OR REPLACE FUNCTION impact_of" in patch_blob
    assert "min_confidence   REAL DEFAULT 0.55" in patch_blob


def test_candidate_internal_import_paths_supports_python_and_typescript() -> None:
    """@brief Verify internal import path expansion for Python and TypeScript modules."""
    assert ingest._candidate_internal_import_paths(
        "pkg/controllers/photo_controller.py",
        "pkg.services.photo_service",
        "python",
    ) == [
        "pkg/services/photo_service.py",
        "pkg/services/photo_service/__init__.py",
    ]

    assert ingest._candidate_internal_import_paths(
        "pkg/controllers/photo_controller.py",
        ".services.photo_service",
        "python",
    ) == [
        "pkg/controllers/services/photo_service.py",
        "pkg/controllers/services/photo_service/__init__.py",
    ]

    assert ingest._candidate_internal_import_paths(
        "src/features/photo/view.ts",
        "../api/client",
        "typescript",
    ) == [
        "src/features/api/client",
        "src/features/api/client.ts",
        "src/features/api/client.tsx",
        "src/features/api/client.js",
        "src/features/api/client.jsx",
        "src/features/api/client.mts",
        "src/features/api/client.cts",
        "src/features/api/client/index.ts",
        "src/features/api/client/index.tsx",
        "src/features/api/client/index.js",
        "src/features/api/client/index.jsx",
        "src/features/api/client/index.mts",
        "src/features/api/client/index.cts",
    ]


def test_resolve_imported_symbol_id_uses_exported_symbols_only() -> None:
    """@brief Verify imported symbol resolution targets exported declarations in the module file."""

    class _SymbolLookupCursor:
        def __init__(self) -> None:
            self.queries: list[tuple[str, tuple]] = []

        def execute(self, query: str, params=None) -> None:
            self.queries.append((" ".join(query.split()), params))

        def fetchone(self):
            return (321,)

    cursor = _SymbolLookupCursor()
    resolved = ingest._resolve_imported_symbol_id(cursor, 77, "PhotoService")
    assert resolved == 321
    assert cursor.queries
    _, params = cursor.queries[0]
    assert params == (77, "PhotoService")
    assert ingest._resolve_imported_symbol_id(cursor, 77, "*") is None
    assert ingest._resolve_imported_symbol_id(cursor, None, "PhotoService") is None


def test_external_package_from_module_normalizes_language_specific_names() -> None:
    """@brief Verify external package name normalization for supported language ecosystems."""
    assert ingest._external_package_from_module("@scope/ui/button", "typescript") == "@scope/ui"
    assert ingest._external_package_from_module("react/jsx-runtime", "typescript") == "react"
    assert ingest._external_package_from_module("requests.sessions", "python") == "requests"
    assert ingest._external_package_from_module("org.apache.commons.lang3.StringUtils", "java") == "org.apache.commons"
    assert ingest._external_package_from_module("vector", "cpp") == "vector"


def test_external_version_for_package_uses_manifest_maps() -> None:
    """@brief Verify version lookups return ecosystem-specific manifest matches when available."""
    manifests = {
        "npm": {"react": "^18.2.0"},
        "pip": {"requests": "==2.31.0"},
        "maven": {"org.apache.commons": "3.12.0"},
    }
    assert ingest._external_version_for_package("react", "react/jsx-runtime", "typescript", manifests) == "^18.2.0"
    assert ingest._external_version_for_package("requests", "requests.sessions", "python", manifests) == "==2.31.0"
    assert ingest._external_version_for_package(
        "org.apache.commons",
        "org.apache.commons.lang3.StringUtils",
        "java",
        manifests,
    ) == "3.12.0"
    assert ingest._external_version_for_package("System", "System", "csharp", manifests) is None


def test_tarjan_strongly_connected_components_detects_cycles_and_acyclic_nodes() -> None:
    """@brief Verify Tarjan SCC decomposition groups cyclic subgraphs correctly."""
    components = ingest._tarjan_strongly_connected_components(
        {
            1: {2},
            2: {1, 3},
            3: set(),
            4: {4},
        }
    )
    normalized = {tuple(sorted(component)) for component in components}
    assert normalized == {(1, 2), (3,), (4,)}


def test_materialize_dependency_cycles_replaces_repo_rows() -> None:
    """@brief Verify cycle materialization clears prior rows and inserts detected SCC cycles."""

    class _CycleCursor:
        def __init__(self) -> None:
            self.deleted_repo = None
            self.inserted_rows: list[tuple] = []
            self._rows = [
                (1, "a.py", 2, "b.py"),
                (2, "b.py", 1, "a.py"),
                (3, "c.py", 3, "c.py"),
                (4, "d.py", 5, "e.py"),
            ]

        def execute(self, query: str, params=None) -> None:
            normalized = " ".join(query.strip().lower().split())
            if normalized.startswith("select d.source_file_id"):
                return
            if normalized.startswith("delete from dependency_cycles"):
                self.deleted_repo = params[0]
                return
            if normalized.startswith("insert into dependency_cycles"):
                self.inserted_rows.append(params)
                return
            raise AssertionError(f"Unexpected query: {query}")

        def fetchall(self):
            return self._rows

    class _CycleConn:
        def __init__(self) -> None:
            self.cursor_instance = _CycleCursor()
            self.commits = 0

        def cursor(self):
            return self.cursor_instance

        def commit(self) -> None:
            self.commits += 1

    conn = _CycleConn()
    count = ingest.materialize_dependency_cycles(conn, "repo")
    assert count == 2
    assert conn.cursor_instance.deleted_repo == "repo"
    assert conn.commits == 1
    assert len(conn.cursor_instance.inserted_rows) == 2


def test_process_file_includes_classifier_warnings(monkeypatch, tmp_path) -> None:
    """@brief Verify classifier fallback messages are returned in process_file results."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    fpath = repo_root / "demo.py"
    fpath.write_text("print('x')\n", encoding="utf-8")

    monkeypatch.setattr(ingest, "register_vector", lambda conn: None)

    result = ingest.process_file(
        fpath=fpath,
        repo_root=repo_root,
        repo_name="repo",
        config={"languages": {"extensions": {"py": "python"}}},
        embedder=_FakeEmbedder(),
        classifier=_FakeClassifier(),
        chunker=_FakeChunker(),
        db_pool=_FakePool(),
        force=True,
        no_classify=False,
    )

    assert result["status"] == "indexed"
    assert len(result.get("warnings", [])) == 1
    assert "Classifier file analysis fallback for demo.py" in result["warnings"][0]


def test_process_file_skips_non_code_files_over_size_cap(monkeypatch, tmp_path) -> None:
    """@brief Verify non-code files above cap are skipped before chunking and persistence."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    fpath = repo_root / "README.md"
    fpath.write_text("x" * 2048, encoding="utf-8")

    monkeypatch.setattr(ingest, "register_vector", lambda conn: None)

    result = ingest.process_file(
        fpath=fpath,
        repo_root=repo_root,
        repo_name="repo",
        config={
            "ingestion": {"non_code_max_bytes": 128},
            "languages": {"extensions": {"md": "markdown"}},
        },
        embedder=_FakeEmbedder(),
        classifier=_FakeClassifier(),
        chunker=_FakeChunker(),
        db_pool=_FakePool(),
        force=True,
        no_classify=False,
    )

    assert result["status"] == "skipped"
    assert "Skipped non-code file over cap" in result["warnings"][0]


def test_clear_repo_per_file_data_runs_deletes_in_dependency_order(monkeypatch) -> None:
    """@brief Verify the upfront `--force` clear runs the five DELETEs in order.

    Locks in the deadlock fix: per-file data must be cleared serially before
    parallel workers start, in the same order used inside `process_file`.
    """
    delete_order: list[str] = []

    class _ClearCursor:
        def execute(self, query: str, params=None) -> None:
            normalized = " ".join(query.strip().lower().split())
            if normalized.startswith("delete from symbol_references"):
                delete_order.append("symbol_references")
            elif normalized.startswith("delete from symbol_relationships"):
                delete_order.append("symbol_relationships")
            elif normalized.startswith("delete from dependencies"):
                delete_order.append("dependencies")
            elif normalized.startswith("delete from symbols"):
                delete_order.append("symbols")
            elif normalized.startswith("delete from code_chunks"):
                delete_order.append("code_chunks")
            assert params == ("repo",)

    class _ClearConn:
        def __init__(self) -> None:
            self._cursor = _ClearCursor()
            self.commits = 0
            self.closed = False

        def cursor(self) -> _ClearCursor:
            return self._cursor

        def commit(self) -> None:
            self.commits += 1

        def close(self) -> None:
            self.closed = True

    fake_conn = _ClearConn()
    monkeypatch.setattr(ingest, "get_db", lambda config: fake_conn)

    ingest.clear_repo_per_file_data({"database": {"url": "ignored"}}, "repo")

    assert delete_order == [
        "symbol_references",
        "symbol_relationships",
        "dependencies",
        "symbols",
        "code_chunks",
    ]
    assert fake_conn.commits == 1
    assert fake_conn.closed is True


def test_apply_env_overrides_honors_classifier_base_url(monkeypatch) -> None:
    """@brief Verify CLASSIFIER_BASE_URL env var overrides the classifier endpoint.

    Container/CI runs need to point the classifier at a host-reachable URL
    without editing the toml; this mirrors the existing EMBED_BASE_URL override.
    """
    monkeypatch.setenv("CLASSIFIER_BASE_URL", "http://host.docker.internal:9001")
    cfg = ingest._apply_env_overrides({"classifier": {"base_url": "http://example:1"}})
    assert cfg["classifier"]["base_url"] == "http://host.docker.internal:9001"

    monkeypatch.delenv("CLASSIFIER_BASE_URL", raising=False)
    untouched = ingest._apply_env_overrides({"classifier": {"base_url": "http://example:1"}})
    assert untouched["classifier"]["base_url"] == "http://example:1"


def test_process_file_clears_symbol_relationships_before_code_chunks(monkeypatch, tmp_path) -> None:
    """@brief Verify per-file re-ingest deletes references and dependencies before chunks/symbols.

    Locks in the deadlock fix: the cascade DELETE on symbol_references(source_chunk_id)
    must not fire under parallel re-ingest, so symbol_references and dependencies
    must be cleared before code_chunks/symbols.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    fpath = repo_root / "demo.py"
    fpath.write_text("print('x')\n", encoding="utf-8")

    delete_order: list[str] = []

    class _DeleteOrderCursor:
        def __init__(self) -> None:
            self._pending_fetch: tuple | None = None

        def execute(self, query: str, params=None) -> None:
            normalized = " ".join(query.strip().lower().split())
            if normalized.startswith("select id, hash from files"):
                self._pending_fetch = (42, "stale-hash")
                return
            if normalized.startswith("delete from symbol_references"):
                delete_order.append("symbol_references")
                return
            if normalized.startswith("delete from symbol_relationships"):
                delete_order.append("symbol_relationships")
                return
            if normalized.startswith("delete from dependencies"):
                delete_order.append("dependencies")
                return
            if normalized.startswith("delete from symbols"):
                delete_order.append("symbols")
                return
            if normalized.startswith("delete from code_chunks"):
                delete_order.append("code_chunks")
                return
            self._pending_fetch = None

        def fetchone(self):
            return self._pending_fetch

    class _DeleteOrderConn:
        def __init__(self) -> None:
            self._cursor = _DeleteOrderCursor()

        def cursor(self):
            return self._cursor

        def commit(self) -> None:
            return None

        def rollback(self) -> None:
            return None

    class _DeleteOrderPool:
        def __init__(self) -> None:
            self._conn = _DeleteOrderConn()

        def getconn(self):
            return self._conn

        def putconn(self, conn) -> None:
            return None

    monkeypatch.setattr(ingest, "register_vector", lambda conn: None)

    result = ingest.process_file(
        fpath=fpath,
        repo_root=repo_root,
        repo_name="repo",
        config={"languages": {"extensions": {"py": "python"}}},
        embedder=_FakeEmbedder(),
        classifier=_FakeClassifier(),
        chunker=_FakeChunker(),
        db_pool=_DeleteOrderPool(),
        force=True,
        no_classify=False,
    )

    assert result["status"] == "indexed"
    assert delete_order == [
        "symbol_references",
        "symbol_relationships",
        "dependencies",
        "symbols",
        "code_chunks",
    ]


def test_process_file_retries_deadlock_and_succeeds(monkeypatch, tmp_path) -> None:
    """@brief Verify transient deadlocks trigger bounded retry and return indexed status."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    fpath = repo_root / "demo.py"
    fpath.write_text("print('x')\n", encoding="utf-8")

    class _RetryCursor:
        def __init__(self) -> None:
            self._pending_fetch: tuple | None = None
            self.calls = 0

        def execute(self, query: str, params=None) -> None:
            normalized = " ".join(query.strip().lower().split())
            if normalized.startswith("select id, hash from files"):
                self._pending_fetch = (42, "stale-hash")
                return
            if normalized.startswith("update files set"):
                self.calls += 1
                if self.calls == 1:
                    raise ingest.psycopg2.errors.DeadlockDetected("deadlock detected")
            self._pending_fetch = None

        def fetchone(self):
            return self._pending_fetch

    class _RetryConn:
        def __init__(self) -> None:
            self._cursor = _RetryCursor()
            self.rollbacks = 0

        def cursor(self):
            return self._cursor

        def commit(self) -> None:
            return None

        def rollback(self) -> None:
            self.rollbacks += 1

    class _RetryPool:
        def __init__(self) -> None:
            self._conn = _RetryConn()

        def getconn(self):
            return self._conn

        def putconn(self, conn) -> None:
            return None

    monkeypatch.setattr(ingest, "register_vector", lambda conn: None)
    monkeypatch.setattr(ingest.time, "sleep", lambda seconds: None)

    retry_pool = _RetryPool()

    result = ingest.process_file(
        fpath=fpath,
        repo_root=repo_root,
        repo_name="repo",
        config={"languages": {"extensions": {"py": "python"}}},
        embedder=_FakeEmbedder(),
        classifier=_FakeClassifier(),
        chunker=_FakeChunker(),
        db_pool=retry_pool,
        force=True,
        no_classify=False,
    )

    assert result["status"] == "indexed"
    assert retry_pool._conn.rollbacks == 1
    assert retry_pool._conn._cursor.calls == 2


def test_reindex_handler_on_deleted_executes_delete_query(monkeypatch) -> None:
    """@brief Verify ReindexHandler.on_deleted removes the file from the database."""
    from watchdog.events import FileDeletedEvent

    deleted_queries = []

    class _MockCursor:
        def execute(self, query, params):
            if "DELETE FROM files" in query:
                deleted_queries.append((query, params))

    class _MockConn:
        def cursor(self):
            return _MockCursor()

        def commit(self):
            pass

    class _MockPool:
        def getconn(self):
            return _MockConn()

        def putconn(self, conn):
            pass

    repo_root = Path("/repo")
    handler = ingest.ReindexHandler(
        repo_root=repo_root,
        repo_name="test-repo",
        config={},
        embedder=_FakeEmbedder(),
        classifier=_FakeClassifier(),
        chunker=_FakeChunker(),
        db_pool=_MockPool(),
    )

    # Simulate deletion of /repo/src/main.py
    event = FileDeletedEvent(src_path="/repo/src/main.py")
    handler.on_deleted(event)

    assert len(deleted_queries) == 1
    query, params = deleted_queries[0]
    assert "DELETE FROM files" in query
    assert params == ("test-repo", "src/main.py")


def test_prune_stale_files_executes_correct_deletions() -> None:
    """@brief Verify prune_stale_files identifies and deletes missing files."""
    deleted_queries = []

    class _MockCursor:
        def execute(self, query, params=None):
            if "SELECT path FROM files" in query:
                self.results = [("stale.py",), ("active.py",)]
            elif "DELETE FROM files" in query:
                deleted_queries.append((query, params))

        def fetchall(self):
            return self.results

    class _MockConn:
        def cursor(self):
            return _MockCursor()

        def commit(self):
            pass

    repo_root = Path("/repo")
    # Only active.py exists on disk
    current_files = [Path("/repo/active.py")]

    stale = ingest.prune_stale_files(
        _MockConn(),
        "test-repo",
        repo_root,
        current_files
    )

    assert stale == ["stale.py"]
    assert len(deleted_queries) == 1
    query, params = deleted_queries[0]
    assert "DELETE FROM files" in query
    assert params == ("test-repo", "stale.py")
