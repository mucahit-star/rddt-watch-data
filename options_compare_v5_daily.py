"""Daily-only comparison of local v4 option snapshots. Research only."""
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
ARCHIVE = ROOT / 'options_archive'
LATEST = ROOT / 'options_archive_latest.json'
OUTPUT = ROOT / 'options_compare_v5_daily_latest.json'
NY = ZoneInfo('America/New_York')


def load(path):
    with path.open(encoding='utf-8') as handle:
        obj = json.load(handle)
    if not isinstance(obj, dict) or not isinstance(obj.get('tickers'), list):
        raise ValueError('Invalid archive structure')
    dt = datetime.fromisoformat(obj['generated_at_utc'].replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('Timezone missing')
    return dt.astimezone(timezone.utc), obj


def val(x):
    try:
        n = float(x)
        return n if math.isfinite(n) else None
    except (ValueError, TypeError):
        return None


def difference(a, b, factor=1, digits=2):
    a, b = val(a), val(b)
    return round((b-a)*factor, digits) if a is not None and b is not None else None


def change_pct(a, b):
    a, b = val(a), val(b)
    return round(100*(b/a-1), 2) if a is not None and b is not None and a>0 else None


def contracts(obj):
    return {c['symbol']: (t['ticker'], c) for t in obj['tickers']
            for c in t.get('contracts', []) if c.get('symbol')}


def save(obj):
    fd, tmp = tempfile.mkstemp(prefix='.v5daily_', suffix='.tmp', dir=ROOT)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(obj, handle, indent=2, ensure_ascii=False, allow_nan=False)
        os.replace(tmp, OUTPUT)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main():
    if not LATEST.exists():
        print('ERROR: Missing options_archive_latest.json')
        return 2
    latest_time, latest = load(LATEST)
    latest_day = latest_time.astimezone(NY).date()
    eligible = []
    for path in ARCHIVE.glob('options_*.json'):
        try:
            dt, data = load(path)
            if dt < latest_time and dt.astimezone(NY).date() < latest_day:
                eligible.append((dt, path, data))
        except (ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
            print('Skipping invalid:', path.name, str(exc)[:80])
    if not eligible:
        print('INSUFFICIENT_HISTORY: No archive from an earlier New York calendar date.')
        print('Existing v5 report has not been overwritten. Collect data on another trading day.')
        return 0
    previous_time, previous_path, previous = max(eligible, key=lambda x: x[0])
    prev_map, new_map = contracts(previous), contracts(latest)
    rows = []
    for symbol in sorted(prev_map.keys() & new_map.keys()):
        ticker, a = prev_map[symbol]
        _, b = new_map[symbol]
        rows.append({
            'ticker': ticker, 'symbol': symbol, 'side': b.get('side'),
            'expiry': b.get('expiry'), 'strike': b.get('strike'),
            'liquidity_checks_passed_latest': bool(b.get('liquidity_checks_passed', False)),
            'quote_freshness_verified': False,
            'mid_change_pct_unverified': change_pct(a.get('mid'), b.get('mid')),
            'spread_change_percentage_points_unverified': difference(a.get('spread_pct'), b.get('spread_pct')),
            'iv_change_percentage_points_unverified': difference(a.get('implied_volatility'), b.get('implied_volatility'), 100),
            'open_interest_raw_difference_unverified': difference(a.get('open_interest'), b.get('open_interest'), 1, 0),
            'volume_raw_difference_unverified': difference(a.get('volume'), b.get('volume'), 1, 0),
            'warnings': sorted(set(a.get('warnings', [])) | set(b.get('warnings', []))),
        })
    if not rows:
        print('NO_MATCH: No identical contract symbols between dates; report unchanged.')
        return 0
    summary = []
    for item in latest['tickers']:
        ticker = item['ticker']
        found = [x for x in rows if x['ticker'] == ticker]
        summary.append({'ticker': ticker, 'matched_contracts': len(found),
                        'liquid_latest': sum(x['liquidity_checks_passed_latest'] for x in found),
                        'call_matched': sum(x['side']=='CALL' for x in found),
                        'put_matched': sum(x['side']=='PUT' for x in found)})
    report = {'version': '5-daily', 'status': 'DAILY_COMPARISON_UNVERIFIED',
              'generated_at_utc': datetime.now(timezone.utc).isoformat(),
              'previous_snapshot_utc': previous_time.isoformat(),
              'latest_snapshot_utc': latest_time.isoformat(),
              'previous_archive_file': previous_path.name,
              'date_basis': 'America/New_York calendar date (not exchange trading calendar)',
              'by_ticker': summary, 'contracts': rows,
              'cautions': ['Different NY calendar dates do not guarantee distinct market sessions (weekends/holidays).',
                           'Quotes may be delayed or stale; no quote timestamps.',
                           'OI changes are not verified directional flows; daily volume resets.',
                           'No trade-entry signals; no verified historical performance.']}
    save(report)
    print('V5 DAILY OK | previous=', previous_time.isoformat(), 'latest=', latest_time.isoformat())
    for s in summary:
        print(f"{s['ticker']:5} matched={s['matched_contracts']:3} liquid_latest={s['liquid_latest']:3}")
    print('Saved:', OUTPUT)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
