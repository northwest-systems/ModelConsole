FROM ubuntu:24.04

# DEBIAN_FRONTEND: apt が対話入力で停止しないようにする。
# PYTHONDONTWRITEBYTECODE: コンテナ内に __pycache__ を増やさない。
# PYTHONUNBUFFERED: server/tui のログを即時出力する。
# MCON_DATA_DIR: config, runtime, vault, audit を置く永続 data volume。
# MCON_VENV: mcon をインストールする専用 Python 仮想環境。
# UV_PYTHON_INSTALL_DIR: uv managed Python の配置先。
# UV_CACHE_DIR: uv の一時キャッシュ。セットアップ時だけ使い、永続化しない。
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MCON_DATA_DIR=/data \
    MCON_VENV=/opt/mcon-venv \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    UV_CACHE_DIR=/tmp/uv-cache

# 基本ライブラリのインストール
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# uv のインストール
RUN curl -LsSf https://astral.sh/uv/install.sh | sh

# PATH を通す
ENV PATH="/usr/local/bin:/root/.local/bin:$PATH"

# Node.js 26 のインストール
RUN curl -fsSL https://deb.nodesource.com/setup_26.x | bash - \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# claude code のインストール
RUN npm install -g @anthropic-ai/claude-code # claude code のインストール
RUN npm install -g @openai/codex # Codex のインストール
RUN npm install -g @github/copilot # Copilot CLI のインストール

WORKDIR /app

# 5. 実行に必要な project metadata と package sources だけをコピーする
COPY pyproject.toml readme.md ./
COPY packages ./packages
COPY bin ./bin

# source checkout から micro package を直接 import する。
ENV PYTHONPATH="/app/packages/mcon/src:/app/packages/cli/src:/app/packages/tui/src:/app/packages/server/src:/app/packages/system/src:/app/packages/auth/src:/app/packages/audit/src:/app/packages/config/src:/app/packages/runtime/src:/app/packages/doctor/src:/app/packages/middleware/src:/app/packages/adapter/src"

# 6. uv managed Python で mcon コマンドを専用 venv にインストールし、標準 PATH 上に配置する
# /root/.local/bin に依存せず、docker compose exec mcon mcon ... でも直接実行できる。
RUN uv python install 3.12 \
    && uv venv --python 3.12 "$MCON_VENV" \
    && uv pip install --python "$MCON_VENV/bin/python" --editable . \
    && chmod +x /app/bin/mcon \
    && ln -sf /app/bin/mcon /usr/local/bin/mcon

# HOME: OAuth/CLI 認証ファイルを /data 配下へ寄せ、コンテナ再作成で失わないようにする。
ENV HOME=/data/home

# data と uv cache は container 内で保持する
RUN mkdir -p "$HOME" /data /tmp/uv-cache
VOLUME ["/data"]

# mcon の serve をそのまま起動する
CMD ["sh", "-lc", "mkdir -p \"$HOME\" && exec mcon serve"]
