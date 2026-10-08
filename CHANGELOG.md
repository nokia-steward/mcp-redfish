# Changelog

All notable changes to this project are documented in this file.

## Unreleased

## [0.5.4] - 2026-10-08

### Fixed

- `get_resource_data` rejects non-HTTPS URLs and explicit ports that do not match the configured Redfish endpoint, instead of silently using the configured endpoint.
- `get_with_headers` retries retryable Redfish request failures using the configured retry policy.

### Changed

- Dependencies are updated within their existing ranges. Direct updates include FastMCP 4.0.11 and python-dotenv 1.2.4; developer tools include mypy 2.4.0 and Ruff 0.16.10. MCP Inspector moves to 2.9.0. Transitive updates include cyclopts 5.1.1 and filelock 4.0.12.

## [0.5.3] - 2026-09-24

### Deprecation

This repository has moved from [nokia/mcp-redfish](https://github.com/nokia/mcp-redfish) to [nokia-steward/mcp-redfish](https://github.com/nokia-steward/mcp-redfish). New releases and versioned container images are published only as `ghcr.io/nokia-steward/mcp-redfish`.

### Changed

- Release images are published to `ghcr.io/nokia-steward/mcp-redfish`.
- Dependencies are updated within the existing ranges. Direct updates include `fastmcp` 4.0.8 and `httpx2` 2.13.1. MCP Inspector moves to 2.8.0. Transitive updates include `cyclopts` 5.0.0 and `filelock` 4.0.3.

## [0.5.0] - 2026-09-08

### Breaking change

HTTP MCP transports (`sse`, `streamable-http`) now require authentication. Set `MCP_AUTH_MODE` (and the matching `MCP_AUTH_*` variables), or set `MCP_HTTP_AUTH=false` to keep unauthenticated HTTP. stdio is unchanged. See README and the release notes.

Old `make run-sse` / `make run-streamable-http` / `MCP_TRANSPORT=sse` without the new env **will not start**. Migration is either configure `MCP_AUTH_MODE` or set `MCP_HTTP_AUTH=false`.

This is a security hardening change on a `0.1.0` tree; it is still spelled out as a break because HTTP was documented as a supported remote mode.

### Breaking change: configuration errors abort

Configuration that fails validation now logs the error and exits non-zero. The legacy fallback parser is removed. Previously a rejected configuration produced a deprecation warning and the server continued with substituted defaults — a malformed `REDFISH_HOSTS` became `[{"address": "127.0.0.1"}]`, an unparseable `REDFISH_TLS_VERIFY` was forced back to enabled, and an unrecognised `MCP_TRANSPORT` was passed through unvalidated.

That last case also let `MCP_TRANSPORT=http` (a transport name FastMCP accepts) start an unauthenticated HTTP listener despite `MCP_HTTP_AUTH` defaulting to `true`. The HTTP fail-closed, bind, and TLS rules now apply to every transport that is not `stdio`, rather than to an allowlist of `sse` and `streamable-http`.

### Added

- Fail-closed MCP HTTP authentication (Phase 0) with explicit bind (`FASTMCP_HOST` unset → `127.0.0.1`) and listen-socket TLS (`MCP_TLS_CERTFILE`/`MCP_TLS_KEYFILE` or `MCP_TLS_TERMINATED`).
- Token verification (Phase 1): `JWTVerifier` (JWKS or public key, issuer and audience required) and `IntrospectionTokenVerifier`.
- Remote OAuth (Phase 2): `RemoteAuthProvider` wrapping the Phase 1 verifier, with public protected-resource metadata.
- OAuth Proxy (Phase 3) and OIDC Proxy (Phase 4): FastMCP proxy providers with consent on, PKCE on, redirect allowlists, required off-loopback signing keys, and encrypted Redis when more than one process shares storage.
- Authentication guide: [docs/MCP_AUTH.md](docs/MCP_AUTH.md). HTTP deployment
  and listener guide: [docs/MCP_HTTP_DEPLOYMENT.md](docs/MCP_HTTP_DEPLOYMENT.md).
  Manual IdP checklist: [docs/MCP_AUTH_MANUAL_IDP.md](docs/MCP_AUTH_MANUAL_IDP.md).
- HTTP e2e coverage for `MCP_HTTP_AUTH=false` (warning + tools) and local JWT Bearer (unauthenticated call fails; authenticated `list_servers` works). Default e2e remains stdio.
- In-process HTTPS OpenID simulator for OAuth Proxy login in the integration
  suite (DCR, PKCE, code exchange) and OIDC Proxy SSRF/discovery checks.
  OAuth/OIDC/Remote OAuth HTTP e2e login and metadata are against CNCF Dex.
  Cloud IdPs remain a manual checklist.
- CNCF Dex in e2e (`make e2e-dex-start`) for OAuth Proxy (fixed app, JWT JWKS
  or introspection of the Dex access token), OIDC Proxy (discovery plus
  ID-token or access-token verification), and Remote OAuth (protected-resource
  metadata plus Dex JWT Bearer without MCP-client DCR). Loopback JWKS and
  introspection require the warned `MCP_AUTH_JWKS_ALLOW_PRIVATE` /
  `MCP_AUTH_INTROSPECTION_ALLOW_PRIVATE` settings. Dex requires the `openid`
  authorize scope; `MCP_AUTH_REQUIRED_SCOPES` on `oauth_proxy` is advertised
  to MCP clients rather than required as a JWT claim.
- Startup refuses `*` in `FASTMCP_HTTP_ALLOWED_HOSTS`/`FASTMCP_HTTP_ALLOWED_ORIGINS`, which would disable the Host/Origin guard that protects HTTP transports from DNS rebinding. Checked against the effective FastMCP settings, so a value supplied through `FASTMCP_ENV_FILE` is caught too. Joins the existing refusal of `FASTMCP_SERVER_AUTH*`. See [docs/MCP_HTTP_DEPLOYMENT.md](docs/MCP_HTTP_DEPLOYMENT.md).
- `FASTMCP_SSRF_TRUST_PROXY=true` is supported for deployments that can only reach their identity provider through an enterprise proxy, and is validated rather than refused: enabling it without `HTTPS_PROXY` or `ALL_PROXY` now aborts at startup, because FastMCP would otherwise refuse every JWKS and OAuth metadata fetch at the first token verification. Startup logs a warning naming the proxy variable in use, never its value. The default (`ssrf_safe=True` with DNS resolution and the address blocklist) already works behind a proxy and remains preferred; see the new "Reaching the identity provider through an enterprise proxy" section of [docs/MCP_AUTH.md](docs/MCP_AUTH.md).
- Post-verification token policy around FastMCP providers: JWTs now require
  `exp` and enforce `nbf`/future `iat`; introspection requires matching issuer,
  audience, and access-token type.
- SSRF-safe, DNS-pinned introspection POSTs with explicit
  `MCP_AUTH_INTROSPECTION_ALLOW_PRIVATE=true` for deliberately private IdPs.
  JWKS fetches have the matching warned `MCP_AUTH_JWKS_ALLOW_PRIVATE=true`
  setting for private or loopback JWKS (OIDC Proxy and JWT JWKS).
- Strict Host/Origin protection by default on non-loopback Streamable HTTP
  binds, with exact `FASTMCP_HTTP_ALLOWED_HOSTS`.

### Fixed

- Configuration errors are reported as a single `Configuration error: …` line instead of an import traceback. Configuration is validated while `src.common` is imported, so the previous handler in `RedfishMCPServer.run()` was unreachable and every rejected value — including an ordinary `REDFISH_HOSTS` typo — printed a stack trace above the message. The breaking-change migration text is now emitted with the fail-closed HTTP error specifically, rather than with every auth error.
- A server that fails to start exits non-zero. `mcp.run()` failures were logged and swallowed, so a process that never bound its port reported success to its supervisor.
- FastMCP's own rejections of an `MCP_AUTH_*` combination (an `HS*` algorithm paired with a PEM public key, for example) are reported as configuration errors rather than a `ValueError` traceback from provider construction.
- Docker images copy only runtime project files, preventing local `.env`,
  certificates, tests, and workspace artifacts from entering image layers.
- stdio now ignores HTTP authentication settings instead of constructing or
  rejecting unused providers.
- Bracketed IPv6 loopback binds are normalized; wildcard-like Host/Origin
  patterns are refused; tools consume the validated Redfish host list.
- HTTP integration servers shut down cleanly, HTTP e2e scrubs inherited auth
  variables, and negative Inspector tests no longer pass on connection timeout.
- Non-loopback SSE is refused by default because SSE has no Host/Origin
  protection. Legacy deployments must explicitly set the warned
  `MCP_ALLOW_REMOTE_SSE=true` break-glass setting.
- Startup logs now report safe effective configuration fields, and DEBUG logs
  provide stable token-rejection and identity-provider egress reason codes.
  FastMCP provider logs are sanitized so tokens, claims, credentials, response
  bodies, key contents, scope names, and proxy URLs are not emitted.
- `oidc_proxy` requires `MCP_AUTH_OIDC_AUDIENCE` when verifying access tokens,
  refuses `MCP_AUTH_JWT_ALGORITHM=none`, and refuses unrestricted OAuth
  redirect hosts (`*` / `https://*`). Startup logs name OIDC discovery as the
  verification source and the public origin without query or userinfo.
  Provider construction errors include `error_type` without URLs or secrets.
