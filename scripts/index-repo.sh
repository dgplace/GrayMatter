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

show_help() {
  cat <<'EOF'
Usage:
  scripts/index-repo.sh [REPO_PATH] [INGEST_ARGS...]

Examples:
  scripts/index-repo.sh
  scripts/index-repo.sh /absolute/path/to/repo --force
  scripts/index-repo.sh ../other-repo --force --no-classify

Notes:
  - REPO_PATH defaults to the current working directory.
  - Remaining arguments are passed through to `python -m codebrain.ingest`.
  - The target repo is mounted at /target inside the container.
EOF
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

if [[ ! -d "$target_repo" ]]; then
  echo "Repository path does not exist: $target_repo" >&2
  exit 1
fi

exec docker compose \
  -f "$compose_file" \
  --profile indexer \
  run --rm \
  -v "$target_repo:/target" \
  indexer \
  python -m codebrain.ingest /target "$@"
