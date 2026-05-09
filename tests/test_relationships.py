"""
@file tests/test_relationships.py
@brief Unit tests for symbol-relationship extraction helpers.
"""

from codebrain.ingestion.relationships import extract_symbol_relationships


def _edge_tuples(edges: list[dict]) -> set[tuple[str, str, str, str | None]]:
    """@brief Convert relationship rows into a comparable tuple set for assertions.

    @param edges Relationship rows returned by extraction helpers.
    @return Tuple set keyed by source symbol, kind, target, and external module.
    """
    return {
        (
            edge["source_symbol_name"],
            edge["relationship_kind"],
            edge["target_name"],
            edge.get("external_module"),
        )
        for edge in edges
    }


def test_extract_symbol_relationships_emits_returns_and_field_type_edges() -> None:
    """@brief Verify CODEBRAIN-22 extraction emits return and field type edges across languages."""
    cases = [
        (
            "typescript",
            [
                {
                    "symbol_name": "PhotoService",
                    "symbol_type": "class",
                    "signature": "export class PhotoService {",
                    "start_line": 1,
                    "content": "export class PhotoService {\n  private client: ApiClient;\n}",
                    "member_symbols": [
                        {
                            "symbol_name": "load",
                            "symbol_type": "method",
                            "signature": "load(id: string): Api.PhotoResponse {",
                            "start_line": 3,
                        }
                    ],
                }
            ],
            {
                ("PhotoService", "field_type", "ApiClient", None),
                ("load", "returns", "PhotoResponse", "Api"),
            },
        ),
        (
            "python",
            [
                {
                    "symbol_name": "PhotoStore",
                    "symbol_type": "class",
                    "signature": "class PhotoStore:",
                    "start_line": 1,
                    "content": "class PhotoStore:\n    client: ApiClient\n",
                    "member_symbols": [
                        {
                            "symbol_name": "load",
                            "symbol_type": "method",
                            "signature": "def load(self) -> PhotoResult:",
                            "start_line": 3,
                        }
                    ],
                }
            ],
            {
                ("PhotoStore", "field_type", "ApiClient", None),
                ("load", "returns", "PhotoResult", None),
            },
        ),
        (
            "java",
            [
                {
                    "symbol_name": "PhotoStore",
                    "symbol_type": "class",
                    "signature": "public class PhotoStore {",
                    "start_line": 1,
                    "content": "public class PhotoStore {\n  private ApiClient client;\n}",
                    "member_symbols": [
                        {
                            "symbol_name": "load",
                            "symbol_type": "method",
                            "signature": "public com.app.PhotoResult load(String id) {",
                            "start_line": 3,
                        }
                    ],
                }
            ],
            {
                ("PhotoStore", "field_type", "ApiClient", None),
                ("load", "returns", "PhotoResult", "com.app"),
            },
        ),
        (
            "csharp",
            [
                {
                    "symbol_name": "PhotoStore",
                    "symbol_type": "class",
                    "signature": "public class PhotoStore {",
                    "start_line": 1,
                    "content": "public class PhotoStore {\n  private Data.ApiClient client;\n}",
                    "member_symbols": [
                        {
                            "symbol_name": "Load",
                            "symbol_type": "method",
                            "signature": "public Task<MyApp.PhotoResult> Load(string id) {",
                            "start_line": 3,
                        }
                    ],
                }
            ],
            {
                ("PhotoStore", "field_type", "ApiClient", "Data"),
                ("Load", "returns", "PhotoResult", "MyApp"),
            },
        ),
        (
            "cpp",
            [
                {
                    "symbol_name": "PhotoStore",
                    "symbol_type": "class",
                    "signature": "class PhotoStore {",
                    "start_line": 1,
                    "content": "class PhotoStore {\n  Client::ApiClient client;\n};",
                    "member_symbols": [
                        {
                            "symbol_name": "load",
                            "symbol_type": "method",
                            "signature": "Engine::PhotoResult load() {",
                            "start_line": 3,
                        }
                    ],
                }
            ],
            {
                ("PhotoStore", "field_type", "ApiClient", "Client"),
                ("load", "returns", "PhotoResult", "Engine"),
            },
        ),
        (
            "swift",
            [
                {
                    "symbol_name": "PhotoStore",
                    "symbol_type": "class",
                    "signature": "final class PhotoStore {",
                    "start_line": 1,
                    "content": "final class PhotoStore {\n  private let client: Api.Client\n}",
                    "member_symbols": [
                        {
                            "symbol_name": "load",
                            "symbol_type": "method",
                            "signature": "func load() -> Services.PhotoResult {",
                            "start_line": 3,
                        }
                    ],
                }
            ],
            {
                ("PhotoStore", "field_type", "Client", "Api"),
                ("load", "returns", "PhotoResult", "Services"),
            },
        ),
    ]

    for language, chunks, expected_edges in cases:
        edges = extract_symbol_relationships(chunks, language)
        tupled_edges = _edge_tuples(edges)
        assert expected_edges.issubset(tupled_edges)
