"""
@file classifier.py
@brief Intent classification and summarization via an OpenAI-compatible LLM API.

Provides chunk-level intent classification and file-level summarization with
strict JSON parsing and conservative fallbacks for malformed model output.
"""

import json
from typing import Callable, Optional

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
    """@brief Classify code intent and summarize files through a chat model."""

    def __init__(self, config: dict):
        """@brief Initialize the classifier from repository configuration.

        @param config Parsed CodeBrain configuration dictionary.
        """
        self.model = config["classifier"]["model"]
        self.url = config["classifier"]["base_url"]
        self.client = httpx.Client(timeout=120.0)

    def _generate(self, prompt: str, max_tokens: int = 200) -> str:
        """@brief Execute one non-streaming chat completion request.

        @param prompt Prompt content sent to the classifier model.
        @param max_tokens Maximum completion tokens to request.
        @return Raw model text response.
        """
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
        """@brief Parse JSON output, removing fenced code blocks when present.

        @param raw Raw model response text.
        @return Parsed JSON object or array.
        """
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return json.loads(cleaned)

    def _emit_warning(
        self,
        on_warning: Optional[Callable[[str], None]],
        message: str,
    ) -> None:
        """@brief Deliver one classifier warning to an optional callback.

        @param on_warning Callback receiving a human-readable warning message.
        @param message Warning payload text.
        """
        if on_warning:
            on_warning(message)

    def analyze_file(
        self,
        file_path: str,
        code: str,
        language: str,
        on_warning: Optional[Callable[[str], None]] = None,
    ) -> tuple[str, str]:
        """@brief Summarize a file and infer its architectural role.

        @param file_path Repository-relative or absolute file path.
        @param code File contents.
        @param language Detected file language.
        @param on_warning Optional callback for model/parse failures.
        @return Tuple of `(summary, role)`, or empty/unknown fallback values.
        """
        prompt = ANALYZE_FILE_PROMPT.format(
            file_path=file_path,
            language=language or "unknown",
            code=code[:3000],
        )
        try:
            data = self._parse_json(self._generate(prompt, max_tokens=150))
            return data.get("summary", ""), data.get("role", "unknown")
        except Exception as exc:
            self._emit_warning(
                on_warning,
                f"Classifier file analysis fallback for {file_path}: {exc}",
            )
            return "", "unknown"

    def classify_chunks_batch(
        self,
        chunks: list[dict],
        language: str,
        file_path: str,
        on_warning: Optional[Callable[[str], None]] = None,
    ) -> list[tuple[str, str]]:
        """@brief Classify file chunks in fixed-size batches.

        @param chunks Chunk dictionaries to classify.
        @param language Detected file language.
        @param file_path Source file path for prompt context.
        @param on_warning Optional callback for model/parse failures.
        @return List of `(intent, description)` tuples in input order.
        """
        if not chunks:
            return []

        results: list[tuple[str, str]] = []
        for i in range(0, len(chunks), _CHUNK_BATCH_SIZE):
            batch = chunks[i : i + _CHUNK_BATCH_SIZE]
            results.extend(
                self._classify_batch(
                    batch,
                    language,
                    file_path,
                    on_warning=on_warning,
                )
            )
        return results

    def _classify_batch(
        self,
        chunks: list[dict],
        language: str,
        file_path: str,
        on_warning: Optional[Callable[[str], None]] = None,
    ) -> list[tuple[str, str]]:
        """@brief Classify one chunk batch with fallback-safe parsing.

        @param chunks Chunk dictionaries included in the batch.
        @param language Detected file language.
        @param file_path Source file path for prompt context.
        @param on_warning Optional callback for model/parse failures.
        @return List of `(intent, description)` tuples for the batch.
        """
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
        except Exception as exc:
            self._emit_warning(
                on_warning,
                f"Classifier chunk intent fallback for {file_path}: {exc}",
            )
            return [("utility", "")] * len(chunks)

    # ── Legacy single-call methods (used by watch mode / fallback) ──────────

    def classify_intent(self, code: str, language: str, file_path: str) -> tuple[str, str]:
        """@brief Classify a single code block using the batch classifier path.

        @param code Source code block to classify.
        @param language Detected file language.
        @param file_path Source file path for prompt context.
        @return One `(intent, description)` tuple.
        """
        results = self.classify_chunks_batch(
            [{"content": code, "start_line": 0, "end_line": 0}], language, file_path
        )
        return results[0]

    def summarize_file(self, file_path: str, code: str, language: str) -> str:
        """@brief Return only the file summary from `analyze_file`.

        @param file_path Source file path for prompt context.
        @param code File contents.
        @param language Detected file language.
        @return File summary text or an empty fallback string.
        """
        summary, _ = self.analyze_file(file_path, code, language)
        return summary

    def classify_role(self, file_path: str, code: str, language: str) -> str:
        """@brief Return only the architectural role from `analyze_file`.

        @param file_path Source file path for prompt context.
        @param code File contents.
        @param language Detected file language.
        @return Architectural role label or the `unknown` fallback.
        """
        _, role = self.analyze_file(file_path, code, language)
        return role
