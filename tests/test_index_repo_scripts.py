"""
@file tests/test_index_repo_scripts.py
@brief Regression tests for index-repo helper script orchestration.
"""

from pathlib import Path


def _script_source(script_name: str) -> str:
    """@brief Load an index helper script by filename.

    @param script_name Name of the script in the repository scripts directory.
    @return Script source text.
    """
    script_path = Path(__file__).resolve().parents[1] / "scripts" / script_name
    return script_path.read_text(encoding="utf-8")


def test_unix_remote_database_run_starts_only_model_proxies_before_no_deps() -> None:
    """@brief Verify remote DB shell runs skip Compose dependency startup."""
    source = _script_source("index-repo.sh")

    assert 'up -d embed_proxy classifier_proxy' in source
    assert "docker_compose_indexer+=(--no-deps)" in source
    assert 'docker_compose_indexer+=(-e "DATABASE_URL=$database_url")' in source


def test_windows_remote_database_run_starts_only_model_proxies_before_no_deps() -> None:
    """@brief Verify remote DB batch runs skip Compose dependency startup."""
    source = _script_source("index-repo.bat")

    assert 'up -d embed_proxy classifier_proxy' in source
    assert 'set "deps_arg=--no-deps"' in source
    assert 'run --rm %deps_arg% %db_env_arg%' in source
