from __future__ import annotations

import json
import subprocess
import sys
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from claude_agent_trading.report_generation_daily import (
    ReportGenerationDailyConfig,
    ReportGenerationRangeResult,
    build_daily_prompt,
)


class BuildDailyPromptTests(unittest.TestCase):
    def test_format(self) -> None:
        self.assertEqual(
            build_daily_prompt(
                "claude-sonnet-4-6", "TSLA", "2025-03-05", Path("/tmp/out")
            ),
            "you are claude-sonnet-4-6. Generate equity research report for TSLA on 2025-03-05. "
            "When calling upsert_report.py, pass --output-root=/tmp/out.",
        )


class RangeResultSerializationTests(unittest.TestCase):
    def test_to_dict_is_json_serializable(self) -> None:
        config = ReportGenerationDailyConfig(
            symbol="TSLA",
            start=date(2025, 3, 3),
            end=date(2025, 3, 3),
            output_dir=Path("out"),
            db_path=Path("db.duckdb"),
            project_root=Path("project"),
        )
        result = ReportGenerationRangeResult(
            config=config,
            per_day=[],
            total_cost_usd=0.0,
            num_errors=0,
        )

        data = result.to_dict()
        self.assertNotIn("trading_results_root", data["config"])
        json.dumps(data)


class UpsertReportTests(unittest.TestCase):
    def test_default_agent_naming_matches_report_evaluation_convention(self) -> None:
        with TemporaryDirectory() as tmpdir:
            output_root = Path(tmpdir) / "reports"
            script = (
                Path(__file__).resolve().parents[1]
                / ".claude"
                / "skills"
                / "report_generation"
                / "scripts"
                / "upsert_report.py"
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--symbol",
                    "TSLA",
                    "--target-date",
                    "2025-03-03",
                    "--rating",
                    "BUY",
                    "--model",
                    "claude-sonnet-4-6",
                    "--output-root",
                    str(output_root),
                ],
                input="# Daily Equity Research Report: TSLA\n",
                text=True,
                capture_output=True,
                check=True,
            )

            summary = json.loads(completed.stdout)
            summary_path = Path(summary["path"])
            report_path = Path(summary["report_path"])

            self.assertEqual(
                summary_path.name,
                "claude-code_report_generation_TSLA_claude-sonnet-4-6.json",
            )
            self.assertEqual(
                report_path.name,
                "claude-code_report_generation_TSLA_20250303_claude-sonnet-4-6.md",
            )
            self.assertTrue(summary_path.is_file())
            self.assertTrue(report_path.is_file())


if __name__ == "__main__":
    unittest.main()
