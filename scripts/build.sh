#!/usr/bin/env bash
#
# /**
#  * @file build.sh
#  * @brief Rebuild the CodeBrain Docker images and recreate the affected containers.
#  *
#  * Default mode rebuilds every image and recreates the `mcp` service plus its
#  * published-port sidecars. Pass --reset to also recreate
#  * `postgres` (the named `postgres_data` volume is preserved).
#  * Pass --wipe to drop the named volume before recreating, which forces
#  * `schema.sql` to be re-applied; this destroys all indexed data.
#  */

set -euo pipefail

POSTGRES_VOLUME="codebrain_postgres_data"

mode="mcp"
assume_yes=false

show_help() {
  cat <<EOF
Usage:
  scripts/build.sh             Rebuild images and recreate \`mcp\` and frontdoor sidecars.
  scripts/build.sh --reset     Rebuild images and recreate \`postgres\`, \`mcp\`, and frontdoor sidecars.
                               Indexed data is preserved.
  scripts/build.sh --wipe      Drop the \`${POSTGRES_VOLUME}\` named volume,
                               then rebuild images and recreate \`postgres\`,
                               \`mcp\`, and frontdoor sidecars. Schema.sql is
                               re-applied on first init. DESTROYS ALL INDEXED
                               DATA. Prompts for confirmation unless -y/--yes
                               is also passed.
  scripts/build.sh -h|--help   Show this help.

Flags:
  -y, --yes                    Skip the --wipe confirmation prompt.

All modes build all images (including the indexer image consumed by
\`scripts/index-repo.sh\` and \`scripts/watch-repo.sh\`).
EOF
}

# Two-argument parse: mode (positional) + optional -y/--yes anywhere.
for arg in "$@"; do
  case "$arg" in
    -y|--yes)
      assume_yes=true
      ;;
    ""|--mcp|--reset|--wipe|-h|--help)
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      show_help
      exit 2
      ;;
  esac
done

case "${1:-}" in
  ""|--mcp|-y|--yes)
    mode="mcp"
    ;;
  --reset)
    mode="reset"
    ;;
  --wipe)
    mode="wipe"
    ;;
  -h|--help)
    show_help
    exit 0
    ;;
esac

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
compose_file="$repo_root/docker/docker-compose.yml"

translated_endpoints="$(python3 "$script_dir/resolve-container-endpoints.py" "$repo_root")"
while IFS= read -r line; do
  [[ -z "$line" ]] && continue
  export "$line"
done <<< "$translated_endpoints"

if [[ "$mode" == "wipe" ]]; then
  if [[ "$assume_yes" != true ]]; then
    cat <<EOF
About to DESTROY all indexed data by dropping the \`${POSTGRES_VOLUME}\`
named volume. Every repository indexed in this CodeBrain instance will
be lost and must be re-ingested.

Pass --yes to skip this prompt.
EOF
    read -r -p "Type 'wipe' to confirm: " confirmation
    if [[ "$confirmation" != "wipe" ]]; then
      echo "Aborted." >&2
      exit 1
    fi
  fi

  echo "Stopping postgres + mcp services..."
  docker compose \
    -f "$compose_file" \
    --profile indexer \
    --profile tools \
    rm -sf postgres embed_proxy classifier_proxy postgres_frontdoor mcp mcp_frontdoor

  echo "Removing volume ${POSTGRES_VOLUME}..."
  if docker volume inspect "$POSTGRES_VOLUME" >/dev/null 2>&1; then
    docker volume rm "$POSTGRES_VOLUME"
  else
    echo "Volume ${POSTGRES_VOLUME} did not exist; continuing."
  fi
fi

docker compose \
  -f "$compose_file" \
  --profile indexer \
  --profile tools \
  build

case "$mode" in
  reset|wipe)
    recreate_targets=(postgres embed_proxy classifier_proxy postgres_frontdoor mcp mcp_frontdoor)
    ;;
  *)
    recreate_targets=(embed_proxy classifier_proxy postgres_frontdoor mcp mcp_frontdoor)
    ;;
esac

exec docker compose \
  -f "$compose_file" \
  --profile indexer \
  --profile tools \
  up -d --force-recreate "${recreate_targets[@]}"
