#!/usr/bin/env python3
"""
fetch_prices.py — runs in GitHub Actions (NOT in the Claude sandbox).

Reads tickers.txt, fetches latest close/last price for each ticker,
writes prices.json. Primary source: yfinance (batch). Fallback per
missing ticker: Stooq free CSV endpoint.

Output schema (prices.json):
{
  "as_of": "2026-06-11T21:35:02Z",      # when this file was generated (UTC)
  "prices": {
    "VTI": {"price": 295.12, "quote_date": "2026-06-11", "source": "yfinance"},
    ...
  },
  "errors": ["XYZ: not found on yfinance or stooq"]
}
"""

import csv
import io
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

TICKERS_FILE = Path(__file__).parent / "tickers.txt"
OUTPUT_FILE = Path(__file__).parent / "prices.json"

STOOQ_URL = "https://stooq.com/q/l/?s={symbol}.us&f=sd2t2ohlcv&h&e=csv"


def load_tickers() -> list[str]:
    tickers = []
    for line in TICKERS_FILE.read_text().splitlines():
        line = line.strip().upper()
        if line and not line.startswith("#"):
            tickers.append(line)
    return tickers


def fetch_yfinance(tickers: list[str]) -> dict:
    """Batch fetch via yfinance. Returns {ticker: {price, quote_date, source}}."""
    results = {}
    try:
        import yfinance as yf
    except ImportError:
        print("yfinance not installed; skipping to Stooq fallback", file=sys.stderr)
        return results

    try:
        # 5d window so we still get a close over weekends/holidays
        data = yf.download(
            tickers=" ".join(tickers),
            period="5d",
            interval="1d",
            group_by="ticker",
            auto_adjust=False,
            progress=False,
            threads=True,
        )
    except Exception as e:
        print(f"yfinance batch download failed: {e}", file=sys.stderr)
        return results

    for t in tickers:
        try:
            df = data[t] if len(tickers) > 1 else data
            closes = df["Close"].dropna()
            if closes.empty:
                continue
            last_date = closes.index[-1]
            results[t] = {
                "price": round(float(closes.iloc[-1]), 4),
                "quote_date": last_date.strftime("%Y-%m-%d"),
                "source": "yfinance",
            }
        except Exception:
            continue  # ticker missing from batch result; fallback handles it
    return results


def fetch_stooq(ticker: str) -> dict | None:
    """Single-ticker fallback via Stooq's free CSV endpoint."""
    url = STOOQ_URL.format(symbol=ticker.lower())
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "price-feed/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode("utf-8")
        rows = list(csv.DictReader(io.StringIO(text)))
        if not rows:
            return None
        row = rows[0]
        close = row.get("Close", "")
        if not close or close in ("N/D", "N/A"):
            return None
        return {
            "price": round(float(close), 4),
            "quote_date": row.get("Date", ""),
            "source": "stooq",
        }
    except Exception as e:
        print(f"stooq fetch failed for {ticker}: {e}", file=sys.stderr)
        return None


def main() -> int:
    tickers = load_tickers()
    print(f"Fetching {len(tickers)} tickers: {', '.join(tickers)}")

    prices = fetch_yfinance(tickers)
    missing = [t for t in tickers if t not in prices]

    if missing:
        print(f"yfinance missed {len(missing)}: {', '.join(missing)} — trying Stooq")
    for t in missing:
        result = fetch_stooq(t)
        if result:
            prices[t] = result

    errors = [f"{t}: not found on yfinance or stooq" for t in tickers if t not in prices]
    for e in errors:
        print(f"ERROR: {e}", file=sys.stderr)

    out = {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "prices": {t: prices[t] for t in sorted(prices)},
        "errors": errors,
    }
    OUTPUT_FILE.write_text(json.dumps(out, indent=2) + "\n")
    print(f"Wrote {OUTPUT_FILE} with {len(prices)}/{len(tickers)} tickers")

    # Fail the Action loudly only if we got nothing at all;
    # partial results still commit (better than stale-everything).
    return 0 if prices else 1


if __name__ == "__main__":
    sys.exit(main())
