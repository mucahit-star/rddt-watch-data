"""Free options-chain snapshot archive. Research only; no trade execution."""
import json
import math
import os
import tempfile
import time
from datetime import datetime, timezone, date
from pathlib import Path

import yfinance as yf

ROOT = Path(__file__).resolve().parent
ARCHIVE = ROOT / 'options_archive'
TICKERS = ['ORCL','VST','CEG','AAON','NOW','CGNX','KOID','MSTR','BE','GEV','MRVL','MU','AAOI','TSM','COHR']
MIN_DTE, MAX_DTE = 7, 45
MAX_EXPIRIES = 2
MAX_CONTRACTS_PER_SIDE = 12


def number(value, digits=5):
    try:
        x = float(value)
        return round(x, digits) if math.isfinite(x) else None
    except (ValueError, TypeError):
        return None


def integer(value):
    x = number(value, 0)
    return int(x) if x is not None else None


def serialize_contract(row, side, expiry, spot):
    strike = number(row.get('strike'), 2)
    bid, ask = number(row.get('bid'), 4), number(row.get('ask'), 4)
    mid = round((bid + ask) / 2, 4) if bid is not None and ask is not None and bid > 0 and ask >= bid else None
    spread = round(100 * (ask - bid) / mid, 2) if mid and mid > 0 else None
    oi, vol = integer(row.get('openInterest')), integer(row.get('volume'))
    iv = number(row.get('impliedVolatility'))
    last_trade = row.get('lastTradeDate')
    try:
        last_trade = last_trade.isoformat() if last_trade is not None and not str(last_trade) == 'NaT' else None
    except (AttributeError, ValueError):
        last_trade = None
    warnings = []
    if bid is None or ask is None or bid <= 0 or ask <= bid:
        warnings.append('invalid_or_missing_bid_ask')
    if spread is None or spread > 15:
        warnings.append('spread_over_15pct_or_unknown')
    if oi is None or oi < 100:
        warnings.append('open_interest_under_100_or_unknown')
    if vol is None or vol < 10:
        warnings.append('volume_under_10_or_unknown')
    if iv is None or iv <= 0:
        warnings.append('iv_missing_or_invalid')
    return {'symbol': str(row.get('contractSymbol', '')), 'side': side, 'expiry': expiry,
            'strike': strike, 'spot_at_fetch': spot, 'bid': bid, 'ask': ask, 'mid': mid,
            'spread_pct': spread, 'last_price': number(row.get('lastPrice'), 4),
            'volume': vol, 'open_interest': oi, 'implied_volatility': iv,
            'last_trade_at': last_trade, 'quote_timestamp_unavailable': True,
            'liquidity_checks_passed': len(warnings) == 0,
            'warnings': warnings}


def collect(symbol, now):
    item = {'ticker': symbol, 'status': 'ERROR', 'spot': None, 'expiries': [], 'contracts': [], 'errors': []}
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period='5d', interval='1d', auto_adjust=True)
        if hist.empty:
            raise ValueError('No price history')
        spot = number(hist['Close'].iloc[-1], 2)
        if spot is None or spot <= 0:
            raise ValueError('Invalid reference price')
        item['spot'] = spot
        today = now.date()
        valid = []
        for expiry in ticker.options or []:
            try:
                dte = (date.fromisoformat(expiry) - today).days
                if MIN_DTE <= dte <= MAX_DTE:
                    valid.append(expiry)
            except ValueError:
                pass
        for expiry in sorted(valid)[:MAX_EXPIRIES]:
            try:
                chain = ticker.option_chain(expiry)
                count = 0
                for side, df in [('CALL', chain.calls), ('PUT', chain.puts)]:
                    if df is None or df.empty:
                        continue
                    frame = df.copy()
                    frame['distance'] = (frame['strike'].astype(float) - spot).abs()
                    for _, row in frame.sort_values('distance').head(MAX_CONTRACTS_PER_SIDE).iterrows():
                        item['contracts'].append(serialize_contract(row, side, expiry, spot))
                        count += 1
                item['expiries'].append({'date': expiry, 'contracts_saved': count})
            except Exception as exc:
                item['errors'].append(f'{expiry}: {type(exc).__name__}: {str(exc)[:130]}')
        item['status'] = 'OK' if item['contracts'] and not item['errors'] else ('PARTIAL' if item['contracts'] else 'NO_DATA')
        if not valid:
            item['errors'].append('No expiry in 7-45 calendar days')
    except Exception as exc:
        item['errors'].append(f'{type(exc).__name__}: {str(exc)[:130]}')
    return item


def atomic_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.options_', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False, indent=2, allow_nan=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main():
    now = datetime.now(timezone.utc)
    results = []
    for ticker in TICKERS:
        result = collect(ticker, now)
        results.append(result)
        print(f"{ticker:5} {result['status']:8} contracts={len(result['contracts']):3} errors={len(result['errors'])}", flush=True)
        time.sleep(0.4)
    total = sum(len(r['contracts']) for r in results)
    report = {'schema_version': 1, 'generated_at_utc': now.isoformat(), 'source': 'yfinance (unofficial, delayed or incomplete)',
              'quote_freshness': 'Not independently verified; lastTradeDate is trade time, NOT bid/ask quote time',
              'disclaimer': 'Research snapshots only. Not live quotes, execution signals, or recommendations.',
              'tickers': results, 'total_contracts': total}
    path = ARCHIVE / f"options_{now.strftime('%Y%m%dT%H%M%SZ')}.json"
    atomic_json(path, report)
    atomic_json(ROOT / 'options_archive_latest.json', report)
    print('Saved:', path)
    print('Latest:', ROOT / 'options_archive_latest.json')
    if total == 0:
        print('ERROR: No contracts were retrieved. Do not treat this as a valid snapshot.')
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
