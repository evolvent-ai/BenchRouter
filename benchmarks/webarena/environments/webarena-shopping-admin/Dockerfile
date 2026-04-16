FROM python:3.10-slim-bookworm

WORKDIR /app/webarena

# System deps for Playwright + Docker CE (combined to avoid duplicate apt-get update)
RUN apt-get update && apt-get install -y \
    wget curl gnupg ca-certificates git \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 libgbm1 \
    libpango-1.0-0 libcairo2 libasound2 libxshmfence1 \
    libx11-6 libx11-xcb1 libxcb1 libxext6 libxfixes3 \
    libdbus-1-3 libexpat1 libfontconfig1 libgcc-s1 libglib2.0-0 \
    libgtk-3-0 libpangocairo-1.0-0 libstdc++6 libatspi2.0-0 \
    fonts-liberation fonts-noto-color-emoji \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && ARCH=$(dpkg --print-architecture) \
    && CODENAME=$(. /etc/os-release && echo "$VERSION_CODENAME") \
    && echo "deb [arch=${ARCH} signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian ${CODENAME} stable" \
       > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y docker-ce docker-ce-cli containerd.io \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first (better layer caching — source changes won't invalidate this)
COPY webarena-src/requirements.txt /app/webarena/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt pyyaml \
    && playwright install chromium

# Copy WebArena source
COPY webarena-src/ /app/webarena/

# Convert prompt templates to JSON (dummy URLs needed to satisfy env_config.py assertions)
RUN SHOPPING="http://x" SHOPPING_ADMIN="http://x" REDDIT="http://x" \
    GITLAB="http://x" WIKIPEDIA="http://x" MAP="http://x" HOMEPAGE="http://x" \
    python -c "from agent.prompts import to_json; to_json.run()"

WORKDIR /app
