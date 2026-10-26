#!/usr/bin/env python3
# Copyright 2025 Nokia
# Licensed under the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause

"""Upgrade uv packages while honoring explicit exact-version holds.

A locked package with a known vulnerability may move to the oldest release
that fixes every reported advisory, even when that release is younger than
the publication cutoff. Later releases of that package, and packages that
are not part of the fix, stay on the cutoff.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

_REVISION_PATTERN = re.compile(r"(?m)^revision = (\d+)\s*$")
_UNSATISFIABLE_RESOLUTION = "no solution found"


@dataclass(frozen=True)
class Artifact:
    url: str
    uploaded: datetime | None


@dataclass(frozen=True)
class LockEntry:
    version: Version
    markers: frozenset[str]
    artifacts: frozenset[Artifact]


Snapshot = dict[str, tuple[LockEntry, ...]]
Preserved = dict[str, Requirement]


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


def _format_cutoff(value: datetime) -> str:
    utc_value = value.astimezone(UTC)
    if utc_value.microsecond:
        return utc_value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return utc_value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _revision(lock_text: str) -> int:
    match = _REVISION_PATTERN.search(lock_text)
    if match is None:
        raise ValueError("uv.lock is missing its revision")
    return int(match.group(1))


def load_snapshot(lockfile: Path) -> Snapshot:
    """Read registry package versions, forks, and artifact timestamps."""
    document = tomllib.loads(lockfile.read_text(encoding="utf-8"))
    snapshot: dict[str, list[LockEntry]] = {}
    for package in document.get("package", []):
        source = package.get("source", {})
        if not isinstance(source, dict) or "registry" not in source:
            continue
        name = canonicalize_name(str(package["name"]))
        raw_markers = package.get("resolution-markers", [])
        if not isinstance(raw_markers, list) or not all(
            isinstance(marker, str) for marker in raw_markers
        ):
            raise ValueError(f"{name} has invalid resolution markers")
        artifacts: set[Artifact] = set()
        for distribution in [package.get("sdist"), *package.get("wheels", [])]:
            if not isinstance(distribution, dict):
                continue
            artifacts.add(
                Artifact(
                    url=str(distribution.get("url", "")),
                    uploaded=_timestamp(distribution.get("upload-time")),
                )
            )
        snapshot.setdefault(name, []).append(
            LockEntry(
                version=Version(str(package["version"])),
                markers=frozenset(raw_markers),
                artifacts=frozenset(artifacts),
            )
        )
    return {
        name: tuple(
            sorted(entries, key=lambda entry: (entry.version, sorted(entry.markers)))
        )
        for name, entries in snapshot.items()
    }


def load_preserved(pyproject: Path) -> Preserved:
    """Load exact package holds from tool.dependency-updates.preserve."""
    document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    raw = document.get("tool", {}).get("dependency-updates", {}).get("preserve", [])
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ValueError("tool.dependency-updates.preserve must be a list of strings")
    preserved: Preserved = {}
    for item in raw:
        try:
            requirement = Requirement(item)
        except InvalidRequirement as error:
            raise ValueError(f"invalid preserved dependency: {item}") from error
        specifiers = list(requirement.specifier)
        if (
            requirement.url is not None
            or requirement.extras
            or requirement.marker is not None
            or len(specifiers) != 1
            or specifiers[0].operator != "=="
            or "*" in specifiers[0].version
        ):
            raise ValueError(
                f"preserved dependency must use one exact == version: {item}"
            )
        name = canonicalize_name(requirement.name)
        if name in preserved:
            raise ValueError(f"duplicate preserved dependency: {name}")
        preserved[name] = requirement
    return preserved


def validate_preserved(snapshot: Snapshot, preserved: Preserved) -> None:
    """Ensure every configured hold exists at its exact locked version."""
    for name, requirement in preserved.items():
        entries = snapshot.get(name)
        if not entries:
            raise ValueError(f"preserved dependency is not in uv.lock: {name}")
        if any(entry.version not in requirement.specifier for entry in entries):
            versions = ", ".join(str(entry.version) for entry in entries)
            raise ValueError(
                f"preserved dependency {requirement} does not match uv.lock: {versions}"
            )


def fresh_packages(
    before: Snapshot,
    after: Snapshot,
    cutoff: datetime,
    limits: Mapping[str, datetime],
) -> set[str]:
    """Return packages that gained an artifact newer than their limit, or undated."""
    fresh: set[str] = set()
    for name, entries in sorted(after.items()):
        limit = limits.get(name, cutoff)
        old_entries = before.get(name, ())
        for entry in entries:
            previous = next(
                (
                    old
                    for old in old_entries
                    if old.version == entry.version and old.markers == entry.markers
                ),
                None,
            )
            added = (
                entry.artifacts
                if previous is None
                else entry.artifacts - previous.artifacts
            )
            undated_new_release = previous is None and not entry.artifacts
            fresh_or_undated_artifact = any(
                artifact.uploaded is None or artifact.uploaded > limit
                for artifact in added
            )
            if undated_new_release or fresh_or_undated_artifact:
                fresh.add(name)
    return fresh


def remove_persisted_cutoff(lockfile: Path, revision: int) -> None:
    """Remove cutoff metadata uv persists, without changing selected versions."""
    output: list[str] = []
    skipping = False
    for line in lockfile.read_text(encoding="utf-8").splitlines(keepends=True):
        if _REVISION_PATTERN.match(line):
            output.append(f"revision = {revision}\n")
            continue
        if line.strip() == "[options]" or line.startswith("[options."):
            skipping = True
            continue
        if skipping:
            if line.startswith("[["):
                skipping = False
                if output and output[-1].strip():
                    output.append("\n")
                output.append(line)
            continue
        output.append(line)
    lockfile.write_text("".join(output), encoding="utf-8")


def _is_unsatisfiable(stderr: str) -> bool:
    return _UNSATISFIABLE_RESOLUTION in stderr.casefold()


def _restore(lockfile: Path, contents: bytes) -> None:
    lockfile.write_bytes(contents)


def _resolve(
    lockfile: Path,
    upgrade_packages: set[str],
    limits: dict[str, datetime],
    revision: int,
) -> subprocess.CompletedProcess[str]:
    command = ["uv", "lock"]
    for package in sorted(upgrade_packages):
        command.extend(["--upgrade-package", package])
    for limited_package, limit in sorted(limits.items()):
        command.extend(
            ["--exclude-newer-package", f"{limited_package}={_format_cutoff(limit)}"]
        )
    environment = os.environ.copy()
    environment.pop("UV_EXCLUDE_NEWER", None)
    completed = subprocess.run(
        command,
        cwd=lockfile.parent,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode == 0 and lockfile.is_file():
        remove_persisted_cutoff(lockfile, revision)
    return completed


@dataclass(frozen=True)
class _Advisory:
    identifier: str
    locked: Version
    fixes: tuple[Version, ...]


def _audit_document(lockfile: Path) -> dict[str, object]:
    """Return uv audit JSON, including when vulnerabilities make it exit non-zero."""
    completed = subprocess.run(
        [
            "uv",
            "audit",
            "--frozen",
            "--preview-features",
            "audit-command",
            "--preview-features",
            "json-output",
            "--output-format",
            "json",
        ],
        cwd=lockfile.parent,
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            "uv audit did not return JSON, so security cooldown exceptions "
            "were not evaluated"
        ) from error
    if not isinstance(document, dict):
        raise RuntimeError("uv audit JSON was not an object")
    return document


def _fixes_by_package(document: Mapping[str, object]) -> dict[str, list[_Advisory]]:
    vulnerabilities = document.get("vulnerabilities", [])
    if not isinstance(vulnerabilities, list):
        raise ValueError("uv audit JSON is missing vulnerabilities")
    grouped: dict[str, list[_Advisory]] = {}
    for entry in vulnerabilities:
        if not isinstance(entry, dict):
            raise ValueError("uv audit vulnerability entry is not an object")
        dependency = entry.get("dependency")
        raw_fixes = entry.get("fix_versions", [])
        if not isinstance(dependency, dict) or not isinstance(raw_fixes, list):
            raise ValueError("uv audit vulnerability is missing fix metadata")
        name = canonicalize_name(str(dependency.get("name", "")))
        try:
            locked = Version(str(dependency["version"]))
            fixes = tuple(Version(str(item)) for item in raw_fixes)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"uv audit returned an unusable version for {name}"
            ) from error
        grouped.setdefault(name, []).append(
            _Advisory(str(entry.get("id", "unknown")), locked, fixes)
        )
    return grouped


def _minimum_complete_fix(
    advisories: list[_Advisory],
) -> tuple[Version, Version, tuple[str, ...]] | None:
    """Return the locked version, the oldest complete fix, and advisory ids."""
    if any(not advisory.fixes for advisory in advisories):
        return None
    locked = max(advisory.locked for advisory in advisories)
    candidates = sorted(
        {fix for advisory in advisories for fix in advisory.fixes if fix > locked}
    )
    for candidate in candidates:
        if all(
            any(fix <= candidate for fix in advisory.fixes) for advisory in advisories
        ):
            identifiers = tuple(
                sorted({advisory.identifier for advisory in advisories})
            )
            return locked, candidate, identifiers
    return None


def pypi_upload_time(name: str, version: Version) -> datetime:
    """Return the newest artifact upload time for one PyPI release."""
    quoted = urllib.parse.quote(name)
    request = urllib.request.Request(
        f"https://pypi.org/pypi/{quoted}/{version}/json",
        headers={"Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError(f"{name}=={version} PyPI metadata was not an object")
    urls = payload.get("urls", [])
    if not isinstance(urls, list):
        raise ValueError(f"{name}=={version} PyPI metadata has no files")
    uploaded: list[datetime] = []
    for distribution in urls:
        if not isinstance(distribution, dict) or distribution.get("yanked"):
            continue
        timestamp = _timestamp(distribution.get("upload_time_iso_8601"))
        if timestamp is not None:
            uploaded.append(timestamp)
    if not uploaded:
        raise ValueError(f"{name}=={version} has no PyPI upload time")
    return max(uploaded)


def security_fix_cutoffs(
    lockfile: Path,
    cutoff: datetime,
    preserved: Preserved,
    *,
    audit_document: Mapping[str, object] | None = None,
    upload_time: Callable[[str, Version], datetime] | None = None,
) -> dict[str, datetime]:
    """Raise cutoffs to one second after the oldest complete vulnerability fix."""
    if audit_document is None:
        audit_document = _audit_document(lockfile)
    if upload_time is None:
        upload_time = pypi_upload_time
    raised: dict[str, datetime] = {}
    for name, advisories in sorted(_fixes_by_package(audit_document).items()):
        selected = _minimum_complete_fix(advisories)
        if selected is None:
            continue
        locked, fix, identifiers = selected
        if name in preserved:
            print(
                f"Preserved package {name} has a known vulnerability; "
                "the hold blocks the security update."
            )
            continue
        try:
            uploaded = upload_time(name, fix)
        except (
            OSError,
            urllib.error.URLError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            raise RuntimeError(
                f"could not read the publication time for {name}=={fix}"
            ) from error
        if uploaded.utcoffset() is None:
            raise RuntimeError(f"{name}=={fix} has no timezone-aware publication time")
        limit = uploaded + timedelta(seconds=1)
        if limit <= cutoff:
            continue
        raised[name] = limit
        print(
            "Security cooldown exception: "
            f"{name} {locked} -> {fix} "
            f"(published {_format_cutoff(uploaded)}; "
            f"fixes {', '.join(identifiers)})."
        )
    return raised


def _resolve_until_stable(
    lockfile: Path,
    before: Snapshot,
    before_bytes: bytes,
    preserved: Preserved,
    upgrade_packages: set[str],
    limits: dict[str, datetime],
    revision: int,
    cutoff: datetime | None,
) -> bool:
    """Apply one cutoff map. Return false when the attempt is discarded."""
    while True:
        completed = _resolve(lockfile, upgrade_packages, limits, revision)
        if completed.returncode != 0:
            _restore(lockfile, before_bytes)
            if _is_unsatisfiable(completed.stderr):
                print(
                    "Kept existing lock: no eligible resolution satisfies the policy."
                )
                return False
            completed.check_returncode()

        try:
            after = load_snapshot(lockfile)
        except (OSError, UnicodeError, tomllib.TOMLDecodeError, ValueError):
            _restore(lockfile, before_bytes)
            raise

        changed_holds = {
            name for name in preserved if after.get(name) != before.get(name)
        }
        if changed_holds:
            _restore(lockfile, before_bytes)
            names = ", ".join(sorted(changed_holds))
            print(
                "Kept existing lock: resolution would change preserved "
                f"package(s): {names}."
            )
            return False

        if cutoff is None:
            break
        fresh = fresh_packages(before, after, cutoff, limits)
        if not fresh:
            break
        new_limits = fresh - limits.keys()
        if not new_limits:
            _restore(lockfile, before_bytes)
            names = ", ".join(sorted(fresh))
            print(
                "Kept existing lock: resolver retained fresh or undated "
                f"artifact(s) for limited package(s): {names}."
            )
            return False
        limits.update(dict.fromkeys(sorted(new_limits), cutoff))
        _restore(lockfile, before_bytes)

    if lockfile.read_bytes() == before_bytes:
        print("Python dependencies are already current under the update policy.")
    else:
        print(
            "Python dependency update complete; preserved packages: "
            + (", ".join(sorted(preserved)) if preserved else "none")
            + "."
        )
    return True


def update_dependencies(
    lockfile: Path,
    pyproject: Path,
    cutoff: datetime | None,
    raised_cutoffs: Mapping[str, datetime] | None = None,
) -> None:
    """Update all unheld packages in one resolution."""
    before_bytes = lockfile.read_bytes()
    revision = _revision(before_bytes.decode("utf-8"))
    before = load_snapshot(lockfile)
    preserved = load_preserved(pyproject)
    validate_preserved(before, preserved)
    upgrade_packages = set(before) - preserved.keys()
    if cutoff is None:
        _resolve_until_stable(
            lockfile,
            before,
            before_bytes,
            preserved,
            upgrade_packages,
            {},
            revision,
            None,
        )
        return

    if raised_cutoffs is None:
        raised_cutoffs = security_fix_cutoffs(lockfile, cutoff, preserved)
    limits = dict.fromkeys(sorted(upgrade_packages), cutoff)
    for name, moment in raised_cutoffs.items():
        if name in limits and moment > cutoff:
            limits[name] = moment
    applied = _resolve_until_stable(
        lockfile,
        before,
        before_bytes,
        preserved,
        upgrade_packages,
        limits,
        revision,
        cutoff,
    )
    if applied or not any(moment > cutoff for moment in limits.values()):
        return
    print(
        "Could not apply the security cooldown exception with the rest of "
        "the update policy; retrying with the publication cutoff only."
    )
    _restore(lockfile, before_bytes)
    targeted = sorted(raised_cutoffs)
    if targeted:
        print(
            "Retrying security fixes for affected package(s) only: "
            + ", ".join(targeted)
            + "."
        )
        if _resolve_until_stable(
            lockfile,
            before,
            before_bytes,
            preserved,
            set(targeted),
            dict(raised_cutoffs),
            revision,
            None,
        ):
            return
        _restore(lockfile, before_bytes)
    _resolve_until_stable(
        lockfile,
        before,
        before_bytes,
        preserved,
        upgrade_packages,
        dict.fromkeys(sorted(upgrade_packages), cutoff),
        revision,
        cutoff,
    )


def _cutoff(value: str) -> datetime:
    try:
        cutoff = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "--exclude-newer must be an ISO 8601 timestamp"
        ) from error
    if cutoff.utcoffset() is None:
        raise argparse.ArgumentTypeError("--exclude-newer must include a timezone")
    return cutoff


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exclude-newer", type=_cutoff)
    parser.add_argument("--lockfile", type=Path, default=Path("uv.lock"))
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    args = parser.parse_args(argv)

    lockfile = args.lockfile.resolve()
    update_dependencies(lockfile, args.pyproject.resolve(), args.exclude_newer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
