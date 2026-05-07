#!/usr/bin/env bash
#
# /**
#  * @file watch-repo.sh
#  * @brief Run CodeBrain watch-mode ingestion for a target repository.
#  *
#  * Wraps the indexer container invocation and appends `--watch` unless the
#  * caller already supplied it explicitly.
#  */

set -euo pipefail

show_help() {
  cat <<'EOF'
Usage:
  scripts/watch-repo.sh [REPO_PATH] [INGEST_ARGS...]

Examples:
  scripts/watch-repo.sh
  scripts/watch-repo.sh /absolute/path/to/repo
  scripts/watch-repo.sh ../other-repo --force --no-classify

Notes:
  - REPO_PATH defaults to the current working directory.
  - `--watch` is added automatically if you do not pass it yourself.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  show_help
  exit 0
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

forward_args=("$@")
has_watch_flag=false
for arg in "${forward_args[@]}"; do
  if [[ "$arg" == "--watch" ]]; then
    has_watch_flag=true
    break
  fi
done

if [[ "$has_watch_flag" == false ]]; then
  forward_args+=("--watch")
fi

exec "$script_dir/index-repo.sh" "${forward_args[@]}"
