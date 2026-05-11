#!/usr/bin/env python3
"""
/**
 * @file resolve-container-endpoints.py
 * @brief Emit container-safe endpoint environment overrides for ingestion runs.
 *
 * Reads candidate CodeBrain config files and prints KEY=VALUE lines for
 * EMBED_BASE_URL and CLASSIFIER_BASE_URL plus proxy-upstream target values.
 * Endpoint hosts are reached through in-stack proxy sidecars. Non-local hosts
 * are only allowed when they match configured embedding/classifier values.
 */
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlparse

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

ALLOWED_LOCAL_HOSTS = {
    "127.0.0.1",
    "localhost",
    "::1",
    "0.0.0.0",
    "host.docker.internal",
}

PROXY_BASE_URL_BY_ENV = {
    "EMBED_BASE_URL": "http://embed_proxy:11434",
    "CLASSIFIER_BASE_URL": "http://classifier_proxy:3000",
}

PROXY_TARGET_ENV_BY_ENDPOINT = {
    "EMBED_BASE_URL": "EMBED_PROXY_TARGET",
    "CLASSIFIER_BASE_URL": "CLASSIFIER_PROXY_TARGET",
}


def fail(message: str) -> None:
    """
    /**
     * @brief Raise a fatal script error with a human-readable message.
     * @param message Error text shown to the caller.
     */
    """
    raise SystemExit(message)


def load_config_base_urls(repo_root: Path) -> dict[str, str]:
    """
    /**
     * @brief Load endpoint base URLs from repository config files.
     * @param repo_root Absolute repository root path.
     * @return Mapping keyed by config section (`embeddings`/`classifier`).
     */
    """
    local_config_path = repo_root / ".env" / "codebrain.toml"
    if not local_config_path.is_file():
        fail(
            "Missing required .env/codebrain.toml. Copy codebrain.example.toml "
            "to .env/codebrain.toml and configure it."
        )

    config_base_urls: dict[str, str] = {}
    for path in (repo_root / "codebrain.toml", local_config_path):
        if not path.is_file():
            continue
        with path.open("rb") as handle:
            data = tomllib.load(handle)
        for section_name in ("embeddings", "classifier"):
            section = data.get(section_name)
            if not isinstance(section, dict):
                continue
            base_url = section.get("base_url", "")
            if isinstance(base_url, str):
                config_base_urls[section_name] = base_url
    return config_base_urls


def _default_port_for_scheme(scheme: str) -> int | None:
    """
    /**
     * @brief Resolve default network port for supported URL schemes.
     * @param scheme URL scheme.
     * @return Default port for the scheme, or None when unsupported.
     */
    """
    if scheme == "http":
        return 80
    if scheme == "https":
        return 443
    return None


def _parse_endpoint_host_port(url: str) -> tuple[str, int]:
    """
    /**
     * @brief Parse and validate an endpoint URL into normalized host/port.
     * @param url Endpoint URL to parse.
     * @return Normalized lower-cased host and resolved numeric port.
     */
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    scheme = parsed.scheme.lower()
    if not parsed.scheme or not host:
        fail(f"Invalid URL value: {url}")
    try:
        port = parsed.port
    except ValueError:
        fail(f"Invalid URL value: {url}")
        port = None
    if port is None:
        port = _default_port_for_scheme(scheme)
    if port is None:
        fail(f"Unsupported URL scheme (expected http/https): {url}")
    return host, port


def resolve_container_proxy_values(
    env_name: str, url: str, configured_url: str = ""
) -> dict[str, str]:
    """
    /**
     * @brief Resolve endpoint/proxy env overrides for one configured endpoint URL.
     * @param env_name Endpoint env var name (`EMBED_BASE_URL` or `CLASSIFIER_BASE_URL`).
     * @param url Candidate endpoint URL to validate and map.
     * @param configured_url Endpoint URL defined in repo config for this endpoint.
     * @return Mapping of environment keys to resolved values.
     */
    """
    if not url.strip():
        return {}

    host, port = _parse_endpoint_host_port(url)
    configured_host_port: tuple[str, int] | None = None
    if configured_url.strip():
        configured_host_port = _parse_endpoint_host_port(configured_url)

    proxy_base_url = PROXY_BASE_URL_BY_ENV.get(env_name)
    proxy_target_env = PROXY_TARGET_ENV_BY_ENDPOINT.get(env_name)
    if proxy_base_url is None or proxy_target_env is None:
        fail(f"Unsupported endpoint variable: {env_name}")
    if host not in ALLOWED_LOCAL_HOSTS:
        if configured_host_port is None:
            fail(f"Non-local endpoint is blocked by policy: {url}")
        if (host, port) != configured_host_port:
            fail(
                "Non-local endpoint does not match configured policy value: "
                f"{url}"
            )

    upstream_host = "host.docker.internal" if host in ALLOWED_LOCAL_HOSTS else host
    return {
        env_name: proxy_base_url,
        proxy_target_env: f"{upstream_host}:{port}",
    }


def main() -> int:
    """
    /**
     * @brief Resolve and print endpoint overrides as KEY=VALUE lines.
     * @return Process exit code.
     */
    """
    if len(sys.argv) != 2:
        fail("Usage: resolve-container-endpoints.py <repo_root>")

    repo_root = Path(sys.argv[1])
    config_base_urls = load_config_base_urls(repo_root)

    for env_name, section_name in (
        ("EMBED_BASE_URL", "embeddings"),
        ("CLASSIFIER_BASE_URL", "classifier"),
    ):
        existing = os.environ.get(env_name, "").strip()
        if existing:
            for output_key, output_value in resolve_container_proxy_values(
                env_name, existing, config_base_urls.get(section_name, "")
            ).items():
                print(f"{output_key}={output_value}")
            continue

        resolved_values = resolve_container_proxy_values(
            env_name,
            config_base_urls.get(section_name, ""),
            config_base_urls.get(section_name, ""),
        )
        for output_key, output_value in resolved_values.items():
            print(f"{output_key}={output_value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
