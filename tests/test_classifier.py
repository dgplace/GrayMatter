"""
@file tests/test_classifier.py
@brief Unit tests for the intent classifier wrapper.
"""

import httpx

from codebrain.classifier import (
    IntentClassifier,
    infer_architectural_role,
    normalize_architectural_role,
)


def _classifier() -> IntentClassifier:
    """@brief Build a classifier instance with a test-only configuration."""
    return IntentClassifier(
        {
            "classifier": {
                "model": "test-model",
                "base_url": "http://example.test",
            }
        }
    )


def test_parse_json_strips_markdown_fences() -> None:
    """@brief Verify fenced JSON responses are accepted."""
    classifier = _classifier()

    parsed = classifier._parse_json('```json\n{"summary":"ok","role":"service"}\n```')

    assert parsed == {"summary": "ok", "role": "service"}


def test_parse_json_extracts_first_balanced_json_segment() -> None:
    """@brief Verify parser tolerates leading/trailing prose around JSON."""
    classifier = _classifier()

    parsed = classifier._parse_json(
        'I will now respond in JSON.\n{"summary":"ok","role":"service"}\nDone.',
    )

    assert parsed == {"summary": "ok", "role": "service"}


def test_parse_json_rejects_empty_responses() -> None:
    """@brief Verify parser reports empty classifier payloads clearly."""
    classifier = _classifier()

    try:
        classifier._parse_json("   ")
        raise AssertionError("Expected ValueError for empty payload")
    except ValueError as exc:
        assert "Empty classifier response" in str(exc)


def test_classify_chunks_batch_normalizes_invalid_output(monkeypatch) -> None:
    """@brief Verify invalid intents and short model responses fall back safely."""
    classifier = _classifier()
    chunks = [
        {"content": "alpha()", "start_line": 1, "end_line": 1},
        {"content": "beta()", "start_line": 2, "end_line": 2},
    ]

    monkeypatch.setattr(
        classifier,
        "_generate",
        lambda prompt, max_tokens=200: (
            '[{"intent":"not-a-real-intent","description":"bad category"}]'
        ),
    )

    results = classifier.classify_chunks_batch(chunks, "python", "demo.py")

    assert results == [("utility", "bad category"), ("utility", "")]


def test_classify_chunks_batch_splits_malformed_batches(monkeypatch) -> None:
    """@brief Verify malformed multi-chunk responses retry as smaller batches."""
    classifier = _classifier()
    chunks = [
        {"content": "alpha()", "start_line": 1, "end_line": 1},
        {"content": "beta()", "start_line": 2, "end_line": 2},
    ]
    warnings: list[str] = []

    def _generate(prompt, max_tokens=200):
        if "exactly 2 objects" in prompt:
            return '[{"intent":"utility","description":"truncated"'
        if "alpha()" in prompt:
            return '[{"intent":"business-logic","description":"handles alpha"}]'
        if "beta()" in prompt:
            return '[{"intent":"integration","description":"handles beta"}]'
        return "[]"

    monkeypatch.setattr(classifier, "_generate", _generate)

    results = classifier.classify_chunks_batch(
        chunks,
        "python",
        "demo.py",
        on_warning=warnings.append,
    )

    assert results == [
        ("business-logic", "handles alpha"),
        ("integration", "handles beta"),
    ]
    assert warnings == []


def test_classify_chunks_batch_caps_split_retries(monkeypatch) -> None:
    """@brief Verify persistent malformed responses do not explode request count."""
    classifier = _classifier()
    chunks = [
        {"content": f"chunk_{idx}()", "start_line": idx, "end_line": idx}
        for idx in range(1, 5)
    ]
    warnings: list[str] = []
    calls = {"count": 0}

    def _generate(prompt, max_tokens=200):
        calls["count"] += 1
        return ""

    monkeypatch.setattr(classifier, "_generate", _generate)

    results = classifier.classify_chunks_batch(
        chunks,
        "python",
        "demo.py",
        on_warning=warnings.append,
    )

    assert results == [("utility", "")] * 4
    assert calls["count"] == 3
    assert len(warnings) == 2


def test_analyze_file_falls_back_when_response_is_not_json(monkeypatch) -> None:
    """@brief Verify malformed model output returns deterministic fallback values."""
    classifier = _classifier()
    monkeypatch.setattr(classifier, "_generate", lambda prompt, max_tokens=200: "not-json")

    assert classifier.analyze_file("demo.py", "print('x')", "python") == ("", "python module")


def test_analyze_file_reports_warning_on_fallback(monkeypatch) -> None:
    """@brief Verify analyze_file emits a warning when model output cannot be parsed."""
    classifier = _classifier()
    warnings: list[str] = []
    monkeypatch.setattr(classifier, "_generate", lambda prompt, max_tokens=200: "not-json")

    classifier.analyze_file("demo.py", "print('x')", "python", on_warning=warnings.append)

    assert len(warnings) == 1
    assert "Classifier file analysis fallback for demo.py" in warnings[0]


def test_analyze_file_handles_leading_text_before_json(monkeypatch) -> None:
    """@brief Verify file analysis can parse JSON wrapped in auxiliary text."""
    classifier = _classifier()
    monkeypatch.setattr(
        classifier,
        "_generate",
        lambda prompt, max_tokens=200: (
            "Sure, here is the JSON:\n"
            '{"summary":"Parses inputs","role":"utility library"}'
        ),
    )

    summary, role = classifier.analyze_file("demo.py", "print('x')", "python")

    assert summary == "Parses inputs"
    assert role == "utility library"


def test_normalize_architectural_role_bundles_case_and_spacing_variants() -> None:
    """@brief Verify role labels are canonicalized before storage."""
    assert normalize_architectural_role(" UI Component ") == "ui component"
    assert normalize_architectural_role("UI-component") == "ui component"
    assert normalize_architectural_role("UI_Component") == "ui component"
    assert normalize_architectural_role("") == "unknown"


def test_analyze_file_normalizes_role(monkeypatch) -> None:
    """@brief Verify file analysis returns canonical role labels."""
    classifier = _classifier()
    monkeypatch.setattr(
        classifier,
        "_generate",
        lambda prompt, max_tokens=200: (
            '{"summary":"Renders controls","role":"UI Component"}'
        ),
    )

    summary, role = classifier.analyze_file("demo.tsx", "export function Demo() {}", "typescript")

    assert summary == "Renders controls"
    assert role == "ui component"


def test_infer_architectural_role_uses_path_language_and_framework_cues() -> None:
    """@brief Verify deterministic role fallback avoids unknown file roles."""
    assert infer_architectural_role("DEM/DEMView.swift", "import SwiftUI\nstruct DEMView: View {}", "swift") == "ui component"
    assert infer_architectural_role("Model/ParserGen/mtypes.cpp", "", "cpp") == "parser"
    assert infer_architectural_role("codebrain.toml", "", "toml") == "configuration"
    assert infer_architectural_role("Source/Worker.cpp", "", "cpp") == "cpp module"


def test_classify_chunks_batch_reports_warning_on_fallback(monkeypatch) -> None:
    """@brief Verify chunk classification emits a warning when model output is malformed."""
    classifier = _classifier()
    warnings: list[str] = []
    monkeypatch.setattr(classifier, "_generate", lambda prompt, max_tokens=200: "not-json")

    classifier.classify_chunks_batch(
        [{"content": "alpha()", "start_line": 1, "end_line": 1}],
        "python",
        "demo.py",
        on_warning=warnings.append,
    )

    assert len(warnings) == 1
    assert "Classifier chunk intent fallback for demo.py" in warnings[0]


def test_generate_retries_once_on_retryable_server_error(monkeypatch) -> None:
    """@brief Verify classifier generation retries transient server errors before succeeding."""
    classifier = IntentClassifier(
        {
            "classifier": {
                "model": "test-model",
                "base_url": "http://example.test",
                "max_retries": 1,
                "retry_backoff_seconds": 0.0,
            }
        }
    )
    call_count = {"value": 0}

    def _post_retry_once(url, json):
        call_count["value"] += 1
        request = httpx.Request("POST", url)
        if call_count["value"] == 1:
            return httpx.Response(500, json={"error": "temporary"}, request=request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "{\"summary\":\"ok\",\"role\":\"utility\"}"}}]},
            request=request,
        )

    monkeypatch.setattr(classifier.client, "post", _post_retry_once)

    output = classifier._generate("prompt")
    assert output == "{\"summary\":\"ok\",\"role\":\"utility\"}"
    assert call_count["value"] == 2
