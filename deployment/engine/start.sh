#!/usr/bin/env bash
# Superchat's engine start: renders the engine config from the environment, then starts the
# server. Runs in the engine image (deployment/engine.Dockerfile), which bakes the agent bundles,
# the config templates and this script in at /opt/bundle.
#
# The template is ENGINE_CONFIG_TEMPLATE; unset, it is the LiteLLM one when LITELLM_BASE_URL is set
# and the Anthropic-key one otherwise. Its __NAME__ placeholders are literal values filled in here
# (URLs: the engine does not expand them); its ${NAME} references are secrets the engine expands
# itself. Every one must be set. Only the Anthropic template names a default model; people pick
# their own in Settings > Models.
set -euo pipefail

bundle=/opt/bundle
# Superchat's defaults. Each one can be overridden from the environment.
export ARTIFACT_DIR="${ARTIFACT_DIR:-/data/artifacts}"
export OMNIGENT_DATA_DIR="${OMNIGENT_DATA_DIR:-/data}"
export OMNIGENT_CONFIG_HOME="${OMNIGENT_CONFIG_HOME:-/data/config}"
export OMNIGENT_DISABLE_KEYRING="${OMNIGENT_DISABLE_KEYRING:-1}"
export OMNIGENT_BUILTIN_AGENT_DIRS="${OMNIGENT_BUILTIN_AGENT_DIRS:-$bundle/agents/superchat-claude:$bundle/agents/superchat-pi}"
export OMNIGENT_AUTH_PROVIDER="${OMNIGENT_AUTH_PROVIDER:-header}"
export OMNIGENT_AUTH_TENANT_HEADER="${OMNIGENT_AUTH_TENANT_HEADER:-X-Omnigent-Tenant}"
export OMNIGENT_SUPERCHAT_DEFAULT_AGENT="${OMNIGENT_SUPERCHAT_DEFAULT_AGENT:-superchat-claude}"
export OMNIGENT_HOST_PROVIDER="${OMNIGENT_HOST_PROVIDER:-docker}"
export OMNIGENT_ADMIN_LIST_PATH="${OMNIGENT_ADMIN_LIST_PATH:-/data/admins}"
export OMNIGENT_QUIET_MOMENT_MIN_GAP_SECONDS="${OMNIGENT_QUIET_MOMENT_MIN_GAP_SECONDS:-21600}"
export OMNIGENT_BROWSER_BACKEND="${OMNIGENT_BROWSER_BACKEND:-local}"
export OMNIGENT_PROACTIVE_PROVISION="${OMNIGENT_PROACTIVE_PROVISION:-1}"

: "${DATABASE_URL:?Set DATABASE_URL to the engine Postgres reference}"
: "${OMNIGENT_AUTH_HEADER_SECRET:?Set OMNIGENT_AUTH_HEADER_SECRET}"
: "${OMNIGENT_VAULT_KEY:?Set OMNIGENT_VAULT_KEY}"

mkdir -p "$OMNIGENT_DATA_DIR" "$OMNIGENT_CONFIG_HOME" "$ARTIFACT_DIR"
if [[ -n "${ENGINE_CONFIG_TEMPLATE:-}" ]]; then
  template="$ENGINE_CONFIG_TEMPLATE"
elif [[ -n "${LITELLM_BASE_URL:-}" ]]; then
  template="$bundle/engine/config.litellm.yaml.tmpl"
else
  template="$bundle/engine/config.yaml.tmpl"
fi
python - "$template" "$OMNIGENT_DATA_DIR/config.yaml" <<'PY'
import os
import re
import sys

source, target = sys.argv[1], sys.argv[2]
lines = open(source, encoding="utf-8").read().splitlines(keepends=True)
live = [line for line in lines if not line.lstrip().startswith("#")]
placeholders = {m for line in live for m in re.findall(r"__([A-Z][A-Z0-9_]*)__", line)}
references = {m for line in live for m in re.findall(r"\$\{([A-Z][A-Z0-9_]*)\}", line)}
missing = sorted(name for name in placeholders | references if not os.environ.get(name))
if missing:
    sys.exit(f"engine config {source}: set {', '.join(missing)}")


def value(match: re.Match[str]) -> str:
    # URLs are written without a trailing slash; the config appends paths to them.
    return os.environ[match.group(1)].strip().rstrip("/")


rendered = "".join(
    line if line.lstrip().startswith("#") else re.sub(r"__([A-Z][A-Z0-9_]*)__", value, line)
    for line in lines
)
with open(target, "w", encoding="utf-8") as handle:
    handle.write(rendered)
PY
export OMNIGENT_CONFIG="$OMNIGENT_DATA_DIR/config.yaml"

exec python /app/entrypoint.py
