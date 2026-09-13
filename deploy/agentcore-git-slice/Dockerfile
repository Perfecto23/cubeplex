# syntax=docker/dockerfile:1.7

# The digests are the ARM64-capable manifests read back before this slice was
# implemented.  build.sh always supplies --platform linux/arm64.
ARG PYTHON_BASE=python:3.12.14-slim-trixie@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
ARG LAMBDA_BASE=public.ecr.aws/lambda/python:3.12@sha256:8847903df9b6c023c7872f02c95f7bcd170a2f16b3eb9eace18a5d32297aa40e

FROM ${PYTHON_BASE} AS worker-base

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    HOME=/home/agentcore \
    PYTHONPATH=/app

RUN set -eux; \
    printf '%s\n' 'deb [signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] https://deb.debian.org/debian trixie-backports main' \
        > /etc/apt/sources.list.d/trixie-backports.list; \
    apt-get update; \
    apt-get upgrade -y; \
    apt-get install -y --no-install-recommends bash ca-certificates git tini; \
    apt-get install -y --no-install-recommends -t trixie-backports \
        libcurl3t64-gnutls=8.21.0-2~bpo13+1; \
    rm -rf /var/lib/apt/lists/*; \
    groupadd --system --gid 10001 agentcore; \
    useradd --system --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin agentcore

FROM worker-base AS worker-builder

WORKDIR /build
RUN python -m venv /opt/venv

COPY deploy/agentcore-git-slice/requirements-frozen.txt /build/requirements-frozen.txt
RUN /opt/venv/bin/pip install --no-cache-dir --disable-pip-version-check \
    -r /build/requirements-frozen.txt

COPY deploy/agentcore-git-slice/cubeplex_git_slice /build/cubeplex_git_slice

FROM worker-base AS worker

WORKDIR /app
COPY --from=worker-builder /opt/venv /opt/venv
COPY --from=worker-builder --chown=agentcore:agentcore \
    /build/cubeplex_git_slice /app/cubeplex_git_slice
RUN mkdir -p /workspace /home/agentcore \
    && chown -R agentcore:agentcore /app /workspace /home/agentcore

USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "cubeplex_git_slice.runtime"]

FROM ${LAMBDA_BASE} AS broker

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN dnf --releasever=2023.12.20260831 install -y \
        ca-certificates git \
        openssl-fips-provider-latest-3.5.7-2.amzn2023.0.2 \
        openssl-snapsafe-libs-3.5.7-2.amzn2023.0.2 \
    && dnf clean all \
    && rm -rf /var/cache/dnf

COPY deploy/agentcore-git-slice/requirements-frozen.txt /tmp/requirements-frozen.txt
RUN python -m pip install --no-cache-dir --disable-pip-version-check \
    -r /tmp/requirements-frozen.txt --target "${LAMBDA_TASK_ROOT}"

COPY deploy/agentcore-git-slice/cubeplex_git_slice "${LAMBDA_TASK_ROOT}/cubeplex_git_slice"
# The private source archive keeps owner-only modes; Lambda assigns its own UID.
RUN chmod -R a+rX "${LAMBDA_TASK_ROOT}/cubeplex_git_slice"

CMD ["cubeplex_git_slice.broker.lambda_handler"]
