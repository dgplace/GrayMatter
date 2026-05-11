"""
@file tests/test_compose_network_policy.py
@brief Regression tests for Docker Compose network isolation policy.
"""

from pathlib import Path


def _compose_source() -> str:
    """@brief Load docker-compose source for policy assertions."""
    compose_path = Path(__file__).resolve().parents[1] / "docker" / "docker-compose.yml"
    return compose_path.read_text(encoding="utf-8")


def _service_block(source: str, service_name: str, next_section_name: str) -> str:
    """@brief Extract one top-level service block from compose YAML text.

    @param source Full docker-compose YAML text.
    @param service_name Service name to extract.
    @param next_section_name Next section marker used as an end marker.
    @return Service block text, excluding the end marker service.
    """
    start_marker = f"\n  {service_name}:\n"
    start_index = source.find(start_marker)
    assert start_index >= 0, f"Missing service block: {service_name}"
    end_index = source.find(f"\n  {next_section_name}:\n", start_index + 1)
    if end_index < 0:
        end_index = source.find(f"\n{next_section_name}:\n", start_index + 1)
    assert end_index >= 0, f"Missing next section marker: {next_section_name}"
    return source[start_index:end_index]


def test_indexer_uses_internal_network_only() -> None:
    """@brief Verify indexer remains isolated on the internal-only network."""
    source = _compose_source()
    indexer_block = _service_block(source, "indexer", "mcp")

    assert "networks:\n      - codebrain_internal" in indexer_block
    assert "codebrain_host_access" not in indexer_block


def test_only_proxy_services_attach_to_host_access_network() -> None:
    """@brief Verify only model proxy sidecars bridge to host-access network."""
    source = _compose_source()

    embed_proxy_block = _service_block(source, "embed_proxy", "classifier_proxy")
    classifier_proxy_block = _service_block(source, "classifier_proxy", "postgres")
    postgres_block = _service_block(source, "postgres", "indexer")
    mcp_block = _service_block(source, "mcp", "volumes")

    assert "codebrain_host_access" in embed_proxy_block
    assert "codebrain_host_access" in classifier_proxy_block
    assert "codebrain_host_access" not in postgres_block
    assert "codebrain_host_access" not in mcp_block


def test_internal_network_is_marked_internal_true() -> None:
    """@brief Verify core service network remains externally isolated."""
    source = _compose_source()
    assert "codebrain_internal:" in source
    assert "internal: true" in source
