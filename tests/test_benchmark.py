from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_agent_trading.benchmark import (
    BenchmarkTask,
    _build_prompt,
    load_tasks_file,
    run_benchmark_batch,
    run_benchmark_task,
)
from claude_agent_trading.core import AgentResult


class BenchmarkRunnerTests(unittest.TestCase):
    def test_build_trading_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "data" / "trading").mkdir(parents=True)
            task = BenchmarkTask(task_type="trading", ticker="TSLA")
            prompt = _build_prompt(task, root)
            self.assertIn("Please make trading decision for TSLA.", prompt)
            self.assertIn(str((root / "data" / "trading").resolve()), prompt)

    def test_build_report_evaluation_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "data" / "trading").mkdir(parents=True)
            (root / "results" / "report_generation").mkdir(parents=True)
            task = BenchmarkTask(
                task_type="report_evaluation",
                ticker="TSLA",
                target_agent="codex",
                target_model="gpt-5",
            )
            prompt = _build_prompt(task, root)
            self.assertIn("Evaluate the codex/TSLA/gpt-5 run.", prompt)

    def test_load_tasks_file_reads_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "tasks.jsonl"
            path.write_text(
                json.dumps({"task_type": "trading", "ticker": "TSLA"}) + "\n",
                encoding="utf-8",
            )
            tasks = load_tasks_file(path)
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0].task_type, "trading")
            self.assertEqual(tasks[0].ticker, "TSLA")

    def test_benchmark_root_required(self) -> None:
        task = BenchmarkTask(task_type="trading", ticker="TSLA")
        with self.assertRaises(ValueError) as ctx:
            run_benchmark_task(task)
        self.assertIn("benchmark_root is required", str(ctx.exception))

    @patch("claude_agent_trading.benchmark.run_agent")
    def test_run_benchmark_task_returns_result(self, mock_run_agent) -> None:
        mock_run_agent.return_value = AgentResult(
            result="done", cost_usd=0.1, turns=2,
            duration_ms=100, session_id="sess-1", is_error=False,
        )
        with tempfile.TemporaryDirectory() as benchmark_tmp:
            benchmark_root = Path(benchmark_tmp)
            (benchmark_root / "data" / "trading").mkdir(parents=True)

            result = run_benchmark_task(
                BenchmarkTask(
                    task_type="trading",
                    ticker="TSLA",
                    benchmark_root=str(benchmark_root),
                )
            )

            self.assertFalse(result.agent_result.is_error)
            self.assertEqual(result.agent_result.result, "done")
            mock_run_agent.assert_called_once()

    @patch("claude_agent_trading.benchmark.run_agent")
    def test_batch_continues_after_error_by_default(self, mock_run_agent) -> None:
        mock_run_agent.side_effect = [
            AgentResult(result="ok", is_error=False),
            AgentResult(result="boom", is_error=True),
            AgentResult(result="ok2", is_error=False),
        ]
        with tempfile.TemporaryDirectory() as benchmark_tmp:
            benchmark_root = Path(benchmark_tmp)
            (benchmark_root / "data" / "trading").mkdir(parents=True)

            tasks = [
                BenchmarkTask(task_type="trading", ticker="TSLA", benchmark_root=str(benchmark_root)),
                BenchmarkTask(task_type="trading", ticker="AAPL", benchmark_root=str(benchmark_root)),
                BenchmarkTask(task_type="trading", ticker="MSFT", benchmark_root=str(benchmark_root)),
            ]
            result = run_benchmark_batch(tasks, tasks_file="tasks.jsonl")
            self.assertEqual(len(result.results), 3)

    def test_unsupported_task_type_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            task = BenchmarkTask(task_type="unknown_skill", ticker="TSLA")
            with self.assertRaises(ValueError) as ctx:
                _build_prompt(task, Path(tmpdir))
            self.assertIn("Unsupported task_type", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
