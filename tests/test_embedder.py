"""
@file tests/test_embedder.py
@brief Unit tests for the embedding client wrapper.
"""

import httpx

from embedder import EmbeddingClient


def test_openai_headers_include_bearer_token() -> None:
    """@brief Verify OpenAI-style requests include the configured API key."""
    client = EmbeddingClient(
        {
            "embeddings": {
                "model": "embed-model",
                "dimensions": 3,
                "api_style": "openai",
                "base_url": "http://example.test",
                "api_key": "secret-token",
            }
        }
    )

    assert client._headers() == {
        "Content-Type": "application/json",
        "Authorization": "Bearer secret-token",
    }


def test_embed_batch_sorts_openai_results_by_index(monkeypatch) -> None:
    """@brief Verify OpenAI-style batch embeddings are returned in request order."""
    client = EmbeddingClient(
        {
            "embeddings": {
                "model": "embed-model",
                "dimensions": 2,
                "api_style": "openai",
                "base_url": "http://example.test",
            }
        }
    )

    monkeypatch.setattr(
        client,
        "_post",
        lambda input_data: {
            "data": [
                {"index": 1, "embedding": [3.0, 4.0]},
                {"index": 0, "embedding": [1.0, 2.0]},
            ]
        },
    )

    assert client.embed_batch(["first", "second"]) == [[1.0, 2.0], [3.0, 4.0]]


def test_embed_raises_on_dimension_mismatch(monkeypatch) -> None:
    """@brief Verify dimension validation rejects provider responses with bad vector sizes."""
    client = EmbeddingClient(
        {
            "embeddings": {
                "model": "embed-model",
                "dimensions": 3,
                "api_style": "ollama",
                "base_url": "http://example.test",
            }
        }
    )

    monkeypatch.setattr(client, "_post", lambda input_data: {"embeddings": [[1.0, 2.0]]})

    try:
        client.embed("hello")
    except ValueError as exc:
        assert "Expected 3 dimensions, got 2" in str(exc)
    else:
        raise AssertionError("Expected embed() to reject invalid dimensions")


def test_embed_raises_with_endpoint_context_on_transport_failure(monkeypatch) -> None:
    """@brief Verify transport failures include endpoint and model context."""
    client = EmbeddingClient(
        {
            "embeddings": {
                "model": "embed-model",
                "dimensions": 3,
                "api_style": "ollama",
                "base_url": "http://127.0.0.1:11434",
            }
        }
    )

    def _raise_http_error(*_args, **_kwargs):
        request = httpx.Request("POST", "http://127.0.0.1:11434/api/embed")
        raise httpx.ConnectTimeout("timed out", request=request)

    monkeypatch.setattr(client.client, "post", _raise_http_error)

    try:
        client.embed("hello")
    except RuntimeError as exc:
        message = str(exc)
        assert "Embedding request transport failed" in message
        assert "endpoint=http://127.0.0.1:11434/api/embed" in message
        assert "model=embed-model" in message
    else:
        raise AssertionError("Expected embed() to raise RuntimeError on transport failures")
