"""Free US equity/options watchlist scanner v2. Research only; no order execution."""
import json
import math
from datetime import datetime, timedelta, timezone, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

TICKERS = ["ORCL", "VST", "CEG", "AAON", "NOW", "CGNX", "KOID", "MSTR", "BE", "GEV", "MRVL", "MU", "AAOI", "TSM", "COHR"]
OUT = Path(__file__).resolve().parent / "market_scan_latest.json"
NY = ZoneInfo("America/New_York")


def num(value, digits=4):
    try:
        value = float(value)
        return round(value, digits) if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def market_session_complete(candle_date, now=None):
    """Conservative check; exchange holiday/early-close calendar not included."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(NY)
    if candle_date < local.date():
        return True
    if candle_date > local.date():
        return False
    return local.weekday() < 5 and local.time() >= time(16, 15)


def get_contract(ticker, direction, last, now):
    try:
        expiries = sorted(ticker.options or [])
        today = now.astimezone(NY).date()
        candidates = [x for x in expiries if 7 <= (datetime.strptime(x, "%Y-%m-%d").date() - today).days <= 45]
        if not candidates:
            return None, "No options expiry in 7-45 days"
        expiry = candidates[0]
        chain = ticker.option_chain(expiry)
        df = (chain.calls if direction == "CALL" else chain.puts).copy()
        if df.empty:
            return None, "Empty options chain"
        df["distance"] = (df["strike"].astype(float) - last).abs()
        eligible = []
        for _, row in df.sort_values("distance").head(16).iterrows():
            bid, ask = num(row.get("bid")), num(row.get("ask"))
            oi, vol, iv = num(row.get("openInterest")), num(row.get("volume")), num(row.get("impliedVolatility"))
            if bid is None or ask is None or bid <= 0 or ask <= bid:
                continue
            mid = (bid + ask) / 2
            spread = (ask - bid) / mid
            if spread > 0.15 or (oi or 0) < 100 or (vol or 0) < 10 or iv is None or iv <= 0:
                continue
            eligible.append({"symbol": str(row.get("contractSymbol")), "strike": num(row.get("strike"), 2),
                             "expiry": expiry, "bid": bid, "ask": ask, "mid": num(mid, 2),
                             "spread_pct": num(100 * spread, 2), "open_interest": int(oi),
                             "volume": int(vol), "implied_volatility": iv})
        if not eligible:
            return None, "No liquid contract (bid/ask, OI, volume, IV filters)"
        return eligible[0], "Options liquidity passed; quote freshness unverified"
    except Exception as exc:
        return None, "Options data unavailable: " + str(exc)[:160]


def analyze(symbol, now=None):
    now = now or datetime.now(timezone.utc)
    row = {"ticker": symbol, "status": "NO_SIGNAL", "call": None, "put": None,
           "reasons": [], "checks": {}}
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period="6mo", interval="1d", auto_adjust=True)
        if df.empty or len(df) < 55:
            row["status"] = "DATA_UNAVAILABLE"
            row["reasons"].append("Insufficient price history")
            return row
        df = df.dropna(subset=["Close", "Volume"])
        close, volume = df["Close"].astype(float), df["Volume"].astype(float)
        if len(close) < 55:
            row["status"] = "DATA_UNAVAILABLE"
            row["reasons"].append("Insufficient valid price rows")
            return row
        change = close.diff()
        up = change.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
        down = (-change.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
        rsi = 100 if down.iloc[-1] == 0 else 100 - 100/(1 + up.iloc[-1]/down.iloc[-1])
        ema20 = close.ewm(span=20, adjust=False).mean().iloc[-1]
        ema50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
        last = float(close.iloc[-1])
        candle_date = df.index[-1].date()
        completed = market_session_complete(candle_date, now)
        # Compare only completed candles: partial volume is not a valid daily volume ratio.
        vol20 = volume.iloc[-21:-1].mean()
        relvol = float(volume.iloc[-1] / vol20) if completed and vol20 > 0 else None
        age_days = (now.astimezone(NY).date() - candle_date).days
        row.update({"market_date": candle_date.isoformat(), "last_price": num(last, 2),
                    "price_type": "daily candle (may be intraday)" if not completed else "completed daily candle",
                    "ema20": num(ema20, 2), "ema50": num(ema50, 2),
                    "rsi14": num(rsi, 2), "relative_volume": num(relvol, 2),
                    "daily_candle_complete": completed, "data_age_calendar_days": age_days})
        if age_days < 0 or age_days > 4:
            row["status"] = "DATA_STALE"
            row["reasons"].append("Daily candle too old or future-dated; no signal")
            return row
        bullish = last > ema20 > ema50 and 52 <= rsi <= 70
        bearish = last < ema20 < ema50 and 30 <= rsi <= 48
        row["checks"] = {"bullish_trend_rsi": bool(bullish), "bearish_trend_rsi": bool(bearish),
                         "relative_volume_confirmed": bool(relvol is not None and relvol >= 1.2)}
        if not bullish and not bearish:
            row["reasons"].append("Neither CALL nor PUT trend+RSI conditions met")
            return row
        direction = "CALL" if bullish else "PUT"
        row["direction"] = direction
        row["status"] = "TECHNICAL_WATCH_" + direction
        if not completed:
            row["reasons"].append("Current daily candle incomplete; wait for session close for volume confirmation")
            return row
        if relvol is None or relvol < 1.2:
            row["reasons"].append("Relative daily volume below 1.20; no options confirmation requested")
            return row
        contract, reason = get_contract(ticker, direction, last, now)
        row["reasons"].append(reason)
        if contract:
            row[direction.lower()] = contract
            row["status"] = "WATCH_" + direction
        return row
    except Exception as exc:
        row["status"] = "DATA_UNAVAILABLE"
        row["reasons"].append("Price data unavailable: " + str(exc)[:160])
        return row


def main():
    now = datetime.now(timezone.utc)
    results = [analyze(symbol, now) for symbol in TICKERS]
    report = {"version": 2, "generated_at_utc": now.isoformat(),
              "source": "yfinance (unofficial; delayed or missing data possible)",
              "disclaimer": "WATCH is NOT an entry recommendation. Verify live quotes, Greeks, events and risk manually.",
              "results": results}
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for item in results:
        print(f"{item['ticker']:6} {item['status']:23} {'; '.join(item['reasons'])}")
    print("Saved:", OUT)


if __name__ == "__main__":
    main()
