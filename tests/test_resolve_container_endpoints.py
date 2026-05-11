"""
@file tests/test_resolve_container_endpoints.py
@brief Regression tests for container endpoint resolver behavior.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_resolver_module():
    """@brief Load the resolver script module from the scripts directory."""
    script_path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "resolve-container-endpoints.py"
    )
    spec = importlib.util.spec_from_file_location("resolve_container_endpoints", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_resolve_container_proxy_values_maps_local_host_to_host_gateway() -> None:
    """@brief Verify localhost endpoints map to proxy URL plus host-gateway upstream."""
    resolver = _load_resolver_module()
    resolved = resolver.resolve_container_proxy_values(
        "EMBED_BASE_URL", "http://127.0.0.1:11434"
    )
    assert resolved == {
        "EMBED_BASE_URL": "http://embed_proxy:11434",
        "EMBED_PROXY_TARGET": "host.docker.internal:11434",
    }


def test_resolve_container_proxy_values_allows_non_local_host_targets() -> None:
    """@brief Verify configured non-local endpoints are forwarded through sidecar targets."""
    resolver = _load_resolver_module()
    resolved = resolver.resolve_container_proxy_values(
        "EMBED_BASE_URL",
        "http://192.168.0.22:11434",
        "http://192.168.0.22:11434",
    )
    assert resolved == {
        "EMBED_BASE_URL": "http://embed_proxy:11434",
        "EMBED_PROXY_TARGET": "192.168.0.22:11434",
    }


def test_resolve_container_proxy_values_blocks_unconfigured_non_local_host() -> None:
    """@brief Verify non-local endpoints must match the configured policy value."""
    resolver = _load_resolver_module()
    with pytest.raises(SystemExit, match="does not match configured policy value"):
        resolver.resolve_container_proxy_values(
            "EMBED_BASE_URL",
            "http://203.0.113.10:11434",
            "http://192.168.0.22:11434",
        )


def test_resolve_container_proxy_values_uses_default_https_port_when_omitted() -> None:
    """@brief Verify https endpoints without explicit ports default to 443."""
    resolver = _load_resolver_module()
    resolved = resolver.resolve_container_proxy_values(
        "CLASSIFIER_BASE_URL",
        "https://models.example.internal",
        "https://models.example.internal",
    )
    assert resolved == {
        "CLASSIFIER_BASE_URL": "http://classifier_proxy:3000",
        "CLASSIFIER_PROXY_TARGET": "models.example.internal:443",
    }
