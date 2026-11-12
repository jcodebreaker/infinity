#!/usr/bin/env bash
set -uo pipefail
export HOME="${HOME:-/home/superchat}"
AGENT_HOME="$HOME"
mkdir -p "$AGENT_HOME" "$AGENT_HOME/.local/bin" "$AGENT_HOME/.config" /tmp/superchat
# The workspace layout delegated work lands in (docs/adr/0007): what the person should see, Goal
# folders are created per Goal, what they hand over, and the browser's downloads.
mkdir -p "$AGENT_HOME/workspace/your_files" "$AGENT_HOME/workspace/goals" \
  "$AGENT_HOME/workspace/user/media_library" "$AGENT_HOME/workspace/Downloads"
# Login shells re-apply ~/.local/bin from /etc/profile.d/superchat-local-bin.sh.
export PATH="$AGENT_HOME/.local/bin:/usr/local/bin:$PATH"
export NPM_CONFIG_PREFIX="$AGENT_HOME/.local"
export PIP_USER=1
cd "$AGENT_HOME"

# This script is PID 1 and only keeps the container alive: the runner and every tool arrive
# through `docker exec`. Without a handler, PID 1 ignores SIGTERM and `docker stop` waits its
# full grace period, so every stop, sleep and host switch took ten seconds. Install the
# handler before any child starts so a stop during startup is honoured too.
shutdown() {
  trap - TERM INT
  kill -TERM -- -1 2>/dev/null || true
  exit 0
}
trap shutdown TERM INT

# The warm deck browser (docs/adr/0026): one headless Chromium the deck tools attach to instead
# of each launching their own. It restarts its Chromium itself; this loop restarts it.
( while true; do superchat-deck-export --serve; sleep 2; done ) &

# `wait` on a background child returns as soon as a trapped signal arrives.
sleep infinity &
IDLE_PID=$!
wait "$IDLE_PID"
