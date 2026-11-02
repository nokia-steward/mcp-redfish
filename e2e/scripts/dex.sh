#!/bin/bash
# Copyright 2025 Nokia
# Licensed under the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause

# Manage CNCF Dex for MCP OAuth Proxy and OIDC Proxy e2e tests.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CERT_DIR="${SCRIPT_DIR}/../certs"
TEMPLATE_FILE="${SCRIPT_DIR}/../config/dex.yaml.template"
GENERATED_FILE="${SCRIPT_DIR}/../config/dex.generated.yaml"

DEX_IMAGE="${DEX_IMAGE:-ghcr.io/dexidp/dex:v2.46.1@sha256:65b72ca0f4b0bf6d4ada84ec21da8851782563404ac7b8f926923d27ceced9ab}"
DEX_PORT="${DEX_PORT:-5556}"
DEX_HOST="${DEX_HOST:-127.0.0.1}"
DEX_MCP_PORT="${DEX_MCP_PORT:-18080}"
CONTAINER_NAME="${DEX_CONTAINER_NAME:-dex-e2e}"
DEX_ISSUER="${DEX_ISSUER:-https://${DEX_HOST}:${DEX_PORT}/dex}"

CONTAINER_RUNTIME="${CONTAINER_RUNTIME:-}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

detect_container_runtime() {
    if [[ -n "${CONTAINER_RUNTIME}" ]]; then
        case "${CONTAINER_RUNTIME}" in
            docker|podman)
                if command -v "${CONTAINER_RUNTIME}" &> /dev/null && "${CONTAINER_RUNTIME}" info &> /dev/null; then
                    log_info "Using ${CONTAINER_RUNTIME} as container runtime (forced)"
                    return 0
                else
                    log_error "Forced container runtime '${CONTAINER_RUNTIME}' is not available or not running"
                    exit 1
                fi
                ;;
            *)
                log_error "Invalid CONTAINER_RUNTIME value: ${CONTAINER_RUNTIME}. Use 'docker' or 'podman'"
                exit 1
                ;;
        esac
    fi

    if command -v docker &> /dev/null && docker info &> /dev/null; then
        CONTAINER_RUNTIME="docker"
        log_info "Using Docker as container runtime"
        return 0
    fi

    if command -v podman &> /dev/null && podman info &> /dev/null; then
        CONTAINER_RUNTIME="podman"
        log_info "Using Podman as container runtime"
        return 0
    fi

    log_error "Neither Docker nor Podman is available or running"
    log_error "Please install and start either Docker or Podman"
    exit 1
}

render_config() {
    if [[ ! -f "${TEMPLATE_FILE}" ]]; then
        log_error "Dex template not found: ${TEMPLATE_FILE}"
        exit 1
    fi
    sed \
        -e "s|__DEX_ISSUER__|${DEX_ISSUER}|g" \
        -e "s|__DEX_MCP_PORT__|${DEX_MCP_PORT}|g" \
        "${TEMPLATE_FILE}" > "${GENERATED_FILE}"
}

pull_dex_image() {
    detect_container_runtime
    log_info "Pulling Dex image: ${DEX_IMAGE}"
    "${CONTAINER_RUNTIME}" pull "${DEX_IMAGE}"
}

start_dex() {
    detect_container_runtime

    if [[ ! -f "${CERT_DIR}/server.crt" || ! -f "${CERT_DIR}/server.key" ]]; then
        log_warn "SSL certificates not found, generating them..."
        "${SCRIPT_DIR}/generate-cert.sh"
    fi

    # Dex runs as a non-root user and cannot read the 0600 emulator key.
    TLS_DIR="${CERT_DIR}/dex"
    mkdir -p "${TLS_DIR}"
    cp "${CERT_DIR}/server.crt" "${TLS_DIR}/tls.crt"
    cp "${CERT_DIR}/server.key" "${TLS_DIR}/tls.key"
    chmod 644 "${TLS_DIR}/tls.crt" "${TLS_DIR}/tls.key"

    render_config

    if "${CONTAINER_RUNTIME}" ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        log_warn "Restarting Dex to apply the latest configuration"
        "${CONTAINER_RUNTIME}" stop "${CONTAINER_NAME}" >/dev/null 2>&1 || true
        "${CONTAINER_RUNTIME}" rm "${CONTAINER_NAME}" >/dev/null 2>&1 || true
    elif "${CONTAINER_RUNTIME}" ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        log_info "Removing existing container ${CONTAINER_NAME}"
        "${CONTAINER_RUNTIME}" rm "${CONTAINER_NAME}"
    fi

    log_info "Starting Dex..."
    log_info "  Image: ${DEX_IMAGE}"
    log_info "  Issuer: ${DEX_ISSUER}"
    log_info "  MCP callback port: ${DEX_MCP_PORT}"
    log_info "  Container: ${CONTAINER_NAME}"

    "${CONTAINER_RUNTIME}" run -d \
        --name "${CONTAINER_NAME}" \
        -p "${DEX_HOST}:${DEX_PORT}:5556/tcp" \
        -v "${GENERATED_FILE}:/etc/dex/config.yaml:ro" \
        -v "${TLS_DIR}/tls.crt:/etc/dex/tls.crt:ro" \
        -v "${TLS_DIR}/tls.key:/etc/dex/tls.key:ro" \
        "${DEX_IMAGE}" \
        dex serve /etc/dex/config.yaml

    log_info "Waiting for Dex to start..."
    for _ in {1..30}; do
        if curl -k -s "${DEX_ISSUER}/.well-known/openid-configuration" > /dev/null 2>&1; then
            log_info "✓ Dex is ready at ${DEX_ISSUER}"
            return 0
        fi
        sleep 1
    done

    log_error "Dex failed to start within 30 seconds"
    "${CONTAINER_RUNTIME}" logs "${CONTAINER_NAME}" 2>&1 | tail -40
    "${CONTAINER_RUNTIME}" rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
    return 1
}

stop_dex() {
    detect_container_runtime
    if "${CONTAINER_RUNTIME}" ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        log_info "Stopping Dex..."
        "${CONTAINER_RUNTIME}" stop "${CONTAINER_NAME}"
        "${CONTAINER_RUNTIME}" rm "${CONTAINER_NAME}" >/dev/null 2>&1 || true
        log_info "✓ Dex stopped"
    else
        log_warn "Container ${CONTAINER_NAME} is not running"
    fi
}

status_dex() {
    detect_container_runtime
    if "${CONTAINER_RUNTIME}" ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        log_info "Dex is running"
        echo "Container: ${CONTAINER_NAME}"
        echo "Issuer: ${DEX_ISSUER}"
        echo "Discovery: ${DEX_ISSUER}/.well-known/openid-configuration"
        echo "MCP callback: http://127.0.0.1:${DEX_MCP_PORT}/auth/callback"
        return 0
    else
        log_warn "Dex is not running"
        return 1
    fi
}

logs_dex() {
    detect_container_runtime
    if "${CONTAINER_RUNTIME}" ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        "${CONTAINER_RUNTIME}" logs "${CONTAINER_NAME}" "$@"
    else
        log_error "Container ${CONTAINER_NAME} does not exist"
        return 1
    fi
}

usage() {
    echo "Usage: $0 {start|stop|status|logs|pull}"
    echo ""
    echo "Commands:"
    echo "  start   - Start Dex"
    echo "  stop    - Stop Dex"
    echo "  status  - Check if Dex is running"
    echo "  logs    - Show Dex logs"
    echo "  pull    - Pull the Dex image"
    echo ""
    echo "Environment variables:"
    echo "  DEX_IMAGE           - Container image (default: ${DEX_IMAGE})"
    echo "  DEX_PORT            - Host port (default: ${DEX_PORT})"
    echo "  DEX_HOST            - Host bind address (default: ${DEX_HOST})"
    echo "  DEX_MCP_PORT        - MCP listen port registered as Dex redirect (default: ${DEX_MCP_PORT})"
    echo "  DEX_CONTAINER_NAME  - Container name (default: ${DEX_CONTAINER_NAME:-dex-e2e})"
    echo "  CONTAINER_RUNTIME   - docker or podman (default: auto-detect)"
}

case "${1:-}" in
    start)
        pull_dex_image
        start_dex
        ;;
    stop)
        stop_dex
        ;;
    status)
        status_dex
        ;;
    logs)
        logs_dex "${@:2}"
        ;;
    pull)
        pull_dex_image
        ;;
    *)
        usage
        exit 1
        ;;
esac
