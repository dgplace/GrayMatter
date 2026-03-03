"""
Intent classification and summarization via OpenAI-compatible LLM proxy.
"""

import json
import httpx


INTENT_CATEGORIES = [
    "data-model",
    "business-logic",
    "api-endpoint",
    "utility",
    "configuration",
    "test",
    "infrastructure",
    "ui-component",
    "integration",
    "orchestration",
    "type-definition",
    "middleware",
    "migration",
]

# Batch-classifies all chunks in one call
BATCH_CLASSIFY_PROMPT = """Classify each code chunk below into one intent category and describe what it does.

Categories: data-model, business-logic, api-endpoint, utility, configuration, test, infrastructure, ui-component, integration, orchestration, type-definition, middleware, migration

File: {file_path}
Language: {language}

{chunks}

Respond with ONLY a JSON array with exactly {count} objects, one per chunk in order:
[{{"intent": "<category>", "description": "<one sentence>"}}, ...]"""

# Combines file summary + role into one call
ANALYZE_FILE_PROMPT = """Analyze this source file.

File: {file_path}
Language: {language}

Code:
```
{code}
```

Respond with ONLY this JSON object:
{{"summary": "<1-2 sentences on what this file does and its key exports>", "role": "<architectural role, e.g. API controller, database model, React component, utility library, test suite, config, middleware, migration, service layer, CLI entry point>"}}"""

_CHUNK_BATCH_SIZE = 8  # chunks per LLM call


class IntentClassifier:
    def __init__(self, config: dict):
        self.model = config["classifier"]["model"]
        self.url = config["classifier"]["base_url"]
        self.client = httpx.Client(timeout=120.0)

    def _generate(self, prompt: str, max_tokens: int = 200) -> str:
        response = self.client.post(
            f"{self.url}/v1/chat/completions",
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "temperature": 0.1,
                "max_tokens": max_tokens,
            },
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"].strip()

    def _parse_json(self, raw: str) -> dict | list:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return json.loads(cleaned)

    def analyze_file(self, file_path: str, code: str, language: str) -> tuple[str, str]:
        """Get file summary and architectural role in one LLM call.
        Returns (summary, role).
        """
        prompt = ANALYZE_FILE_PROMPT.format(
            file_path=file_path,
            language=language or "unknown",
            code=code[:3000],
        )
        try:
            data = self._parse_json(self._generate(prompt, max_tokens=150))
            return data.get("summary", ""), data.get("role", "unknown")
        except Exception:
            return "", "unknown"

    def classify_chunks_batch(
        self, chunks: list[dict], language: str, file_path: str
    ) -> list[tuple[str, str]]:
        """Classify all chunks in a file using one LLM call per batch of 8.
        Returns list of (intent, description) matching the input chunk order.
        """
        if not chunks:
            return []

        results: list[tuple[str, str]] = []
        for i in range(0, len(chunks), _CHUNK_BATCH_SIZE):
            batch = chunks[i : i + _CHUNK_BATCH_SIZE]
            results.extend(self._classify_batch(batch, language, file_path))
        return results

    def _classify_batch(
        self, chunks: list[dict], language: str, file_path: str
    ) -> list[tuple[str, str]]:
        chunk_blocks = "\n\n".join(
            f"[{i}] Lines {c['start_line']}-{c['end_line']}:\n```\n{c['content'][:500]}\n```"
            for i, c in enumerate(chunks)
        )
        prompt = BATCH_CLASSIFY_PROMPT.format(
            file_path=file_path,
            language=language or "unknown",
            chunks=chunk_blocks,
            count=len(chunks),
        )
        try:
            data = self._parse_json(self._generate(prompt, max_tokens=100 * len(chunks)))
            if not isinstance(data, list):
                raise ValueError("Expected list")
            results = []
            for item in data[: len(chunks)]:
                intent = item.get("intent", "utility")
                if intent not in INTENT_CATEGORIES:
                    intent = "utility"
                results.append((intent, item.get("description", "")))
            # Pad if model returned fewer items than expected
            while len(results) < len(chunks):
                results.append(("utility", ""))
            return results
        except Exception:
            return [("utility", "")] * len(chunks)

    # ── Legacy single-call methods (used by watch mode / fallback) ──────────

    def classify_intent(self, code: str, language: str, file_path: str) -> tuple[str, str]:
        results = self.classify_chunks_batch(
            [{"content": code, "start_line": 0, "end_line": 0}], language, file_path
        )
        return results[0]

    def summarize_file(self, file_path: str, code: str, language: str) -> str:
        summary, _ = self.analyze_file(file_path, code, language)
        return summary

    def classify_role(self, file_path: str, code: str, language: str) -> str:
        _, role = self.analyze_file(file_path, code, language)
        return role
