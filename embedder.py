"""
Embedding client for local providers.
Supports OpenAI-compatible /v1/embeddings (LM Studio) and Ollama /api/embed.
"""

import httpx


class EmbeddingClient:
    def __init__(self, config: dict):
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
        headers = {"Content-Type": "application/json"}
        if self.api_style == "openai" and self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _post(self, input_data: str | list[str]) -> dict:
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
        if self.api_style == "openai":
            items = sorted(data["data"], key=lambda item: item["index"])
            return [item["embedding"] for item in items]
        return data["embeddings"]

    def _truncate(self, text: str) -> str:
        if len(text) > self.max_input_chars:
            return text[: self.max_input_chars]
        return text

    def _validate_dimensions(self, embeddings: list[list[float]]) -> None:
        for embedding in embeddings:
            if len(embedding) != self.dimensions:
                raise ValueError(
                    f"Expected {self.dimensions} dimensions, got {len(embedding)}"
                )

    def embed(self, text: str) -> list[float]:
        """Generate embedding for a single text string."""
        data = self._post(self._truncate(text))
        embeddings = self._extract_embeddings(data)
        self._validate_dimensions(embeddings)
        return embeddings[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts in a single request when the provider supports it."""
        embeddings = self._extract_embeddings(self._post([self._truncate(t) for t in texts]))
        self._validate_dimensions(embeddings)
        return embeddings
