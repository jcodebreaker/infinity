# syntax=docker/dockerfile:1.7
# Superchat engine: one container that is the API, the web and the published apps.
#
# Stages
#   web      builds web/app (the React app) with pnpm
#   agents   renders the agent bundles from engine/agents/templates
#   python   installs the engine package (engine/omnigent) into a venv
#   engine   what runs: python:3.12-slim + the venv + the web build + the rendered bundles
#
# Build from the repo root (deployment/build.sh does this with the right tags):
#   docker build -f deployment/engine.Dockerfile -t superchat/engine:local .
# docs/setup.md (Builds) explains what each stage is for.

ARG NODE_IMAGE=node:22-slim
ARG PYTHON_IMAGE=python:3.12-slim

# ── web: web/app → /src/web/app/dist ───────────────────────────────────────────
FROM ${NODE_IMAGE} AS web
ARG NPM_CONFIG_REGISTRY=https://registry.npmjs.org
ENV COREPACK_NPM_REGISTRY=${NPM_CONFIG_REGISTRY} \
    COREPACK_HOME=/usr/local/share/corepack \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    CI=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && corepack enable
# Company CA, optional (build secret `ca_bundle`, a PEM file). Without it this is a no-op.
RUN --mount=type=secret,id=ca_bundle,target=/usr/local/share/ca-certificates/ca_bundle.crt,required=false \
    if [ -s /usr/local/share/ca-certificates/ca_bundle.crt ]; then update-ca-certificates; fi
ENV NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt
WORKDIR /src
# The pnpm workspace is web/app and web/packages/*; the web app needs the packages it imports.
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml .npmrc tsconfig.base.json ./
COPY patches/ ./patches/
COPY web/packages/ ./web/packages/
COPY web/app/ ./web/app/
RUN pnpm install --frozen-lockfile --filter "@superchat/web..."
RUN pnpm --filter @superchat/web build

# ── agents: engine/agents/templates → /src/engine/agents/bundles ───────────────
# The image ships bundles rendered from the templates, never the committed copy (ADR 0019).
# render-agents.mjs reads the templates and the deck kit's layout menu and writes bundles/.
FROM ${NODE_IMAGE} AS agents
WORKDIR /src
COPY engine/agents/render-agents.mjs ./engine/agents/
COPY engine/agents/templates/ ./engine/agents/templates/
COPY engine/omnigent/superchat/decks/kit/menu.md ./engine/omnigent/superchat/decks/kit/
RUN node engine/agents/render-agents.mjs

# ── python: engine/omnigent → /opt/venv ────────────────────────────────────────
FROM ${PYTHON_IMAGE} AS python
ARG PYPI_INDEX_URL=https://pypi.org/simple
# CPU-only torch for the memory extra (txtai). PyPI's Linux torch carries ~5 GB of CUDA libraries
# the engine never uses. Empty keeps PyPI's.
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
# memory: the Memory service's index. postgres: the database driver. s3: the S3/R2 artifact store.
# otel: trace export, used only when OTEL_EXPORTER_OTLP_ENDPOINT is set (ADR 0041).
ARG ENGINE_EXTRAS=memory,postgres,s3,otel
ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN --mount=type=secret,id=ca_bundle,target=/usr/local/share/ca-certificates/ca_bundle.crt,required=false \
    if [ -s /usr/local/share/ca-certificates/ca_bundle.crt ]; then update-ca-certificates; fi
ENV PIP_CERT=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
WORKDIR /src
# The package, its sibling SDK (a hard dependency, installed from the same tree) and the
# attribution files the image redistributes.
COPY engine/pyproject.toml engine/setup.py engine/LICENSE engine/NOTICE ./
COPY engine/sdks/python-client/ ./sdks/python-client/
COPY engine/omnigent/ ./omnigent/
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:${PATH}
RUN if [ -n "${TORCH_INDEX_URL}" ]; then pip install --index-url "${TORCH_INDEX_URL}" torch; fi \
 && pip install --index-url "${PYPI_INDEX_URL}" ".[${ENGINE_EXTRAS}]" ./sdks/python-client \
 && ! grep -R --include='*.pth' -nE '/src(/|$)' /opt/venv/lib/python*/site-packages

# ── engine: the image that runs ────────────────────────────────────────────────
FROM ${PYTHON_IMAGE} AS engine
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/venv/bin:${PATH}
# curl for the health check; CA certificates for outbound HTTPS (model gateways, S3).
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN --mount=type=secret,id=ca_bundle,target=/usr/local/share/ca-certificates/ca_bundle.crt,required=false \
    if [ -s /usr/local/share/ca-certificates/ca_bundle.crt ]; then update-ca-certificates; fi
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
COPY --from=python /opt/venv /opt/venv
COPY --from=python /src/LICENSE /src/NOTICE /opt/engine/
# The server entrypoint: loads config, runs migrations, builds the app, serves.
COPY deployment/entrypoint.py /app/entrypoint.py
# The agent bundles the engine offers (rendered in the agents stage), the config templates and the
# start script that renders one of them from the environment, then runs the entrypoint.
COPY --from=agents /src/engine/agents/bundles/ /opt/bundle/agents/
COPY --chmod=755 deployment/engine/start.sh deployment/engine/config.yaml.tmpl \
     deployment/engine/config.litellm.yaml.tmpl deployment/engine/config.byok.yaml.tmpl /opt/bundle/engine/
# The web, served at "/" with an SPA fallback; /v1 and /health stay the API, /apps/<slug> the
# published mini apps.
COPY --from=web /src/web/app/dist/ /opt/web/
RUN sed -i 's/\r$//' /opt/bundle/engine/start.sh \
 && mkdir -p /data/artifacts
ENV OMNIGENT_WEB_UI_DIST=/opt/web \
    HOST=0.0.0.0 \
    PORT=8000 \
    ARTIFACT_DIR=/data/artifacts
WORKDIR /app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1
CMD ["/opt/bundle/engine/start.sh"]
