"""Market Scanner v6.1: offline, conservative option contract ranking.
Uses existing v3 and v4 JSON files; no broker connection or trade signals.
Run: py market_scanner_v6_1.py
"""
import json
import math
import os
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCAN = ROOT / 'market_scan_v3_latest.json'
ARCHIVE = ROOT / 'options_archive_latest.json'
OUT = ROOT / 'market_scan_v6_1_latest.json'
MAX_AGE_HOURS = 36


def load(path):
    with path.open(encoding='utf-8') as f:
        return json.load(f)


def timestamp(obj):
    dt = datetime.fromisoformat(obj['generated_at_utc'].replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('Timestamp has no timezone')
    return dt.astimezone(timezone.utc)


def finite(x):
    try:
        n = float(x)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def ranking(scan, archive, now):
    scan_time, archive_time = timestamp(scan), timestamp(archive)
    if scan_time > now + timedelta(minutes=5) or archive_time > now + timedelta(minutes=5):
        raise ValueError('Source timestamp is in the future')
    age_scan = (now - scan_time).total_seconds() / 3600
    age_archive = (now - archive_time).total_seconds() / 3600
    fresh = max(age_scan, age_archive) <= MAX_AGE_HOURS
    technical = {x['ticker']: x for x in scan['results'] if isinstance(x, dict) and x.get('ticker')}
    candidates = []
    preliminary = []
    rejected = []
    for stock in archive['tickers']:
        ticker = stock.get('ticker')
        tech = technical.get(ticker, {})
        for c in stock.get('contracts', []):
            side = c.get('side')
            if side not in ('CALL', 'PUT'):
                continue
            symbol = c.get('symbol')
            score = finite(tech.get('call_score' if side == 'CALL' else 'put_score'))
            spread = finite(c.get('spread_pct'))
            oi = finite(c.get('open_interest'))
            vol = finite(c.get('volume'))
            iv = finite(c.get('implied_volatility'))
            bid, ask = finite(c.get('bid')), finite(c.get('ask'))
            try:
                expiry = datetime.strptime(c['expiry'], '%Y-%m-%d').date()
                dte = (expiry - archive_time.date()).days
            except (ValueError, TypeError, KeyError):
                dte = None
            reasons = []
            if not fresh:
                reasons.append('SOURCE_DATA_STALE')
            if tech.get('candidate_direction') != side or tech.get('status') not in ('TECHNICAL_WATCH_'+side, 'WATCH_'+side):
                reasons.append('NO_MATCHING_TECHNICAL_WATCH')
            if not tech.get('daily_candle_complete'):
                reasons.append('DAILY_CANDLE_INCOMPLETE')
            if tech.get('age_weekdays') is None or tech['age_weekdays'] > 1:
                reasons.append('TECHNICAL_CANDLE_OLD_OR_UNKNOWN')
            if stock.get('status') not in ('OK', 'PARTIAL'):
                reasons.append('ARCHIVE_TICKER_NOT_OK')
            if bid is None or ask is None or bid <= 0 or ask <= bid:
                reasons.append('INVALID_BID_ASK')
            if spread is None or spread > 15 or spread < 0:
                reasons.append('SPREAD_TOO_WIDE_OR_MISSING')
            if oi is None or oi < 100:
                reasons.append('LOW_OR_MISSING_OPEN_INTEREST')
            if vol is None or vol < 10:
                reasons.append('LOW_OR_MISSING_VOLUME')
            if iv is None or iv <= 0:
                reasons.append('INVALID_IV')
            if dte is None or not 7 <= dte <= 45:
                reasons.append('DTE_OUT_OF_RANGE')
            if score is None:
                reasons.append('TECHNICAL_SCORE_MISSING')
            # All 0-100 values are heuristics, never probabilities.
            liquidity = 0
            if spread is not None and 0 <= spread <= 15:
                liquidity += 40 if spread <= 5 else (30 if spread <= 10 else 15)
            if oi is not None:
                liquidity += 30 if oi >= 1000 else (20 if oi >= 300 else (10 if oi >= 100 else 0))
            if vol is not None:
                liquidity += 30 if vol >= 200 else (20 if vol >= 50 else (10 if vol >= 10 else 0))
            total = round(0.65 * (score or 0) + 0.35 * liquidity, 1)
            only_incomplete = reasons == ['DAILY_CANDLE_INCOMPLETE']
            status = ('PRELIMINARY_WATCH' if only_incomplete else
                      ('VALIDATED_WATCH' if not reasons else 'REJECTED'))
            item = {'ticker': ticker, 'side': side, 'symbol': symbol, 'expiry': c.get('expiry'),
                    'strike': c.get('strike'), 'dte_calendar_days': dte,
                    'technical_score': score, 'liquidity_score': liquidity, 'combined_score': total,
                    'bid': bid, 'ask': ask, 'spread_pct': spread, 'open_interest': oi,
                    'volume': vol, 'implied_volatility': iv,
                    'data_status': status,
                    'rejection_reasons': reasons,
                    'quote_freshness_verified': False,
                    'trade_entry_signal': False}
            (preliminary if only_incomplete else (candidates if not reasons else rejected)).append(item)
    candidates.sort(key=lambda x: (-x['combined_score'], x['spread_pct'], x['ticker'], x['symbol'] or ''))
    preliminary.sort(key=lambda x: (-x['combined_score'], x['spread_pct'], x['ticker'], x['symbol'] or ''))
    rejected.sort(key=lambda x: (-x['combined_score'], x['ticker'], x['symbol'] or ''))
    return {'version': '6.1', 'generated_at_utc': now.isoformat(),
            'source_scan_utc': scan_time.isoformat(), 'source_archive_utc': archive_time.isoformat(),
            'source_age_hours': {'scan': round(age_scan, 2), 'archive': round(age_archive, 2)},
            'source_snapshot_recent': fresh,
            'quote_freshness_verified': False,
            'note': 'Offline heuristic ranking only. VALIDATED_WATCH is not a trade signal. PRELIMINARY_WATCH awaits completed daily candle. Quote timestamps unverified; scores are not probabilities. IV rank, Greeks and backtest are NOT computed.',
            'top_call': [c for c in candidates if c['side'] == 'CALL'][:10],
            'top_put': [c for c in candidates if c['side'] == 'PUT'][:10],
            'top_preliminary_call': [c for c in preliminary if c['side'] == 'CALL'][:10],
            'top_preliminary_put': [c for c in preliminary if c['side'] == 'PUT'][:10],
            'eligible_research_count': len(candidates), 'preliminary_count': len(preliminary),
            'rejected_count': len(rejected),
            'preliminary': preliminary, 'rejected': rejected}


def atomic_save(obj):
    fd, tmp = tempfile.mkstemp(prefix='.v6_1_', suffix='.tmp', dir=ROOT)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False, indent=2, allow_nan=False)
        os.replace(tmp, OUT)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main():
    if not SCAN.exists() or not ARCHIVE.exists():
        print('ERROR: Need market_scan_v3_latest.json and options_archive_latest.json')
        return 2
    try:
        scan, archive = load(SCAN), load(ARCHIVE)
        if not isinstance(scan.get('results'), list) or not isinstance(archive.get('tickers'), list):
            raise ValueError('Unexpected JSON structure')
        report = ranking(scan, archive, datetime.now(timezone.utc))
        atomic_save(report)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        print('ERROR:', type(exc).__name__, str(exc)[:200])
        return 2
    print('V6.1 OK | source_snapshot_recent=', report['source_snapshot_recent'])
    print('Research candidates:', report['eligible_research_count'], 'Rejected:', report['rejected_count'])
    print('Preliminary:', report['preliminary_count'])
    for side in ('call', 'put'):
        rows = report['top_'+side]
        print('TOP', side.upper()+':', ', '.join(f"{x['ticker']} {x['symbol']} ({x['combined_score']})" for x in rows) or 'None')
    for side in ('call', 'put'):
        rows = report['top_preliminary_'+side]
        print('PRELIMINARY', side.upper()+':')
        for x in rows:
            print(' ', x['ticker'], x['symbol'], 'strike=', x['strike'], 'expiry=', x['expiry'],
                  'score=', x['combined_score'], 'spread%=', x['spread_pct'],
                  'OI=', x['open_interest'], 'volume=', x['volume'])
    print('Quote freshness unverified; NO trade entry signals.')
    print('Saved:', OUT)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
