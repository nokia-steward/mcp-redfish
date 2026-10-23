# Copilot Instructions for MCP Redfish Server

## Project Architecture

This is a **FastMCP-based Model Context Protocol (MCP) server** that provides a natural language interface to Redfish-enabled infrastructure. The architecture follows a layered design:

- **`src/main.py`**: Entry point with `RedfishMCPServer` class handling SSDP discovery and server startup
- **`src/common/server.py`**: FastMCP instance initialization (`mcp = FastMCP("Redfish MCP Server", auth=...)`)
- **`src/common/auth.py`**: Maps `MCP_AUTH_*` to FastMCP token verifiers
- **`src/common/validation.py`**: Fail-closed HTTP auth, bind, and TLS rules
- **`src/tools/`**: MCP tool implementations using `@mcp.tool()` decorators (auto-registered via imports)
- **`src/common/client.py`**: Redfish client wrapper with retry logic using `tenacity` library
- **`src/common/discovery.py`**: SSDP discovery for automatic Redfish endpoint detection

**Tool Registration Pattern**: Tools are auto-registered by importing modules in `src/tools/__init__.py`. Each tool uses `@mcp.tool()` decorator from the global `mcp` instance.

## Key Development Patterns

### MCP Tool Development
```python
from ..common.server import mcp

@mcp.tool()
async def tool_name(param: str) -> dict:
    """Tool description for AI agents."""
    # Implementation
```

### Configuration Hierarchy
1. **Environment variables** (`.env` file recommended)
2. **Per-host overrides** in `REDFISH_HOSTS` JSON array
3. **Global defaults** from `src/common/config.py`

Critical: Set test environment variables BEFORE importing src modules in tests (see `test/conftest.py`).

### Redfish Client Usage
```python
from ..common.client import RedfishClient
from .. import common

# Get server config, create client, handle cleanup
server_cfg = common.hosts.get_hosts()[0]
client = RedfishClient(server_cfg, common.config)
try:
    response = client.get("/redfish/v1/Systems")
finally:
    client.logout()  # Always cleanup
```

## Development Workflow

Supported runtimes: Python 3.14 recommended; Python 3.13 deprecated ([policy](../docs/PYTHON_RUNTIME.md)).

### Essential Commands
```bash
make dev          # Full dev setup with pre-commit hooks
make run-stdio    # Run server (recommended)
make inspect      # Debug with MCP Inspector
make test         # Run tests
make all-checks   # Comprehensive quality checks (lint, type, security, test)
make e2e-test     # Full e2e testing with Redfish emulator
```

Dependency policy is canonical in
[Automated Dependency Updates](../README.md#automated-dependency-updates).
Preserve the shared seven-day cutoff, the oldest-fix exception for known
vulnerabilities, `npm ci --ignore-scripts`, and the warned
`bypass_cooldown` override. `.uv-version` is the only uv installer pin; the
release indicator never edits it. Renovate covers only Actions and container
references. `.github/dependabot.yml` allows uv security updates only. Do not
duplicate this policy in other docs.

### Testing Strategy
- **Unit tests**: `test/tools/`, `test/common/` - Mock Redfish responses
- **Integration tests**: `test/integration/` - Real client interactions
- **E2E tests**: `e2e/` using MCP Inspector CLI v2 + DMTF Redfish Emulator
- **Property-based tests**: `test/property/` using Hypothesis

**Critical**: Environment variables in `test/conftest.py` set fast retry configuration for tests.

### Transport Modes
- **stdio** (default): Direct MCP client integration. MCP HTTP auth is ignored.
- **streamable-http**: Preferred HTTP transport. **Breaking change:** HTTP MCP transports (`sse`, `streamable-http`) now require authentication. Set `MCP_AUTH_MODE` (and the matching `MCP_AUTH_*` variables), or set `MCP_HTTP_AUTH=false` to keep unauthenticated HTTP. stdio is unchanged. See README and the release notes.
- **sse**: Same HTTP auth rule as streamable-http. Non-loopback SSE is refused unless the warned legacy break-glass setting `MCP_ALLOW_REMOTE_SSE=true` is explicit. Prefer streamable-http for remote use.

Set via `MCP_TRANSPORT` environment variable. `make run-sse` / `make run-streamable-http` will not start without auth env. Do not bake `MCP_HTTP_AUTH=false` into those targets. See [docs/MCP_AUTH.md](../docs/MCP_AUTH.md) for authentication and [docs/MCP_HTTP_DEPLOYMENT.md](../docs/MCP_HTTP_DEPLOYMENT.md) for bind, Host/Origin, TLS, container, and Kubernetes settings.

Clients on HTTP send `Authorization: Bearer <token>`. Do not show a naked HTTP `url` with no auth.
Non-loopback Streamable HTTP also requires exact client-facing names in
`FASTMCP_HTTP_ALLOWED_HOSTS`; do not generate wildcard Host/Origin patterns.
JWT examples must use expiring tokens. Introspection examples must include
`MCP_AUTH_INTROSPECTION_ISSUER` and `MCP_AUTH_INTROSPECTION_AUDIENCE`.
Private/internal IdPs additionally require the explicit warned setting
`MCP_AUTH_INTROSPECTION_ALLOW_PRIVATE=true`; never enable it silently.
Private/loopback JWKS uses `MCP_AUTH_JWKS_ALLOW_PRIVATE=true` the same way.
Never add logs containing tokens, token claims, passwords, client secrets, key
contents, required scope names, identity-provider response bodies, or proxy
URLs. Use stable reason codes at DEBUG for authentication failures.

## Configuration Deep Dive

### REDFISH_HOSTS JSON Structure
```json
[
  {
    "address": "192.168.1.100",    // Required
    "port": 443,                   // Optional: defaults to REDFISH_PORT
    "username": "admin",           // Optional: defaults to REDFISH_USERNAME
    "password": "secret",          // Optional: defaults to REDFISH_PASSWORD
    "auth_method": "session",      // Optional: "basic" or "session"
    "tls_server_ca_cert": "/path"  // Optional: CA certificate
  }
]
```

### Discovery Feature
- **SSDP discovery**: Auto-detects Redfish endpoints using UPnP/SSDP
- **Background thread**: Runs periodically when `REDFISH_DISCOVERY_ENABLED=true`
- **Updates host list**: Discovered hosts added via `common.hosts.update_discovered_hosts()`

## Error Handling Patterns

### Retry Configuration
All Redfish operations use `tenacity` with configurable retry:
- `REDFISH_MAX_RETRIES`, `REDFISH_INITIAL_DELAY`, `REDFISH_MAX_DELAY`
- Exponential backoff with optional jitter
- Custom retry logic in `client.py:should_retry_redfish_exception()`

### Exception Hierarchy
- `ValidationError`: Invalid input parameters
- `ToolError`: Runtime errors during tool execution
- Always use specific exception types for better error handling

## Quality Standards

### Code Style
- **Formatter**: ruff (88 char line length, double quotes)
- **Type hints**: Required for public functions (mypy with relaxed external lib settings)
- **Import order**: stdlib, third-party, local (ruff handles sorting)

### Testing Requirements
- **Coverage**: 60% minimum (enforced in CI)
- **Markers**: `@pytest.mark.unit`, `@pytest.mark.integration`, `@pytest.mark.slow`
- **Fast configuration**: Set retry env vars before importing src modules

### E2E Testing
The project uses DMTF Redfish Interface Emulator for comprehensive testing:
- **Setup**: `make e2e-emulator-setup` (creates certificates, starts emulator); `make e2e-dex-start` for OAuth Proxy and OIDC Proxy e2e
- **Framework**: `e2e/framework.py` with MCP Inspector CLI v2 integration
- **Agent tests**: Optional OpenAI API integration for full agent workflows

## Integration Points

### External Dependencies
- **python-redfish-library**: Core Redfish API client
- **FastMCP**: MCP server framework
- **tenacity**: Retry logic for network operations
- **python-dotenv**: Environment configuration

### VS Code Integration
stdio is the recommended path. HTTP samples must include Bearer auth (or document `MCP_HTTP_AUTH=false` for loopback lab only):
```json
"mcp": {
  "servers": {
    "redfish-mcp": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp",
      "headers": {
        "Authorization": "Bearer <jwt>"
      }
    }
  }
}
```

## Common Gotchas

1. **Tool registration**: Import tool modules in `src/tools/__init__.py` to register with MCP
2. **Client cleanup**: Always call `client.logout()` in finally blocks
3. **Test environment**: Set retry config before importing src modules
4. **Host configuration**: Validate JSON syntax in `REDFISH_HOSTS`
5. **Type annotations**: Use relaxed settings for external libraries without stubs
