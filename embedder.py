"""
@file embedder.py
@brief Embedding client abstraction for local and OpenAI-compatible providers.

Wraps the embedding transport differences between OpenAI-style `/v1/embeddings`
and Ollama `/api/embed` endpoints while enforcing the configured embedding
dimension for all returned vectors.
"""

import httpx


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
        self.max_input_chars = embed_cfg.get("max_input_chars", 4000)
        self.client = httpx.Client(timeout=60.0)

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
        @raises RuntimeError If the provider returns a non-success status.
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
            payload = {"model": self.model, "input": input_data}
            endpoint = "/api/embed"

        response = self.client.post(
            f"{self.url}{endpoint}",
            json=payload,
            headers=self._headers(),
        )
        if not response.is_success:
            raise RuntimeError(
                f"Embedding request failed {response.status_code}: {response.text}"
            )
        return response.json()

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

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """@brief Generate embeddings for multiple texts in one request.

        @param texts Source texts to embed.
        @return Embedding vectors in the same order as the input list.
        """
        embeddings = self._extract_embeddings(self._post([self._truncate(t) for t in texts]))
        self._validate_dimensions(embeddings)
        return embeddings
