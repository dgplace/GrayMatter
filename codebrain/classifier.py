"""
@file classifier.py
@brief Intent classification and summarization via an OpenAI-compatible LLM API.

Provides chunk-level intent classification and file-level summarization with
strict JSON parsing and conservative fallbacks for malformed model output.
"""

import json
import re
import time
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
_CHUNK_RETRY_SPLIT_DEPTH = 1


def normalize_architectural_role(role: object) -> str:
    """@brief Canonicalize freeform architectural role labels.

    @param role Raw role value returned by the classifier model.
    @return Lowercase, whitespace-normalized role label, or `unknown`.
    """
    if not isinstance(role, str):
        return "unknown"
    normalized = re.sub(r"[-_]+", " ", role.strip().casefold())
    normalized = " ".join(normalized.split())
    return normalized or "unknown"


def infer_architectural_role(file_path: str, code: str, language: str) -> str:
    """@brief Infer a deterministic architectural role when model output fails.

    @param file_path Source file path used for path/name conventions.
    @param code File contents used for lightweight framework cues.
    @param language Detected source language.
    @return Canonical architectural role label.
    """
    path = file_path.replace("\\", "/").casefold()
    name = path.rsplit("/", 1)[-1]
    lang = (language or "").casefold()
    sample = code[:4000].casefold()

    if "/test" in path or name.startswith("test_") or name.endswith(("_test.py", ".test.ts", ".spec.ts", "tests.swift")):
        return "test suite"
    if name in {"dockerfile", "makefile"} or name.endswith((".toml", ".yaml", ".yml", ".json", ".ini", ".cfg")):
        return "configuration"
    if "migration" in path:
        return "migration"
    if name.endswith((".md", ".rst", ".txt")):
        return "documentation"
    if "controller" in path or "route" in path or "api" in path:
        return "api controller"
    if "parser" in path or "grammar" in path:
        return "parser"
    if "model" in path or "types" in path or "mtypes" in name:
        return "data model"
    if "service" in path:
        return "service layer"
    if "middleware" in path:
        return "middleware"
    if "cli" in path or name in {"main.py", "main.ts", "main.swift", "main.cpp"}:
        return "cli entry point"
    if lang in {"swift", "typescript", "javascript"}:
        if (
            name.endswith(("view.swift", ".tsx", ".jsx"))
            or "swiftui" in sample
            or ": view" in sample
            or "react" in sample
        ):
            return "ui component"
    if lang:
        return normalize_architectural_role(f"{lang} module")
    return "source file"


class IntentClassifier:
    """@brief Classify code intent and summarize files through a chat model."""

    def __init__(self, config: dict):
        """@brief Initialize the classifier from repository configuration.

        @param config Parsed CodeBrain configuration dictionary.
        """
        classifier_cfg = config["classifier"]
        self.model = classifier_cfg["model"]
        self.url = classifier_cfg["base_url"]
        self.request_timeout_seconds = float(classifier_cfg.get("request_timeout_seconds", 120.0))
        self.max_retries = max(0, int(classifier_cfg.get("max_retries", 2)))
        self.retry_backoff_seconds = max(0.0, float(classifier_cfg.get("retry_backoff_seconds", 1.0)))
        self.client = httpx.Client(timeout=self.request_timeout_seconds)

    def _generate(self, prompt: str, max_tokens: int = 200) -> str:
        """@brief Execute one non-streaming chat completion request.

        @param prompt Prompt content sent to the classifier model.
        @param max_tokens Maximum completion tokens to request.
        @return Raw model text response.
        """
        endpoint_url = f"{self.url}/v1/chat/completions"
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "temperature": 0.1,
            "max_tokens": max_tokens,
        }
        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.post(endpoint_url, json=payload)
            except httpx.HTTPError as exc:
                if attempt < self.max_retries and self._is_retryable_transport_error(exc):
                    self._sleep_before_retry(attempt)
                    continue
                raise RuntimeError(
                    "Classifier request transport failed "
                    f"(endpoint={endpoint_url}, model={self.model}): {exc}"
                ) from exc

            if response.is_success:
                message = response.json()["choices"][0]["message"]
                content = message.get("content") or ""
                return str(content).strip()

            if attempt < self.max_retries and self._is_retryable_status_code(response.status_code):
                self._sleep_before_retry(attempt)
                continue

            raise RuntimeError(
                "Classifier request failed "
                f"(endpoint={endpoint_url}, model={self.model}, status={response.status_code}): "
                f"{response.text}"
            )

        raise RuntimeError("Unreachable classifier retry loop exit")

    def _is_retryable_transport_error(self, error: httpx.HTTPError) -> bool:
        """@brief Determine whether a classifier transport error should be retried.

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

        @param status_code Classifier response status code.
        @return True for transient throttling/server statuses.
        """
        return status_code in {408, 409, 425, 429, 500, 502, 503, 504}

    def _sleep_before_retry(self, attempt: int) -> None:
        """@brief Apply bounded exponential backoff between retry attempts.

        @param attempt Zero-based retry attempt counter.
        """
        delay = min(8.0, self.retry_backoff_seconds * (2**attempt))
        if delay > 0:
            time.sleep(delay)

    def _extract_first_json_segment(self, raw: str) -> str:
        """@brief Extract the first balanced JSON object/array from freeform text.

        @param raw Raw model response text.
        @return JSON substring candidate.
        @raises ValueError When no balanced JSON segment is found.
        """
        start = None
        for idx, ch in enumerate(raw):
            if ch in "{[":
                start = idx
                break

        if start is None:
            raise ValueError("No JSON object/array found in classifier response")

        opener = raw[start]
        closer = "}" if opener == "{" else "]"
        depth = 0
        in_string = False
        escaped = False

        for idx in range(start, len(raw)):
            ch = raw[idx]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == "\"":
                    in_string = False
                continue

            if ch == "\"":
                in_string = True
                continue
            if ch == opener:
                depth += 1
                continue
            if ch == closer:
                depth -= 1
                if depth == 0:
                    return raw[start : idx + 1]

        raise ValueError("Unbalanced JSON object/array in classifier response")

    def _parse_json(self, raw: str) -> dict | list:
        """@brief Parse JSON output, removing fenced code blocks when present.

        @param raw Raw model response text.
        @return Parsed JSON object or array.
        """
        cleaned = raw.strip()
        if not cleaned:
            raise ValueError("Empty classifier response")
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            candidate = self._extract_first_json_segment(cleaned)
            return json.loads(candidate)

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
            inferred_role = infer_architectural_role(file_path, code, language)
            role = normalize_architectural_role(data.get("role"))
            if role == "unknown":
                role = inferred_role
            return data.get("summary", ""), role
        except Exception as exc:
            self._emit_warning(
                on_warning,
                f"Classifier file analysis fallback for {file_path}: {exc}",
            )
            return "", infer_architectural_role(file_path, code, language)

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
        retry_split_depth: int = _CHUNK_RETRY_SPLIT_DEPTH,
    ) -> list[tuple[str, str]]:
        """@brief Classify one chunk batch with fallback-safe parsing.

        @param chunks Chunk dictionaries included in the batch.
        @param language Detected file language.
        @param file_path Source file path for prompt context.
        @param on_warning Optional callback for model/parse failures.
        @param retry_split_depth Remaining split retries for malformed batches.
        @return List of `(intent, description)` tuples for the batch.
        """
        try:
            return self._classify_batch_once(chunks, language, file_path)
        except Exception as exc:
            if retry_split_depth > 0 and len(chunks) > 1:
                split_at = max(1, len(chunks) // 2)
                return self._classify_batch(
                    chunks[:split_at],
                    language,
                    file_path,
                    on_warning=on_warning,
                    retry_split_depth=retry_split_depth - 1,
                ) + self._classify_batch(
                    chunks[split_at:],
                    language,
                    file_path,
                    on_warning=on_warning,
                    retry_split_depth=retry_split_depth - 1,
                )
            self._emit_warning(
                on_warning,
                f"Classifier chunk intent fallback for {file_path}: {exc}",
            )
            return [("utility", "")] * len(chunks)

    def _classify_batch_once(
        self,
        chunks: list[dict],
        language: str,
        file_path: str,
    ) -> list[tuple[str, str]]:
        """@brief Classify one chunk batch without recursive fallback.

        @param chunks Chunk dictionaries included in the batch.
        @param language Detected file language.
        @param file_path Source file path for prompt context.
        @return List of `(intent, description)` tuples for the batch.
        @raises ValueError When model output cannot be parsed as the expected list.
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
