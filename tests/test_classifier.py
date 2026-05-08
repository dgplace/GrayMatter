"""
@file tests/test_classifier.py
@brief Unit tests for the intent classifier wrapper.
"""

from codebrain.classifier import IntentClassifier


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


def test_analyze_file_falls_back_when_response_is_not_json(monkeypatch) -> None:
    """@brief Verify malformed model output returns conservative fallback values."""
    classifier = _classifier()
    monkeypatch.setattr(classifier, "_generate", lambda prompt, max_tokens=200: "not-json")

    assert classifier.analyze_file("demo.py", "print('x')", "python") == ("", "unknown")


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
