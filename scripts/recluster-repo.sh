#!/usr/bin/env bash
#
# /**
#  * @file recluster-repo.sh
#  * @brief Rebuild clusters/logical modules for an indexed repo without file re-ingestion.
#  *
#  * The first argument is a host repository path used only to derive the
#  * default `--repo-name` (basename). Remaining arguments are forwarded to
#  * `python -m codebrain.recluster`.
#  */

set -euo pipefail

# /**
#  * @brief Print CLI usage text for the recluster helper script.
#  */
show_help() {
  cat <<'EOF_HELP'
Usage:
  scripts/recluster-repo.sh [REPO_PATH] [RECLUSTER_ARGS...]

Examples:
  scripts/recluster-repo.sh
  scripts/recluster-repo.sh /absolute/path/to/repo
  scripts/recluster-repo.sh /absolute/path/to/repo --resolution-multiplier 2.0
  scripts/recluster-repo.sh /absolute/path/to/repo --resolution 2.0 --min-files 2

Notes:
  - REPO_PATH defaults to the current working directory.
  - Remaining arguments are passed through to `python -m codebrain.recluster`.
  - If no resolution args are provided, the command defaults to 2x configured resolution.
  - This command does NOT re-index files; it only rebuilds clusters and logical modules.
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

recluster_args=("$@")

has_repo_name_flag=false
has_resolution_flag=false
for ((i = 0; i < ${#recluster_args[@]}; i++)); do
  arg="${recluster_args[$i]}"
  if [[ "$arg" == "--repo-name" || "$arg" == --repo-name=* ]]; then
    has_repo_name_flag=true
  fi
  if [[ "$arg" == "--resolution" || "$arg" == --resolution=* || "$arg" == "--resolution-multiplier" || "$arg" == --resolution-multiplier=* ]]; then
    has_resolution_flag=true
  fi
done

repo_name_args=()
if [[ "$has_repo_name_flag" == false ]]; then
  repo_name_args=(--repo-name "$target_repo_name")
fi

resolution_args=()
if [[ "$has_resolution_flag" == false ]]; then
  resolution_args=(--resolution-multiplier 2.0)
fi

translated_endpoints="$(python3 "$script_dir/resolve-container-endpoints.py" "$repo_root")"
while IFS= read -r line; do
  [[ -z "$line" ]] && continue
  export "$line"
done <<< "$translated_endpoints"

docker compose -f "$compose_file" --profile indexer run --rm \
  indexer python -m codebrain.recluster \
  "${repo_name_args[@]}" \
  "${resolution_args[@]}" \
  "${recluster_args[@]}"
