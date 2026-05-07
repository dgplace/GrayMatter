#!/usr/bin/env bash
#
# /**
#  * @file build.sh
#  * @brief Rebuild the CodeBrain Docker services and restart the running stack.
#  */

set -euo pipefail

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  cat <<'EOF'
Usage:
  scripts/build.sh

Rebuilds the Compose services used by CodeBrain, including the
indexer image consumed by `scripts/index-repo.sh` and
`scripts/watch-repo.sh`, then recreates the long-running containers.
EOF
  exit 0
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"

docker compose \
  -f "$repo_root/docker/docker-compose.yml" \
  --profile indexer \
  --profile tools \
  build

exec docker compose \
  -f "$repo_root/docker/docker-compose.yml" \
  --profile indexer \
  --profile tools \
  up -d --force-recreate postgres mcp
