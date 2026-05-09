"""
@file tests/test_resolver.py
@brief Unit tests for the resolver pipeline stage.
"""

import json
from pathlib import Path

import resolver


class _ResolverCursor:
    """@brief Deterministic cursor stub for resolver unit tests."""

    def __init__(self) -> None:
        self._pending_fetchone = None
        self._pending_fetchall = []
        self.updated_rows = []

    def execute(self, query: str, params=None) -> None:
        """@brief Provide canned responses for resolver SQL calls.

        @param query SQL statement text.
        @param params SQL parameters.
        @return None.
        """
        normalized = " ".join(query.strip().lower().split())
        if normalized.startswith(
            "select s.id, s.file_id from symbols s join files f on f.id = s.file_id where f.repo = %s and f.path = %s and s.name = %s and s.start_line <= %s and s.end_line >= %s"
        ):
            repo_name, target_path, target_name, target_line, _ = params
            if (
                repo_name == "fixture-repo"
                and target_path == "src/greeter.ts"
                and target_name == "Greeter"
                and target_line == 1
            ):
                self._pending_fetchone = (901, 91)
            elif (
                repo_name == "fixture-repo"
                and target_path == "src/greeter.ts"
                and target_name == "greet"
                and target_line == 2
            ):
                self._pending_fetchone = (902, 91)
            elif (
                repo_name == "fixture-python"
                and target_path == "src/helpers.py"
                and target_name == "Greeter"
                and target_line == 1
            ):
                self._pending_fetchone = (911, 191)
            elif (
                repo_name == "fixture-python"
                and target_path == "src/helpers.py"
                and target_name == "greet"
                and target_line == 2
            ):
                self._pending_fetchone = (912, 191)
            elif (
                repo_name == "fixture-java"
                and target_path == "src/Helpers.java"
                and target_name == "Greeter"
                and target_line == 1
            ):
                self._pending_fetchone = (921, 291)
            elif (
                repo_name == "fixture-java"
                and target_path == "src/Helpers.java"
                and target_name == "greet"
                and target_line == 2
            ):
                self._pending_fetchone = (922, 291)
            elif (
                repo_name == "fixture-cpp"
                and target_path == "src/engine.hpp"
                and target_name == "Renderer"
                and target_line == 1
            ):
                self._pending_fetchone = (931, 391)
            elif (
                repo_name == "fixture-cpp"
                and target_path == "src/engine.hpp"
                and target_name == "draw"
                and target_line == 2
            ):
                self._pending_fetchone = (932, 391)
            else:
                self._pending_fetchone = None
            self._pending_fetchall = []
            return

        if normalized.startswith(
            "select s.id, s.file_id from symbols s join files f on f.id = s.file_id where lower(s.name) = lower(%s)"
        ):
            target_name = params[0].lower()
            source_language = params[1]
            source_file_id = params[4]
            if target_name == "photoservice":
                self._pending_fetchall = [(101, 11)]
            elif target_name == "helper":
                self._pending_fetchall = [(203, 43), (202, 22)] if source_file_id == 43 else [(202, 22)]
            elif target_name == "ambiguoushelper":
                self._pending_fetchall = [(301, 31), (302, 32)]
            elif target_name == "embed":
                if source_language == "typescript":
                    self._pending_fetchall = [(810, 81)]
                elif source_language == "python":
                    self._pending_fetchall = [(910, 91)]
                else:
                    self._pending_fetchall = [(910, 91), (810, 81)]
            else:
                self._pending_fetchall = []
            self._pending_fetchone = None
            return

        if normalized.startswith("select id from symbols where file_id = %s"):
            self._pending_fetchall = [(7,), (8,)]
            self._pending_fetchone = None
            return

        if normalized.startswith(
            "select sr.id, sr.source_file_id, f.path, f.language, sr.source_symbol_name, sr.target_name, sr.reference_kind, sr.line_no from symbol_references sr join files f on f.id = sr.source_file_id where sr.target_symbol_id = any(%s) and sr.source_file_id <> %s"
        ):
            self._pending_fetchall = [
                (301, 41, "src/a.py", "python", "refreshPhotos", "PhotoService", "type_reference", 10),
                (302, 42, "src/b.py", "python", "refreshPhotos", "MissingService", "call", 11),
                (303, 43, "src/c.py", "python", "refreshPhotos", "helper", "call", 12),
            ]
            self._pending_fetchone = None
            return

        if normalized.startswith(
            "select sr.id, sr.source_file_id, f.path, f.language, sr.source_symbol_name, sr.target_name, sr.reference_kind, sr.line_no from symbol_references sr join files f on f.id = sr.source_file_id where f.repo = %s"
        ):
            if params[0] == "fixture-repo":
                self._pending_fetchall = [
                    (501, 51, "src/main.ts", "typescript", "main", "Greeter", "type_reference", 1),
                    (502, 51, "src/main.ts", "typescript", "main", "Greeter", "type_reference", 2),
                    (503, 51, "src/main.ts", "typescript", "main", "greet", "member_call", 3),
                ]
            elif params[0] == "fixture-python":
                self._pending_fetchall = [
                    (601, 61, "src/main.py", "python", "run", "Greeter", "type_reference", 4),
                    (602, 61, "src/main.py", "python", "run", "greet", "member_call", 5),
                ]
            elif params[0] == "fixture-java":
                self._pending_fetchall = [
                    (701, 71, "src/Main.java", "java", "run", "Greeter", "type_reference", 3),
                    (702, 71, "src/Main.java", "java", "run", "greet", "member_call", 4),
                ]
            elif params[0] == "fixture-cpp":
                self._pending_fetchall = [
                    (801, 81, "src/main.cpp", "cpp", "run", "Renderer", "type_reference", 3),
                    (802, 81, "src/main.cpp", "cpp", "run", "draw", "member_call", 4),
                ]
            else:
                self._pending_fetchall = [
                    (401, 41, "src/a.py", "python", "refreshPhotos", "PhotoService", "type_reference", 10),
                    (402, 42, "src/b.py", "python", "refreshPhotos", "MissingService", "call", 11),
                    (403, 43, "src/c.py", "python", "refreshPhotos", "helper", "call", 12),
                ]
            self._pending_fetchone = None
            return

        if normalized.startswith("select count(*) from files where repo = %s"):
            self._pending_fetchone = (20,)
            self._pending_fetchall = []
            return

        if normalized.startswith("update symbol_references set target_symbol_id = %s"):
            self.updated_rows.append(params)
            self._pending_fetchone = None
            self._pending_fetchall = []
            return

        raise AssertionError(f"Unexpected SQL in resolver test stub: {query}")

    def fetchone(self):
        """@brief Return the pending single-row payload."""
        return self._pending_fetchone

    def fetchall(self):
        """@brief Return the pending multi-row payload."""
        return self._pending_fetchall


def test_extract_symbol_references_deduplicates_and_skips_stopwords() -> None:
    """@brief Verify lexical reference extraction avoids duplicates and ignored keywords."""
    references = resolver.extract_symbol_references(
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


def test_extract_symbol_references_emits_instantiation_edges_across_languages() -> None:
    """@brief Verify CODEBRAIN-26 instantiation extraction across supported language forms."""
    cases = [
        {
            "language": "typescript",
            "chunks": [
                {
                    "content": "\n".join(["const svc = new PhotoService();", "new helper();", "helper();"]),
                    "start_line": 10,
                    "end_line": 12,
                    "symbol_name": "bootstrap",
                    "symbol_type": "function",
                }
            ],
            "expected": {(10, "PhotoService")},
            "unexpected": {(11, "helper")},
        },
        {
            "language": "java",
            "chunks": [
                {
                    "content": "\n".join(["PhotoService svc = new PhotoService();", "helper();"]),
                    "start_line": 20,
                    "end_line": 21,
                    "symbol_name": "bootstrap",
                    "symbol_type": "function",
                }
            ],
            "expected": {(20, "PhotoService")},
            "unexpected": {(21, "helper")},
        },
        {
            "language": "csharp",
            "chunks": [
                {
                    "content": "\n".join(["var svc = new PhotoService();", "new helper();"]),
                    "start_line": 30,
                    "end_line": 31,
                    "symbol_name": "bootstrap",
                    "symbol_type": "method",
                }
            ],
            "expected": {(30, "PhotoService")},
            "unexpected": {(31, "helper")},
        },
        {
            "language": "cpp",
            "chunks": [
                {
                    "content": "\n".join(["auto ptr = new PhotoService();", "PhotoService svc;", "helper();"]),
                    "start_line": 40,
                    "end_line": 42,
                    "symbol_name": "main",
                    "symbol_type": "function",
                }
            ],
            "expected": {(40, "PhotoService"), (41, "PhotoService")},
            "unexpected": {(42, "helper")},
        },
        {
            "language": "swift",
            "chunks": [
                {
                    "content": "\n".join(["let svc = PhotoService()", "let svc2 = PhotoService.init()", "helper()"]),
                    "start_line": 50,
                    "end_line": 52,
                    "symbol_name": "bootstrap",
                    "symbol_type": "function",
                }
            ],
            "expected": {(50, "PhotoService"), (51, "PhotoService")},
            "unexpected": {(52, "helper")},
        },
        {
            "language": "python",
            "chunks": [
                {
                    "content": "class PhotoService:\n    pass",
                    "start_line": 60,
                    "end_line": 61,
                    "symbol_name": "PhotoService",
                    "symbol_type": "class",
                },
                {
                    "content": "\n".join(["svc = PhotoService()", "factory = ServiceFactory()", "helper()"]),
                    "start_line": 70,
                    "end_line": 72,
                    "symbol_name": "bootstrap",
                    "symbol_type": "function",
                },
            ],
            "expected": {(70, "PhotoService"), (71, "ServiceFactory")},
            "unexpected": {(72, "helper")},
        },
    ]

    for case in cases:
        references = resolver.extract_symbol_references(case["chunks"], language=case["language"])
        instantiations = {
            (ref["line_no"], ref["target_name"])
            for ref in references
            if ref["reference_kind"] == "instantiation"
        }
        assert case["expected"].issubset(instantiations)
        assert case["unexpected"].isdisjoint(instantiations)


def test_extract_symbol_references_emits_swift_call_member_and_instantiation_edges() -> None:
    """@brief Verify Swift extraction emits call/member_call/instantiation references."""
    references = resolver.extract_symbol_references(
        [
            {
                "content": "\n".join(
                    [
                        "let service = PhotoService()",
                        "service.fetch()",
                        "helper()",
                    ]
                ),
                "start_line": 10,
                "end_line": 12,
                "symbol_name": "bootstrap",
                "symbol_type": "function",
            }
        ],
        language="swift",
    )

    observed = {(ref["line_no"], ref["target_name"], ref["reference_kind"]) for ref in references}
    assert (10, "PhotoService", "instantiation") in observed
    assert (11, "fetch", "member_call") in observed
    assert (12, "helper", "call") in observed


def test_resolve_references_marks_python_class_calls_as_instantiation() -> None:
    """@brief Verify Python class constructor calls resolve as instantiation references."""
    cur = _ResolverCursor()
    resolved = resolver.resolve_references(
        cur,
        [
            {
                "content": "class PhotoService:\n    pass",
                "start_line": 1,
                "end_line": 2,
                "symbol_name": "PhotoService",
                "symbol_type": "class",
            },
            {
                "content": "svc = PhotoService()\nhelper()",
                "start_line": 5,
                "end_line": 6,
                "symbol_name": "bootstrap",
                "symbol_type": "function",
            },
        ],
        language="python",
        source_file_id=11,
    )

    instantiations = [row for row in resolved if row["reference_kind"] == "instantiation"]
    assert instantiations == [
        {
            "chunk_index": 1,
            "source_symbol_name": "bootstrap",
            "target_name": "PhotoService",
            "reference_kind": "instantiation",
            "line_no": 5,
            "target_symbol_id": 101,
            "target_file_id": 11,
            "resolution_confidence": resolver.HEURISTIC_NAME_CONFIDENCE,
            "resolution_method": "heuristic_name",
            "reference_kind_v2": "instantiation",
        }
    ]


def test_resolve_references_keeps_swift_edges_heuristic_confidence() -> None:
    """@brief Verify Swift references remain heuristic with confidence strictly below exact."""
    cur = _ResolverCursor()
    resolved = resolver.resolve_references(
        cur,
        [
            {
                "content": "\n".join(
                    [
                        "let service = PhotoService()",
                        "service.fetch()",
                        "helper()",
                    ]
                ),
                "start_line": 20,
                "end_line": 22,
                "symbol_name": "bootstrap",
                "symbol_type": "function",
            }
        ],
        language="swift",
        source_file_id=43,
    )

    assert any(row["reference_kind"] == "instantiation" for row in resolved)
    assert any(row["reference_kind"] == "member_call" for row in resolved)
    assert any(row["reference_kind"] == "call" for row in resolved)
    assert all(row["resolution_confidence"] < resolver.EXACT_MATCH_CONFIDENCE for row in resolved)


def test_resolve_references_returns_uniform_resolver_shape() -> None:
    """@brief Verify resolver records always include method, confidence, and richer kind fields."""
    cur = _ResolverCursor()
    resolved = resolver.resolve_references(
        cur,
        [
            {
                "content": "\n".join(["PhotoService()", "helper()", "missing()"]),
                "start_line": 1,
                "end_line": 3,
                "symbol_name": "refreshPhotos",
            }
        ],
    )

    assert resolved == [
        {
            "chunk_index": 0,
            "source_symbol_name": "refreshPhotos",
            "target_name": "PhotoService",
            "reference_kind": "type_reference",
            "line_no": 1,
            "target_symbol_id": 101,
            "target_file_id": 11,
            "resolution_confidence": resolver.HEURISTIC_NAME_CONFIDENCE,
            "resolution_method": "heuristic_name",
            "reference_kind_v2": "type_reference",
        },
        {
            "chunk_index": 0,
            "source_symbol_name": "refreshPhotos",
            "target_name": "helper",
            "reference_kind": "call",
            "line_no": 2,
            "target_symbol_id": 202,
            "target_file_id": 22,
            "resolution_confidence": resolver.HEURISTIC_NAME_CONFIDENCE,
            "resolution_method": "heuristic_name",
            "reference_kind_v2": "call",
        },
        {
            "chunk_index": 0,
            "source_symbol_name": "refreshPhotos",
            "target_name": "missing",
            "reference_kind": "call",
            "line_no": 3,
            "target_symbol_id": None,
            "target_file_id": None,
            "resolution_confidence": 0.0,
            "resolution_method": "unresolved",
            "reference_kind_v2": "call",
        },
    ]


def test_build_reference_records_defers_cross_file_resolution() -> None:
    """@brief Verify unresolved reference records can be persisted before a serial refresh pass."""
    records = resolver.build_reference_records(
        [
            {
                "content": "\n".join(["PhotoService()", "helper()"]),
                "start_line": 1,
                "end_line": 2,
                "symbol_name": "refreshPhotos",
            }
        ],
    )

    assert records == [
        {
            "chunk_index": 0,
            "source_symbol_name": "refreshPhotos",
            "target_name": "PhotoService",
            "reference_kind": "type_reference",
            "line_no": 1,
            "target_symbol_id": None,
            "target_file_id": None,
            "resolution_confidence": 0.0,
            "resolution_method": "unresolved",
            "reference_kind_v2": "type_reference",
        },
        {
            "chunk_index": 0,
            "source_symbol_name": "refreshPhotos",
            "target_name": "helper",
            "reference_kind": "call",
            "line_no": 2,
            "target_symbol_id": None,
            "target_file_id": None,
            "resolution_confidence": 0.0,
            "resolution_method": "unresolved",
            "reference_kind_v2": "call",
        },
    ]


def test_build_reference_records_emits_python_instantiation_when_language_supplied() -> None:
    """@brief Verify the bulk-ingest path tags Python class calls as instantiation when language is passed."""
    records = resolver.build_reference_records(
        [
            {
                "content": "PhotoService()",
                "start_line": 1,
                "end_line": 1,
                "symbol_name": "bootstrap",
            }
        ],
        language="python",
    )

    instantiations = [row for row in records if row["reference_kind"] == "instantiation"]
    assert instantiations == [
        {
            "chunk_index": 0,
            "source_symbol_name": "bootstrap",
            "target_name": "PhotoService",
            "reference_kind": "instantiation",
            "line_no": 1,
            "target_symbol_id": None,
            "target_file_id": None,
            "resolution_confidence": 0.0,
            "resolution_method": "unresolved",
            "reference_kind_v2": "instantiation",
        }
    ]


def test_capture_incremental_refresh_emits_warning_only_guardrails() -> None:
    """@brief Verify incremental refresh plans warn on high file fan-out without broadening scope."""
    cur = _ResolverCursor()

    plan = resolver.capture_incremental_refresh(cur, "CodeBrain", 5)

    assert [row["id"] for row in plan["rows"]] == [301, 302, 303]
    assert plan["warnings"] == [
        "Resolver incremental refresh touched 3/20 source files; continuing with selective re-resolution."
    ]


def test_reresolve_inbound_references_updates_only_captured_rows() -> None:
    """@brief Verify inbound re-resolution rewrites only the planned reference rows."""
    cur = _ResolverCursor()
    plan = {
        "rows": [
            {
                "id": 301,
                "source_file_id": 41,
                "source_path": "src/a.py",
                "language": "python",
                "target_name": "PhotoService",
                "reference_kind": "type_reference",
                "line_no": 10,
            },
            {
                "id": 302,
                "source_file_id": 42,
                "source_path": "src/b.py",
                "language": "python",
                "target_name": "MissingService",
                "reference_kind": "call",
                "line_no": 11,
            },
            {
                "id": 303,
                "source_file_id": 43,
                "source_path": "src/c.py",
                "language": "python",
                "target_name": "helper",
                "reference_kind": "call",
                "line_no": 12,
            },
        ],
        "warnings": [],
    }

    updated = resolver.re_resolve_inbound_references(cur, plan)

    assert updated == 3
    assert cur.updated_rows == [
        (101, resolver.HEURISTIC_NAME_CONFIDENCE, "heuristic_name", "type_reference", 301),
        (None, 0.0, "unresolved", "call", 302),
        (203, resolver.HEURISTIC_NAME_CONFIDENCE, "heuristic_name", "call", 303),
    ]


def test_refresh_repo_references_updates_repo_rows_serially() -> None:
    """@brief Verify the serial repo refresh resolves previously persisted unresolved rows."""
    cur = _ResolverCursor()

    updated = resolver.refresh_repo_references(cur, "CodeBrain")

    assert updated == 3
    assert cur.updated_rows == [
        (101, resolver.HEURISTIC_NAME_CONFIDENCE, "heuristic_name", "type_reference", 401),
        (None, 0.0, "unresolved", "call", 402),
        (203, resolver.HEURISTIC_NAME_CONFIDENCE, "heuristic_name", "call", 403),
    ]


def test_refresh_repo_references_prefers_scip_typescript_exact_matches(monkeypatch) -> None:
    """@brief Verify TypeScript repo refresh uses SCIP matches before heuristic fallback."""
    fixture_root = Path(__file__).parent / "fixtures" / "scip_typescript"
    fixture_index = json.loads((fixture_root / "scip_print.json").read_text(encoding="utf-8"))
    strategy = resolver.TypeScriptScipResolverStrategy()
    monkeypatch.setattr(resolver, "_has_scip_tools", lambda: True)
    monkeypatch.setattr(resolver, "_has_node_modules", lambda _repo_root, _configs: True)
    monkeypatch.setattr(strategy, "_load_scip_index", lambda repo_root: fixture_index)
    monkeypatch.setattr(resolver, "RESOLVER_STRATEGIES", (strategy,))

    cur = _ResolverCursor()
    updated = resolver.refresh_repo_references(cur, "fixture-repo", repo_root=fixture_root)

    assert updated == 3
    assert cur.updated_rows == [
        (901, resolver.EXACT_MATCH_CONFIDENCE, "scip_typescript", "type_reference", 501),
        (901, resolver.EXACT_MATCH_CONFIDENCE, "scip_typescript", "type_reference", 502),
        (902, resolver.EXACT_MATCH_CONFIDENCE, "scip_typescript", "member_call", 503),
    ]


def test_refresh_repo_references_prefers_scip_python_exact_matches(monkeypatch) -> None:
    """@brief Verify Python repo refresh uses SCIP matches before heuristic fallback."""
    fixture_root = Path(__file__).parent / "fixtures" / "scip_python"
    fixture_index = json.loads((fixture_root / "scip_print.json").read_text(encoding="utf-8"))
    strategy = resolver.PythonScipResolverStrategy()
    monkeypatch.setattr(resolver, "_has_scip_python_tools", lambda: True)
    monkeypatch.setattr(resolver, "_has_supported_python_scip_runtime", lambda: True)
    monkeypatch.setattr(strategy, "_load_scip_index", lambda repo_root: fixture_index)
    monkeypatch.setattr(resolver, "RESOLVER_STRATEGIES", (strategy,))

    cur = _ResolverCursor()
    updated = resolver.refresh_repo_references(cur, "fixture-python", repo_root=fixture_root)

    assert updated == 2
    assert cur.updated_rows == [
        (911, resolver.EXACT_MATCH_CONFIDENCE, "scip_python", "type_reference", 601),
        (912, resolver.EXACT_MATCH_CONFIDENCE, "scip_python", "member_call", 602),
    ]


def test_refresh_repo_references_prefers_scip_java_exact_matches(monkeypatch) -> None:
    """@brief Verify Java repo refresh uses SCIP matches before heuristic fallback."""
    fixture_root = Path(__file__).parent / "fixtures" / "scip_java"
    fixture_index = json.loads((fixture_root / "scip_print.json").read_text(encoding="utf-8"))
    strategy = resolver.JavaScipResolverStrategy()
    monkeypatch.setattr(resolver, "_has_scip_java_tools", lambda: True)
    monkeypatch.setattr(resolver, "_has_java_project_markers", lambda _repo_root: True)
    monkeypatch.setattr(strategy, "_load_scip_index", lambda repo_root: fixture_index)
    monkeypatch.setattr(resolver, "RESOLVER_STRATEGIES", (strategy,))

    cur = _ResolverCursor()
    updated = resolver.refresh_repo_references(cur, "fixture-java", repo_root=fixture_root)

    assert updated == 2
    assert cur.updated_rows == [
        (921, resolver.EXACT_MATCH_CONFIDENCE, "scip_java", "type_reference", 701),
        (922, resolver.EXACT_MATCH_CONFIDENCE, "scip_java", "member_call", 702),
    ]


def test_refresh_repo_references_prefers_scip_clang_exact_matches(monkeypatch) -> None:
    """@brief Verify C/C++ repo refresh uses SCIP matches before heuristic fallback."""
    fixture_root = Path(__file__).parent / "fixtures" / "scip_cpp"
    fixture_index = json.loads((fixture_root / "scip_print.json").read_text(encoding="utf-8"))
    strategy = resolver.ClangScipResolverStrategy()
    monkeypatch.setattr(resolver, "_has_scip_clang_tools", lambda: True)
    monkeypatch.setattr(resolver, "_find_compile_commands", lambda _repo_root: fixture_root / "compile_commands.json")
    monkeypatch.setattr(strategy, "_load_scip_index", lambda repo_root, compdb_path: fixture_index)
    monkeypatch.setattr(resolver, "RESOLVER_STRATEGIES", (strategy,))

    cur = _ResolverCursor()
    updated = resolver.refresh_repo_references(cur, "fixture-cpp", repo_root=fixture_root)

    assert updated == 2
    assert cur.updated_rows == [
        (931, resolver.EXACT_MATCH_CONFIDENCE, "scip_clang", "type_reference", 801),
        (932, resolver.EXACT_MATCH_CONFIDENCE, "scip_clang", "member_call", 802),
    ]


def test_resolve_reference_rows_marks_ambiguous_heuristic_matches_below_threshold() -> None:
    """@brief Verify ambiguous fallback matches keep edges but lower confidence below 0.55."""
    cur = _ResolverCursor()

    resolved = resolver._resolve_reference_rows(
        cur,
        [
            {
                "id": 701,
                "source_file_id": 43,
                "source_path": "src/c.py",
                "language": "python",
                "source_symbol_name": "refreshPhotos",
                "target_name": "helper",
                "reference_kind": "call",
                "line_no": 12,
            },
            {
                "id": 702,
                "source_file_id": 50,
                "source_path": "src/d.py",
                "language": "python",
                "source_symbol_name": "refreshPhotos",
                "target_name": "ambiguousHelper",
                "reference_kind": "call",
                "line_no": 13,
            },
        ],
    )

    assert [
        (
            row["target_symbol_id"],
            row["target_file_id"],
            row["resolution_confidence"],
            row["resolution_method"],
        )
        for row in resolved
    ] == [
        (203, 43, resolver.HEURISTIC_NAME_CONFIDENCE, "heuristic_name"),
        (301, 31, resolver.AMBIGUOUS_HEURISTIC_CONFIDENCE, "heuristic_name_ambiguous"),
    ]


def test_resolve_reference_rows_scopes_heuristics_by_source_language_family() -> None:
    """@brief Verify heuristic fallback avoids cross-language symbol co-resolution."""
    cur = _ResolverCursor()

    resolved = resolver._resolve_reference_rows(
        cur,
        [
            {
                "id": 901,
                "source_file_id": 71,
                "source_path": "src/mcp/tools.ts",
                "language": "typescript",
                "source_symbol_name": "registerTools",
                "target_name": "embed",
                "reference_kind": "call",
                "line_no": 50,
            },
            {
                "id": 902,
                "source_file_id": 72,
                "source_path": "codebrain/ingest.py",
                "language": "python",
                "source_symbol_name": "process_file",
                "target_name": "embed",
                "reference_kind": "call",
                "line_no": 80,
            },
        ],
    )

    assert [
        (row["id"], row["target_symbol_id"], row["target_file_id"], row["resolution_method"])
        for row in resolved
    ] == [
        (901, 810, 81, "heuristic_name"),
        (902, 910, 91, "heuristic_name"),
    ]


def test_refresh_repo_references_skips_scip_when_cli_is_unavailable(monkeypatch) -> None:
    """@brief Verify missing SCIP binaries fall back to heuristic resolution instead of aborting ingest."""
    fixture_root = Path(__file__).parent / "fixtures" / "scip_typescript"
    monkeypatch.setattr(resolver, "_has_scip_tools", lambda: False)

    cur = _ResolverCursor()
    updated = resolver.refresh_repo_references(cur, "fixture-repo", repo_root=fixture_root)

    assert updated == 3
    assert cur.updated_rows == [
        (None, 0.0, "unresolved", "type_reference", 501),
        (None, 0.0, "unresolved", "type_reference", 502),
        (None, 0.0, "unresolved", "member_call", 503),
    ]


def test_collect_exact_matches_skips_non_scip_languages(tmp_path) -> None:
    """@brief Verify SCIP exact-match strategies don't fire for non-SCIP languages.

    Locks in CODEBRAIN-18: rows tagged with languages outside SCIP coverage must
    bypass exact-match strategies so the heuristic resolver path is used.
    """
    rows = [
        {
            "language": "java",
            "source_path": "src/Service.java",
            "target_name": "PhotoService",
            "line_no": 1,
        },
        {
            "language": "swift",
            "source_path": "src/Helper.swift",
            "target_name": "helper",
            "line_no": 2,
        },
        {
            "language": "csharp",
            "source_path": "src/Program.cs",
            "target_name": "Logger",
            "line_no": 3,
        },
        {
            "language": "cpp",
            "source_path": "src/main.cpp",
            "target_name": "renderFrame",
            "line_no": 4,
        },
    ]

    assert resolver._collect_exact_matches(tmp_path, rows) == {}


def test_resolve_reference_rows_uses_heuristic_for_non_scip_languages(tmp_path) -> None:
    """@brief Verify non-SCIP languages flow through the heuristic resolver path.

    Locks in CODEBRAIN-18: heuristic fallback edges must remain reachable for
    languages without SCIP coverage, with resolution_confidence below the exact
    ceiling and reference_kind_v2 populated whenever the kind was determined.
    """
    cur = _ResolverCursor()

    resolved = resolver._resolve_reference_rows(
        cur,
        [
            {
                "id": 801,
                "source_file_id": 11,
                "source_path": "src/Service.java",
                "language": "java",
                "source_symbol_name": "refresh",
                "target_name": "PhotoService",
                "reference_kind": "type_reference",
                "line_no": 7,
            },
            {
                "id": 802,
                "source_file_id": 12,
                "source_path": "src/Helper.swift",
                "language": "swift",
                "source_symbol_name": "load",
                "target_name": "helper",
                "reference_kind": "call",
                "line_no": 9,
            },
            {
                "id": 803,
                "source_file_id": 50,
                "source_path": "src/Program.cs",
                "language": "csharp",
                "source_symbol_name": "run",
                "target_name": "ambiguousHelper",
                "reference_kind": "type_reference",
                "line_no": 11,
            },
            {
                "id": 804,
                "source_file_id": 60,
                "source_path": "src/main.cpp",
                "language": "cpp",
                "source_symbol_name": "draw",
                "target_name": "missingSymbol",
                "reference_kind": "call",
                "line_no": 13,
            },
        ],
        repo_root=tmp_path,
    )

    expected = [
        (
            101,
            resolver.HEURISTIC_NAME_CONFIDENCE,
            "heuristic_name",
            "type_reference",
        ),
        (
            202,
            resolver.HEURISTIC_NAME_CONFIDENCE,
            "heuristic_name",
            "call",
        ),
        (
            301,
            resolver.AMBIGUOUS_HEURISTIC_CONFIDENCE,
            "heuristic_name_ambiguous",
            "type_reference",
        ),
        (None, 0.0, "unresolved", "call"),
    ]
    assert [
        (
            row["target_symbol_id"],
            row["resolution_confidence"],
            row["resolution_method"],
            row["reference_kind_v2"],
        )
        for row in resolved
    ] == expected
    for row in resolved:
        assert row["resolution_confidence"] < resolver.EXACT_MATCH_CONFIDENCE
        assert row["reference_kind_v2"] is not None
