FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

# Keep a local source checkout so TAU2_DATA_DIR can point to /app/tau2-bench/data.
RUN git clone --depth 1 https://github.com/sierra-research/tau2-bench.git /app/tau2-bench

# Install core + knowledge extras to support all text benchmark domains
# (airline/retail/telecom/mock/banking_knowledge).
RUN python -m pip install --no-cache-dir \
    '/app/tau2-bench[knowledge]' \
    'pyyaml'

ENV TAU2_DATA_DIR=/app/tau2-bench/data
