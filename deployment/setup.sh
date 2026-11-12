#!/usr/bin/env bash
# One-command local setup: writes .env from .env.docker.example, fills its secrets, builds the two
# images and starts the stack. Safe to re-run: existing values are kept.
#
# The same by hand, from the repo root (docs/setup.md explains each step):
#   docker compose --env-file .env -f deployment/compose.yaml --profile build build host
#   docker compose --env-file .env -f deployment/compose.yaml build engine
#   docker compose --env-file .env -f deployment/compose.yaml up -d --wait
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$ROOT/.env"
compose=(docker compose --env-file "$ENV_FILE" -f "$ROOT/deployment/compose.yaml")

docker info >/dev/null 2>&1 || { echo "error: Docker isn't running." >&2; exit 1; }

# .env from the template; keys the template gained since are appended.
touch "$ENV_FILE"
while IFS= read -r line; do
  [[ "$line" == \#* || -z "$line" ]] && continue
  grep -q "^${line%%=*}=" "$ENV_FILE" || printf '%s\n' "$line" >>"$ENV_FILE"
done <"$ROOT/.env.docker.example"

env_get() { grep "^${1}=" "$ENV_FILE" | head -n1 | cut -d= -f2- || true; }
env_set() {
  awk -v k="$1" -v v="$2" 'BEGIN{FS=OFS="="} $1==k{$0=k"="v} {print}' "$ENV_FILE" >"$ENV_FILE.tmp"
  mv "$ENV_FILE.tmp" "$ENV_FILE"
}
fill() { [[ -n "$(env_get "$1")" ]] || env_set "$1" "$2"; }

# Secrets, and any model key this shell already has.
fill POSTGRES_PASSWORD "$(openssl rand -hex 16)"
fill OMNIGENT_PROXY_SECRET "$(openssl rand -hex 32)"
fill OMNIGENT_VAULT_KEY "$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '=')"
for key in ANTHROPIC_API_KEY OPENROUTER_API_KEY LITELLM_BASE_URL LITELLM_API_KEY; do
  [[ -z "${!key:-}" ]] || fill "$key" "${!key}"
done
[[ -n "$(env_get ANTHROPIC_API_KEY)$(env_get LITELLM_BASE_URL)" ]] ||
  echo "warning: set ANTHROPIC_API_KEY (or LITELLM_BASE_URL + LITELLM_API_KEY) in .env for the engine." >&2

# The host image first (the "build" profile keeps it out of `up`; the engine starts hosts
# itself), then the engine, then the stack.
"${compose[@]}" --profile build build host
"${compose[@]}" build engine
"${compose[@]}" up -d --wait --wait-timeout 300
echo "Superchat is running: http://127.0.0.1:$(env_get SUPERCHAT_WEB_PORT | grep . || echo 5173)"
