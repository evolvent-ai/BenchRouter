FROM python:3.12-slim
WORKDIR /app
COPY . /app/simple_evals/
RUN pip install --no-cache-dir openai pandas jinja2 numpy tqdm requests blobfile anthropic scipy human-eval tabulate pyyaml
