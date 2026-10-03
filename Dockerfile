# syntax=docker/dockerfile:1

# Stage 1: model weights only. No source code ever enters this stage, so
# Docker keeps this ~2.3 GB layer cached across everyday code-change builds.
FROM python:3.12-slim AS models
RUN pip install --no-cache-dir huggingface_hub \
 && HF_HOME=/hf python -c "from huggingface_hub import snapshot_download; snapshot_download('BAAI/bge-reranker-v2-m3'); snapshot_download('Qdrant/bm25')"

# Stage 2: the app.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10 \
    HF_HOME=/app/hf-cache

WORKDIR /app

# gosu drops from root to the app user after the entrypoint fixes ownership.
RUN apt-get update \
 && apt-get install -y --no-install-recommends gosu \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./

# RAGent2 comes from the private feed, so it is not in pyproject. Pass the
# index at build time:
#   docker build --build-arg PIP_EXTRA_INDEX_URL=https://... .
# torch is installed CPU-only, first, from PyTorch's own index -- otherwise
# ragent2 pulls the full CUDA build through the Azure Artifacts proxy, which
# is a 500+ MB download this service will never use.
#
# The dependencies are installed before the source is copied, so this step
# depends on pyproject.toml alone and a code change does not repeat it -- it
# is a 20 to 40 minute download over the Azure feed. An empty package is
# enough for pip to read the dependencies out of pyproject.toml; the real one
# is installed over it below.
ARG PIP_EXTRA_INDEX_URL=""
ARG RAGENT_SPEC="ragent2"
RUN mkdir -p src/ole5 \
 && touch src/ole5/__init__.py \
 && pip install --no-cache-dir . \
 && pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
 && if [ -n "$PIP_EXTRA_INDEX_URL" ]; then \
      pip install --no-cache-dir --extra-index-url "$PIP_EXTRA_INDEX_URL" "$RAGENT_SPEC"; \
    fi \
 && pip install --no-cache-dir "sentence-transformers==4.1.0" "transformers==4.57.6"

# From here down every step is seconds: no downloads, only the package itself.
COPY src ./src
RUN pip install --no-cache-dir --no-deps --force-reinstall .

COPY migrations ./migrations
COPY prompts ./prompts
# The maintenance scripts (weekly, junk, similar, ...), so they can be run
# inside the container with the app's own settings and models:
#   docker compose exec ole5 python scripts/weekly.py status
COPY scripts ./scripts

RUN useradd --create-home --uid 10001 ole5 \
 && mkdir -p /app/data/uploads \
 && chown -R ole5:ole5 /app/data

# Pre-downloaded models from Stage 1, owned by the app user so they stay
# readable after the gosu privilege drop.
COPY --from=models --chown=10001:10001 /hf /app/hf-cache

# Root at container start only long enough to fix ownership on the
# bind-mounted ./data volume -- its permissions come from the host, not this
# image, so the chown above does not survive the mount. Then drops privilege
# for the actual process; nothing in the app itself ever runs as root.
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 8420

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8420/api/health', timeout=4).status==200 else 1)"

ENTRYPOINT ["entrypoint.sh"]
CMD ["python", "-m", "ole5"]