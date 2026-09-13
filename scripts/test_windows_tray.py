import json
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

from scripts import start_windows_tray
from scripts import windows_tray


class WindowsTrayTests(unittest.TestCase):
    def test_duration_is_compact(self):
        self.assertEqual(windows_tray.duration(8), "8s")
        self.assertEqual(windows_tray.duration(65), "1m 5s")
        self.assertEqual(windows_tray.duration(3665), "1h 1m")

    def test_parse_requires_attach_or_launch(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            windows_tray.parse_args([])
        args = windows_tray.parse_args(["--pid", "123"])
        self.assertEqual(args.pid, 123)
        args = windows_tray.parse_args([
            "--icon", "docs/assets/memorycore-ai-icon.ico",
            "--", sys.executable, "-c", "print('ok')",
        ])
        self.assertEqual(args.command[:2], [sys.executable, "-c"])
        self.assertEqual(str(args.icon), "docs\\assets\\memorycore-ai-icon.ico")

    def test_default_icon_path_uses_project_asset(self):
        icon = windows_tray.default_icon_path()
        self.assertIsNotNone(icon)
        self.assertEqual(icon.name, "memorycore-ai-icon.ico")
        self.assertTrue(icon.exists())

    def test_metrics_summary_handles_missing_file(self):
        missing = Path(tempfile.gettempdir()) / "missing-memorycore-ai-metrics.jsonl"
        if missing.exists():
            missing.unlink()
        result = windows_tray.metrics_summary(missing)
        self.assertTrue(result["configured"])
        self.assertFalse(result["exists"])
        self.assertEqual(result["rows"], 0)

    def test_preferences_validate_and_round_trip(self):
        with tempfile.TemporaryDirectory(prefix="memorycore-tray-pref-") as tmp:
            prefs = Path(tmp) / "preferences.json"
            windows_tray.update_preferences(
                prefs,
                min_idle_seconds=12 * 60 * 60,
                auto_archive_after_seconds=48 * 60 * 60,
                auto_delete_after_days=90,
            )
            loaded = windows_tray.load_preferences(prefs)
            self.assertEqual(loaded["min_idle_seconds"], 12 * 60 * 60)
            self.assertEqual(loaded["auto_archive_after_seconds"], 48 * 60 * 60)
            self.assertEqual(loaded["auto_delete_after_days"], 90)
            with self.assertRaises(ValueError):
                windows_tray.update_preferences(prefs, min_idle_seconds=30)

    def test_database_size_is_payload_free(self):
        with tempfile.TemporaryDirectory(prefix="memorycore-tray-db-") as tmp:
            database = Path(tmp) / "memory.sqlite3"
            database.write_bytes(b"synthetic")
            result = windows_tray.file_size(database)
            self.assertTrue(result["exists"])
            self.assertEqual(result["bytes"], len(b"synthetic"))

    def test_usage_summary_reports_verified_token_savings(self):
        with tempfile.TemporaryDirectory(prefix="memorycore-tray-usage-") as tmp:
            report = Path(tmp) / "usage.json"
            report.write_text(json.dumps({
                "comparable": True,
                "model_profile": {"model": "synthetic-model", "reasoning_effort": "medium"},
                "quality_regressions": 0,
                "baseline": {"observed_tokens": {"total": 1100, "cached_input": 200}},
                "repaired": {"observed_tokens": {"total": 700, "cached_input": 200,
                                                 "cache_write_input": 0, "reasoning": 40}},
                "total_token_saving": 1 - 700 / 1100,
                "uncached_input_saving": 0.5,
                "cost_saving": None,
            }), encoding="utf-8")
            result = windows_tray.usage_summary(report)
            self.assertTrue(result["configured"])
            self.assertEqual(result["tokens_used"]["total"], 700)
            self.assertEqual(result["tokens_used"]["reasoning"], 40)
            self.assertAlmostEqual(result["total_token_saving"], 1 - 700 / 1100)

    def test_status_labels_include_user_facing_stats(self):
        with tempfile.TemporaryDirectory(prefix="memorycore-tray-labels-") as tmp:
            database = Path(tmp) / "memory.sqlite3"
            database.write_bytes(b"synthetic")
            usage = Path(tmp) / "usage.json"
            usage.write_text(json.dumps({
                "comparable": True,
                "repaired": {"observed_tokens": {"total": 700}},
                "total_token_saving": 0.25,
                "codex_tokens": {"latest_request": {
                    "input_tokens": 1000,
                    "cached_input_tokens": 800,
                    "cache_write_input_tokens": 50,
                    "uncached_input_tokens": 200,
                    "cached_input_percent": 80.0,
                    "uncached_input_percent": 20.0,
                    "output_tokens": 40,
                    "reasoning_output_tokens": 10,
                    "total_tokens": 1040,
                }},
                "context_budget_sentinel": {
                    "estimated_fresh_task_input_saving_percent": 90.0,
                    "estimated_memorycore_packet_tokens": 100,
                },
                "memory_operations": {"packet_cache_effect": {
                    "cacheable_recalls": 10,
                    "hits": 9,
                    "misses": 1,
                    "hit_rate_percent": 90.0,
                    "estimated_full_retrievals_avoided": 9,
                }},
                "model_cost_projection": {"rows": [
                    {"model": "low", "estimated_cost_saving_percent": 70.0},
                    {"model": "high", "estimated_cost_saving_percent": 88.0},
                ]},
            }), encoding="utf-8")
            prefs = Path(tmp) / "preferences.json"
            windows_tray.save_preferences(prefs, {
                **windows_tray.DEFAULT_PREFERENCES,
                "min_idle_seconds": 6 * 60 * 60,
                "auto_archive_after_seconds": 48 * 60 * 60,
                "auto_delete_after_days": 30,
            })
            process = windows_tray.MemoryCoreProcess(pid=123)
            process.started_at = windows_tray.now()
            snapshot = windows_tray.status_snapshot(
                process,
                database=database,
                preferences=prefs,
                usage_report=usage,
            )
            labels = snapshot["labels"]
            self.assertEqual(labels["idle_generation_hours"], 6)
            self.assertEqual(labels["auto_archive_hours"], 48)
            self.assertEqual(labels["auto_delete_days"], 30)
            self.assertEqual(labels["database"], "present")
            self.assertEqual(labels["token_saving_percent"], 25)
            self.assertEqual(labels["provider_cached_input_percent"], 80.0)
            self.assertEqual(labels["provider_cache_write_input_tokens"], 50)
            self.assertEqual(labels["memorycore_packet_cache_hit_rate_percent"], 90.0)
            self.assertEqual(labels["memorycore_full_retrievals_avoided"], 9)
            self.assertEqual(labels["estimated_fresh_task_input_saving_percent"], 90.0)
            self.assertEqual(labels["best_projected_cost_saving_percent"], 88.0)

    def test_status_cache_summary_can_use_metrics_packet_cache(self):
        with tempfile.TemporaryDirectory(prefix="memorycore-tray-cache-") as tmp:
            metrics = Path(tmp) / "metrics.jsonl"
            metrics.write_text(json.dumps({
                "latency_ms": 1,
                "error": None,
                "memory_arguments": ["recall"],
                "packet": {},
                "retrieval": {"packet_cache": "hit"},
                "packet_cache": {"hits": 7, "misses": 3},
                "resource": {},
            }) + "\n", encoding="utf-8")
            process = windows_tray.MemoryCoreProcess(pid=123)
            process.started_at = windows_tray.now()
            snapshot = windows_tray.status_snapshot(process, metrics=metrics)
            labels = snapshot["labels"]
            self.assertEqual(labels["memorycore_packet_cache_hit_rate_percent"], 70.0)
            self.assertEqual(labels["memorycore_packet_cache_hits"], 7)
            self.assertEqual(labels["memorycore_packet_cache_misses"], 3)

    def test_status_json_launches_and_writes_snapshot(self):
        with tempfile.TemporaryDirectory(prefix="memorycore-tray-test-") as tmp:
            status = Path(tmp) / "status.json"
            with contextlib.redirect_stdout(io.StringIO()):
                windows_tray.main([
                    "--status-json",
                    "--cwd", tmp,
                    "--database", str(Path(tmp) / "missing.sqlite3"),
                    "--preferences", str(Path(tmp) / "preferences.json"),
                    "--usage-report", str(Path(tmp) / "missing-usage.json"),
                    "--set-min-idle-hours", "6",
                    "--set-auto-delete-days", "30",
                    "--status-file", str(status),
                    "--", sys.executable, "-c", "import time; time.sleep(0.2)",
                ])
            data = json.loads(status.read_text(encoding="utf-8"))
            self.assertEqual(data["name"], "MemoryCore AI")
            self.assertEqual(data["mode"], "launched")
            self.assertIn("metrics", data)
            self.assertEqual(data["preferences"]["min_idle_seconds"], 6 * 60 * 60)
            self.assertEqual(data["preferences"]["auto_delete_after_days"], 30)
            self.assertFalse(data["database"]["exists"])
            self.assertFalse(data["usage"]["exists"])

    def test_standard_launcher_builds_local_tray_command(self):
        with tempfile.TemporaryDirectory(prefix="memorycore-tray-launcher-") as tmp:
            home = Path(tmp) / "memorycore-ai-local"
            host = home / "host.json"
            database = home / "vault" / "custom.sqlite3"
            host.parent.mkdir(parents=True)
            host.write_text(json.dumps({"database": str(database)}), encoding="utf-8")
            args = start_windows_tray.parse_args([
                "--home", str(home),
                "--python", sys.executable,
                "--status-json",
            ])
            generated = start_windows_tray.tray_argv(args)
            self.assertIn("--status-json", generated)
            self.assertIn(str(database), generated)
            self.assertIn("scripts.monitored_native_mcp", generated)
            self.assertIn(str(home / "tray-preferences.json"), generated)


if __name__ == "__main__":
    unittest.main()
