"""Conservative cross-session options archive comparison. Research only."""
import json
import math
import os
import tempfile
from datetime import datetime, timezone, time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
ARCHIVE = ROOT / 'options_archive'
LATEST = ROOT / 'options_archive_latest.json'
OUTPUT = ROOT / 'options_compare_v5_1_latest.json'
NY = ZoneInfo('America/New_York')


def load(path):
    with path.open(encoding='utf-8') as f:
        obj = json.load(f)
    if not isinstance(obj, dict) or not isinstance(obj.get('tickers'), list):
        raise ValueError('Invalid archive structure')
    dt = datetime.fromisoformat(obj['generated_at_utc'].replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('Timestamp missing timezone')
    return dt.astimezone(timezone.utc), obj


def session_date(dt):
    """Only accept NY weekdays at/after 16:20, not an exchange holiday guarantee."""
    local = dt.astimezone(NY)
    if local.weekday() >= 5 or local.time() < time(16, 20):
        return None
    return local.date()


def num(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def diff(a, b, factor=1, digits=2):
    a, b = num(a), num(b)
    return round((b-a)*factor, digits) if a is not None and b is not None else None


def pct(a, b):
    a, b = num(a), num(b)
    return round(100*(b/a-1), 2) if a is not None and b is not None and a > 0 else None


def contracts(obj):
    result = {}
    for ticker in obj['tickers']:
        for c in ticker.get('contracts', []):
            symbol = c.get('symbol')
            if symbol:
                result[symbol] = (ticker.get('ticker'), c)
    return result


def save(obj):
    fd, tmp = tempfile.mkstemp(prefix='.v51_', suffix='.tmp', dir=ROOT)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False, indent=2, allow_nan=False)
        os.replace(tmp, OUTPUT)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main():
    if not LATEST.exists():
        print('ERROR: Missing options_archive_latest.json')
        return 2
    try:
        latest_dt, latest = load(LATEST)
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        print('ERROR: Invalid latest archive:', exc)
        return 2
    latest_session = session_date(latest_dt)
    if latest_session is None:
        print('INSUFFICIENT_HISTORY: Latest snapshot is not from NY weekday after 16:20.')
        print('No report written. V5.1 exit code 3.')
        return 3
    eligible = []
    if ARCHIVE.exists():
        for path in ARCHIVE.glob('options_*.json'):
            try:
                dt, obj = load(path)
                prev_session = session_date(dt)
                if prev_session is not None and prev_session < latest_session and dt < latest_dt:
                    eligible.append((dt, path, obj))
            except (ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
                print('Skipping invalid archive:', path.name, str(exc)[:80])
    if not eligible:
        print('INSUFFICIENT_HISTORY: No earlier NY weekday post-close archive.')
        print('No report written. V5.1 exit code 3.')
        return 3
    previous_dt, previous_path, previous = max(eligible, key=lambda x: x[0])
    prev, curr = contracts(previous), contracts(latest)
    rows = []
    for symbol in sorted(prev.keys() & curr.keys()):
        ticker, a = prev[symbol]
        _, b = curr[symbol]
        rows.append({
            'ticker': ticker, 'symbol': symbol, 'side': b.get('side'),
            'expiry': b.get('expiry'), 'strike': b.get('strike'),
            'liquidity_checks_passed_latest': bool(b.get('liquidity_checks_passed')),
            'quote_freshness_verified': False,
            'mid_change_pct_unverified': pct(a.get('mid'), b.get('mid')),
            'spread_change_percentage_points_unverified': diff(a.get('spread_pct'), b.get('spread_pct')),
            'iv_change_percentage_points_unverified': diff(a.get('implied_volatility'), b.get('implied_volatility'), 100),
            'open_interest_raw_difference_unverified': diff(a.get('open_interest'), b.get('open_interest'), digits=0),
            'volume_raw_difference_unverified': diff(a.get('volume'), b.get('volume'), digits=0),
            'warnings': sorted(set(a.get('warnings') or []) | set(b.get('warnings') or [])),
        })
    if not rows:
        print('INSUFFICIENT_HISTORY: No matching contract symbols across sessions.')
        return 3
    summary = []
    for item in latest['tickers']:
        ticker = item.get('ticker')
        subset = [r for r in rows if r['ticker'] == ticker]
        summary.append({'ticker': ticker, 'matched_contracts': len(subset),
                        'liquid_latest': sum(r['liquidity_checks_passed_latest'] for r in subset),
                        'call_matched': sum(r['side'] == 'CALL' for r in subset),
                        'put_matched': sum(r['side'] == 'PUT' for r in subset)})
    report = {'version': '5.1', 'status': 'CROSS_SESSION_COMPARISON_UNVERIFIED',
              'generated_at_utc': datetime.now(timezone.utc).isoformat(),
              'previous_snapshot_utc': previous_dt.isoformat(),
              'latest_snapshot_utc': latest_dt.isoformat(),
              'previous_archive_file': previous_path.name,
              'date_basis': 'NY weekday after 16:20; exchange holidays not verified',
              'by_ticker': summary, 'contracts': rows,
              'cautions': ['NYSE/Nasdaq holiday calendar not checked.',
                           'Quote freshness unverified; bid/ask may be stale.',
                           'OI difference is not directional flow; volume resets daily.',
                           'No entry signals or validated performance.']}
    save(report)
    print('V5.1 OK | previous=', previous_dt.isoformat(), 'latest=', latest_dt.isoformat())
    print('Matched contracts:', len(rows))
    print('Saved:', OUTPUT)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
