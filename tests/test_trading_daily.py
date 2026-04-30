from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from claude_agent_trading.core import AgentResult
from claude_agent_trading.trading_daily import (
    TradingDailyConfig,
    _find_output_file,
    _precheck,
    build_daily_prompt,
    iter_trading_days,
    run_trading_range,
)


class IterTradingDaysTests(unittest.TestCase):
    def test_skip_weekends(self) -> None:
        # 2025-03-03 is Mon. Range covers Mon-Sun.
        days = list(
            iter_trading_days(
                date(2025, 3, 3), date(2025, 3, 9), skip_weekends=True
            )
        )
        self.assertEqual(
            [d.isoformat() for d in days],
            ["2025-03-03", "2025-03-04", "2025-03-05", "2025-03-06", "2025-03-07"],
        )

    def test_no_skip_weekends(self) -> None:
        days = list(
            iter_trading_days(
                date(2025, 3, 8), date(2025, 3, 9), skip_weekends=False
            )
        )
        self.assertEqual([d.isoformat() for d in days], ["2025-03-08", "2025-03-09"])

    def test_weekend_only_range_empty_when_skipping(self) -> None:
        days = list(
            iter_trading_days(
                date(2025, 3, 8), date(2025, 3, 9), skip_weekends=True
            )
        )
        self.assertEqual(days, [])

    def test_end_before_start_raises(self) -> None:
        with self.assertRaises(ValueError):
            list(
                iter_trading_days(
                    date(2025, 3, 5), date(2025, 3, 3), skip_weekends=True
                )
            )

    def test_single_day(self) -> None:
        days = list(
            iter_trading_days(
                date(2025, 3, 3), date(2025, 3, 3), skip_weekends=True
            )
        )
        self.assertEqual([d.isoformat() for d in days], ["2025-03-03"])


class BuildDailyPromptTests(unittest.TestCase):
    def test_format(self) -> None:
        self.assertEqual(
            build_daily_prompt(
                "claude-sonnet-4-6", "TSLA", "2025-03-05", Path("/tmp/out")
            ),
            "you are claude-sonnet-4-6. Trade TSLA on 2025-03-05. "
            "When calling upsert_decision.py, pass --output-root=/tmp/out.",
        )


class RunTradingRangeTests(unittest.TestCase):
    def test_prompt_includes_resolved_model_and_output_root(self) -> None:
        with TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "out"
            out.mkdir()
            db = Path(tmpdir) / "env.duckdb"
            db.write_text("")
            config = TradingDailyConfig(
                symbol="TSLA",
                start=date(2025, 3, 3),
                end=date(2025, 3, 3),
                output_dir=out,
                db_path=db,
                model="test-model",
            )

            with (
                patch("claude_agent_trading.trading_daily._precheck"),
                patch("claude_agent_trading.trading_daily._load_mcp_servers", return_value={}),
                patch(
                    "claude_agent_trading.trading_daily.run_agent",
                    return_value=AgentResult(result="ok"),
                ) as run_agent,
            ):
                run_trading_range(config)

            run_agent.assert_called_once()
            self.assertEqual(
                run_agent.call_args.kwargs["prompt"],
                "you are test-model. Trade TSLA on 2025-03-03. "
                f"When calling upsert_decision.py, pass --output-root={out.resolve().as_posix()}.",
            )
            self.assertEqual(run_agent.call_args.kwargs["model"], "test-model")


class FindOutputFileTests(unittest.TestCase):
    def test_missing_dir_returns_none(self) -> None:
        with TemporaryDirectory() as tmpdir:
            self.assertIsNone(_find_output_file(Path(tmpdir) / "nope", "TSLA"))

    def test_no_matching_files_returns_none(self) -> None:
        with TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            (out / "trading_AAPL_x_y.json").write_text("{}")
            self.assertIsNone(_find_output_file(out, "TSLA"))

    def test_picks_latest_by_mtime(self) -> None:
        import time

        with TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            older = out / "trading_TSLA_codex_gpt-5.json"
            older.write_text('{"v": 1}')
            time.sleep(0.01)
            newer = out / "trading_TSLA_claude-code_claude-sonnet-4-6.json"
            newer.write_text('{"v": 2}')

            found = _find_output_file(out, "TSLA")
            self.assertIsNotNone(found)
            self.assertEqual(found.name, newer.name)


class PrecheckTests(unittest.TestCase):
    def setUp(self) -> None:
        import os
        # Skip the real dep-probe subprocess — fastmcp / duckdb / pandas-ta
        # aren't required to test the other precheck branches.
        self._prev_skip = os.environ.get("TRADING_MCP_SKIP_PROBE")
        os.environ["TRADING_MCP_SKIP_PROBE"] = "1"

    def tearDown(self) -> None:
        import os
        if self._prev_skip is None:
            os.environ.pop("TRADING_MCP_SKIP_PROBE", None)
        else:
            os.environ["TRADING_MCP_SKIP_PROBE"] = self._prev_skip

    def _make_project(self, root: Path) -> None:
        """Build a minimal project layout. Caller decides which files exist."""
        skill = root / ".claude" / "skills" / "trading"
        (skill / "scripts" / "mcp").mkdir(parents=True)
        (skill / "scripts" / "env").mkdir(parents=True)
        (skill / "SKILL.md").write_text("stub")
        (skill / ".mcp.json").write_text("{}")
        (skill / "scripts" / "mcp" / "trading_mcp.py").write_text("# stub\n")

    def _make_db(self, root: Path) -> Path:
        db = root / "env.duckdb"
        db.write_text("")
        return db

    def test_missing_skill_raises(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._make_project(root)
            (root / ".claude" / "skills" / "trading" / "SKILL.md").unlink()
            config = TradingDailyConfig(
                symbol="TSLA",
                start=date(2025, 3, 3),
                end=date(2025, 3, 3),
                output_dir=root / "out",
                db_path=self._make_db(root),
                project_root=root,
            )
            with self.assertRaisesRegex(FileNotFoundError, "SKILL.md"):
                _precheck(config)

    def test_missing_mcp_json_raises(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._make_project(root)
            (root / ".claude" / "skills" / "trading" / ".mcp.json").unlink()
            config = TradingDailyConfig(
                symbol="TSLA",
                start=date(2025, 3, 3),
                end=date(2025, 3, 3),
                output_dir=root / "out",
                db_path=self._make_db(root),
                project_root=root,
            )
            with self.assertRaisesRegex(FileNotFoundError, r"\.mcp\.json"):
                _precheck(config)

    def test_missing_duckdb_raises(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._make_project(root)
            config = TradingDailyConfig(
                symbol="TSLA",
                start=date(2025, 3, 3),
                end=date(2025, 3, 3),
                output_dir=root / "out",
                db_path=root / "does_not_exist.duckdb",
                project_root=root,
            )
            with self.assertRaisesRegex(FileNotFoundError, "DuckDB"):
                _precheck(config)

    def test_precheck_creates_output_dir(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._make_project(root)
            out = root / "out" / "nested"
            config = TradingDailyConfig(
                symbol="TSLA",
                start=date(2025, 3, 3),
                end=date(2025, 3, 3),
                output_dir=out,
                db_path=self._make_db(root),
                project_root=root,
            )
            _precheck(config)
            self.assertTrue(out.is_dir())


if __name__ == "__main__":
    unittest.main()
