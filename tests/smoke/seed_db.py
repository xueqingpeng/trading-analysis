"""Seed trading/db/trading_env.duckdb with a small sample dataset for smoke-testing.

Generates:
- ~32 trading days of AAPL prices (2025-01-02 through 2025-02-14)
- Weekdays only (Mon-Fri)
- One forward-filled "holiday" (2025-01-20 MLK Day) with price == prior trading day
- A few news rows and one 10-Q filing row

The DuckDB path defaults to {repo_root}/trading/db/trading_env.duckdb but can
be overridden via TRADING_DB_PATH.
"""

import os
import random
from datetime import date, timedelta
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = REPO_ROOT / "trading" / "db" / "trading_env.duckdb"
DB_PATH = Path(os.environ.get("TRADING_DB_PATH", str(DEFAULT_DB)))
SCHEMA_SQL = (REPO_ROOT / "trading" / "mcp" / "schema.sql").read_text()

DB_PATH.parent.mkdir(parents=True, exist_ok=True)
if DB_PATH.exists():
    DB_PATH.unlink()

con = duckdb.connect(str(DB_PATH))
con.execute(SCHEMA_SQL)

random.seed(42)
start = date(2025, 1, 2)
end = date(2025, 2, 15)

price = 240.00
rows = []
last_trading_price = None
d = start
while d <= end:
    if d.weekday() < 5:
        if d == date(2025, 1, 20):
            rows.append((d.isoformat(), last_trading_price, "neutral"))
        else:
            price = round(price + random.uniform(-3.0, 3.2), 2)
            momentum = (
                "up" if rows and price > rows[-1][1]
                else ("down" if rows and price < rows[-1][1] else "neutral")
            )
            rows.append((d.isoformat(), price, momentum))
            last_trading_price = price
    d += timedelta(days=1)

con.executemany(
    "INSERT INTO prices (ticker, date, price, momentum) VALUES ('AAPL', ?, ?, ?)",
    rows,
)

news_rows = [
    ("AAPL", "2025-01-06", 1, "Apple announces new iPhone software features at CES."),
    ("AAPL", "2025-01-15", 1, "Analysts raise AAPL price target on services growth."),
    ("AAPL", "2025-01-15", 2, "Apple Vision Pro reported softer than expected demand."),
    ("AAPL", "2025-02-03", 1, "Apple reports strong fiscal Q1 earnings, beats EPS estimates."),
    ("AAPL", "2025-02-10", 1, "Apple expands App Store developer tools."),
]
con.executemany(
    "INSERT INTO news (ticker, date, item_id, content) VALUES (?, ?, ?, ?)",
    news_rows,
)

filing_rows = [
    ("AAPL", "2025-02-03", "10-Q", "Apple Inc. Q1 FY25 10-Q: revenue $124.3B, services $26.3B."),
]
con.executemany(
    "INSERT INTO filings (ticker, filing_date, form_type, content) VALUES (?, ?, ?, ?)",
    filing_rows,
)

con.commit()
con.close()

print(f"Seeded DB at: {DB_PATH}")
print(f"Prices rows: {len(rows)}")
print(f"First: {rows[0]}")
print(f"MLK  : {[r for r in rows if r[0] == '2025-01-20']}")
print(f"Last : {rows[-1]}")
print(f"News : {len(news_rows)} rows")
print(f"Filings: {len(filing_rows)} rows")
