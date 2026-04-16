FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/xbench-ai/xbench-evals.git /app/xbench-evals

RUN python -m pip install --no-cache-dir \
    'openai>=1.57.0' \
    'numpy>=1.26.4' \
    'tqdm' \
    'pyyaml'
