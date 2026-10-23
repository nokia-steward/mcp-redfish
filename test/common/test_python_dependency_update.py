# Copyright 2025 Nokia
# Licensed under the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause

import subprocess
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import update_python_dependencies


def _entry(
    name: str,
    version: str,
    uploaded: str | None,
    marker: str | None = None,
) -> list[str]:
    upload_field = f', upload-time = "{uploaded}"' if uploaded is not None else ""
    lines = [
        "[[package]]",
        f'name = "{name}"',
        f'version = "{version}"',
        'source = { registry = "https://pypi.org/simple" }',
    ]
    if marker is not None:
        lines.append(f'resolution-markers = ["{marker}"]')
    lines.extend(
        [
            "wheels = [",
            (
                f'    {{ url = "https://example.test/{name}-{version}.whl", '
                f'hash = "sha256:test", size = 1{upload_field} }},'
            ),
            "]",
            "",
        ]
    )
    return lines


def _lock(packages: list[tuple[str, str, str | None, str | None]]) -> str:
    lines = ["version = 1", "revision = 5", 'requires-python = ">=3.13"', ""]
    for name, version, uploaded, marker in packages:
        lines.extend(_entry(name, version, uploaded, marker))
    return "\n".join(lines)


class TestPythonDependencyUpdate(unittest.TestCase):
    cutoff = datetime(2026, 10, 1, tzinfo=UTC)

    @staticmethod
    def _pyproject(directory: str, preserve: list[str]) -> Path:
        pyproject = Path(directory) / "pyproject.toml"
        values = ", ".join(f'"{value}"' for value in preserve)
        pyproject.write_text(
            f"[tool.dependency-updates]\npreserve = [{values}]\n",
            encoding="utf-8",
        )
        return pyproject

    def test_preserved_requirement_must_be_exact_and_match_lock(self) -> None:
        with TemporaryDirectory() as directory:
            lockfile = Path(directory) / "uv.lock"
            pyproject = self._pyproject(directory, ["example>=2"])
            lockfile.write_text(
                _lock([("example", "2.0.0", "2026-09-01T00:00:00Z", None)]),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "one exact == version"):
                update_python_dependencies.load_preserved(pyproject)

            pyproject.write_text(
                '[tool.dependency-updates]\npreserve = ["example==1.0.0"]\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "does not match uv.lock"):
                update_python_dependencies.validate_preserved(
                    update_python_dependencies.load_snapshot(lockfile),
                    update_python_dependencies.load_preserved(pyproject),
                )

    def test_one_resolution_excludes_preserved_package(self) -> None:
        with TemporaryDirectory() as directory:
            lockfile = Path(directory) / "uv.lock"
            pyproject = self._pyproject(directory, ["held==2.0.0"])
            lockfile.write_text(
                _lock(
                    [
                        ("eligible", "1.0.0", "2026-09-01T00:00:00Z", None),
                        ("held", "2.0.0", "2026-10-02T00:00:00Z", None),
                    ]
                ),
                encoding="utf-8",
            )

            def resolve(
                _lockfile, upgrade_packages, limits, _revision
            ) -> subprocess.CompletedProcess[str]:
                self.assertEqual(upgrade_packages, {"eligible"})
                self.assertEqual(set(limits), {"eligible"})
                lockfile.write_text(
                    _lock(
                        [
                            ("eligible", "1.1.0", "2026-09-15T00:00:00Z", None),
                            ("held", "2.0.0", "2026-10-02T00:00:00Z", None),
                        ]
                    ),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess([], 0, "", "")

            with patch("update_python_dependencies._resolve", side_effect=resolve):
                update_python_dependencies.update_dependencies(
                    lockfile, pyproject, self.cutoff, raised_cutoffs={}
                )

            self.assertIn(
                'name = "eligible"\nversion = "1.1.0"',
                lockfile.read_text(encoding="utf-8"),
            )

    def test_fresh_new_transitive_is_retried_with_cutoff(self) -> None:
        with TemporaryDirectory() as directory:
            lockfile = Path(directory) / "uv.lock"
            pyproject = self._pyproject(directory, [])
            lockfile.write_text(
                _lock([("eligible", "1.0.0", "2026-09-01T00:00:00Z", None)]),
                encoding="utf-8",
            )
            calls: list[set[str]] = []

            def resolve(
                _lockfile, _upgrade_packages, limits, _revision
            ) -> subprocess.CompletedProcess[str]:
                calls.append(set(limits))
                child_version, uploaded = (
                    ("2.0.0", "2026-10-02T00:00:00Z")
                    if "fresh-child" not in limits
                    else ("1.0.0", "2026-09-15T00:00:00Z")
                )
                lockfile.write_text(
                    _lock(
                        [
                            ("eligible", "1.1.0", "2026-09-15T00:00:00Z", None),
                            ("fresh-child", child_version, uploaded, None),
                        ]
                    ),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess([], 0, "", "")

            with patch("update_python_dependencies._resolve", side_effect=resolve):
                update_python_dependencies.update_dependencies(
                    lockfile, pyproject, self.cutoff, raised_cutoffs={}
                )

            self.assertEqual(calls, [{"eligible"}, {"eligible", "fresh-child"}])
            self.assertIn(
                'name = "fresh-child"\nversion = "1.0.0"',
                lockfile.read_text(encoding="utf-8"),
            )

    def test_resolution_that_changes_hold_is_restored_without_failure(self) -> None:
        with TemporaryDirectory() as directory:
            lockfile = Path(directory) / "uv.lock"
            pyproject = self._pyproject(directory, ["held==2.0.0"])
            original = _lock(
                [
                    ("eligible", "1.0.0", "2026-09-01T00:00:00Z", None),
                    ("held", "2.0.0", "2026-10-02T00:00:00Z", None),
                ]
            )
            lockfile.write_text(original, encoding="utf-8")

            def resolve(*_args) -> subprocess.CompletedProcess[str]:
                lockfile.write_text(
                    _lock(
                        [
                            ("eligible", "1.1.0", "2026-09-15T00:00:00Z", None),
                            ("held", "1.0.0", "2026-09-01T00:00:00Z", None),
                        ]
                    ),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess([], 0, "", "")

            with patch("update_python_dependencies._resolve", side_effect=resolve):
                update_python_dependencies.update_dependencies(
                    lockfile, pyproject, self.cutoff, raised_cutoffs={}
                )

            self.assertEqual(lockfile.read_text(encoding="utf-8"), original)

    def test_remove_persisted_cutoff_restores_revision(self) -> None:
        with TemporaryDirectory() as directory:
            lockfile = Path(directory) / "uv.lock"
            lockfile.write_text(
                "version = 1\nrevision = 3\n\n[options]\n\n"
                "[options.exclude-newer-package]\n"
                'example = "2026-10-01T00:00:00Z"\n\n'
                + "\n".join(_entry("example", "1.0.0", "2026-09-01T00:00:00Z")),
                encoding="utf-8",
            )
            update_python_dependencies.remove_persisted_cutoff(lockfile, 5)
            result = lockfile.read_text(encoding="utf-8")
            self.assertNotIn("[options", result)
            self.assertIn("revision = 5", result)

    def test_security_fix_cutoff_stops_at_oldest_complete_fix(self) -> None:
        cutoff = datetime(2026, 10, 14, tzinfo=UTC)
        certifi_uploaded = datetime(2026, 10, 20, 2, 24, 15, tzinfo=UTC)
        virtualenv_uploaded = datetime(2026, 9, 1, tzinfo=UTC)
        audit = {
            "vulnerabilities": [
                {
                    "dependency": {"name": "certifi", "version": "2026.7.22"},
                    "id": "PYSEC-2026-4320",
                    "fix_versions": ["2026.10.19"],
                },
                {
                    "dependency": {"name": "Certifi", "version": "2026.7.22"},
                    "id": "GHSA-6q48-6c7w-9gw2",
                    "fix_versions": ["2026.10.18", "2026.10.19"],
                },
                {
                    "dependency": {"name": "virtualenv", "version": "21.14.7"},
                    "id": "GHSA-virtualenv",
                    "fix_versions": ["21.14.9"],
                },
                {
                    "dependency": {"name": "held", "version": "1.0.0"},
                    "id": "GHSA-held",
                    "fix_versions": ["1.2.0"],
                },
                {
                    "dependency": {"name": "unfixed", "version": "1.0.0"},
                    "id": "GHSA-unfixed",
                    "fix_versions": [],
                },
            ]
        }

        def upload_time(name: str, version: object) -> datetime:
            if name == "certifi" and str(version) == "2026.10.19":
                return certifi_uploaded
            if name == "virtualenv":
                return virtualenv_uploaded
            if name == "held":
                return datetime(2026, 10, 20, tzinfo=UTC)
            raise AssertionError(f"unexpected lookup {name}=={version}")

        preserved = update_python_dependencies.load_preserved(
            self._pyproject_with_hold()
        )
        with patch("sys.stdout"):
            raised = update_python_dependencies.security_fix_cutoffs(
                Path("unused"),
                cutoff,
                preserved,
                audit_document=audit,
                upload_time=upload_time,
            )
        self.assertEqual(raised, {"certifi": certifi_uploaded + timedelta(seconds=1)})

    def _pyproject_with_hold(self) -> Path:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return self._pyproject(directory.name, ["held==1.0.0"])

    def test_security_fix_accepts_only_that_fresh_release(self) -> None:
        with TemporaryDirectory() as directory:
            lockfile = Path(directory) / "uv.lock"
            pyproject = self._pyproject(directory, [])
            lockfile.write_text(
                _lock([("certifi", "2026.7.22", "2026-07-22T00:00:00Z", None)]),
                encoding="utf-8",
            )
            raised = datetime(2026, 10, 20, 2, 24, 16, tzinfo=UTC)
            calls: list[dict[str, datetime]] = []

            def resolve(
                _lockfile, _upgrade_packages, limits, _revision
            ) -> subprocess.CompletedProcess[str]:
                calls.append(dict(limits))
                lockfile.write_text(
                    _lock([("certifi", "2026.10.19", "2026-10-20T02:24:15Z", None)]),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess([], 0, "", "")

            with patch("update_python_dependencies._resolve", side_effect=resolve):
                update_python_dependencies.update_dependencies(
                    lockfile,
                    pyproject,
                    self.cutoff,
                    raised_cutoffs={"certifi": raised},
                )

            self.assertEqual(calls, [{"certifi": raised}])
            self.assertIn(
                'name = "certifi"\nversion = "2026.10.19"',
                lockfile.read_text(encoding="utf-8"),
            )

    def test_unresolvable_security_fix_falls_back_to_cutoff(self) -> None:
        with TemporaryDirectory() as directory:
            lockfile = Path(directory) / "uv.lock"
            pyproject = self._pyproject(directory, [])
            original = _lock([("certifi", "2026.7.22", "2026-07-22T00:00:00Z", None)])
            lockfile.write_text(original, encoding="utf-8")
            raised = datetime(2026, 10, 20, 2, 24, 16, tzinfo=UTC)
            calls: list[datetime] = []

            def resolve(
                _lockfile, _upgrade_packages, limits, _revision
            ) -> subprocess.CompletedProcess[str]:
                calls.append(limits["certifi"])
                if limits["certifi"] > self.cutoff:
                    return subprocess.CompletedProcess([], 1, "", "No solution found")
                lockfile.write_text(
                    _lock([("certifi", "2026.8.1", "2026-09-01T00:00:00Z", None)]),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess([], 0, "", "")

            with patch("update_python_dependencies._resolve", side_effect=resolve):
                update_python_dependencies.update_dependencies(
                    lockfile,
                    pyproject,
                    self.cutoff,
                    raised_cutoffs={"certifi": raised},
                )

            self.assertEqual(calls, [raised, self.cutoff])
            self.assertIn(
                'name = "certifi"\nversion = "2026.8.1"',
                lockfile.read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
