ARG TOOL_IMAGE=ghcr.io/example-org/tool:1.0@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
FROM ${TOOL_IMAGE} AS tool
FROM docker.io/library/python:3.12-slim@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
COPY --from=tool /tool /usr/local/bin/tool
