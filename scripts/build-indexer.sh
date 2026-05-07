#!/usr/bin/env bash
#
# /**
#  * @file build-indexer.sh
#  * @brief Rebuild the CodeBrain indexer image with all parser and SCIP tooling.
#  */

set -euo pipefail

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  cat <<'EOF'
Usage:
  scripts/build-indexer.sh

Rebuilds the Docker image used by `scripts/index-repo.sh` and
`scripts/watch-repo.sh`.
EOF
  exit 0
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"

exec docker compose \
  -f "$repo_root/docker/docker-compose.yml" \
  --profile indexer \
  build indexer
