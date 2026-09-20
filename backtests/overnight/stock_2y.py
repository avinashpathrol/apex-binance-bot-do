#!/usr/bin/env python3
"""Two-year stock-data check of the overnight-hold strategy (Yahoo daily bars),
plus a night-by-night comparison against the Binance perp backtest.
Price-only (dividends ignored: ~0.005%/night for SPY, negligible for MU/NVDA)."""
import csv, math, os, statistics
from datetime import date, timedelta
import analyze as A

D = os.path.dirname(os.path.abspath(__file__))
START = date(2024, 9, 19)          # last two years only (as requested)
END = date(2026, 9, 17)            # last completed entry night (Sep 17 close -> Sep 18 open)
FEE_RT = 0.0010
# average funding cost per night measured on the actual Binance perps (longs pay when positive)
FUND = {'MU': 0.00043, 'NVDA': 0.00013, 'SPY': -0.00022}
TICK = {'MU': 'MUUSDT', 'NVDA': 'NVDAUSDT', 'SPY': 'SPYUSDT'}


def load(sym):
    rows = []
    with open(os.path.join(D, 'yahoo', 'yf_%s.csv' % sym)) as f:
        r = csv.DictReader(f)
        for x in r:
            try:
                d = date(int(x['Date'][:4]), int(x['Date'][5:7]), int(x['Date'][8:10]))
                o, c = float(x['Open']), float(x['Close'])
            except Exception:
                continue
            if d >= START - timedelta(days=10) and o > 0 and c > 0:
                rows.append((d, o, c))
    rows.sort()
    return rows


def nights_of(rows):
    """(entry_date, next_date, overnight_ret, intraday_ret_of_entry_day, gap_days)"""
    out = []
    for i in range(len(rows) - 1):
        d, o, c = rows[i]
        d2, o2, c2 = rows[i + 1]
        if START <= d <= END:
            out.append((d, d2, o2 / c - 1, c / o - 1, (d2 - d).days))
    return out


def tstat(xs):
    n, m = len(xs), sum(xs) / len(xs)
    sd = statistics.stdev(xs)
    return m, sd, (m / (sd / math.sqrt(n)) if sd else 0.0)


def corr(a, b):
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    den = math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b))
    return num / den if den else 0.0


def pct(x, d=2):
    return ('%+.' + str(d) + 'f%%') % (x * 100)


def compound(rs):
    v = 1.0
    for r in rs:
        v *= (1 + r)
    return v - 1


data = {k: nights_of(load(k)) for k in TICK}

print('=' * 100)
print('A) IS MY BINANCE CALCULATION FLAWED?  actual STOCK overnight move vs the PERP\'s, same nights (perp window only)')
print('   (perp = buy 16:00 ET -> sell 9:30 ET, no stop, before costs; stock = official close -> next official open)')
pbars = {k: A.load(TICK[k]) for k in TICK}
for k in TICK:
    bars, fund = pbars[k]
    ts0 = min(bars)
    st, pp = [], []
    for (d, d2, ov, intr, gap) in data[k]:
        if A.et_ms(d, 15, 50) < ts0:
            continue
        r = A.run_night(bars, fund, d, d2, (16, 0), (9, 30), use_sl=False)
        if r:
            st.append(ov)
            pp.append(r['gross'])
    diffs = [p - s for p, s in zip(pp, st)]
    same = sum(1 for p, s in zip(pp, st) if (p > 0) == (s > 0))
    print('  %-5s n=%3d nights | STOCK avg overnight %s | PERP avg overnight %s | avg difference %s | correlation %.3f | same direction %d%% | typical gap between them +/-%s' %
          (k, len(st), pct(sum(st) / len(st), 3), pct(sum(pp) / len(pp), 3), pct(sum(diffs) / len(diffs), 3), corr(st, pp),
           round(100 * same / len(st)), pct(statistics.median([abs(x) for x in diffs]), 2)))

print('\n' + '=' * 100)
print('B) TWO-YEAR STOCK BACKTEST  %s -> %s   (buy the official close, sell the next official open; 1x notional)' % (START, END))
print('   costs = 0.10%% round-trip Binance fees + measured funding per night (MU %s, NVDA %s, SPY %s)' %
      (pct(FUND['MU'], 3), pct(FUND['NVDA'], 3), pct(FUND['SPY'], 3)))
hdr = '  %-5s %5s %8s %8s %7s %7s | %9s %8s | %-18s %-18s %-14s'
print(hdr % ('', 'nights', 'avg night', 'median', 'win%', 't-stat', 'NET avg', 'net t', 'overnight-only 2y', 'daytime-only 2y', 'buy & hold'))
for k in TICK:
    ov = [n[2] for n in data[k]]
    dy = [n[3] for n in data[k]]
    net = [x - FEE_RT - FUND[k] for x in ov]
    m, sd, t = tstat(ov)
    mn, _, tn = tstat(net)
    bh = compound([(1 + a) * (1 + b) - 1 for a, b in zip(ov, dy)])
    print(hdr % (k, len(ov), pct(m, 3), pct(statistics.median(ov), 3), '%d%%' % round(100 * sum(1 for x in ov if x > 0) / len(ov)), '%.2f' % t,
                 pct(mn, 3), '%.2f' % tn, '%s gross' % pct(compound(ov), 0), pct(compound(dy), 0), pct(bh, 0)))
    print('        after costs, compounded 1x every night for 2 years: %s   |  avg net per night %s   |  costs eat %.0f%% of the gross edge' %
          (pct(compound(net), 0), pct(mn, 3), 100 * (FEE_RT + FUND[k]) / m if m > 0 else float('nan')))

print('\n' + '=' * 100)
print('C) IS IT CONSISTENT?  average overnight return per night, by quarter (gross)')
qs = {}
for k in TICK:
    for (d, d2, ov, intr, gap) in data[k]:
        qs.setdefault((d.year, (d.month - 1) // 3 + 1), {}).setdefault(k, []).append(ov)
print('  %-8s' % 'quarter' + ''.join('%22s' % ('%s (n)' % k) for k in TICK))
for q in sorted(qs):
    print('  %d-Q%d  ' % q + ''.join('%22s' % ('%s (%d)' % (pct(sum(qs[q][k]) / len(qs[q][k]), 2), len(qs[q][k]))) for k in TICK))
for k in TICK:
    pos = sum(1 for q in qs if sum(qs[q][k]) / len(qs[q][k]) > 0)
    print('  %-5s positive in %d of %d quarters' % (k, pos, len(qs)))

print('\n' + '=' * 100)
print('D) MU, ROLLING 57-NIGHT WINDOWS (net of costs) -- was the recent flat stretch unusual within these 2 years?')
mu_net = [n[2] - FEE_RT - FUND['MU'] for n in data['MU']]
w = [sum(mu_net[i:i + 57]) / 57 for i in range(len(mu_net) - 56)]
latest = w[-1]
print('  windows: %d | best %s | worst %s | median %s | share of windows with avg <= 0: %.0f%% | latest window %s (rank: worse than %.0f%% of windows)' %
      (len(w), pct(max(w)), pct(min(w)), pct(statistics.median(w)), 100 * sum(1 for x in w if x <= 0) / len(w), pct(latest),
       100 * sum(1 for x in w if x > latest) / len(w)))
mn_all, _, t_all = tstat(mu_net)
print('  MU full two years, NET: avg %s per night, t-stat %.2f (n=%d)' % (pct(mn_all, 3), t_all, len(mu_net)))

print('\n' + '=' * 100)
print('E) WEEKNIGHTS vs WEEKENDS (Fri->Mon or holiday), gross overnight, two years')
for k in TICK:
    wk = [n[2] for n in data[k] if n[4] == 1]
    we = [n[2] for n in data[k] if n[4] >= 3]
    print('  %-5s weeknights n=%3d avg %s (t=%.2f) | weekends n=%3d avg %s (t=%.2f)' %
          (k, len(wk), pct(sum(wk) / len(wk), 3), tstat(wk)[2], len(we), pct(sum(we) / len(we), 3), tstat(we)[2]))

print('\n' + '=' * 100)
print('F) THE BIGGEST SINGLE NIGHTS -- how much of the 2-year profit is just a few lucky gaps?  (gross overnight)')
for k in TICK:
    ov = sorted(n[2] for n in data[k])
    tot = sum(ov)
    print('  %-5s total %s | 5 best nights %s (%.0f%% of total) | avg WITHOUT the 5 best nights %s | 5 worst nights %s' %
          (k, pct(tot, 0), pct(sum(ov[-5:]), 0), 100 * sum(ov[-5:]) / tot, pct(sum(ov[:-5]) / (len(ov) - 5), 3), pct(sum(ov[:5]), 0)))
