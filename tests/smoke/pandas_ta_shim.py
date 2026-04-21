"""Minimal pandas_ta shim covering sma/rsi/bbands/macd only.

trading_mcp.py imports `pandas_ta as _ta` and calls _ta.sma, _ta.rsi,
_ta.bbands, _ta.macd with the column orderings the real library uses.
PyPI no longer ships a pandas-ta version compatible with Python 3.11, so
this shim is installed into sys.modules BEFORE importing trading_mcp.

Formulas:
- sma   : pandas rolling mean
- rsi   : Wilder's smoothing (EMA with alpha=1/length)
- bbands: columns ordered [lower, middle, upper] (positional access in
          the server is `.iloc[:, 0]` = lower, 1 = middle, 2 = upper)
- macd  : columns ordered [macd_line, hist, signal] (positional access is
          .iloc[:,0] = macd, 1 = hist, 2 = signal)
"""

from __future__ import annotations
import pandas as pd


def sma(close: pd.Series, length: int = 20) -> pd.Series:
    return close.rolling(window=length, min_periods=length).mean()


def rsi(close: pd.Series, length: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()
    avg_loss = loss.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()
    rs = avg_gain / avg_loss.replace(0.0, float("nan"))
    return 100.0 - (100.0 / (1.0 + rs))


def bbands(close: pd.Series, length: int = 20, std: float = 2.0) -> pd.DataFrame:
    middle = close.rolling(window=length, min_periods=length).mean()
    stddev = close.rolling(window=length, min_periods=length).std(ddof=0)
    upper = middle + std * stddev
    lower = middle - std * stddev
    return pd.DataFrame({"lower": lower, "middle": middle, "upper": upper})


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    ema_fast = close.ewm(span=fast, adjust=False, min_periods=fast).mean()
    ema_slow = close.ewm(span=slow, adjust=False, min_periods=slow).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    hist = macd_line - signal_line
    return pd.DataFrame({"macd": macd_line, "hist": hist, "signal": signal_line})
