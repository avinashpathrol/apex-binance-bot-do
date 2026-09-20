#!/usr/bin/env python3
import io, json, os, sys, time
from datetime import date, datetime, timedelta, timezone
import requests

_o = sys.stdout; sys.stdout = io.StringIO()
import screen as S
sys.stdout = _o

D = S.D
FIN = ['MU', 'SNDK', 'WDC', 'LITE', 'CRDO', 'MRVL']
perps = {r['base']: r for r in json.load(open(os.path.join(D, 'binance_equity_perps.json')))}
earn = json.load(open(os.path.join(D, 'earn2.json')))
earn['MU'] = json.load(open(os.path.join(D, '..', 'backtest', 'earn.json')))['MU']
d_of = lambda s: date(int(s[:4]), int(s[5:7]), int(s[8:10]))
mu_rows = S.load('MU')
nxt = {mu_rows[i][0]: mu_rows[i + 1][0] for i in range(len(mu_rows) - 1)}


def fmt_usd(x):
    return '$%.1fM' % (x / 1e6) if x >= 1e6 else '$%.0fK' % (x / 1e3)


print('=' * 118)
print('1) THE ACTUAL BINANCE PERPS  (listing, liquidity, real funding cost per overnight hold)')
print('  %-6s %-11s %-10s %14s %16s %16s %14s' % ('', 'symbol', 'listed', 'avg $ volume/day', 'funding/weeknight', 'funding/weekend', 'funding/event'))
fund_night = {}
for b in FIN:
    sym = perps[b]['symbol']
    onboard = perps[b]['onboard']
    k = requests.get('https://fapi.binance.com/fapi/v1/klines', params={'symbol': sym, 'interval': '1d', 'limit': 31}, timeout=30).json()
    vol = sum(float(x[7]) for x in k[:-1]) / max(1, len(k) - 1)
    f, s = [], int(datetime(int(onboard[:4]), int(onboard[5:7]), int(onboard[8:10]), tzinfo=timezone.utc).timestamp() * 1000) - 86400000
    while True:
        r = requests.get('https://fapi.binance.com/fapi/v1/fundingRate', params={'symbol': sym, 'startTime': s, 'limit': 1000}, timeout=30).json()
        if not r: break
        f += r; s = r[-1]['fundingTime'] + 1
        if len(r) < 1000: break
        time.sleep(0.3)
    wk, we = [], []
    for d, d2 in nxt.items():
        if d < d_of(onboard) or d > S.END: continue
        t0 = int(datetime(d.year, d.month, d.day, 20, 0, tzinfo=timezone.utc).timestamp() * 1000)
        t1 = int(datetime(d2.year, d2.month, d2.day, 13, 30, tzinfo=timezone.utc).timestamp() * 1000)
        c = sum(float(x['fundingRate']) for x in f if t0 < x['fundingTime'] <= t1)
        (wk if (d2 - d).days == 1 else we).append(c)
    fund_night[b] = (sum(wk) / len(wk) if wk else S.FUND, sum(we) / len(we) if we else S.FUND, len(wk), len(we))
    print('  %-6s %-11s %-10s %16s %16s %16s %14s' % (b, sym, onboard, fmt_usd(vol), S.pct(fund_night[b][0], 3), S.pct(fund_night[b][1], 3),
                                                     S.pct(sum(float(x['fundingRate']) for x in f) / len(f), 4)))
    time.sleep(0.4)

print('\n' + '=' * 118)
print('2) EDGE WITH REAL FUNDING, EARNINGS NIGHTS SEPARATED  (stock data, 2 years or since listing; net = 0.10% fees + real funding)')
print('  %-6s %6s | %9s %5s %9s %5s | %6s %10s %11s | %10s %9s' %
      ('', 'nights', 'gross avg', 't', 'NET avg', 't', '#earn', 'earn avg', '% of profit', 'NET ex-earn', 't ex-earn'))
for b in FIN:
    ed = {d_of(x) for x in earn[b]}
    ds = sorted(S.N[b])
    ov = {d: S.N[b][d][0] for d in ds}
    fw = lambda d: fund_night[b][0] if (nxt.get(d, d) - d).days == 1 else fund_night[b][1]
    net = {d: ov[d] - S.FEE_RT - fw(d) for d in ds}
    m, _, t = S.tstat(list(ov.values())); mn, _, tn = S.tstat(list(net.values()))
    e_nights = [d for d in ds if d in ed]
    o_nights = [d for d in ds if d not in ed]
    e_avg = sum(ov[d] for d in e_nights) / len(e_nights) if e_nights else 0
    share = sum(ov[d] for d in e_nights) / sum(ov.values()) if e_nights else 0
    mo, _, to = S.tstat([net[d] for d in o_nights])
    print('  %-6s %6d | %9s %5.1f %9s %5.1f | %6d %10s %10.0f%% | %10s %9.1f' %
          (b, len(ds), S.pct(m, 3), t, S.pct(mn, 3), tn, len(e_nights), S.pct(e_avg, 1), share * 100, S.pct(mo, 3), to))

print('\n' + '=' * 118)
print('3) TAIL RISK OVERNIGHT (stock close -> next open)  and  "do they move together with MU?"')
mu_e = sorted(d_of(x) for x in earn['MU'] if S.START <= d_of(x) <= S.END)
print('  %-6s %10s %10s %26s %30s' % ('', 'nights <= -3.5%', 'nights >= +3.5%', 'worst 3 nights', 'on the 8 MU-earnings nights: avg move / same direction as MU'))
for b in FIN:
    ov = [S.N[b][d][0] for d in sorted(S.N[b])]
    ex = sorted(ov)
    if b == 'MU':
        mv = '(the reference)'
    else:
        c = [d for d in mu_e if d in S.N[b]]
        mvs = [S.N[b][d][0] for d in c]; mus = [S.N['MU'][d][0] for d in c]
        same = sum(1 for a, m2 in zip(mvs, mus) if (a > 0) == (m2 > 0))
        mv = '%s avg | same direction on %d of %d' % (S.pct(sum(mvs) / len(mvs), 1), same, len(c)) if c else 'n/a (not listed yet)'
    print('  %-6s %14.0f%% %14.0f%%  %-26s %s' % (b, 100 * sum(1 for x in ov if x <= -0.035) / len(ov), 100 * sum(1 for x in ov if x >= 0.035) / len(ov),
                                              ', '.join(S.pct(x, 1) for x in ex[:3]), mv))
