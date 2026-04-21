"""Exercise the SKILL.md decision logic against the seeded MCP tools.

No Claude API call. This reproduces the deterministic branches of the
skill (weekday check, forward-fill check, data-missing stop, upsert) and
verifies they match the SKILL.md spec. A real run would fold in a Claude
reasoning step at 'decide' instead of the heuristic used here.

Target dates exercised:
  - 2025-02-08 (Saturday)            -> data missing, STOP per skill Step 2
  - 2025-01-20 (MLK, forward-filled) -> forced HOLD via forward-fill
  - 2025-02-14 (normal Friday)       -> decide + upsert
  - 2030-01-01 (no data)             -> skill must stop, no upsert
  - None                             -> resolve from get_latest_date
  - 2025-02-14 again                 -> idempotent overwrite

Outputs a sample action-list JSON under
{repo_root}/tests/smoke/results/trading/.
"""

import datetime as dt
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
OUT_DIR = HERE / "results" / "trading"

spec = importlib.util.spec_from_file_location("pandas_ta", SHIM)
shim_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shim_mod)
sys.modules["pandas_ta"] = shim_mod
os.environ["TRADING_DB_PATH"] = str(DB_PATH)
sys.path.insert(0, str(TRADING_MCP.parent))
import trading_mcp as tmcp  # noqa: E402

TICKER = "AAPL"
AGENT = "claude-code"
MODEL = "claude-sonnet-4-6"


def decide(ticker: str, target_date: str | None) -> dict:
    result = {"ticker": ticker, "target_date_input": target_date}

    if not target_date:
        target_date = tmcp.get_latest_date(ticker)
        result["target_date_resolved_from_latest"] = True
    if not target_date:
        result["outcome"] = "STOP_no_data_in_db"
        return result
    result["target_date"] = target_date

    t = dt.date.fromisoformat(target_date)
    window_start = (t - dt.timedelta(days=4)).isoformat()
    rows = tmcp.get_prices(ticker, window_start, target_date)
    by_date = {r["date"]: r for r in rows}
    today_row = by_date.get(target_date)

    if not today_row:
        result["outcome"] = "STOP_target_date_missing_from_db"
        result["rows_in_window"] = len(rows)
        return result

    price_today = today_row["price"]
    result["price_today"] = price_today

    weekday = t.weekday()
    if weekday >= 5:
        result.update({"action": "HOLD", "outcome": "forced_HOLD_weekend", "weekday": weekday})
        _upsert(ticker, target_date, price_today, "HOLD")
        return result

    priors = [r for r in rows if r["date"] < target_date]
    if priors:
        last_prior = priors[-1]
        if last_prior["price"] == price_today:
            result.update({
                "action": "HOLD",
                "outcome": "forced_HOLD_forward_fill",
                "matched_prior_date": last_prior["date"],
                "matched_prior_price": last_prior["price"],
            })
            _upsert(ticker, target_date, price_today, "HOLD")
            return result

    wider = tmcp.get_prices(ticker, (t - dt.timedelta(days=30)).isoformat(), target_date)
    news = tmcp.get_news(ticker, (t - dt.timedelta(days=7)).isoformat(), target_date)
    momentum_today = today_row["momentum"]
    action = {"up": "BUY", "down": "SELL"}.get(momentum_today, "HOLD")
    result.update({
        "action": action,
        "outcome": "decided_from_momentum_heuristic",
        "context": {
            "wider_rows": len(wider),
            "news_rows": len(news),
            "momentum_today": momentum_today,
        },
    })

    _upsert(ticker, target_date, price_today, action)
    return result


def _upsert(ticker: str, target_date: str, price: float, action: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"trading_{ticker}_{AGENT}_{MODEL}.json"

    if out_path.exists():
        doc = json.loads(out_path.read_text())
    else:
        doc = {"status": "in_progress", "recommendations": []}

    by_date = {r["date"]: r for r in doc.get("recommendations", [])}
    by_date[target_date] = {"date": target_date, "price": price, "recommended_action": action}

    recs = sorted(by_date.values(), key=lambda r: r["date"])
    doc = {
        "status": "in_progress",
        "symbol": ticker,
        "agent": AGENT,
        "model": MODEL,
        "start_date": recs[0]["date"],
        "end_date": recs[-1]["date"],
        "recommendations": recs,
    }
    out_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False))


out_path = OUT_DIR / f"trading_{TICKER}_{AGENT}_{MODEL}.json"
if out_path.exists():
    out_path.unlink()

cases = [
    ("2025-02-08", "Saturday -> data missing per seed, STOP"),
    ("2025-01-20", "MLK Day -> forward-fill HOLD"),
    ("2025-02-14", "normal Friday -> decide+upsert"),
    ("2030-01-01", "future date with no data -> STOP"),
    (None,         "no date -> resolve from get_latest_date"),
    ("2025-02-14", "idempotent re-run on same date -> overwrite record"),
]

print(f"Output file will be: {out_path}")
print()
for target_date, note in cases:
    print("=" * 72)
    print(f"CASE: target_date={target_date!r}  ({note})")
    print("=" * 72)
    r = decide(TICKER, target_date)
    print(json.dumps(r, indent=2, default=str))
    print()

print("=" * 72)
print("FINAL ACTION-LIST FILE")
print("=" * 72)
print(out_path.read_text())
