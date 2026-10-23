# Copyright 2025 Nokia
# Licensed under the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause

"""Load and resolve the pinned MCP Inspector CLI version for e2e tooling."""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "inspector-version.toml"
LOCK_PATH = Path(__file__).parent / "inspector-version.lock"
NPM_REGISTRY_URL = "https://registry.npmjs.org/{package}"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


class NoEligibleInspectorVersion(ValueError):
    pass


@dataclass(frozen=True)
class InspectorVersionConfig:
    package: str
    constraint: str
    locked_version: str

    @property
    def package_spec(self) -> str:
        return f"{self.package}@{self.locked_version}"


def parse_version(version: str) -> tuple[int, int, int]:
    match = VERSION_RE.match(version)
    if not match:
        raise ValueError(f"Unsupported version format: {version}")
    major, minor, patch = (int(part) for part in version.split("."))
    return major, minor, patch


def parse_constraint(constraint: str) -> tuple[tuple[int, int, int], int]:
    minimum_match = re.search(r">=(\d+)\.(\d+)\.(\d+)", constraint)
    maximum_major_match = re.search(r",<(\d+)", constraint)
    if minimum_match is None or maximum_major_match is None:
        raise ValueError(
            f"Inspector constraint must use '>=X.Y.Z,<N' format, got: {constraint!r}"
        )
    minimum = (
        int(minimum_match.group(1)),
        int(minimum_match.group(2)),
        int(minimum_match.group(3)),
    )
    maximum_major = int(maximum_major_match.group(1))
    return minimum, maximum_major


def version_satisfies_constraint(version: str, constraint: str) -> bool:
    minimum, maximum_major = parse_constraint(constraint)
    major, minor, patch = parse_version(version)
    if major >= maximum_major:
        return False
    return (major, minor, patch) >= minimum


def load_inspector_version_config() -> InspectorVersionConfig:
    with CONFIG_PATH.open("rb") as handle:
        config = tomllib.load(handle)
    locked_version = LOCK_PATH.read_text(encoding="utf-8").strip()
    return InspectorVersionConfig(
        package=config["package"],
        constraint=config["constraint"],
        locked_version=locked_version,
    )


def load_inspector_package_spec() -> str:
    return load_inspector_version_config().package_spec


def write_locked_version(version: str) -> None:
    parse_version(version)
    LOCK_PATH.write_text(f"{version}\n", encoding="utf-8")


def direct_security_fix_version(
    document: dict, package: str, constraint: str
) -> str | None:
    """Return the newest in-range direct fix reported by one npm audit document."""
    vulnerabilities = document.get("vulnerabilities", {})
    if not isinstance(vulnerabilities, dict):
        raise ValueError("npm audit JSON is missing vulnerabilities")
    versions: list[str] = []
    for entry in vulnerabilities.values():
        if not isinstance(entry, dict):
            continue
        available = entry.get("fixAvailable")
        if not isinstance(available, dict) or available.get("name") != package:
            continue
        version = available.get("version")
        if not isinstance(version, str) or not VERSION_RE.match(version):
            continue
        if not version_satisfies_constraint(version, constraint):
            continue
        versions.append(version)
    if not versions:
        return None
    return sorted(versions, key=parse_version)[-1]


def npm_audit_document(prefix: Path) -> dict:
    """Return npm audit JSON, including when vulnerabilities make it exit non-zero."""
    completed = subprocess.run(
        ["npm", "audit", "--json", "--package-lock-only", "--prefix", str(prefix)],
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            "npm audit did not return JSON, so security cooldown exceptions "
            "were not evaluated"
        ) from error
    if not isinstance(document, dict):
        raise RuntimeError("npm audit JSON was not an object")
    return document


def npm_version_publication_time(package: str, version: str) -> datetime:
    timestamp = fetch_npm_metadata(package).get("time", {}).get(version)
    if not isinstance(timestamp, str):
        raise RuntimeError(f"{package}@{version} has no npm publication time")
    try:
        published = datetime.fromisoformat(timestamp)
    except ValueError as error:
        raise RuntimeError(
            f"{package}@{version} has an invalid npm publication time"
        ) from error
    if published.utcoffset() is None:
        raise RuntimeError(
            f"{package}@{version} has no timezone-aware publication time"
        )
    return published


def lock_cutoff_for_pinned_version(
    package: str, locked_version: str, cutoff: datetime | None
) -> datetime | None:
    """Widen npm --before so an already-pinned direct version can be resolved."""
    if cutoff is None:
        return None
    published = npm_version_publication_time(package, locked_version)
    if published > cutoff:
        return published + timedelta(seconds=1)
    return cutoff


def npm_direct_security_fix(
    package: str, constraint: str
) -> tuple[str, datetime] | None:
    """Return an in-range direct vulnerability fix and its publication time."""
    version = direct_security_fix_version(
        npm_audit_document(CONFIG_PATH.parent), package, constraint
    )
    if version is None:
        return None
    published = npm_version_publication_time(package, version)
    return version, published


def update_inspector_npm_lock(exclude_newer: datetime | None) -> None:
    config = load_inspector_version_config()
    manifest_path = CONFIG_PATH.parent / "package.json"
    lock_path = CONFIG_PATH.parent / "package-lock.json"
    original_manifest = manifest_path.read_bytes()
    original_lock = lock_path.read_bytes() if lock_path.exists() else None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["dependencies"][config.package] = config.locked_version
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    command = [
        "npm",
        "update",
        "--prefix",
        str(CONFIG_PATH.parent),
        "--package-lock-only",
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
    ]
    if exclude_newer is not None:
        command.extend(["--before", exclude_newer.isoformat()])
    try:
        subprocess.run(command, check=True)
        if exclude_newer is not None:
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            metadata_by_package: dict[str, dict] = {}
            for path, entry in lock["packages"].items():
                if not path:
                    continue
                package = entry.get("name") or path.rsplit("node_modules/", 1)[-1]
                if package not in metadata_by_package:
                    metadata_by_package[package] = fetch_npm_metadata(package)
                timestamp = (
                    metadata_by_package[package].get("time", {}).get(entry["version"])
                )
                if not isinstance(timestamp, str):
                    raise ValueError("npm lock contains an unknown publication time")
                published = datetime.fromisoformat(timestamp)
                if published.utcoffset() is None or published > exclude_newer:
                    raise ValueError("npm lock does not satisfy the publication cutoff")
    except Exception:
        manifest_path.write_bytes(original_manifest)
        if original_lock is None:
            lock_path.unlink(missing_ok=True)
        else:
            lock_path.write_bytes(original_lock)
        raise


def fetch_npm_metadata(package: str) -> dict:
    encoded_package = package.removeprefix("@").replace("/", "%2F")
    if package.startswith("@"):
        encoded_package = f"@{encoded_package}"
    request = urllib.request.Request(
        NPM_REGISTRY_URL.format(package=encoded_package),
        headers={"Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("Expected npm package metadata")
    return payload


def fetch_npm_versions(
    package: str, *, exclude_newer: datetime | None = None
) -> list[str]:
    if exclude_newer is not None and exclude_newer.utcoffset() is None:
        raise ValueError("Inspector cutoff must include a timezone")
    payload = fetch_npm_metadata(package)
    versions = []
    for version in payload.get("versions", {}):
        if not VERSION_RE.match(version):
            continue
        if exclude_newer is not None:
            timestamp = payload.get("time", {}).get(version)
            if not isinstance(timestamp, str):
                continue
            try:
                published = datetime.fromisoformat(timestamp)
            except ValueError:
                continue
            if published.utcoffset() is None or published > exclude_newer:
                continue
        versions.append(version)
    return sorted(versions, key=parse_version)


def latest_version_on_npm(package: str) -> str:
    versions = fetch_npm_versions(package)
    if not versions:
        raise ValueError(f"No semver releases found for {package}")
    return versions[-1]


def latest_version_in_constraint(
    package: str, constraint: str, *, exclude_newer: datetime | None = None
) -> str:
    versions = (
        fetch_npm_versions(package, exclude_newer=exclude_newer)
        if exclude_newer is not None
        else fetch_npm_versions(package)
    )
    matching = sorted(
        (
            version
            for version in versions
            if version_satisfies_constraint(version, constraint)
        ),
        key=parse_version,
    )
    if not matching:
        if exclude_newer is not None:
            raise NoEligibleInspectorVersion(
                "No Inspector releases satisfy the constraint and publication cutoff"
            )
        raise ValueError(
            f"No npm releases for {package} satisfy constraint {constraint}"
        )
    return matching[-1]


def major_version(version: str) -> int:
    return parse_version(version)[0]
