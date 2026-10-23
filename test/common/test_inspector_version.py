# Copyright 2025 Nokia
# Licensed under the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause

import io
import json
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import check_uv_version

from e2e.inspector_version import (
    InspectorVersionConfig,
    NoEligibleInspectorVersion,
    direct_security_fix_version,
    fetch_npm_versions,
    latest_version_in_constraint,
    load_inspector_version_config,
    parse_constraint,
    update_inspector_npm_lock,
    version_satisfies_constraint,
)


class TestUvVersionIndicator(unittest.TestCase):
    def test_malformed_metadata_is_rejected(self) -> None:
        for payload in (
            [],
            {},
            {"tag_name": 123},
            {"tag_name": "invalid"},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    check_uv_version.parse_uv_release(payload)

    def test_drafts_and_prereleases_are_excluded(self) -> None:
        for flag in ("draft", "prerelease"):
            with self.subTest(flag=flag):
                self.assertIsNone(
                    check_uv_version.parse_uv_release(
                        {"tag_name": "0.14.0", flag: True}
                    )
                )

    @patch("check_uv_version.urllib.request.urlopen")
    def test_fetch_stable_release(self, urlopen) -> None:
        payload = {"tag_name": "v0.12.8", "published_at": "2026-10-01T00:00:00Z"}
        urlopen.return_value = io.BytesIO(json.dumps([payload]).encode())
        self.assertEqual(
            check_uv_version.fetch_uv_releases(),
            [("0.12.8", datetime(2026, 10, 1, tzinfo=UTC))],
        )

    def test_missing_or_invalid_publication_time(self) -> None:
        for timestamp in (None, "invalid", "2026-10-01"):
            with self.subTest(timestamp=timestamp):
                payload = {"tag_name": "0.12.8", "published_at": timestamp}
                self.assertEqual(
                    check_uv_version.parse_uv_release(payload), ("0.12.8", None)
                )

    @patch("check_uv_version.fetch_uv_releases")
    @patch("check_uv_version.datetime")
    def test_summary_cooldown_boundary(self, clock, fetch_release) -> None:
        clock.now.return_value = datetime(2026, 10, 8, tzinfo=UTC)
        for latest, published, status in (
            ("0.12.8", datetime(2026, 10, 1, tzinfo=UTC), "Cooldown complete"),
            (
                "0.12.8",
                datetime(2026, 10, 1, 0, 0, 1, tzinfo=UTC),
                "Within the 7-day cooldown",
            ),
            ("0.12.8", None, "Publication time unknown"),
            ("0.12.7", None, "No newer stable release"),
            ("0.12.6", None, "No newer stable release"),
        ):
            with (
                self.subTest(latest=latest, published=published),
                TemporaryDirectory() as directory,
            ):
                fetch_release.return_value = [(latest, published)]
                summary = Path(directory) / "summary.md"
                self.assertEqual(
                    check_uv_version.main(
                        ["--pinned-version", "0.12.7", "--summary-file", str(summary)]
                    ),
                    0,
                )
                self.assertIn(status, summary.read_text())

    @patch("check_uv_version.fetch_uv_releases", side_effect=ValueError)
    def test_lookup_failure_reports_unknown(self, _fetch_release) -> None:
        with TemporaryDirectory() as directory:
            summary = Path(directory) / "summary.md"
            self.assertEqual(
                check_uv_version.main(
                    ["--pinned-version", "0.12.7", "--summary-file", str(summary)]
                ),
                0,
            )
            self.assertIn("Lookup failed", summary.read_text())

    @patch("check_uv_version.urllib.request.urlopen")
    @patch("check_uv_version.datetime")
    def test_eligible_upgrade_is_found_on_later_page(self, clock, urlopen) -> None:
        clock.now.return_value = datetime(2026, 10, 13, tzinfo=UTC)
        first_page = [
            {"tag_name": "0.14.0", "published_at": "2026-10-12T00:00:00Z"}
        ] * 100
        second_page = [
            {"tag_name": "0.13.0", "published_at": "2026-10-06T00:00:00Z"},
            {"tag_name": "0.12.9", "published_at": "2026-10-01T00:00:00Z"},
            {"tag_name": "0.13.9", "published_at": None},
            {"tag_name": "0.16.0", "prerelease": True},
        ]
        urlopen.side_effect = [
            io.BytesIO(json.dumps(first_page).encode()),
            io.BytesIO(json.dumps(second_page).encode()),
        ]
        clock.fromisoformat.side_effect = datetime.fromisoformat
        with TemporaryDirectory() as directory:
            summary = Path(directory) / "summary.md"
            self.assertEqual(
                check_uv_version.main(
                    ["--pinned-version", "0.12.7", "--summary-file", str(summary)]
                ),
                0,
            )
            self.assertIn("**Newest eligible upgrade:** `0.13.0`", summary.read_text())
            self.assertIn("Within the 7-day cooldown", summary.read_text())
        self.assertEqual(urlopen.call_count, 2)
        self.assertIn("page=2", urlopen.call_args.args[0].full_url)

    @patch("check_uv_version.urllib.request.urlopen")
    def test_failure_on_later_page_does_not_report_partial_results(
        self, urlopen
    ) -> None:
        release = {"tag_name": "0.13.0", "published_at": "2026-01-01T00:00:00Z"}
        urlopen.side_effect = [
            io.BytesIO(json.dumps([release] * 100).encode()),
            check_uv_version.urllib.error.URLError("unavailable"),
        ]
        with TemporaryDirectory() as directory:
            summary = Path(directory) / "summary.md"
            self.assertEqual(
                check_uv_version.main(
                    ["--pinned-version", "0.12.7", "--summary-file", str(summary)]
                ),
                0,
            )
            self.assertIn("Lookup failed", summary.read_text())
            self.assertNotIn("Newest eligible upgrade", summary.read_text())


class TestInspectorVersion(unittest.TestCase):
    @patch("e2e.inspector_version.subprocess.run")
    @patch("e2e.inspector_version.fetch_npm_metadata")
    @patch("e2e.inspector_version.load_inspector_version_config")
    def test_npm_graph_rejects_fresh_or_unknown_transitive_dates(
        self, load_config, metadata, _run
    ) -> None:
        config = InspectorVersionConfig("inspector", ">=2.4.0,<3", "2.9.0")
        load_config.return_value = config
        cutoff = datetime(2026, 10, 6, tzinfo=UTC)
        for timestamp in (None, "invalid", "2026-10-01", "2026-10-07T00:00:00Z"):
            with self.subTest(timestamp=timestamp), TemporaryDirectory() as directory:
                (Path(directory) / "package.json").write_text(
                    json.dumps({"dependencies": {config.package: "2.9.0"}})
                )
                (Path(directory) / "package-lock.json").write_text(
                    json.dumps(
                        {"packages": {"node_modules/dependency": {"version": "1.0.0"}}}
                    )
                )
                metadata.return_value = {"time": {"1.0.0": timestamp}}
                with (
                    patch(
                        "e2e.inspector_version.CONFIG_PATH",
                        Path(directory) / "policy.toml",
                    ),
                    self.assertRaises(ValueError),
                ):
                    update_inspector_npm_lock(cutoff)

    @patch("e2e.inspector_version.subprocess.run")
    @patch("e2e.inspector_version.load_inspector_version_config")
    def test_npm_lock_uses_shared_cutoff_and_exact_pin(self, load_config, run) -> None:
        config = InspectorVersionConfig(
            "@modelcontextprotocol/inspector", ">=2.4.0,<3", "2.9.0"
        )
        load_config.return_value = config
        cutoff = datetime(2026, 10, 6, tzinfo=UTC)
        with TemporaryDirectory() as directory:
            manifest = Path(directory) / "package.json"
            manifest.write_text(json.dumps({"dependencies": {config.package: "2.4.0"}}))
            (Path(directory) / "package-lock.json").write_text(
                json.dumps({"packages": {"": {}}})
            )
            with patch(
                "e2e.inspector_version.CONFIG_PATH", Path(directory) / "policy.toml"
            ):
                update_inspector_npm_lock(cutoff)
            self.assertEqual(
                json.loads(manifest.read_text())["dependencies"][config.package],
                "2.9.0",
            )
        command = run.call_args.args[0]
        self.assertIn("--package-lock-only", command)
        self.assertIn("--ignore-scripts", command)
        self.assertEqual(command[-2:], ["--before", cutoff.isoformat()])
        run.assert_called_once()

    @patch("e2e.inspector_version.subprocess.run", side_effect=RuntimeError("failed"))
    @patch("e2e.inspector_version.load_inspector_version_config")
    def test_npm_lock_restores_manifest_when_resolution_fails(
        self, load_config, _run
    ) -> None:
        config = InspectorVersionConfig(
            "@modelcontextprotocol/inspector", ">=2.4.0,<3", "2.9.0"
        )
        load_config.return_value = config
        with TemporaryDirectory() as directory:
            manifest = Path(directory) / "package.json"
            original_manifest = json.dumps({"dependencies": {config.package: "2.8.0"}})
            manifest.write_text(original_manifest)
            lock = Path(directory) / "package-lock.json"
            original_lock = '{"lockfileVersion": 3}\n'
            lock.write_text(original_lock)
            with patch(
                "e2e.inspector_version.CONFIG_PATH", Path(directory) / "policy.toml"
            ):
                with self.assertRaisesRegex(RuntimeError, "failed"):
                    update_inspector_npm_lock(datetime(2026, 10, 6, tzinfo=UTC))
            self.assertEqual(manifest.read_text(), original_manifest)
            self.assertEqual(lock.read_text(), original_lock)

    @patch("e2e.inspector_version.urllib.request.urlopen")
    def test_fetch_versions_filters_publication_times(self, urlopen) -> None:
        payload = {
            "versions": dict.fromkeys(
                ["2.4.0", "2.4.1", "2.5.0", "2.6.0", "2.7.0", "2.8.0", "3.0.0"]
            ),
            "time": {
                "2.4.0": "2026-09-30T00:00:00Z",
                "2.4.1": "2026-10-01T02:00:00+02:00",
                "2.5.0": "2026-10-01T00:00:01Z",
                "2.6.0": "invalid",
                "2.8.0": "2026-09-30T00:00:00",
                "3.0.0": "2026-10-08T00:00:00Z",
            },
        }
        urlopen.side_effect = lambda *args, **kwargs: io.BytesIO(
            json.dumps(payload).encode()
        )
        cutoff = datetime(2026, 10, 1, tzinfo=UTC)
        self.assertEqual(
            latest_version_in_constraint(
                "@modelcontextprotocol/inspector",
                ">=2.4.0,<3",
                exclude_newer=cutoff,
            ),
            "2.4.1",
        )
        self.assertEqual(fetch_npm_versions("inspector")[-1], "3.0.0")

    def test_direct_security_fix_uses_newest_in_range_version(self) -> None:
        document = {
            "vulnerabilities": {
                "client": {
                    "fixAvailable": {
                        "name": "@modelcontextprotocol/inspector",
                        "version": "2.12.0",
                    }
                },
                "server": {"fixAvailable": True},
                "inspector": {
                    "fixAvailable": {
                        "name": "@modelcontextprotocol/inspector",
                        "version": "3.0.0",
                    }
                },
                "other": {
                    "fixAvailable": {
                        "name": "leftpad",
                        "version": "9.0.0",
                    }
                },
            }
        }
        self.assertEqual(
            direct_security_fix_version(
                document, "@modelcontextprotocol/inspector", ">=2.4.0,<3"
            ),
            "2.12.0",
        )
        self.assertIsNone(
            direct_security_fix_version(
                {"vulnerabilities": {"inspector": {"fixAvailable": True}}},
                "@modelcontextprotocol/inspector",
                ">=2.4.0,<3",
            )
        )

    def test_fetch_versions_rejects_naive_cutoff(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone"):
            fetch_npm_versions("inspector", exclude_newer=datetime(2026, 10, 1))

    @patch("e2e.inspector_version.fetch_npm_versions", return_value=[])
    def test_no_eligible_release(self, _fetch_versions) -> None:
        with self.assertRaises(NoEligibleInspectorVersion):
            latest_version_in_constraint(
                "inspector",
                ">=2.4.0,<3",
                exclude_newer=datetime(2026, 10, 1, tzinfo=UTC),
            )

    def test_parse_constraint(self) -> None:
        minimum, maximum_major = parse_constraint(">=2.4.0,<3")
        self.assertEqual(minimum, (2, 4, 0))
        self.assertEqual(maximum_major, 3)

    def test_version_satisfies_constraint(self) -> None:
        constraint = ">=2.4.0,<3"
        self.assertTrue(version_satisfies_constraint("2.4.0", constraint))
        self.assertTrue(version_satisfies_constraint("2.9.1", constraint))
        self.assertFalse(version_satisfies_constraint("2.3.9", constraint))
        self.assertFalse(version_satisfies_constraint("3.0.0", constraint))

    def test_locked_version_satisfies_constraint(self) -> None:
        config = load_inspector_version_config()
        self.assertTrue(
            version_satisfies_constraint(config.locked_version, config.constraint),
            msg=(
                f"Locked inspector version {config.locked_version} does not satisfy "
                f"constraint {config.constraint}"
            ),
        )

    @patch("e2e.inspector_version.fetch_npm_versions")
    def test_latest_version_in_constraint(self, fetch_versions) -> None:
        fetch_versions.return_value = ["2.4.0", "2.5.0", "3.0.0", "2.4.1"]
        latest = latest_version_in_constraint(
            "@modelcontextprotocol/inspector", ">=2.4.0,<3"
        )
        self.assertEqual(latest, "2.5.0")


class TestCheckInspectorVersionScript(unittest.TestCase):
    @patch(
        "check_inspector_version.latest_version_on_npm",
        side_effect=check_uv_version.urllib.error.URLError("offline"),
    )
    @patch(
        "check_inspector_version.load_inspector_version_config",
        return_value=InspectorVersionConfig(
            "@modelcontextprotocol/inspector", ">=2.4.0,<3", "2.9.0"
        ),
    )
    def test_latest_lookup_failure_remains_visible(self, _load_config, _latest) -> None:
        import check_inspector_version

        finding = check_inspector_version.collect_inspector_major_update()
        self.assertIsNotNone(finding)
        self.assertEqual(finding.latest_version, "Unknown")
        self.assertTrue(finding.eligibility_unknown)
        summary = check_inspector_version.format_summary_markdown(finding)
        self.assertIn("status is unknown", summary)
        self.assertIn("| Unknown |", summary)

    @patch(
        "check_inspector_version.fetch_npm_versions", return_value=["2.9.0", "3.1.0"]
    )
    @patch("check_inspector_version.latest_version_on_npm", return_value="4.0.0")
    @patch("check_inspector_version.load_inspector_version_config")
    def test_eligible_major_is_reported_separately(
        self, load_config, _latest, eligible
    ) -> None:
        import check_inspector_version

        load_config.return_value = InspectorVersionConfig(
            "inspector", ">=2.4.0,<3", "2.9.0"
        )
        cutoff = datetime(2026, 10, 6, tzinfo=UTC)
        finding = check_inspector_version.collect_inspector_major_update(cutoff)
        self.assertIsNotNone(finding)
        self.assertEqual(finding.latest_version, "4.0.0")
        self.assertEqual(finding.eligible_version, "3.1.0")
        eligible.assert_called_once_with("inspector", exclude_newer=cutoff)

    @patch("check_inspector_version.latest_version_on_npm", return_value="3.0.0")
    @patch(
        "check_inspector_version.load_inspector_version_config",
        return_value=InspectorVersionConfig(
            package="@modelcontextprotocol/inspector",
            constraint=">=2.4.0,<3",
            locked_version="2.4.0",
        ),
    )
    def test_check_major_reports_new_major(self, _load_config, _latest_version) -> None:
        import check_inspector_version

        with patch(
            "check_inspector_version.fetch_npm_versions", return_value=["3.0.0"]
        ):
            self.assertEqual(check_inspector_version.main([]), 0)
        self.assertFalse(Path("-v").exists())

    @patch("check_inspector_version.latest_version_on_npm", return_value="3.0.0")
    @patch(
        "check_inspector_version.load_inspector_version_config",
        return_value=InspectorVersionConfig(
            package="@modelcontextprotocol/inspector",
            constraint=">=2.4.0,<3",
            locked_version="2.4.0",
        ),
    )
    def test_check_major_writes_summary_file(
        self, _load_config, _latest_version
    ) -> None:
        import check_inspector_version

        summary = Path(self.id().replace(".", "_") + ".md")
        try:
            with patch(
                "check_inspector_version.fetch_npm_versions", return_value=["3.0.0"]
            ):
                self.assertEqual(
                    check_inspector_version.main(["--summary-file", str(summary)]),
                    0,
                )
            self.assertTrue(summary.is_file())
            self.assertIn("major-version indicator", summary.read_text())
        finally:
            summary.unlink(missing_ok=True)

    @patch(
        "check_inspector_version.fetch_npm_versions",
        side_effect=check_uv_version.urllib.error.URLError("unavailable"),
    )
    @patch("check_inspector_version.latest_version_on_npm", return_value="4.0.0")
    @patch("check_inspector_version.load_inspector_version_config")
    def test_major_notice_marks_failed_eligibility_lookup_unknown(
        self, load_config, _latest, _eligible
    ) -> None:
        import check_inspector_version

        load_config.return_value = InspectorVersionConfig(
            "inspector", ">=2.4.0,<3", "2.9.0"
        )
        finding = check_inspector_version.collect_inspector_major_update(
            datetime(2026, 10, 6, tzinfo=UTC)
        )
        self.assertIsNotNone(finding)
        self.assertTrue(finding.eligibility_unknown)
        self.assertIn(
            "| Unknown |", check_inspector_version.format_summary_markdown(finding)
        )


class TestUpdateInspectorVersionScript(unittest.TestCase):
    @patch("update_inspector_version.write_locked_version")
    @patch(
        "update_inspector_version.latest_version_in_constraint",
        return_value="2.5.0",
    )
    @patch(
        "update_inspector_version.load_inspector_version_config",
        return_value=InspectorVersionConfig(
            package="@modelcontextprotocol/inspector",
            constraint=">=2.4.0,<3",
            locked_version="2.4.0",
        ),
    )
    def test_update_lock_bumps_allowed_version(
        self, _load_config, _latest_allowed, write_locked
    ) -> None:
        import update_inspector_version

        self.assertEqual(
            update_inspector_version.main(["--exclude-newer", "2026-10-01T00:00:00Z"]),
            0,
        )
        _latest_allowed.assert_called_once_with(
            "@modelcontextprotocol/inspector",
            ">=2.4.0,<3",
            exclude_newer=datetime(2026, 10, 1, tzinfo=UTC),
        )
        write_locked.assert_called_once_with("2.5.0")

    def test_default_cooldown_and_no_downgrade(self) -> None:
        import update_inspector_version

        config = InspectorVersionConfig("inspector", ">=2.4.0,<3", "2.5.0")
        now = datetime(2026, 10, 8, tzinfo=UTC)
        with (
            patch(
                "update_inspector_version.load_inspector_version_config",
                return_value=config,
            ),
            patch("update_inspector_version.datetime") as clock,
            patch(
                "update_inspector_version.latest_version_in_constraint",
                return_value="2.4.0",
            ) as latest,
            patch("update_inspector_version.write_locked_version") as write_locked,
        ):
            clock.now.return_value = now
            self.assertEqual(update_inspector_version.main([]), 0)
            latest.assert_called_once_with(
                "inspector",
                ">=2.4.0,<3",
                exclude_newer=datetime(2026, 10, 1, tzinfo=UTC),
            )
            write_locked.assert_not_called()

    def test_bypass_and_no_eligible_release_keep_lock(self) -> None:
        import update_inspector_version

        config = InspectorVersionConfig("inspector", ">=2.4.0,<3", "2.4.0")
        with (
            patch(
                "update_inspector_version.load_inspector_version_config",
                return_value=config,
            ),
            patch(
                "update_inspector_version.latest_version_in_constraint",
                side_effect=NoEligibleInspectorVersion,
            ) as latest,
            patch("update_inspector_version.write_locked_version") as write_locked,
        ):
            self.assertEqual(update_inspector_version.main(["--cooldown-days", "0"]), 0)
            latest.assert_called_once_with(
                "inspector", ">=2.4.0,<3", exclude_newer=None
            )
            write_locked.assert_not_called()

    def test_security_fix_raises_npm_cutoff_only_past_cooled_release(self) -> None:
        import update_inspector_version

        config = InspectorVersionConfig(
            "@modelcontextprotocol/inspector", ">=2.4.0,<3", "2.9.0"
        )
        published = datetime(2026, 10, 21, 18, 35, 58, tzinfo=UTC)
        cutoff = datetime(2026, 10, 16, tzinfo=UTC)
        with (
            patch(
                "update_inspector_version.load_inspector_version_config",
                return_value=config,
            ),
            patch(
                "update_inspector_version.latest_version_in_constraint",
                return_value="2.11.0",
            ),
            patch(
                "update_inspector_version.npm_direct_security_fix",
                return_value=("2.12.0", published),
            ),
            patch("update_inspector_version.write_locked_version") as write_locked,
            patch(
                "update_inspector_version.lock_cutoff_for_pinned_version",
                side_effect=lambda _package, _version, cutoff: cutoff,
            ),
            patch("update_inspector_version.update_inspector_npm_lock") as update_lock,
        ):
            self.assertEqual(
                update_inspector_version.main(
                    ["--exclude-newer", "2026-10-16T00:00:00Z", "--npm-lock"]
                ),
                0,
            )
        write_locked.assert_called_once_with("2.12.0")
        update_lock.assert_called_once_with(published + timedelta(seconds=1))
        self.assertGreater(update_lock.call_args.args[0], cutoff)

    def test_kept_pin_newer_than_cutoff_extends_npm_lock_cutoff(self) -> None:
        import update_inspector_version

        config = InspectorVersionConfig(
            "@modelcontextprotocol/inspector", ">=2.4.0,<3", "2.12.0"
        )
        published = datetime(2026, 10, 21, 18, 35, 58, tzinfo=UTC)
        cutoff = datetime(2026, 10, 16, tzinfo=UTC)
        with (
            patch(
                "update_inspector_version.load_inspector_version_config",
                return_value=config,
            ),
            patch(
                "update_inspector_version.latest_version_in_constraint",
                side_effect=NoEligibleInspectorVersion,
            ),
            patch(
                "update_inspector_version.npm_direct_security_fix",
                return_value=None,
            ),
            patch("update_inspector_version.write_locked_version") as write_locked,
            patch(
                "update_inspector_version.lock_cutoff_for_pinned_version",
                return_value=published + timedelta(seconds=1),
            ) as widen_cutoff,
            patch("update_inspector_version.update_inspector_npm_lock") as update_lock,
        ):
            self.assertEqual(
                update_inspector_version.main(
                    [
                        "--exclude-newer",
                        cutoff.isoformat().replace("+00:00", "Z"),
                        "--npm-lock",
                    ]
                ),
                0,
            )
        write_locked.assert_not_called()
        widen_cutoff.assert_called_once_with(
            config.package, config.locked_version, cutoff
        )
        update_lock.assert_called_once_with(published + timedelta(seconds=1))

    def test_cooled_inspector_release_keeps_shared_cutoff(self) -> None:
        import update_inspector_version

        config = InspectorVersionConfig(
            "@modelcontextprotocol/inspector", ">=2.4.0,<3", "2.9.0"
        )
        cutoff = datetime(2026, 10, 16, tzinfo=UTC)
        with (
            patch(
                "update_inspector_version.load_inspector_version_config",
                return_value=config,
            ),
            patch(
                "update_inspector_version.latest_version_in_constraint",
                return_value="2.12.0",
            ),
            patch(
                "update_inspector_version.npm_direct_security_fix",
                return_value=("2.11.0", datetime(2026, 10, 21, tzinfo=UTC)),
            ) as security_fix,
            patch("update_inspector_version.write_locked_version") as write_locked,
            patch(
                "update_inspector_version.lock_cutoff_for_pinned_version",
                return_value=cutoff,
            ),
            patch("update_inspector_version.update_inspector_npm_lock") as update_lock,
        ):
            self.assertEqual(
                update_inspector_version.main(
                    [
                        "--exclude-newer",
                        cutoff.isoformat().replace("+00:00", "Z"),
                        "--npm-lock",
                    ]
                ),
                0,
            )
        security_fix.assert_called_once()
        write_locked.assert_called_once_with("2.12.0")
        update_lock.assert_called_once_with(cutoff)

    def test_invalid_cutoff_arguments(self) -> None:
        import update_inspector_version

        for argv in (
            ["--cooldown-days", "-1"],
            ["--exclude-newer", "invalid"],
            ["--exclude-newer", "2026-10-01"],
        ):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as error:
                update_inspector_version.main(argv)
            self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
