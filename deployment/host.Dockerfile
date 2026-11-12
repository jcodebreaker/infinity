# syntax=docker/dockerfile:1.7
# The host: the container each person gets. The runner runs here, next to the person's files,
# and dials the engine back. Headless: no desktop, no screen.
#
# Stages
#   chromium   Chromium alone, copied out of Playwright's image (deck and dashboard export, browser tools)
#   node       Node 22 from the official image (the claude and pi CLIs are npm packages)
#   venv       the engine package (its runner half is what runs here) installed on the same Ubuntu
#   host       ubuntu:24.04 + Python data stack + Chromium + pptx runtime + the two CLIs + Superchat helpers
#
# Build from the repo root (deployment/build.sh does this with the right tags):
#   docker build -f deployment/host.Dockerfile -t superchat/host:local .

ARG UBUNTU_IMAGE=ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55
ARG NODE_IMAGE=node:22-slim
ARG CHROMIUM_IMAGE=mcr.microsoft.com/playwright/python:v1.63.0-noble@sha256:72bd171a9ffc2b4b59532aaa6210e21014d07093120dc25528870c0b840da1f0

# ── chromium: only the browser, none of Playwright's other browsers ────────────
FROM ${CHROMIUM_IMAGE} AS chromium
RUN mkdir /out && cp -a /ms-playwright/chromium-[0-9]* /out/

# ── node: alias so the runtime can COPY a pinned Node out of it ────────────────
FROM ${NODE_IMAGE} AS node

# ── venv: engine/omnigent → /opt/venv, built on the runtime's own Ubuntu ───────
FROM ${UBUNTU_IMAGE} AS venv
ARG PYPI_INDEX_URL=https://pypi.org/simple
ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates python3 python3-venv python3-pip build-essential \
 && rm -rf /var/lib/apt/lists/*
RUN --mount=type=secret,id=ca_bundle,target=/usr/local/share/ca-certificates/ca_bundle.crt,required=false \
    if [ -s /usr/local/share/ca-certificates/ca_bundle.crt ]; then update-ca-certificates; fi
ENV PIP_CERT=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
WORKDIR /src
COPY engine/pyproject.toml engine/setup.py engine/LICENSE engine/NOTICE ./
COPY engine/sdks/python-client/ ./sdks/python-client/
COPY engine/omnigent/ ./omnigent/
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --index-url "${PYPI_INDEX_URL}" ".[otel]" ./sdks/python-client \
 && ! grep -R --include='*.pth' -nE '/src(/|$)' /opt/venv/lib/python*/site-packages

# ── host: the image that runs ──────────────────────────────────────────────
FROM ${UBUNTU_IMAGE} AS host
ARG PYPI_INDEX_URL=https://pypi.org/simple
# Pin the two CLIs here, each to the version its bundle (superchat-claude, superchat-pi) is verified against.
ARG CLAUDE_CODE_VERSION=2.1.302
ARG PI_VERSION=0.87.1
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH=/opt/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    IS_SANDBOX=1

# System packages:
#   python3 …           the Python the model's shell runs (the data stack below goes in here)
#   git tmux            workspaces and the terminal sessions the runner opens
#   procps lsof         process and port lookups the runner does
#   bubblewrap          the runner's default Linux sandbox for shell commands; it fails loud without it
#   imagemagick jq openssh-client patch   tools the model's shell uses
#   fonts-*             decks render with the same fonts as in the person's browser
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl \
      python3 python3-venv python3-pip python-is-python3 \
      git tmux procps lsof bubblewrap \
      imagemagick jq openssh-client patch \
      fonts-liberation fonts-dejavu-core fonts-noto-color-emoji \
 && rm -rf /var/lib/apt/lists/* \
 && rm -f /usr/lib/python3*/EXTERNALLY-MANAGED
RUN --mount=type=secret,id=ca_bundle,target=/usr/local/share/ca-certificates/ca_bundle.crt,required=false \
    if [ -s /usr/local/share/ca-certificates/ca_bundle.crt ]; then update-ca-certificates; fi
ENV NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt \
    PIP_CERT=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

# Node, copied from the official image (same glibc family, pinned by NODE_IMAGE).
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# The two harness CLIs the engine's bundles use (superchat-claude, superchat-pi).
RUN npm install -g --no-audit --no-fund \
      "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
      "@earendil-works/pi-coding-agent@${PI_VERSION}" \
 && npm cache clean --force

# Python's data stack and the pptx libraries, in the system Python the model's shell runs
# (that shell leaves /opt/venv out of its PATH). PyYAML is for the engine's host-config write.
# Agg keeps matplotlib headless.
RUN PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin python3 -m pip install --no-cache-dir --break-system-packages \
      --index-url "${PYPI_INDEX_URL}" \
      pandas==2.2.3 openpyxl==3.1.5 xlsxwriter==3.2.0 matplotlib==3.9.2 duckdb==1.1.3 \
      python-pptx==1.0.2 "markitdown[pptx]==0.1.8" pyyaml==6.0.2
ENV MPLBACKEND=Agg

# Headless Chromium; `chromium` on PATH is what the superchat-* helpers look up. Its system
# libraries come from Playwright's own list.
COPY --from=chromium /out/ /opt/chromium/
RUN set -eu; \
    ln -sf "$(ls -d /opt/chromium/chromium-*/chrome-linux*/chrome | head -n1)" /usr/local/bin/chromium; \
    npx -y "playwright@1.63.0" install-deps chromium; \
    rm -rf /var/lib/apt/lists/*; npm cache clean --force; \
    chromium --version

# The pptx runtime (PptxGenJS with its pinned fixes, react-icons, sharp), on NODE_PATH so
# `require('pptxgenjs')` works from any script the model writes.
COPY host/share/pptx/runtime/ /tmp/pptx-runtime/
RUN sh /tmp/pptx-runtime/install.sh /opt/superchat-pptx \
 && rm -rf /tmp/pptx-runtime && npm cache clean --force
ENV NODE_PATH=/opt/superchat-pptx/node_modules

# The runner. Login shells (`bash -lc`) reset PATH, so profile.d puts the venv back in front.
COPY --from=venv /opt/venv /opt/venv
RUN ln -sf /opt/venv/bin/omnigent /usr/local/bin/omnigent \
 && echo 'export PATH="/opt/venv/bin:${PATH}"' > /etc/profile.d/omnigent-venv.sh

# Superchat's helpers and the skill files the model's instructions point at.
COPY --chmod=755 host/bin/start.sh /usr/local/bin/superchat-host
COPY --chmod=755 host/bin/superchat-browser host/bin/superchat-page-browser \
     host/bin/superchat-deck-export host/bin/superchat-dashboard /usr/local/bin/
COPY --chmod=644 host/bin/superchat-local-bin.sh /etc/profile.d/superchat-local-bin.sh
COPY --chmod=644 host/share/deck-export/deck-capture-page.js \
     host/share/deck-export/vendor/dom-to-pptx/dom-to-pptx.bundle.js.gz /usr/local/share/superchat/deck-export/
COPY --chmod=644 host/share/deck-export/vendor/dom-to-pptx/LICENSE /usr/local/share/superchat/deck-export/dom-to-pptx.LICENSE
COPY --chmod=644 host/share/dashboard/template.html /usr/local/share/superchat/dashboard/template.html
COPY --chmod=644 engine/omnigent/superchat/decks/kit/vendor/chart.js/chart.umd.js /usr/local/share/superchat/dashboard/chart.umd.js
COPY --chmod=644 engine/omnigent/superchat/decks/kit/vendor/chart.js/LICENSE.md /usr/local/share/superchat/dashboard/chart.js.LICENSE.md
COPY host/share/pptx/skill/pptx-generator/ /usr/local/share/superchat/skills/pptx-generator/

# The host user is uid 1000 with its home at /home/superchat (a per-person volume at runtime).
# CRs are stripped so the helpers' shebangs work from a CRLF checkout.
RUN find /usr/local/share/superchat -type d -exec chmod 755 {} + \
 && chmod -R a+rX /usr/local/share/superchat /opt/superchat-pptx \
 && gzip -d /usr/local/share/superchat/deck-export/dom-to-pptx.bundle.js.gz \
 && sed -i 's/\r$//' /usr/local/bin/superchat-* /etc/profile.d/superchat-local-bin.sh \
 && if id -u ubuntu >/dev/null 2>&1; then userdel --remove ubuntu; fi \
 && groupadd --gid 1000 superchat \
 && useradd --uid 1000 --gid superchat --create-home --shell /bin/bash superchat
ENV HOME=/home/superchat
WORKDIR /home/superchat
USER 1000:1000
# PID 1 only keeps the container alive; the runner and every tool arrive through `docker exec`.
CMD ["/usr/local/bin/superchat-host"]
