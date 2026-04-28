"""Claude Agent Trading package."""

from .benchmark import (
    BatchRunResult,
    BenchmarkRunResult,
    BenchmarkTask,
    load_tasks_file,
    run_benchmark_batch,
    run_benchmark_task,
)
from .core import AgentResult, run_agent
from .trading_daily import (
    DailyResult,
    TradingDailyConfig,
    TradingRangeResult,
    run_trading_range,
)
from .hedging_daily import (
    HedgingDailyConfig,
    HedgingRangeResult,
    run_hedging_range,
)

__all__ = [
    "run_agent",
    "AgentResult",
    "BenchmarkTask",
    "BenchmarkRunResult",
    "BatchRunResult",
    "run_benchmark_task",
    "run_benchmark_batch",
    "load_tasks_file",
    "TradingDailyConfig",
    "DailyResult",
    "TradingRangeResult",
    "run_trading_range",
    "HedgingDailyConfig",
    "HedgingRangeResult",
    "run_hedging_range",
]
