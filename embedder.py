"""
Local embedding via Ollama.
"""

import httpx


class OllamaEmbedder:
    def __init__(self, config: dict):
        self.model = config["embeddings"]["model"]
        self.url = config["embeddings"]["ollama_url"]
        self.dimensions = config["embeddings"]["dimensions"]
        self.client = httpx.Client(timeout=60.0)

    def _post(self, input):
        response = self.client.post(
            f"{self.url}/api/embed",
            json={"model": self.model, "input": input},
        )
        if not response.is_success:
            raise RuntimeError(
                f"Ollama embed failed {response.status_code}: {response.text}"
            )
        return response.json()

    def embed(self, text: str) -> list[float]:
        """Generate embedding for a single text string."""
        # nomic-embed-text is compiled with a 2048-token context in Ollama.
        # At ~2 chars/token for dense code, 4000 chars is the safe ceiling.
        if len(text) > 4000:
            text = text[:4000]

        data = self._post(text)

        # Ollama returns {"embeddings": [[...]]}
        embedding = data["embeddings"][0]

        if len(embedding) != self.dimensions:
            raise ValueError(
                f"Expected {self.dimensions} dimensions, got {len(embedding)}"
            )
        return embedding

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts. Ollama's /api/embed supports batch input."""
        texts = [t[:4000] for t in texts]
        return self._post(texts)["embeddings"]
