# syntax=docker/dockerfile:1.7

ARG BASE_IMAGE=986420599013.dkr.ecr.us-west-2.amazonaws.com/cubeplex-git-slice-20260913/worker@sha256:454e29089075d2e3c49bb91f9d73624218e7a8eb953e5d9cd2ed9c323953974d
FROM ${BASE_IMAGE}

ENV PYTHONPATH=/app

USER root
COPY deploy/agentcore-native-entry/cubeplex_native /app/cubeplex_native
RUN chmod -R a+rX /app/cubeplex_native

USER 10001:10001
CMD ["python", "-m", "cubeplex_native.runtime"]
