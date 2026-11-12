#!/usr/bin/env bash
# Stop the Docker stack and delete all its data: the Postgres volume, host state under
# ./data, and stopped containers. Asks for confirmation first. Pass -y / --yes to skip the prompt.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
files=(-f "$ROOT/deployment/compose.yaml")
compose=(docker compose "${files[@]}")
if [[ -f "$ROOT/.env" ]]; then
  compose=(docker compose --env-file "$ROOT/.env" "${files[@]}")
fi

assume_yes=0
for arg in "$@"; do
  if [[ "$arg" == "-y" || "$arg" == "--yes" ]]; then
    assume_yes=1
  fi
done

if [[ "$assume_yes" -ne 1 ]]; then
  echo "This deletes the Postgres database and all host data for this stack."
  echo "That includes every account, conversation, and file created in this local Superchat."
  read -r -p "Type 'delete' to continue: " confirm
  if [[ "$confirm" != "delete" ]]; then
    echo "Cancelled. Nothing was deleted."
    exit 1
  fi
fi

"${compose[@]}" down -v
if [[ -d "$ROOT/data" ]]; then
  rm -rf "$ROOT/data"
fi
echo "Superchat's containers, volumes, and ./data are gone. Run ./deployment/setup.sh to start fresh."
