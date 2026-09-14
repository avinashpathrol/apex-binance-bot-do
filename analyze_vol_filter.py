#!/usr/bin/env python3
"""
Volume-filter win-rate analysis.

Reads the closed trades log, fetches the Binance 1H kline for each entry,
computes the 20-bar volume MA at that point, then splits win rates into:
  - vol_ok  : vol >= vol_ma * 0.9  (would have passed the new filter)
  - vol_low : vol <  vol_ma * 0.9  (would have been blocked)

Run on the server:  python3 analyze_vol_filter.py
Or locally after:   scp root@159.65.153.40:/var/www/apex/trades_log.json .
                    python3 analyze_vol_filter.py --log trades_log.json
"""

import json, sys, time, os, argparse
from datetime import datetime, timezone, timedelta
from collections import defaultdict

try:
    import requests
except ImportError:
    print("pip install requests")
    sys.exit(1)

# ── Config ────────────────────────────────────────────────────────────────────
DEFAULT_LOG   = '/var/www/apex/trades_log.json'
KLINE_URL     = 'https://fapi.binance.com/fapi/v1/klines'
INTERVAL      = '1h'
VOL_MA_BARS   = 20
VOL_THRESHOLD = 0.9   # must be >= 90% of vol_ma to pass filter

# ── Args ──────────────────────────────────────────────────────────────────────
ap = argparse.ArgumentParser()
ap.add_argument('--log', default=DEFAULT_LOG, help='Path to trades_log.json')
args = ap.parse_args()

if not os.path.exists(args.log):
    print(f"Trade log not found: {args.log}")
    print("Copy it from the server first:")
    print("  scp root@159.65.153.40:/var/www/apex/trades_log.json .")
    print("  python3 analyze_vol_filter.py --log trades_log.json")
    sys.exit(1)

trades = json.load(open(args.log))
print(f"Loaded {len(trades)} trades from {args.log}\n")

# ── Fetch klines ──────────────────────────────────────────────────────────────
_kline_cache: dict = {}

def fetch_vol_at_entry(symbol: str, opened_at: str) -> tuple:
    """
    Returns (vol, vol_ma, vol_ok) for the 1H candle that contains opened_at.
    Fetches 20+5 bars ending at that candle so we have enough history for MA.
    """
    try:
        # Python 3.6 compat — strip timezone suffix and treat as UTC
        dt    = datetime.strptime(opened_at[:19], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc)
        ts_ms = int(dt.timestamp() * 1000)

        cache_key = f'{symbol}_{ts_ms}'
        if cache_key in _kline_cache:
            return _kline_cache[cache_key]

        r = requests.get(KLINE_URL, params={
            'symbol':    symbol,
            'interval':  INTERVAL,
            'endTime':   ts_ms,
            'limit':     VOL_MA_BARS + 5,   # extra buffer
        }, timeout=10)
        r.raise_for_status()
        klines = r.json()

        if not klines or len(klines) < VOL_MA_BARS:
            return None, None, None

        volumes = [float(k[5]) for k in klines]
        vol     = volumes[-1]
        vol_ma  = sum(volumes[-VOL_MA_BARS:]) / VOL_MA_BARS
        vol_ok  = vol >= vol_ma * VOL_THRESHOLD

        result = (round(vol, 2), round(vol_ma, 2), vol_ok)
        _kline_cache[cache_key] = result
        time.sleep(0.08)   # ~12 req/s — well within Binance public limits
        return result
    except Exception as e:
        return None, None, None

# ── Analyse ───────────────────────────────────────────────────────────────────
by_sym: dict = defaultdict(lambda: {
    'vol_ok':  {'wins': 0, 'total': 0, 'pnl': 0.0},
    'vol_low': {'wins': 0, 'total': 0, 'pnl': 0.0},
    'no_data': {'wins': 0, 'total': 0, 'pnl': 0.0},
})

overall = {
    'vol_ok':  {'wins': 0, 'total': 0, 'pnl': 0.0},
    'vol_low': {'wins': 0, 'total': 0, 'pnl': 0.0},
}

total = len(trades)
for i, t in enumerate(trades, 1):
    sym      = t.get('symbol', '?')
    opened   = t.get('opened_at') or t.get('closed_at')
    pnl      = float(t.get('pnl', 0))
    win      = t.get('win') or pnl > 0

    print(f"  [{i:3d}/{total}] {sym} — fetching vol...", end='\r')

    if not opened:
        bucket = 'no_data'
    else:
        vol, vol_ma, vol_ok = fetch_vol_at_entry(sym, opened)
        if vol is None:
            bucket = 'no_data'
        else:
            bucket = 'vol_ok' if vol_ok else 'vol_low'

    by_sym[sym][bucket]['total'] += 1
    by_sym[sym][bucket]['pnl']   += pnl
    if win:
        by_sym[sym][bucket]['wins'] += 1

    if bucket != 'no_data':
        overall[bucket]['total'] += 1
        overall[bucket]['pnl']   += pnl
        if win:
            overall[bucket]['wins'] += 1

print(' ' * 60)

# ── Print results ─────────────────────────────────────────────────────────────
def wr(d): return f"{d['wins']/d['total']*100:.1f}%" if d['total'] else '—'
def pnl_fmt(d): return f"${d['pnl']:+.4f}" if d['total'] else '—'
def row(lbl, d):
    if d['total'] == 0: return f"  {lbl:10s}  no trades"
    return (f"  {lbl:10s}  {d['total']:3d} trades  "
            f"win {wr(d):6s}  P&L {pnl_fmt(d)}")

DIVIDER = '─' * 62

print(f"\n{'=' * 62}")
print(f"  VOLUME FILTER — Win Rate Analysis")
print(f"  Filter: vol >= vol_ma * {VOL_THRESHOLD:.0%} (20-bar average, 1H)")
print(f"{'=' * 62}\n")

for sym in sorted(by_sym.keys()):
    d = by_sym[sym]
    total_sym = sum(d[b]['total'] for b in ('vol_ok','vol_low','no_data'))
    print(f"  {sym.replace('USDT','')} ({total_sym} trades)")
    print(f"  {'vol ≥ 90% avg':14s}{row('', d['vol_ok']).lstrip()}")
    print(f"  {'vol < 90% avg':14s}{row('', d['vol_low']).lstrip()}")
    if d['no_data']['total']:
        print(f"  {'no data':14s}{row('', d['no_data']).lstrip()}")
    print()

print(DIVIDER)
print("  OVERALL (all symbols combined)\n")
print(row('vol ≥ 90%', overall['vol_ok']))
print(row('vol < 90%', overall['vol_low']))
print()

ok  = overall['vol_ok']
low = overall['vol_low']
if ok['total'] and low['total']:
    ok_wr  = ok['wins']  / ok['total']  * 100
    low_wr = low['wins'] / low['total'] * 100
    delta  = ok_wr - low_wr
    saved_pnl = ok['pnl'] - (ok['pnl'] + low['pnl'])
    print(f"  Win rate delta:  {delta:+.1f} pp  ({'filter helps ✅' if delta > 0 else 'filter hurts ❌' if delta < -2 else 'neutral — ±2pp'})")
    print(f"  Trades blocked if filter applied: {low['total']} ({low['total']/(ok['total']+low['total'])*100:.0f}% of total)")
    blocked_wins  = low['wins']
    blocked_losses = low['total'] - blocked_wins
    print(f"  Of those blocked: {blocked_wins} would have won, {blocked_losses} would have lost")
    print(f"  P&L saved by blocking low-vol: ${-low['pnl']:+.4f} USDT")

print(f"\n{'=' * 62}")
print("  Note: vol data fetched from Binance public API (1H klines).")
print("  'no data' = trade log missing opened_at or API timeout.")
print(f"{'=' * 62}\n")
