#!/usr/bin/env python3
"""
fetch_prices.py — runs in GitHub Actions (NOT in the Claude sandbox).

Reads tickers.txt, fetches the latest daily close for each ticker,
writes prices.json.

Primary source: yfinance batch download over an explicit date window
anchored to US/Eastern time. Tickers the batch misses, or that come back
a session behind the rest, are retried one at a time. Stooq is a last resort.

Why an explicit window: with period="5d", runs that GitHub delays past
00:00 UTC (7-8pm ET) frequently returned the PREVIOUS session's close
(24 of 33 such runs, Jun-Nov 2026), while runs before 00:00 UTC were
right 50 of 51 times. Anchoring start/end to the Eastern date removes the
UTC-rollover dependence. The workflow also runs on weekday mornings as a
backstop, after Yahoo has finalized the prior session.

Guards (added Nov 2026, after GitHub started delaying the 6:15am backstop to
~1:45pm ET and the evening run past 00:00 UTC):
  1. No intraday prices: before 4:30pm ET, today's bar is ignored, so a run
     during market hours returns the previous session's finalized close
     instead of a live price dated today.
  2. Never go backwards: a ticker whose new quote_date is older than the one
     already in prices.json keeps the newer price; a ticker this run missed
     keeps its last known price (still listed in "errors").
  3. No-op runs don't rewrite prices.json, so the workflow's "No price
     changes" check works and as_of means "when prices last changed".

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
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

TICKERS_FILE = Path(__file__).parent / "tickers.txt"
OUTPUT_FILE = Path(__file__).parent / "prices.json"
ET = ZoneInfo("America/New_York")
# Today's daily bar counts only after this ET time (close is 4:00pm; Yahoo's
# official closing price settles a little later).
CLOSE_CUTOFF = time(16, 30)

STOOQ_URL = "https://stooq.com/q/l/?s={symbol}.us&f=sd2t2ohlcv&h&e=csv"


def load_tickers() -> list[str]:
    tickers = []
    for line in TICKERS_FILE.read_text().splitlines():
        line = line.strip().upper()
        if line and not line.startswith("#"):
            tickers.append(line)
    return tickers


def window() -> tuple[str, str]:
    """[start, end) in Eastern dates. end = tomorrow (exclusive) so today's
    session is always inside the window, whatever the UTC clock says."""
    today_et = datetime.now(ET).date()
    return (today_et - timedelta(days=10)).isoformat(), (today_et + timedelta(days=1)).isoformat()


def unfinished_session() -> str | None:
    """Today's ET date while its session may still be open or unsettled,
    else None. Bars dated this day are not closes yet and are ignored."""
    now = datetime.now(ET)
    return now.date().isoformat() if now.time() < CLOSE_CUTOFF else None


def last_close(df) -> dict | None:
    closes = df["Close"].dropna()
    skip = unfinished_session()
    if skip and not closes.empty:
        closes = closes[closes.index.strftime("%Y-%m-%d") != skip]
    if closes.empty:
        return None
    return {
        "price": round(float(closes.iloc[-1]), 4),
        "quote_date": closes.index[-1].strftime("%Y-%m-%d"),
        "source": "yfinance",
    }


def fetch_yfinance(tickers: list[str]) -> dict:
    """Batch fetch via yfinance. Returns {ticker: {price, quote_date, source}}."""
    results = {}
    try:
        import yfinance as yf
    except ImportError:
        print("yfinance not installed; skipping to Stooq fallback", file=sys.stderr)
        return results

    start, end = window()
    print(f"yfinance window: {start} .. {end} (end exclusive, US/Eastern)")
    try:
        data = yf.download(
            tickers=" ".join(tickers),
            start=start,
            end=end,
            interval="1d",
            group_by="ticker",
            auto_adjust=False,
            progress=False,
            threads=True,
        )
    except Exception as e:
        print(f"yfinance batch download failed: {e}", file=sys.stderr)
        data = None

    if data is not None:
        for t in tickers:
            try:
                df = data[t] if len(tickers) > 1 else data
                r = last_close(df)
                if r:
                    results[t] = r
            except Exception:
                continue  # missing from batch; retried below

    # Retry singly: tickers the batch missed, and tickers a session behind the
    # newest date in the batch (mutual funds post NAV later, so a lag there is
    # expected and simply retried, not treated as an error).
    newest = max((r["quote_date"] for r in results.values()), default="")
    retry = [t for t in tickers if t not in results or results[t]["quote_date"] < newest]
    if retry:
        print(f"retrying singly: {', '.join(retry)}")
    for t in retry:
        try:
            df = yf.Ticker(t).history(start=start, end=end, interval="1d", auto_adjust=False)
            r = last_close(df)
            if r and (t not in results or r["quote_date"] >= results[t]["quote_date"]):
                results[t] = r
        except Exception as e:
            print(f"single retry failed for {t}: {e}", file=sys.stderr)
    return results


def fetch_stooq(ticker: str) -> dict | None:
    """Last-resort single-ticker fallback via Stooq's free CSV endpoint."""
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
        if row.get("Date", "") == unfinished_session():
            print(f"stooq: {ticker} only has today's live price; skipped", file=sys.stderr)
            return None
        return {
            "price": round(float(close), 4),
            "quote_date": row.get("Date", ""),
            "source": "stooq",
        }
    except Exception as e:
        print(f"stooq fetch failed for {ticker}: {e}", file=sys.stderr)
        return None


def load_previous() -> dict:
    try:
        return json.loads(OUTPUT_FILE.read_text())
    except Exception:
        return {}


def main() -> int:
    tickers = load_tickers()
    print(f"Fetching {len(tickers)} tickers: {', '.join(tickers)}")
    skip = unfinished_session()
    if skip:
        print(f"Before {CLOSE_CUTOFF:%H:%M} ET: ignoring {skip} bars (session not final)")
    previous = load_previous()
    prev_prices = previous.get("prices") or {}

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

    # Never go backwards: keep a newer previous price over an older new one,
    # and carry forward the last known price for tickers this run missed.
    kept_newer, carried = [], []
    for t in tickers:
        old = prev_prices.get(t)
        if not old or not old.get("quote_date"):
            continue
        if t not in prices:
            prices[t] = old
            carried.append(t)
        elif old["quote_date"] > prices[t]["quote_date"]:
            prices[t] = old
            kept_newer.append(t)
    if kept_newer:
        print(f"Fetched an older session than prices.json has; kept newer price for: {', '.join(kept_newer)}")
    if carried:
        print(f"Kept last known price for missed tickers: {', '.join(carried)}")

    out_prices = {t: prices[t] for t in sorted(prices)}
    if out_prices == prev_prices and errors == (previous.get("errors") or []):
        print("Prices unchanged since last run; leaving prices.json as is.")
        return 0

    out = {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "prices": out_prices,
        "errors": errors,
    }
    OUTPUT_FILE.write_text(json.dumps(out, indent=2) + "\n")
    dates = sorted({p["quote_date"] for p in prices.values()})
    print(f"Wrote {OUTPUT_FILE} with {len(prices)}/{len(tickers)} tickers; quote dates: {dates}")

    # Fail the Action loudly only if we got nothing at all;
    # partial results still commit (better than stale-everything).
    return 0 if prices else 1


if __name__ == "__main__":
    sys.exit(main())
