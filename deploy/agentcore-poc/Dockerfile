# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.12.5@sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1 AS uv
FROM python:3.13-slim@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285 AS dependencies
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install --no-install-recommends -y git ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/poc
COPY deploy/agentcore-poc/pyproject.toml deploy/agentcore-poc/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --python /usr/local/bin/python

FROM python:3.13-slim@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285
ARG SOURCE_COMMIT
ARG SOURCE_TREE_SHA256
LABEL org.opencontainers.image.source="https://github.com/Perfecto23/cubeplex" \
      org.opencontainers.image.revision="${SOURCE_COMMIT}" \
      io.cubeplex.poc.source-tree-sha256="${SOURCE_TREE_SHA256}"
COPY --from=dependencies /opt/poc/.venv /opt/poc/.venv
COPY backend/cubeplex /app/cubeplex
ENV PATH="/opt/poc/.venv/bin:$PATH" \
    PYTHONPATH="/app" \
    PYTHONUNBUFFERED="1" \
    PYTHONDONTWRITEBYTECODE="1" \
    AWS_DEFAULT_REGION="us-west-2"
WORKDIR /app
RUN apt-get update \
    && apt-get upgrade --yes \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin agent
USER 10001
EXPOSE 8080
CMD ["python", "-m", "cubeplex.agentcore_poc.runtime"]
