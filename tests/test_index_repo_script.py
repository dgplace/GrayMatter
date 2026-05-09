"""
@file tests/test_index_repo_script.py
@brief Regression tests for scripts/index-repo.sh helper behavior.
"""

import json
import os
import subprocess
from pathlib import Path


def _write_fake_docker(bin_dir: Path, log_file: Path) -> None:
    """@brief Create a fake docker executable that logs argv payloads.

    @param bin_dir Directory that will hold the fake executable.
    @param log_file JSONL log file path used by the fake executable.
    """
    fake = bin_dir / "docker"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "python3 - \"$DOCKER_LOG_PATH\" \"$@\" <<'PY'\n"
        "import json\n"
        "import sys\n"
        "with open(sys.argv[1], 'a', encoding='utf-8') as fh:\n"
        "    fh.write(json.dumps(sys.argv[2:]) + '\\n')\n"
        "PY\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)


def _run_index_script(tmp_path: Path, script_args: list[str]) -> list[list[str]]:
    """@brief Execute index-repo.sh with a fake docker binary and return logged calls.

    @param tmp_path Pytest temporary directory for fake tooling and target repo.
    @param script_args Arguments forwarded to scripts/index-repo.sh.
    @return Parsed docker argv entries in call order.
    """
    target_repo = tmp_path / "target-repo"
    target_repo.mkdir()

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log_file = tmp_path / "docker-calls.jsonl"
    _write_fake_docker(fake_bin, log_file)

    script_path = Path(__file__).resolve().parents[1] / "scripts" / "index-repo.sh"
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["DOCKER_LOG_PATH"] = str(log_file)

    subprocess.run(
        [str(script_path), str(target_repo), *script_args],
        check=True,
        env=env,
        text=True,
    )

    calls: list[list[str]] = []
    for line in log_file.read_text(encoding="utf-8").splitlines():
        calls.append(json.loads(line))
    return calls


def test_index_repo_synthesize_runs_ingest_then_module_synthesis(tmp_path: Path) -> None:
    """@brief Verify --synthesize triggers a second indexer run for module synthesis."""
    calls = _run_index_script(tmp_path, ["--force", "--synthesize"])
    target_repo = tmp_path / "target-repo"

    assert len(calls) == 2

    ingest = calls[0]
    assert ingest[:2] == ["compose", "-f"]
    assert ingest[-6:] == ["-m", "codebrain.ingest", "/target", "--repo-name", "target-repo", "--force"]
    assert "--synthesize" not in ingest
    assert f"{target_repo}:/target" in ingest

    synthesize = calls[1]
    assert synthesize[:2] == ["compose", "-f"]
    assert synthesize[-4:] == ["-m", "codebrain.synthesize_modules", "--repo", "target-repo"]


def test_index_repo_synthesize_uses_repo_name_override_for_module_pass(tmp_path: Path) -> None:
    """@brief Verify synthesis uses an explicit --repo-name override value."""
    calls = _run_index_script(tmp_path, ["--repo-name", "custom-repo", "--synthesize"])

    assert len(calls) == 2
    ingest = calls[0]
    synthesize = calls[1]

    assert ["--repo-name", "custom-repo"] == ingest[-2:]
    assert synthesize[-4:] == ["-m", "codebrain.synthesize_modules", "--repo", "custom-repo"]
