"""Free end-of-day US equity / options scanner. Informational, not trading advice."""
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

TICKERS = ["ORCL", "VST", "CEG", "AAON", "NOW", "CGNX", "KOID", "MSTR", "BE", "GEV", "MRVL", "MU", "AAOI", "TSM", "COHR"]
OUT = Path(__file__).resolve().parent / "market_scan_latest.json"


def number(x, digits=4):
    try:
        v = float(x)
        return round(v, digits) if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def analyze(symbol):
    obj = {"ticker": symbol, "status": "NO_SIGNAL", "call": None, "put": None}
    try:
        t = yf.Ticker(symbol)
        df = t.history(period="4mo", interval="1d", auto_adjust=True)
        if df.empty or len(df) < 55:
            obj["reason"] = "Insufficient daily history"
            return obj
        close = df["Close"].astype(float)
        volume = df["Volume"].astype(float)
        delta = close.diff()
        gains = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
        losses = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
        if losses.iloc[-1] == 0:
            rsi = 100.0
        else:
            rs = gains.iloc[-1] / losses.iloc[-1]
            rsi = 100 - 100 / (1 + rs)
        ema20 = close.ewm(span=20, adjust=False).mean().iloc[-1]
        ema50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
        vol20 = volume.iloc[-21:-1].mean()
        ratio = float(volume.iloc[-1] / vol20) if vol20 > 0 else None
        last = float(close.iloc[-1])
        candle_date = str(df.index[-1].date())
        obj.update({"market_date": candle_date, "close": number(last, 2), "ema20": number(ema20, 2),
                    "ema50": number(ema50, 2), "rsi14": number(rsi, 2), "relative_volume": number(ratio, 2)})
        bullish = last > ema20 > ema50 and 52 <= rsi <= 70 and ratio is not None and ratio >= 1.2
        bearish = last < ema20 < ema50 and 30 <= rsi <= 48 and ratio is not None and ratio >= 1.2
        direction = "CALL" if bullish else "PUT" if bearish else None
        if not direction:
            obj["reason"] = "Trend / RSI / volume filters not aligned"
            return obj
        expiries = list(t.options)
        if not expiries:
            obj["reason"] = "No options chain available"
            return obj
        today = datetime.now(timezone.utc).date()
        candidates = [e for e in expiries if 7 <= (datetime.strptime(e, "%Y-%m-%d").date() - today).days <= 45]
        if not candidates:
            obj["reason"] = "No expiry between 7 and 45 calendar days"
            return obj
        expiry = candidates[0]
        chain = t.option_chain(expiry)
        contracts = chain.calls if direction == "CALL" else chain.puts
        contracts = contracts.copy()
        if contracts.empty:
            obj["reason"] = "Empty option chain"
            return obj
        contracts["distance"] = (contracts["strike"] - last).abs()
        contracts = contracts.sort_values("distance").head(12)
        eligible = []
        for _, row in contracts.iterrows():
            bid, ask = number(row.get("bid")), number(row.get("ask"))
            oi, vol = number(row.get("openInterest")), number(row.get("volume"))
            iv = number(row.get("impliedVolatility"))
            if bid is None or ask is None or bid <= 0 or ask <= bid:
                continue
            mid = (bid + ask) / 2
            spread = (ask - bid) / mid
            if spread > 0.15 or (oi or 0) < 100 or (vol or 0) < 10 or iv is None or iv <= 0:
                continue
            eligible.append({"symbol": row.get("contractSymbol"), "strike": number(row.get("strike"), 2),
                             "expiry": expiry, "bid": bid, "ask": ask, "mid": number(mid, 2),
                             "spread_pct": number(spread * 100, 2), "open_interest": int(oi),
                             "volume": int(vol), "implied_volatility": iv})
        if not eligible:
            obj["reason"] = "Options failed liquidity / bid-ask / IV filters"
            return obj
        obj[direction.lower()] = min(eligible, key=lambda x: abs(x["strike"] - last))
        obj["status"] = "WATCH_" + direction
        obj["reason"] = "Technical and basic option liquidity filters passed; manual verification required"
    except Exception as e:
        obj["reason"] = "Data unavailable: " + str(e)[:200]
    return obj


def main():
    rows = [analyze(s) for s in TICKERS]
    report = {"generated_at_utc": datetime.now(timezone.utc).isoformat(),
              "source": "yfinance (unofficial; may be delayed)",
              "disclaimer": "WATCH is not an entry recommendation. Confirm quote freshness, Greeks, catalysts and position risk.",
              "results": rows}
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for x in rows:
        print(f"{x['ticker']:6} {x['status']:12} {x.get('reason', '')}")
    print("Saved:", OUT)


if __name__ == "__main__":
    main()
