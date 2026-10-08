"""Market Scanner v3: free, conservative CALL/PUT research watchlist.
Run: py market_scanner_v3.py. Does not place orders or overwrite v2 output.
"""
import json
import math
from datetime import datetime, timezone, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

TICKERS = ['ORCL','VST','CEG','AAON','NOW','CGNX','KOID','MSTR','BE','GEV','MRVL','MU','AAOI','TSM','COHR']
OUT = Path(__file__).resolve().parent / 'market_scan_v3_latest.json'
NY = ZoneInfo('America/New_York')


def number(value, digits=2):
    try:
        v = float(value)
        return round(v, digits) if math.isfinite(v) else None
    except (ValueError, TypeError):
        return None


def is_complete(day, now):
    ny = now.astimezone(NY)
    return day < ny.date() or (day == ny.date() and ny.weekday() < 5 and ny.time() >= time(16, 20))


def age_in_weekdays(day, now):
    today = now.astimezone(NY).date()
    if day > today:
        return 999
    count = 0
    while day < today:
        day += timedelta(days=1)
        if day.weekday() < 5:
            count += 1
    return count


def score_side(price, ema20, ema50, rsi, relvol, side):
    """Transparent heuristic (not calibrated probability), 0..100."""
    call = side == 'CALL'
    trend = (price > ema20 > ema50) if call else (price < ema20 < ema50)
    momentum = (52 <= rsi <= 70) if call else (30 <= rsi <= 48)
    distance = ((price / ema20 - 1) if call else (1 - price / ema20))
    points = 0
    points += 30 if trend else 0
    points += 25 if momentum else 0
    points += 10 if distance > 0.01 else (5 if distance > 0 else 0)
    points += 10 if ((ema20 > ema50) if call else (ema20 < ema50)) else 0
    points += 10 if ((55 <= rsi <= 65) if call else (35 <= rsi <= 45)) else 0
    points += 15 if relvol is not None and relvol >= 1.5 else (10 if relvol is not None and relvol >= 1.2 else 0)
    return min(100, points), trend and momentum


def option_contract(ticker, side, price, now):
    """Quote may be delayed. Missing bid/ask cannot be assumed liquid."""
    try:
        today = now.astimezone(NY).date()
        expiries = sorted(e for e in (ticker.options or [])
                          if 7 <= (datetime.strptime(e, '%Y-%m-%d').date() - today).days <= 45)
        if not expiries:
            return None, 'No expiry between 7 and 45 days'
        expiry = expiries[0]
        chain = ticker.option_chain(expiry)
        frame = (chain.calls if side == 'CALL' else chain.puts).copy()
        if frame.empty:
            return None, 'Empty option chain'
        frame['distance'] = (frame['strike'].astype(float) - price).abs()
        eligible = []
        for _, opt in frame.sort_values('distance').head(20).iterrows():
            bid, ask = number(opt.get('bid'), 4), number(opt.get('ask'), 4)
            oi, volume, iv = number(opt.get('openInterest'), 0), number(opt.get('volume'), 0), number(opt.get('impliedVolatility'), 4)
            if bid is None or ask is None or bid <= 0 or ask <= bid or iv is None or iv <= 0:
                continue
            mid = (bid + ask) / 2
            spread = (ask - bid) / mid
            if spread > 0.15 or (oi or 0) < 100 or (volume or 0) < 10:
                continue
            eligible.append({'contract_symbol': str(opt.get('contractSymbol')), 'expiry': expiry,
                             'strike': number(opt.get('strike')), 'bid': bid, 'ask': ask,
                             'mid': number(mid), 'spread_pct': number(100 * spread),
                             'open_interest': int(oi), 'option_volume': int(volume),
                             'implied_volatility': iv})
        if not eligible:
            return None, 'No contract passed spread/OI/volume/IV filters'
        return eligible[0], 'Basic options liquidity filters passed; quote freshness NOT verified'
    except Exception as exc:
        return None, 'Options unavailable: ' + str(exc)[:150]


def analyze(symbol, now):
    row = {'ticker': symbol, 'status': 'DATA_UNAVAILABLE', 'call_score': None,
           'put_score': None, 'candidate_direction': None, 'option': None, 'reasons': []}
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period='6mo', interval='1d', auto_adjust=True)
        if df.empty:
            row['reasons'].append('No daily history')
            return row
        df = df.dropna(subset=['Close', 'Volume'])
        if len(df) < 55:
            row['reasons'].append('Less than 55 valid candles')
            return row
        close, vol = df['Close'].astype(float), df['Volume'].astype(float)
        last = float(close.iloc[-1])
        candle_date = df.index[-1].date()
        completed = is_complete(candle_date, now)
        age = age_in_weekdays(candle_date, now)
        change = close.diff()
        gains = change.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
        losses = (-change.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
        loss = float(losses.iloc[-1]); gain = float(gains.iloc[-1])
        rsi = 50.0 if gain == 0 and loss == 0 else (100.0 if loss == 0 else 100 - 100/(1 + gain/loss))
        ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
        ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
        mean_vol = float(vol.iloc[-21:-1].mean())
        relvol = float(vol.iloc[-1]/mean_vol) if completed and mean_vol > 0 else None
        call_score, call_technical = score_side(last, ema20, ema50, rsi, relvol, 'CALL')
        put_score, put_technical = score_side(last, ema20, ema50, rsi, relvol, 'PUT')
        row.update({'market_date': candle_date.isoformat(), 'daily_candle_complete': completed,
                    'age_weekdays': age, 'last_price': number(last), 'ema20': number(ema20),
                    'ema50': number(ema50), 'rsi14': number(rsi),
                    'relative_volume': number(relvol), 'call_score': call_score, 'put_score': put_score})
        if age > 1:
            row['status'] = 'DATA_STALE'
            row['reasons'].append('Latest candle is older than one weekday; no signal')
            return row
        if not call_technical and not put_technical:
            row['status'] = 'NO_SIGNAL'
            row['reasons'].append('Neither side meets both EMA trend and RSI criteria')
            return row
        side = 'CALL' if call_technical else 'PUT'
        row['candidate_direction'] = side
        row['status'] = 'TECHNICAL_WATCH_' + side
        if not completed:
            row['reasons'].append('Intraday daily candle; wait for close to confirm relative volume')
            return row
        if relvol is None or relvol < 1.2:
            row['reasons'].append('Completed relative volume below 1.20; no option confirmation')
            return row
        contract, reason = option_contract(ticker, side, last, now)
        row['reasons'].append(reason)
        if contract:
            row['option'] = contract
            row['status'] = 'WATCH_' + side
            row['reasons'].append('WATCH only, not a trade entry: check live bid/ask, events and Greeks')
        return row
    except Exception as exc:
        row['status'] = 'DATA_UNAVAILABLE'
        row['reasons'].append('Data error: ' + str(exc)[:160])
        return row


def main():
    now = datetime.now(timezone.utc)
    rows = [analyze(s, now) for s in TICKERS]
    # Ranking includes technical candidates only; no-signal and stale data excluded.
    top_call = sorted((r for r in rows if r['candidate_direction'] == 'CALL' and r['status'].startswith(('TECHNICAL_WATCH_', 'WATCH_'))),
                      key=lambda r: (-r['call_score'], r['ticker']))[:3]
    top_put = sorted((r for r in rows if r['candidate_direction'] == 'PUT' and r['status'].startswith(('TECHNICAL_WATCH_', 'WATCH_'))),
                     key=lambda r: (-r['put_score'], r['ticker']))[:3]
    report = {'version': 3, 'generated_at_utc': now.isoformat(),
              'source': 'yfinance unofficial (data may be delayed, missing or inaccurate)',
              'score_note': '0-100 heuristic ranking only; NOT win probability or investment advice',
              'top_call_candidates': [{'ticker': r['ticker'], 'score': r['call_score'], 'status': r['status']} for r in top_call],
              'top_put_candidates': [{'ticker': r['ticker'], 'score': r['put_score'], 'status': r['status']} for r in top_put],
              'results': rows}
    # Never replace previous successful report with all failed downloads.
    if all(r['status'] == 'DATA_UNAVAILABLE' for r in rows):
        print('ERROR: All data downloads failed; previous report preserved')
        raise SystemExit(2)
    tmp = OUT.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(OUT)
    for r in rows:
        print(f"{r['ticker']:6} {r['status']:24} CALL {str(r['call_score']):>3} PUT {str(r['put_score']):>3}  {'; '.join(r['reasons'])}")
    print('TOP CALL:', ', '.join(f"{r['ticker']}({r['call_score']})" for r in top_call) or 'None')
    print('TOP PUT :', ', '.join(f"{r['ticker']}({r['put_score']})" for r in top_put) or 'None')
    print('Saved:', OUT)


if __name__ == '__main__':
    main()
