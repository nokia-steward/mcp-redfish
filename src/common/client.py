# Copyright 2025 Nokia
# Licensed under the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause

import logging
import os
from typing import Any

import certifi
import redfish
from fastmcp.exceptions import ToolError, ValidationError
from redfish.rest.v1 import AuthMethod

# Using tenacity for retry logic
from tenacity import (
    after_log,
    before_sleep_log,
    retry,
    stop_after_attempt,
    wait_exponential,
    wait_random_exponential,
)

logger = logging.getLogger(__name__)


def _get_config_value(
    server_cfg: dict[str, Any], common_cfg: Any, key: str, default: Any = None
) -> Any:
    value = server_cfg.get(key)
    if value is not None:
        return value
    return common_cfg.REDFISH_CFG.get(key, default)


def _get_tls_verify(server_cfg: dict[str, Any], common_cfg: Any) -> bool:
    for key in ("tls_verify", "verify_cert"):
        value = server_cfg.get(key)
        if value is not None:
            if not isinstance(value, bool):
                raise ValidationError("tls_verify must be a boolean")
            return value

    for key in ("tls_verify", "verify_cert"):
        value = common_cfg.REDFISH_CFG.get(key)
        if value is not None:
            if not isinstance(value, bool):
                raise ValidationError("tls_verify must be a boolean")
            return value

    return True


def get_retry_configuration():
    """Get consistent retry configuration from environment variables."""
    max_retries = int(os.getenv("REDFISH_MAX_RETRIES", "3"))
    initial_delay = float(os.getenv("REDFISH_INITIAL_DELAY", "1.0"))
    max_delay = float(os.getenv("REDFISH_MAX_DELAY", "60.0"))
    backoff_factor = float(os.getenv("REDFISH_BACKOFF_FACTOR", "2.0"))
    jitter = os.getenv("REDFISH_JITTER", "true").lower() == "true"

    # Configure wait strategy with backoff factor and optional jitter
    wait_strategy: wait_exponential | wait_random_exponential
    if jitter:
        wait_strategy = wait_random_exponential(
            multiplier=initial_delay, max=max_delay, exp_base=backoff_factor
        )
    else:
        wait_strategy = wait_exponential(
            multiplier=initial_delay, max=max_delay, exp_base=backoff_factor
        )

    return {
        "stop": stop_after_attempt(
            max_retries + 1
        ),  # +1 because MAX_RETRIES means "retries after initial attempt"
        "wait": wait_strategy,
        "retry": should_retry_redfish_exception,
    }


def should_retry_redfish_exception(retry_state):
    """Custom retry predicate that retries on network/connection errors but not validation errors."""
    # Extract the exception from the retry state
    if (
        hasattr(retry_state, "outcome")
        and retry_state.outcome
        and retry_state.outcome.exception()
    ):
        exception = retry_state.outcome.exception()
    else:
        return False

    # Check if it's a ToolError wrapping a retryable exception
    if isinstance(exception, ToolError):
        # Check the original exception (if available through __cause__)
        if hasattr(exception, "__cause__") and exception.__cause__:
            cause_is_retryable = isinstance(
                exception.__cause__, ConnectionError | TimeoutError | OSError
            )
            cause_is_validation = isinstance(exception.__cause__, ValidationError)
            return cause_is_retryable and not cause_is_validation

    # Direct check for network exceptions
    is_retryable = isinstance(exception, ConnectionError | TimeoutError | OSError)
    is_validation = isinstance(exception, ValidationError)
    return is_retryable and not is_validation


class RedfishClient:
    def __init__(self, server_cfg: dict[str, Any], common_cfg: Any) -> None:
        self.server_cfg = server_cfg
        self.common_cfg = common_cfg
        self.client = None
        self._setup_client()

    @retry(
        **get_retry_configuration(),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        after=after_log(logger, logging.INFO),
    )
    def _setup_client(self) -> None:
        auth_method = self.server_cfg.get(
            "auth_method"
        ) or self.common_cfg.REDFISH_CFG.get("auth_method")
        if auth_method not in (AuthMethod.BASIC, AuthMethod.SESSION):
            raise ValidationError(
                f"Invalid auth_method: {auth_method}. Allowed values: {AuthMethod.BASIC}, {AuthMethod.SESSION}"
            )
        username = self.server_cfg.get("username") or self.common_cfg.REDFISH_CFG.get(
            "username"
        )
        password = self.server_cfg.get("password") or self.common_cfg.REDFISH_CFG.get(
            "password"
        )
        port = self.server_cfg.get("port") or self.common_cfg.REDFISH_CFG.get(
            "port", 443
        )
        base_url = f"https://{self.server_cfg.get('address')}:{port}"

        tls_verify = _get_tls_verify(self.server_cfg, self.common_cfg)

        ca_cert = None
        if tls_verify:
            ca_cert = (
                _get_config_value(
                    self.server_cfg, self.common_cfg, "tls_server_ca_cert", None
                )
                or certifi.where()
            )
        else:
            logger.warning(
                "TLS certificate verification is disabled for Redfish server %s",
                self.server_cfg.get("address"),
            )

        logger.info(f"Setting up Redfish client for {base_url}")

        try:
            client = redfish.redfish_client(
                base_url=base_url,
                username=username,
                password=password,
                default_prefix="/redfish/v1",
                cafile=ca_cert,
            )

            client.login(auth=auth_method)
            self.client = client
            logger.info("Redfish client setup completed successfully")
        except Exception as e:
            logger.error(f"Failed to create Redfish client: {e}")
            raise ToolError(f"Failed to create Redfish client: {e}") from e

    @retry(
        **get_retry_configuration(),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        after=after_log(logger, logging.DEBUG),
    )
    def get(self, resource_path: str) -> Any:
        """Get resource data with retry logic."""
        if not self.client:
            raise ToolError("Redfish client not initialized")

        logger.debug(f"Performing GET request for resource: {resource_path}")

        try:
            response = self.client.get(resource_path)
        except Exception as e:
            logger.warning(f"Redfish GET request failed for {resource_path}: {e}")
            raise ToolError(f"Redfish GET request failed: {e}") from e

        if response is None:
            logger.error(f"Redfish GET request returned None for {resource_path}")
            raise ToolError("Redfish GET request returned None")

        logger.debug(f"Successfully retrieved resource: {resource_path}")
        return response.dict

    @retry(
        **get_retry_configuration(),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        after=after_log(logger, logging.DEBUG),
    )
    def get_with_headers(self, resource_path: str) -> dict[str, Any]:
        """Get resource data with headers included."""
        if not self.client:
            raise ToolError("Redfish client not initialized")

        logger.debug(f"Performing GET request for resource: {resource_path}")

        try:
            response = self.client.get(resource_path)
        except Exception as e:
            logger.warning(f"Redfish GET request failed for {resource_path}: {e}")
            raise ToolError(f"Redfish GET request failed: {e}") from e

        if response is None:
            logger.error(f"Redfish GET request returned None for {resource_path}")
            raise ToolError("Redfish GET request returned None")

        # Extract specific headers we're interested in
        headers: dict[str, str | list[str]] = {}
        all_headers = response.getheaders()

        # Map of header names to look for (case-insensitive)
        target_headers = {
            "allow": "Allow",
            "content-type": "Content-Type",
            "content-encoding": "Content-Encoding",
            "etag": "ETag",
            "link": "Link",
        }

        # Extract target headers (case-insensitive search)
        for header_name, header_value in all_headers:
            header_lower = header_name.lower()
            if header_lower in target_headers:
                standard_name = target_headers[header_lower]
                if standard_name == "Link":
                    # Handle multiple Link headers
                    if standard_name in headers:
                        existing_value = headers[standard_name]
                        if isinstance(existing_value, list):
                            existing_value.append(header_value)
                        else:
                            headers[standard_name] = [existing_value, header_value]
                    else:
                        headers[standard_name] = header_value
                else:
                    headers[standard_name] = header_value

        logger.debug(f"Successfully retrieved resource: {resource_path}")
        return {"headers": headers, "data": response.dict if response.dict else {}}

    @retry(
        **get_retry_configuration(),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        after=after_log(logger, logging.DEBUG),
    )
    def post(self, resource_path: str, data: dict[str, Any]) -> Any:
        """Post data to resource with retry logic."""
        if not self.client:
            raise ToolError("Redfish client not initialized")

        logger.debug(f"Performing POST request for resource: {resource_path}")

        try:
            response = self.client.post(resource_path, body=data)
        except Exception as e:
            logger.warning(f"Redfish POST request failed for {resource_path}: {e}")
            raise ToolError(f"Redfish POST request failed: {e}") from e

        logger.debug(f"Successfully posted to resource: {resource_path}")
        return response.dict if response else {}

    @retry(
        **get_retry_configuration(),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        after=after_log(logger, logging.DEBUG),
    )
    def patch(self, resource_path: str, data: dict[str, Any]) -> Any:
        """Patch resource data with retry logic."""
        if not self.client:
            raise ToolError("Redfish client not initialized")

        logger.debug(f"Performing PATCH request for resource: {resource_path}")

        try:
            response = self.client.patch(resource_path, body=data)
        except Exception as e:
            logger.warning(f"Redfish PATCH request failed for {resource_path}: {e}")
            raise ToolError(f"Redfish PATCH request failed: {e}") from e

        logger.debug(f"Successfully patched resource: {resource_path}")
        return response.dict if response else {}

    @retry(
        **get_retry_configuration(),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        after=after_log(logger, logging.DEBUG),
    )
    def delete(self, resource_path: str) -> Any:
        """Delete resource with retry logic."""
        if not self.client:
            raise ToolError("Redfish client not initialized")

        logger.debug(f"Performing DELETE request for resource: {resource_path}")

        try:
            response = self.client.delete(resource_path)
        except Exception as e:
            logger.warning(f"Redfish DELETE request failed for {resource_path}: {e}")
            raise ToolError(f"Redfish DELETE request failed: {e}") from e

        logger.debug(f"Successfully deleted resource: {resource_path}")
        return response.dict if response else {}

    def logout(self) -> None:
        """Logout from Redfish session."""
        if self.client:
            try:
                self.client.logout()
                logger.info("Redfish client logged out successfully")
            except Exception as e:
                logger.warning(f"Error during logout: {e}")
