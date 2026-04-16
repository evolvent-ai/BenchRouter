FROM python:3.12.11-slim-bookworm

ARG TOOLATHLON_REPO=https://github.com/hkust-nlp/Toolathlon
ARG TOOLATHLON_REF=0ae75188ad70a12ddf460e0b30ea902f9113b517

ENV DEBIAN_FRONTEND=noninteractive
ENV TOOLATHLON_HOME=/opt/toolathlon
ENV PATH="/root/.local/bin:${PATH}"
ENV UV_HTTP_RETRIES=5
ENV NPM_CONFIG_FETCH_RETRIES=5
ENV NPM_CONFIG_FETCH_RETRY_MINTIMEOUT=20000
ENV NPM_CONFIG_FETCH_RETRY_MAXTIMEOUT=120000

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN set -eux; \
    apt_install() { \
      local attempt; \
      for attempt in 1 2 3 4 5; do \
        apt-get update && apt-get install -y --no-install-recommends "$@" && return 0; \
        echo "apt-get failed on attempt ${attempt}, retrying..." >&2; \
        rm -rf /var/lib/apt/lists/*; \
        sleep $((attempt * 5)); \
      done; \
      return 1; \
    }; \
    apt_install \
      ca-certificates \
      curl \
      wget \
      git \
      gnupg \
      rsync \
      unzip \
      fuse-overlayfs \
      iptables \
      iproute2; \
    rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | sh

RUN set -eux; \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash -; \
    apt-get update; \
    apt-get install -y --no-install-recommends nodejs; \
    rm -rf /var/lib/apt/lists/*; \
    npm --version; \
    node --version

RUN set -eux; \
    rm -f /etc/apt/sources.list.d/nodesource.list; \
    install -m 0755 -d /etc/apt/keyrings; \
    curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg; \
    chmod a+r /etc/apt/keyrings/docker.gpg; \
    arch="$(dpkg --print-architecture)"; \
    codename="$(. /etc/os-release && echo "$VERSION_CODENAME")"; \
    echo "deb [arch=${arch} signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian ${codename} stable" \
      > /etc/apt/sources.list.d/docker.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends docker-ce docker-ce-cli containerd.io; \
    rm -rf /var/lib/apt/lists/*; \
    docker --version; \
    dockerd --version; \
    containerd --version

RUN set -eux; \
    rm -rf "${TOOLATHLON_HOME}"; \
    for attempt in 1 2 3 4 5; do \
      git clone "${TOOLATHLON_REPO}" "${TOOLATHLON_HOME}" && break; \
      echo "git clone failed on attempt ${attempt}, retrying..." >&2; \
      rm -rf "${TOOLATHLON_HOME}"; \
      sleep $((attempt * 5)); \
    done; \
    git -C "${TOOLATHLON_HOME}" checkout "${TOOLATHLON_REF}"

WORKDIR ${TOOLATHLON_HOME}

RUN set -eux; \
    uv sync --frozen

RUN set -eux; \
    npm install

RUN set -eux; \
    uv tool install git+https://github.com/lockon-n/mcp-scholarly@82a6ca268ae0d2e10664be396e1a0ea7aba23229

# Install Playwright browser binaries (required by playwright_with_chunk MCP server)
RUN set -eux; \
    for attempt in 1 2 3 4 5; do \
      uv run playwright install --with-deps chromium && break; \
      echo "playwright install failed on attempt ${attempt}, retrying..." >&2; \
      sleep $((attempt * 10)); \
    done

# Clone local MCP server repos needed by local tasks (yahoo-finance, arxiv-latex)
RUN set -eux; \
    mkdir -p local_servers; \
    git clone https://github.com/lockon-n/yahoo-finance-mcp local_servers/yahoo-finance-mcp && \
    git -C local_servers/yahoo-finance-mcp checkout 469103ba1464486cb7b8bd2c1f6355f42ca64a5b && \
    uv --directory local_servers/yahoo-finance-mcp sync; \
    git clone https://github.com/takashiishida/arxiv-latex-mcp.git local_servers/arxiv-latex-mcp && \
    git -C local_servers/arxiv-latex-mcp checkout f8bd3b3b6d3d066fe29ba356023a0b3e8215da43 && \
    uv --directory local_servers/arxiv-latex-mcp sync

# Patch yahoo-finance MCP config: use the venv Python directly instead of
# "uv run ..." to avoid slow uv startup that causes 60s MCP timeouts.
# Also bump client_session_timeout_seconds 60 → 120 as safety margin.
COPY patch_yahoo_mcp.py /tmp/patch_yahoo_mcp.py
RUN python3 /tmp/patch_yahoo_mcp.py configs/mcp_servers/yahoo-finance.yaml && \
    rm /tmp/patch_yahoo_mcp.py

# Patch run_single_decoupled.sh: add "local_servers" to FILES_TO_COPY so that
# yahoo-finance-mcp and arxiv-latex-mcp are available inside inner containers.
COPY patch_decoupled.py /tmp/patch_decoupled.py
RUN python3 /tmp/patch_decoupled.py scripts && \
    rm /tmp/patch_decoupled.py

# Patch model_provider.py: sanitize MCP tool schemas with type=null so that
# strict OpenAI-compatible APIs (gpt-5.x) don't reject them with 400 errors.
COPY patch_tool_schema.py /tmp/patch_tool_schema.py
RUN python3 /tmp/patch_tool_schema.py utils && \
    rm /tmp/patch_tool_schema.py

# Required by benchrouter SDK runner (runner.py imports yaml)
RUN pip install --no-cache-dir pyyaml

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD uv --version && node --version && docker --version && dockerd --version && containerd --version || exit 1

CMD ["/bin/bash"]
