# Copyright 2025 Nokia
# Licensed under the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause

FROM python:3.14.10-slim@sha256:930e1abba7c4e8f96ed93e8012c35d174d7732752731bbd5e8e757d7ec6f3966
COPY .uv-version /tmp/.uv-version
ARG UV_VERSION
RUN set -eu; \
    version="${UV_VERSION:-$(tr -d '[:space:]' < /tmp/.uv-version)}"; \
    pip install --no-cache-dir "uv==${version}"; \
    rm /tmp/.uv-version

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src

# Install dependencies and activate virtual environment
RUN uv sync --locked --no-dev

# Add virtual environment to PATH
ENV PATH="/app/.venv/bin:$PATH"

# Default CMD is stdio-shaped (no MCP HTTP auth). If you override
# MCP_TRANSPORT to sse or streamable-http, set MCP_AUTH_MODE (and MCP_AUTH_*)
# or MCP_HTTP_AUTH=false. Non-loopback streamable-http also requires exact
# FASTMCP_HTTP_ALLOWED_HOSTS. See docs/MCP_AUTH.md and
# docs/MCP_HTTP_DEPLOYMENT.md.
CMD ["uv", "run", "python", "-m", "src.main"]
