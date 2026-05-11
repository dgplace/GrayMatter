"""
@file tests/test_embedder.py
@brief Unit tests for the embedding client wrapper.
"""

import httpx

from codebrain.embedder import EmbeddingClient


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


def test_embed_retries_transient_transport_failures(monkeypatch) -> None:
    """@brief Verify embed retries transient transport failures before succeeding."""
    client = EmbeddingClient(
        {
            "embeddings": {
                "model": "embed-model",
                "dimensions": 3,
                "api_style": "ollama",
                "base_url": "http://127.0.0.1:11434",
                "max_retries": 1,
                "retry_backoff_seconds": 0.0,
            }
        }
    )
    call_count = {"value": 0}

    def _post_with_one_timeout(url, json, headers):
        call_count["value"] += 1
        if call_count["value"] == 1:
            request = httpx.Request("POST", url)
            raise httpx.ConnectTimeout("timed out", request=request)
        request = httpx.Request("POST", url)
        return httpx.Response(200, json={"embeddings": [[1.0, 2.0, 3.0]]}, request=request)

    monkeypatch.setattr(client.client, "post", _post_with_one_timeout)

    assert client.embed("hello") == [1.0, 2.0, 3.0]
    assert call_count["value"] == 2


def test_embed_batch_splits_when_transient_error_hits_large_batch(monkeypatch) -> None:
    """@brief Verify batch embedding recursively splits on transient retryable failures."""
    client = EmbeddingClient(
        {
            "embeddings": {
                "model": "embed-model",
                "dimensions": 3,
                "api_style": "ollama",
                "base_url": "http://127.0.0.1:11434",
                "max_retries": 0,
                "retry_backoff_seconds": 0.0,
            }
        }
    )

    def _post_timeout_for_multi_input(url, json, headers):
        request = httpx.Request("POST", url)
        payload_input = json["input"]
        if isinstance(payload_input, list) and len(payload_input) > 1:
            raise httpx.ConnectTimeout("timed out", request=request)
        input_count = len(payload_input) if isinstance(payload_input, list) else 1
        return httpx.Response(
            200,
            json={"embeddings": [[1.0, 2.0, 3.0] for _ in range(input_count)]},
            request=request,
        )

    monkeypatch.setattr(client.client, "post", _post_timeout_for_multi_input)

    assert client.embed_batch(["one", "two"], batch_size=2) == [
        [1.0, 2.0, 3.0],
        [1.0, 2.0, 3.0],
    ]
