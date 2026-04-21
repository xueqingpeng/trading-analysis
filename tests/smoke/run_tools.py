"""Smoke-test the trading_mcp tools directly (without going through MCP stdio).

Installs pandas_ta_shim as sys.modules['pandas_ta'] first, then imports
trading_mcp and calls each of the 5 tools against the seeded DB.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
SHIM = HERE / "pandas_ta_shim.py"
TRADING_MCP = REPO_ROOT / "trading" / "mcp" / "trading_mcp.py"
DB_PATH = REPO_ROOT / "trading" / "db" / "trading_env.duckdb"

spec = importlib.util.spec_from_file_location("pandas_ta", SHIM)
shim_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shim_mod)
sys.modules["pandas_ta"] = shim_mod
print(f"[setup] pandas_ta shim installed (funcs: {[n for n in dir(shim_mod) if not n.startswith('_')]})")

os.environ["TRADING_DB_PATH"] = str(DB_PATH)
print(f"[setup] TRADING_DB_PATH={os.environ['TRADING_DB_PATH']}")

sys.path.insert(0, str(TRADING_MCP.parent))
import trading_mcp as tmcp  # noqa: E402

print(f"[setup] trading_mcp imported. FastMCP instance: {tmcp.mcp!r}")
print()

TOOLS = ["get_latest_date", "get_prices", "get_news", "get_filings", "get_indicator"]
for t in TOOLS:
    fn = getattr(tmcp, t, None)
    print(f"[setup] tmcp.{t} = {fn!r}")
print()


def dump(label, result, limit_rows=None):
    if isinstance(result, list) and limit_rows and len(result) > limit_rows:
        preview = result[:limit_rows]
        suffix = f" ... (+{len(result)-limit_rows} more, total {len(result)})"
    else:
        preview = result
        suffix = f" (n={len(result)})" if isinstance(result, list) else ""
    print(f"--- {label}{suffix} ---")
    print(json.dumps(preview, indent=2, default=str))
    print()


print("=" * 72)
print("TEST 1: get_latest_date(AAPL)")
print("=" * 72)
latest = tmcp.get_latest_date("AAPL")
dump("get_latest_date", latest)
assert latest == "2025-02-14", f"expected 2025-02-14, got {latest!r}"

print("=" * 72)
print("TEST 2: get_prices(AAPL, 2025-01-10, 2025-01-22) -- includes MLK forward-fill")
print("=" * 72)
rows = tmcp.get_prices("AAPL", "2025-01-10", "2025-01-22")
dump("get_prices", rows)
by_date = {r["date"]: r["price"] for r in rows}
mlk = by_date.get("2025-01-20")
prior = by_date.get("2025-01-17")
print(f"[check] MLK forward-fill: 2025-01-17={prior}, 2025-01-20={mlk}  ->  equal={mlk == prior}")
assert mlk == prior, "Expected MLK price forward-filled from 2025-01-17"

print("=" * 72)
print("TEST 3: get_news(AAPL, 2025-01-01, 2025-02-14)")
print("=" * 72)
news = tmcp.get_news("AAPL", "2025-01-01", "2025-02-14")
dump("get_news", news)
assert len(news) == 5, f"expected 5 news rows, got {len(news)}"

print("=" * 72)
print("TEST 4: get_filings(AAPL, 2024-02-14, 2025-02-14)")
print("=" * 72)
filings = tmcp.get_filings("AAPL", "2024-02-14", "2025-02-14")
dump("get_filings", filings)
assert len(filings) == 1 and filings[0]["form_type"] == "10-Q"

print("=" * 72)
print("TEST 4b: get_filings(AAPL, 2024-02-14, 2025-02-14, form_type='10-K')")
print("=" * 72)
only_10k = tmcp.get_filings("AAPL", "2024-02-14", "2025-02-14", form_type="10-K")
dump("get_filings (10-K only)", only_10k)
assert only_10k == [], "Expected empty: no 10-K in seed"

print("=" * 72)
print("TEST 5a: get_indicator(AAPL, 2025-02-03, 2025-02-14, 'ma', length=10)")
print("=" * 72)
ma = tmcp.get_indicator("AAPL", "2025-02-03", "2025-02-14", indicator="ma", length=10)
dump("get_indicator[ma, length=10]", ma)
assert len(ma) > 0, "MA expected to have rows (10-day window fits inside seeded history)"

print("=" * 72)
print("TEST 5b: get_indicator(AAPL, 2025-02-03, 2025-02-14, 'rsi', length=7)")
print("=" * 72)
rsi = tmcp.get_indicator("AAPL", "2025-02-03", "2025-02-14", indicator="rsi", length=7)
dump("get_indicator[rsi, length=7]", rsi)

print("=" * 72)
print("TEST 5c: get_indicator(AAPL, 2025-02-03, 2025-02-14, 'bbands', length=10)")
print("=" * 72)
bb = tmcp.get_indicator("AAPL", "2025-02-03", "2025-02-14", indicator="bbands", length=10)
dump("get_indicator[bbands, length=10]", bb)

print("=" * 72)
print("TEST 5d: get_indicator(AAPL, 2025-02-03, 2025-02-14, 'macd')")
print("=" * 72)
macd = tmcp.get_indicator("AAPL", "2025-02-03", "2025-02-14", indicator="macd")
dump("get_indicator[macd]", macd)

print("=" * 72)
print("TEST 6: no-look-ahead sanity -- request date_end > latest date")
print("=" * 72)
future_rows = tmcp.get_prices("AAPL", "2025-02-13", "2025-02-28")
dump("get_prices (end past latest)", future_rows)
dates = [r["date"] for r in future_rows]
assert all(d <= "2025-02-14" for d in dates), "Server should only return rows it has"

print()
print("ALL 5 TOOLS OK")
