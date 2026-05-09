#!/usr/bin/env bash
#
# /**
#  * @file index-repo.sh
#  * @brief Run CodeBrain ingestion for a target repository through the indexer container.
#  *
#  * This helper hides the docker compose profile and bind-mount convention
#  * behind one command. The first argument is the host repository path; any
#  * remaining arguments are forwarded to `python -m codebrain.ingest`.
#  */

set -euo pipefail

# /**
#  * @brief Print CLI usage text for the index helper script.
#  */
show_help() {
  cat <<'EOF_HELP'
Usage:
  scripts/index-repo.sh [REPO_PATH] [INGEST_ARGS...]

Examples:
  scripts/index-repo.sh
  scripts/index-repo.sh /absolute/path/to/repo --force
  scripts/index-repo.sh ../other-repo --force --no-classify
  scripts/index-repo.sh /absolute/path/to/repo --force --synthesize

Notes:
  - REPO_PATH defaults to the current working directory.
  - Remaining arguments are passed through to `python -m codebrain.ingest`.
  - Pass `--synthesize` to run module synthesis after ingestion completes.
  - The target repo is mounted at /target inside the container.
  - The host folder basename is passed via `--repo-name` unless you override it.
EOF_HELP
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  show_help
  exit 0
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
compose_file="$repo_root/docker/docker-compose.yml"

target_repo="${1:-$PWD}"
if [[ $# -gt 0 && "${1:0:1}" != "-" ]]; then
  shift
fi

target_repo="$(cd "$target_repo" && pwd)"
target_repo_name="$(basename "$target_repo")"

if [[ ! -d "$target_repo" ]]; then
  echo "Repository path does not exist: $target_repo" >&2
  exit 1
fi

synthesize_modules=false
ingest_args=()
for arg in "$@"; do
  if [[ "$arg" == "--synthesize" ]]; then
    synthesize_modules=true
    continue
  fi
  ingest_args+=("$arg")
done

has_repo_name_flag=false
effective_repo_name="$target_repo_name"
for ((i = 0; i < ${#ingest_args[@]}; i++)); do
  arg="${ingest_args[$i]}"
  if [[ "$arg" == "--repo-name" ]]; then
    has_repo_name_flag=true
    if ((i + 1 >= ${#ingest_args[@]})); then
      echo "Missing value for --repo-name" >&2
      exit 1
    fi
    effective_repo_name="${ingest_args[$((i + 1))]}"
    continue
  fi
  if [[ "$arg" == --repo-name=* ]]; then
    has_repo_name_flag=true
    effective_repo_name="${arg#--repo-name=}"
  fi
done

repo_name_args=()
if [[ "$has_repo_name_flag" == false ]]; then
  repo_name_args=(--repo-name "$target_repo_name")
fi

# Translate the toml's embedder/classifier base_url so the container can reach
# the host's services. 127.0.0.1 and localhost are rewritten to
# host.docker.internal; anything else (LAN IP, resolvable hostname) passes
# through unchanged so the user's toml stays authoritative.
# /**
#  * @brief Emit container-safe endpoint environment overrides.
#  *
#  * Reads codebrain.toml candidate files and prints KEY=VALUE pairs for
#  * EMBED_BASE_URL and CLASSIFIER_BASE_URL when translation is needed.
#  */
translate_for_container() {
  python3 - "$repo_root" <<'PY'
import os
import sys
from pathlib import Path
from urllib.parse import urlparse, urlunparse

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

repo_root = Path(sys.argv[1])
candidate_paths = [repo_root / ".env" / "codebrain.toml", repo_root / "codebrain.toml"]
cfg: dict = {}
for path in candidate_paths:
    if path.is_file():
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        cfg = {**cfg, **data} if cfg else data

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def translate(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.hostname in LOCAL_HOSTS:
        port = f":{parsed.port}" if parsed.port else ""
        return urlunparse(parsed._replace(netloc=f"host.docker.internal{port}"))
    return url


for env_name, section in (("EMBED_BASE_URL", "embeddings"), ("CLASSIFIER_BASE_URL", "classifier")):
    if os.environ.get(env_name):
        # User-provided override wins; emit it back so compose picks it up.
        print(f"{env_name}={os.environ[env_name]}")
        continue
    raw = cfg.get(section, {}).get("base_url", "")
    translated = translate(raw)
    if translated:
        print(f"{env_name}={translated}")
PY
}

while IFS= read -r line; do
  [[ -z "$line" ]] && continue
  export "$line"
done < <(translate_for_container)

docker_compose_indexer=(
  docker compose
  -f "$compose_file"
  --profile indexer
  run --rm
  -v "$target_repo:/target"
  indexer
)

"${docker_compose_indexer[@]}" \
  python -m codebrain.ingest /target "${repo_name_args[@]}" "${ingest_args[@]}"

if [[ "$synthesize_modules" == true ]]; then
  "${docker_compose_indexer[@]}" \
    python -m codebrain.synthesize_modules --repo "$effective_repo_name"
fi
