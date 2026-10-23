#!/usr/bin/env python3
# Copyright 2025 Nokia
# Licensed under the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause

"""Update the pinned MCP Inspector version within the configured semver range."""

from __future__ import annotations

import argparse
import sys
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from e2e.inspector_version import (  # noqa: E402
    NoEligibleInspectorVersion,
    latest_version_in_constraint,
    load_inspector_version_config,
    lock_cutoff_for_pinned_version,
    npm_direct_security_fix,
    parse_version,
    update_inspector_npm_lock,
    write_locked_version,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    cutoff_options = parser.add_mutually_exclusive_group()
    cutoff_options.add_argument(
        "--exclude-newer", help="UTC-aware ISO 8601 publication cutoff"
    )
    cutoff_options.add_argument(
        "--cooldown-days",
        type=int,
        default=7,
        help="Minimum release age in days (default: 7; 0 explicitly bypasses)",
    )
    parser.add_argument(
        "--npm-lock", action="store_true", help="Update the full npm dependency lock"
    )
    args = parser.parse_args(argv)
    if args.cooldown_days < 0:
        parser.error("--cooldown-days must be non-negative")
    cutoff = None
    if args.exclude_newer is not None:
        try:
            cutoff = datetime.fromisoformat(args.exclude_newer)
        except ValueError:
            parser.error("--exclude-newer must be an ISO 8601 timestamp")
        if cutoff.utcoffset() is None:
            parser.error("--exclude-newer must include a timezone")
    elif args.cooldown_days:
        cutoff = datetime.now(UTC) - timedelta(days=args.cooldown_days)
    else:
        print("::warning::Inspector dependency cooldown explicitly bypassed")
    config = load_inspector_version_config()

    try:
        latest_allowed = latest_version_in_constraint(
            config.package, config.constraint, exclude_newer=cutoff
        )
    except NoEligibleInspectorVersion:
        print("No eligible Inspector release; keeping the current lock.")
        latest_allowed = config.locked_version
    except urllib.error.URLError as exc:
        print(f"::warning title=npm lookup failed::{config.package}: {exc}")
        return 0

    lock_cutoff = cutoff
    if args.npm_lock and cutoff is not None:
        security_fix = npm_direct_security_fix(config.package, config.constraint)
        if security_fix is not None:
            fixed_version, published = security_fix
            if parse_version(fixed_version) > parse_version(latest_allowed):
                latest_allowed = fixed_version
                if published > cutoff:
                    lock_cutoff = published + timedelta(seconds=1)
                print(
                    "Security cooldown exception: "
                    f"{config.package} {fixed_version} "
                    f"(published {published.isoformat()})."
                )

    if parse_version(latest_allowed) <= parse_version(config.locked_version):
        print(f"No eligible Inspector upgrade; keeping {config.locked_version}.")
    else:
        write_locked_version(latest_allowed)
        print(
            f"Updated {config.package} lock: {config.locked_version} -> {latest_allowed}"
        )
    if args.npm_lock:
        pinned_config = load_inspector_version_config()
        effective_lock_cutoff = lock_cutoff_for_pinned_version(
            pinned_config.package,
            pinned_config.locked_version,
            lock_cutoff,
        )
        try:
            update_inspector_npm_lock(effective_lock_cutoff)
        except Exception:
            if latest_allowed != config.locked_version:
                write_locked_version(config.locked_version)
            raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
