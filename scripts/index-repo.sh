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
  scripts/index-repo.sh [REPO_PATH] [--database-url URL] [INGEST_ARGS...]

Examples:
  scripts/index-repo.sh
  scripts/index-repo.sh /absolute/path/to/repo --force
  scripts/index-repo.sh ../other-repo --force --no-classify
  scripts/index-repo.sh /absolute/path/to/repo --force --synthesize

Notes:
  - REPO_PATH defaults to the current working directory.
  - `--database-url` accepts either a full DSN or `HOST:PORT`.
  - `HOST:PORT` expands to `postgresql://codebrain:codebrain_local@HOST:PORT/codebrain`.
  - Hostnames are resolved to IPv4 and rewritten into the `DATABASE_URL` directly.
  - Remaining arguments are passed through to `python -m codebrain.ingest`.
  - Pass `--synthesize` to overlay narrative module_intents inline (single container run).
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

is_ipv4_literal() {
  local value="$1"
  [[ "$value" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]
}

target_repo="$PWD"
target_repo_set=false
database_url=""
ingest_args=()

while [[ $# -gt 0 ]]; do
  arg="$1"
  case "$arg" in
    --database-url=*)
      database_url="${arg#*=}"
      shift
      ;;
    --database-url)
      if [[ $# -lt 2 ]]; then
        echo "Missing value for --database-url" >&2
        exit 1
      fi
      database_url="$2"
      shift 2
      ;;
    *)
      if [[ "$target_repo_set" == false && "${arg:0:1}" != "-" ]]; then
        target_repo="$arg"
        target_repo_set=true
      else
        ingest_args+=("$arg")
      fi
      shift
      ;;
  esac
done

resolve_ipv4() {
  local host="$1"
  local resolved_ip=""
  resolved_ip="$(getent ahostsv4 "$host" 2>/dev/null | awk 'NR==1 {print $1}')"
  if [[ -n "$resolved_ip" ]]; then
    echo "$resolved_ip"
    return 0
  fi
  resolved_ip="$(ping -c 1 "$host" 2>/dev/null | sed -n 's/^PING[^(]*(\([^)]*\)).*/\1/p' | head -n 1)"
  if [[ -n "$resolved_ip" ]]; then
    echo "$resolved_ip"
    return 0
  fi
  return 1
}

if [[ -n "$database_url" && "$database_url" != *"://"* ]]; then
  if [[ "$database_url" =~ ^([^:]+):([0-9]+)$ ]]; then
    db_host="${BASH_REMATCH[1]}"
    db_port="${BASH_REMATCH[2]}"
    if ! is_ipv4_literal "$db_host"; then
      db_host="$(resolve_ipv4 "$db_host" || true)"
      if [[ -z "$db_host" ]]; then
        echo "Failed to resolve database host in --database-url: ${BASH_REMATCH[1]}" >&2
        exit 1
      fi
    fi
    database_url="postgresql://codebrain:codebrain_local@${db_host}:${db_port}/codebrain"
  else
    echo "Invalid --database-url value. Use full DSN or HOST:PORT." >&2
    exit 1
  fi
fi

target_repo="$(cd "$target_repo" && pwd)"
target_repo_name="$(basename "$target_repo")"

if [[ ! -d "$target_repo" ]]; then
  echo "Repository path does not exist: $target_repo" >&2
  exit 1
fi

has_repo_name_flag=false
for ((i = 0; i < ${#ingest_args[@]}; i++)); do
  arg="${ingest_args[$i]}"
  if [[ "$arg" == "--repo-name" || "$arg" == --repo-name=* ]]; then
    has_repo_name_flag=true
    break
  fi
done

repo_name_args=()
if [[ "$has_repo_name_flag" == false ]]; then
  repo_name_args=(--repo-name "$target_repo_name")
fi

translated_endpoints="$(python3 "$script_dir/resolve-container-endpoints.py" "$repo_root")"
while IFS= read -r line; do
  [[ -z "$line" ]] && continue
  export "$line"
done <<< "$translated_endpoints"

docker_compose_indexer=(
  docker compose
  -f "$compose_file"
  --profile indexer
  run --rm
)

if [[ -n "$database_url" ]]; then
  docker compose -f "$compose_file" --profile indexer up -d embed_proxy classifier_proxy
  docker_compose_indexer+=(--no-deps)
  docker_compose_indexer+=(-e "DATABASE_URL=$database_url")
fi

docker_compose_indexer+=(
  -v "$target_repo:/target"
  indexer
)

"${docker_compose_indexer[@]}" \
  python -m codebrain.ingest /target "${repo_name_args[@]}" "${ingest_args[@]}"
