FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch v0.7.1 https://github.com/EvolvingLMMs-Lab/lmms-eval.git /tmp/lmms-eval && \
    cd /tmp/lmms-eval && \
    python -m pip install --no-cache-dir -e '.[openai]' 'pyyaml' && \
    rm -rf /tmp/lmms-eval/.git
