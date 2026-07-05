FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS python-base
WORKDIR /app
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ARG NODE_VERSION=22.16.0
ENV PYTHONPATH=/app/packages/mcon/src \
    UV_CACHE_DIR=/tmp/uv-cache \
    UV_NO_PROGRESS=1 \
    CODEX_HOME=/mcon/codex-home \
    MCON_WORKSPACE=/workspace \
    MCON_SESSION_ROOT=/mcon/session-fs \
    NVM_DIR=/usr/local/nvm \
    NODE_VERSION=${NODE_VERSION} \
    PATH=/usr/local/nvm/versions/node/v${NODE_VERSION}/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
RUN apt-get update \
    && apt-get install -y --no-install-recommends bash bubblewrap ca-certificates curl gawk git \
    && rm -rf /var/lib/apt/lists/*
RUN mkdir -p "$CODEX_HOME" "$MCON_SESSION_ROOT"
RUN mkdir -p "$NVM_DIR" \
    && curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash \
    && source "$NVM_DIR/nvm.sh" \
    && nvm install "$NODE_VERSION" \
    && nvm alias default "$NODE_VERSION" \
    && node --version \
    && npm --version
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts
RUN curl -fsSL https://chatgpt.com/codex/install.sh \
    | CODEX_NON_INTERACTIVE=1 CODEX_INSTALL_DIR=/usr/local/bin sh
COPY pyproject.toml ./
COPY packages ./packages
COPY configs ./configs

FROM python-base AS python-test
COPY tests ./tests
RUN uv run --no-project python -m unittest discover -s tests

FROM python-test AS test

FROM python-base AS runtime
EXPOSE 8765
CMD ["uv", "run", "--no-project", "python", "-m", "mcon", "serve", "--host", "0.0.0.0", "--port", "8765"]
