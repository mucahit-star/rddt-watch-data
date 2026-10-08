"""Compare locally archived options snapshots. No network, no trading, no quote freshness claims."""
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ARCHIVE = ROOT / 'options_archive'
OUTPUT = ROOT / 'options_compare_v5_latest.json'
LATEST = ROOT / 'options_archive_latest.json'
MIN_OI_GAP_HOURS = 18  # OI is normally published on a subsequent session, not intraday.


def load(path):
    with path.open(encoding='utf-8') as f:
        data = json.load(f)
    if not isinstance(data, dict) or not isinstance(data.get('tickers'), list):
        raise ValueError('Unexpected options archive format: ' + str(path))
    stamp = datetime.fromisoformat(data['generated_at_utc'].replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('Snapshot timestamp must include timezone')
    return stamp.astimezone(timezone.utc), data


def number(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def diff(a, b, decimals=4):
    a, b = number(a), number(b)
    return round(b - a, decimals) if a is not None and b is not None else None


def pct(a, b):
    a, b = number(a), number(b)
    return round((b / a - 1) * 100, 2) if a is not None and b is not None and a > 0 else None


def contract_map(data):
    return {c['symbol']: (t['ticker'], c) for t in data['tickers']
            for c in t.get('contracts', []) if c.get('symbol')}


def atomic_write(path, data):
    fd, tmp = tempfile.mkstemp(prefix='.v5_', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main():
    if not LATEST.exists():
        print('ERROR: Missing options_archive_latest.json; run options_archive_v4.py first.')
        return 2
    current_time, current = load(LATEST)
    older = []
    for path in sorted(ARCHIVE.glob('options_*.json')):
        try:
            stamp, data = load(path)
            if stamp < current_time:
                older.append((stamp, path, data))
        except (ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
            print('Skipping invalid archive:', path.name, str(exc)[:100])
    older.sort(key=lambda x: x[0])
    if not older:
        print('NEED_HISTORY: No earlier snapshot. Collect another snapshot later.')
        return 3
    old_time, old_path, previous = older[-1]
    gap_hours = round((current_time - old_time).total_seconds() / 3600, 2)
    old_map = contract_map(previous)
    new_map = contract_map(current)
    rows = []
    for symbol in sorted(set(old_map) & set(new_map)):
        ticker, old = old_map[symbol]
        _, new = new_map[symbol]
        oi_change = diff(old.get('open_interest'), new.get('open_interest'), 0)
        vol_change = diff(old.get('volume'), new.get('volume'), 0)
        # Intraday OI and cumulative daily volume comparisons can be misleading.
        comparable_oi = gap_hours >= MIN_OI_GAP_HOURS and old_time.date() != current_time.date()
        same_utc_day = old_time.date() == current_time.date()
        rows.append({
            'ticker': ticker, 'symbol': symbol, 'side': new.get('side'),
            'expiry': new.get('expiry'), 'strike': new.get('strike'),
            'liquidity_checks_passed_latest': new.get('liquidity_checks_passed', False),
            'quote_freshness_verified': False,
            'mid_old': old.get('mid'), 'mid_new': new.get('mid'),
            'mid_change_pct': pct(old.get('mid'), new.get('mid')),
            'spread_old_pct': old.get('spread_pct'), 'spread_new_pct': new.get('spread_pct'),
            'spread_change_percentage_points': diff(old.get('spread_pct'), new.get('spread_pct'), 2),
            'iv_old': old.get('implied_volatility'), 'iv_new': new.get('implied_volatility'),
            'iv_change_percentage_points': (round(100 * (number(new.get('implied_volatility')) - number(old.get('implied_volatility'))), 2)
                if number(new.get('implied_volatility')) is not None and number(old.get('implied_volatility')) is not None else None),
            'open_interest_old': old.get('open_interest'), 'open_interest_new': new.get('open_interest'),
            'open_interest_change': oi_change if comparable_oi else None,
            'open_interest_change_reliable': False,  # snapshots alone cannot establish publication/freshness
            'open_interest_raw_difference_unverified': oi_change,
            'volume_old': old.get('volume'), 'volume_new': new.get('volume'),
            'volume_raw_difference_unverified': vol_change,
            'volume_comparison_note': 'same UTC day; volume may be cumulative' if same_utc_day else 'different UTC days; daily volume resets',
            'warnings': sorted(set(old.get('warnings', [])) | set(new.get('warnings', []))),
        })
    by_ticker = []
    for t in current['tickers']:
        ticker = t['ticker']
        matches = [r for r in rows if r['ticker'] == ticker]
        by_ticker.append({'ticker': ticker, 'current_status': t.get('status'),
                          'matched_contracts': len(matches),
                          'latest_liquid_contracts': sum(bool(r['liquidity_checks_passed_latest']) for r in matches),
                          'call_matched': sum(r['side'] == 'CALL' for r in matches),
                          'put_matched': sum(r['side'] == 'PUT' for r in matches)})
    report = {
        'version': 5, 'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'previous_snapshot_utc': old_time.isoformat(), 'latest_snapshot_utc': current_time.isoformat(),
        'previous_archive_file': old_path.name, 'hours_between_snapshots': gap_hours,
        'matched_contracts': len(rows), 'by_ticker': by_ticker, 'contracts': rows,
        'cautions': [
            'Research only: not trade entries or real-time quotes.',
            'Bid/ask quote timestamps are unavailable; price, spread and IV changes may reflect stale quotes.',
            'Open interest may update only once daily and may be stale. Raw differences are not verified flow.',
            'Volume is session cumulative and resets; raw differences are not directional trade flow.',
            'Same-day snapshots are not independent daily performance observations.',
            'Contract comparison only includes exact matching contract symbols; no substitution for missing contracts.',
        ],
    }
    if not rows:
        print('NO_MATCH: Snapshots have no matching contract symbols; report not overwritten.')
        return 4
    atomic_write(OUTPUT, report)
    print(f'V5 OK | previous={old_time.isoformat()} latest={current_time.isoformat()} gap={gap_hours}h')
    for item in by_ticker:
        print(f"{item['ticker']:5} matched={item['matched_contracts']:3} liquid_latest={item['latest_liquid_contracts']:3} CALL={item['call_matched']:2} PUT={item['put_matched']:2}")
    print('Saved:', OUTPUT)
    print('NOTE: no entry signals; quote freshness, OI and volume changes unverified.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
