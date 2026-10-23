# Copyright 2025 Nokia
# Licensed under the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class TestDependencyPolicyFiles(unittest.TestCase):
    def test_uv_pin_has_one_source(self) -> None:
        pin = (ROOT / ".uv-version").read_text(encoding="utf-8").strip()
        self.assertRegex(pin, r"^[0-9]+\.[0-9]+\.[0-9]+$")
        action = (ROOT / ".github/actions/install-uv/action.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn(".uv-version", action)
        for name in ("Dockerfile", "Dockerfile.docker"):
            dockerfile = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("COPY .uv-version /tmp/.uv-version", dockerfile)
            self.assertNotIn(f"UV_VERSION={pin}", dockerfile)
        for name in ("ci-cd.yml", "dependency-updates.yml"):
            workflow = (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
            self.assertIn("uses: ./.github/actions/install-uv", workflow)
            self.assertNotIn("astral.sh/uv/", workflow)
            self.assertNotIn(f'UV_VERSION: "{pin}"', workflow)

    def test_indicator_job_does_not_query_uv_twice(self) -> None:
        workflow = (ROOT / ".github/workflows/dependency-updates.yml").read_text(
            encoding="utf-8"
        )
        indicator = workflow.split("update-dependencies:", 1)[0]
        self.assertEqual(indicator.count("check_uv_version.py"), 0)
        self.assertEqual(indicator.count("manage_major_version_indicator_issue.py"), 1)

    def test_security_checks_fail_closed(self) -> None:
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn("uv audit --frozen --preview-features audit-command", makefile)
        self.assertIn("npm audit --prefix e2e --package-lock-only", makefile)
        workflow = (ROOT / ".github/workflows/ci-cd.yml").read_text(encoding="utf-8")
        security_job = workflow.split("name: Security Scan", 1)[1].split(
            "name: Container Build", 1
        )[0]
        self.assertNotIn("continue-on-error", security_job)
        self.assertIn('node-version: "24"', security_job)

    def test_dependabot_uv_entry_is_security_updates_only(self) -> None:
        config = (ROOT / ".github/dependabot.yml").read_text(encoding="utf-8")
        self.assertIn("Security updates only", config)
        self.assertIn('package-ecosystem: "uv"', config)
        self.assertIn("open-pull-requests-limit: 0", config)

    def test_dependency_prs_prefer_a_token_that_can_trigger_ci(self) -> None:
        workflow = (ROOT / ".github/workflows/dependency-updates.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("secrets.PAT_TOKEN || secrets.GITHUB_TOKEN", workflow)

    def test_python_cooldown_preserves_accepted_lock_versions(self) -> None:
        workflow = (ROOT / ".github/workflows/dependency-updates.yml").read_text(
            encoding="utf-8"
        )
        update_job = workflow.split("update-dependencies:", 1)[1]
        self.assertIn("scripts/update_python_dependencies.py", update_job)
        self.assertIn("DEPENDENCY_EXCLUDE_NEWER", update_job)
        python_updater = (ROOT / "scripts/update_python_dependencies.py").read_text(
            encoding="utf-8"
        )
        inspector_updater = (ROOT / "scripts/update_inspector_version.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("Security cooldown exception:", python_updater)
        self.assertIn("Security cooldown exception:", inspector_updater)
        self.assertIn("uv audit", python_updater)
        self.assertNotIn("UV_EXCLUDE_NEWER", update_job)
        self.assertNotIn("uv lock --upgrade\n", update_job)
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn("[tool.dependency-updates]", pyproject)
        self.assertIn('"packaging>=24"', pyproject)
