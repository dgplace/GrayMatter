"""
@file embedder.py
@brief Embedding client abstraction for local and OpenAI-compatible providers.

Wraps the embedding transport differences between OpenAI-style `/v1/embeddings`
and Ollama `/api/embed` endpoints while enforcing the configured embedding
dimension for all returned vectors.
"""

import httpx
import time


class _EmbeddingTransportError(RuntimeError):
    """@brief Retry-aware transport error wrapper for embedding requests."""


class _EmbeddingStatusError(RuntimeError):
    """@brief Retry-aware HTTP status error wrapper for embedding requests."""

    def __init__(self, message: str, status_code: int):
        """@brief Build a status error with an attached HTTP status code.

        @param message Human-readable error message.
        @param status_code HTTP status code returned by the provider.
        """
        super().__init__(message)
        self.status_code = status_code


class EmbeddingClient:
    """@brief Generate embeddings through the configured provider."""

    def __init__(self, config: dict):
        """@brief Initialize the embedding client from repository configuration.

        @param config Parsed CodeBrain configuration dictionary.
        """
        embed_cfg = config["embeddings"]
        self.model = embed_cfg["model"]
        self.dimensions = embed_cfg["dimensions"]
        self.api_style = (
            embed_cfg.get("api_style")
            or ("ollama" if embed_cfg.get("ollama_url") else "openai")
        ).lower()
        default_url = (
            "http://localhost:11434" if self.api_style == "ollama" else "http://localhost:1234"
        )
        self.url = (embed_cfg.get("base_url") or embed_cfg.get("ollama_url") or default_url).rstrip("/")
        self.api_key = embed_cfg.get("api_key")
        self.context_length = embed_cfg.get("context_length", 8192)
        self.max_input_chars = embed_cfg.get("max_input_chars", self.context_length * 4)
        self.request_timeout_seconds = float(embed_cfg.get("request_timeout_seconds", 120.0))
        self.max_retries = max(0, int(embed_cfg.get("max_retries", 2)))
        self.retry_backoff_seconds = max(0.0, float(embed_cfg.get("retry_backoff_seconds", 1.0)))
        self.batch_size = max(1, int(embed_cfg.get("batch_size", 50)))
        self.client = httpx.Client(timeout=self.request_timeout_seconds)

    def _headers(self) -> dict[str, str]:
        """@brief Build request headers for the configured provider.

        @return HTTP headers to send with embedding requests.
        """
        headers = {"Content-Type": "application/json"}
        if self.api_style == "openai" and self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _post(self, input_data: str | list[str]) -> dict:
        """@brief Send a provider-specific embedding request.

        @param input_data A single text string or batch of text strings.
        @return Parsed JSON response payload.
        @raises RuntimeError If request transport or provider status fails.
        """
        if self.api_style == "openai":
            payload = {
                "model": self.model,
                "input": input_data,
                "encoding_format": "float",
                "dimensions": self.dimensions,
            }
            endpoint = "/v1/embeddings"
        else:
            payload = {"model": self.model, "input": input_data, "truncate": True}
            if self.context_length:
                payload["options"] = {"num_ctx": self.context_length}
            endpoint = "/api/embed"

        endpoint_url = f"{self.url}{endpoint}"
        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.post(
                    endpoint_url,
                    json=payload,
                    headers=self._headers(),
                )
            except httpx.HTTPError as exc:
                if attempt < self.max_retries and self._is_retryable_transport_error(exc):
                    self._sleep_before_retry(attempt)
                    continue
                raise _EmbeddingTransportError(
                    "Embedding request transport failed "
                    f"(endpoint={endpoint_url}, model={self.model}, api_style={self.api_style}): {exc}"
                ) from exc

            if response.is_success:
                return response.json()

            if response.status_code == 400 and "exceeds the context length" in response.text:
                if isinstance(input_data, list):
                    truncated_data = [s[: len(s) // 2] for s in input_data]
                else:
                    truncated_data = input_data[: len(input_data) // 2]
                has_payload = (
                    isinstance(truncated_data, list) and any(len(s) > 0 for s in truncated_data)
                ) or (isinstance(truncated_data, str) and len(truncated_data) > 0)
                if has_payload:
                    return self._post(truncated_data)

            if attempt < self.max_retries and self._is_retryable_status_code(response.status_code):
                self._sleep_before_retry(attempt)
                continue

            raise _EmbeddingStatusError(
                "Embedding request failed "
                f"(endpoint={endpoint_url}, model={self.model}, api_style={self.api_style}, "
                f"status={response.status_code}): {response.text}",
                response.status_code,
            )

        raise RuntimeError("Unreachable embedding retry loop exit")

    def _is_retryable_transport_error(self, error: httpx.HTTPError) -> bool:
        """@brief Determine whether a transport error should be retried.

        @param error HTTP transport exception raised by `httpx`.
        @return True when the request is safe to retry.
        """
        return isinstance(
            error,
            (
                httpx.TimeoutException,
                httpx.ConnectError,
                httpx.ReadError,
                httpx.WriteError,
                httpx.PoolTimeout,
                httpx.RemoteProtocolError,
            ),
        )

    def _is_retryable_status_code(self, status_code: int) -> bool:
        """@brief Determine whether an HTTP status code should be retried.

        @param status_code Provider response status code.
        @return True for retryable throttling/transient server statuses.
        """
        return status_code in {408, 409, 425, 429, 500, 502, 503, 504}

    def _sleep_before_retry(self, attempt: int) -> None:
        """@brief Apply bounded exponential backoff between retry attempts.

        @param attempt Zero-based retry attempt counter.
        """
        delay = min(8.0, self.retry_backoff_seconds * (2**attempt))
        if delay > 0:
            time.sleep(delay)

    def _extract_embeddings(self, data: dict) -> list[list[float]]:
        """@brief Normalize provider responses into a list of vectors.

        @param data Raw JSON payload returned by the provider.
        @return Embedding vectors in request order.
        """
        if self.api_style == "openai":
            items = sorted(data["data"], key=lambda item: item["index"])
            return [item["embedding"] for item in items]
        return data["embeddings"]

    def _truncate(self, text: str) -> str:
        """@brief Trim oversized inputs to the configured character budget.

        @param text Input text to truncate.
        @return Original or truncated text.
        """
        if len(text) > self.max_input_chars:
            return text[: self.max_input_chars]
        return text

    def _validate_dimensions(self, embeddings: list[list[float]]) -> None:
        """@brief Enforce the configured embedding dimensionality.

        @param embeddings Embedding vectors returned by the provider.
        @raises ValueError If any vector length differs from `self.dimensions`.
        """
        for embedding in embeddings:
            if len(embedding) != self.dimensions:
                raise ValueError(
                    f"Expected {self.dimensions} dimensions, got {len(embedding)}"
                )

    def embed(self, text: str) -> list[float]:
        """@brief Generate an embedding for a single text string.

        @param text Source text to embed.
        @return One embedding vector matching the configured dimension.
        """
        data = self._post(self._truncate(text))
        embeddings = self._extract_embeddings(data)
        self._validate_dimensions(embeddings)
        return embeddings[0]

    def _embed_batch_payload(self, texts: list[str]) -> list[list[float]]:
        """@brief Embed one already-sized batch payload.

        @param texts Batch texts to send in one provider request.
        @return Batch embedding vectors in input order.
        """
        embeddings = self._extract_embeddings(self._post([self._truncate(t) for t in texts]))
        self._validate_dimensions(embeddings)
        return embeddings

    def _embed_batch_with_split_retry(self, texts: list[str]) -> list[list[float]]:
        """@brief Embed a batch and split recursively when transient failures occur.

        @param texts Batch texts to embed.
        @return Batch embedding vectors in input order.
        @raises RuntimeError When a single-item batch still fails.
        """
        try:
            return self._embed_batch_payload(texts)
        except (_EmbeddingTransportError, _EmbeddingStatusError):
            if len(texts) <= 1:
                raise
            midpoint = len(texts) // 2
            left = self._embed_batch_with_split_retry(texts[:midpoint])
            right = self._embed_batch_with_split_retry(texts[midpoint:])
            return left + right

    def embed_batch(self, texts: list[str], batch_size: int | None = None) -> list[list[float]]:
        """@brief Generate embeddings for multiple texts, batching to avoid timeouts or limits.

        @param texts Source texts to embed.
        @param batch_size Optional per-call batch size override.
        @return Embedding vectors in the same order as the input list.
        """
        resolved_batch_size = max(1, batch_size or self.batch_size)
        all_embeddings = []
        for i in range(0, len(texts), resolved_batch_size):
            batch = texts[i : i + resolved_batch_size]
            all_embeddings.extend(self._embed_batch_with_split_retry(batch))
        return all_embeddings
