#!/usr/bin/env python3
"""Screen every Binance stock-perp underlying for MU-like overnight behavior.
Train/test: rank on year 1, judge on year 2 (out of sample) to avoid winner's-curse."""
import csv, json, math, os, statistics
from datetime import date

D = os.path.dirname(os.path.abspath(__file__))
START, SPLIT, END = date(2024, 9, 19), date(2025, 9, 18), date(2026, 9, 17)
FEE_RT, FUND = 0.0010, 0.0002        # 0.10% round-trip fees + assumed 0.02%/night funding (refined later for finalists)


def load(base):
    rows = []
    with open(os.path.join(D, 'screen', base + '.csv')) as f:
        for x in csv.DictReader(f):
            try:
                y, m, dd = int(x['Date'][:4]), int(x['Date'][5:7]), int(x['Date'][8:10])
                o, c = float(x['Open']), float(x['Close'])
            except Exception:
                continue
            if o > 0 and c > 0:
                rows.append((date(y, m, dd), o, c))
    rows.sort()
    return rows


def nights(rows):
    out = {}
    for i in range(len(rows) - 1):
        d, o, c = rows[i]
        d2, o2, c2 = rows[i + 1]
        if START <= d <= END and (d2 - d).days <= 5:          # ignore data gaps
            out[d] = (o2 / c - 1, c / o - 1)
    return out


def tstat(xs):
    n = len(xs)
    if n < 3:
        return 0.0, 0.0, 0.0
    m, sd = sum(xs) / n, statistics.stdev(xs)
    return m, sd, (m / (sd / math.sqrt(n)) if sd else 0.0)


def corr(a, b):
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    den = math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b))
    return num / den if den else 0.0


def pct(x, d=2):
    return ('%+.' + str(d) + 'f%%') % (x * 100)


bases = sorted(f[:-4] for f in os.listdir(os.path.join(D, 'screen')) if f.endswith('.csv'))
N = {b: nights(load(b)) for b in bases}
mu = {d: v[0] for d, v in N['MU'].items()}

stats = []
flagged = []
for b in bases:
    ds = sorted(N[b])
    if len(ds) < 380:                          # need ~1.5+ years to judge
        continue
    ov = [N[b][d][0] for d in ds]
    if max(abs(x) for x in ov) > 0.5:
        flagged.append((b, max(ov, key=abs)))
        continue
    dy = [N[b][d][1] for d in ds]
    net = [x - FEE_RT - FUND for x in ov]
    m, sd, t = tstat(ov)
    mn, _, tn = tstat(net)
    tr = [N[b][d][0] - FEE_RT - FUND for d in ds if d < SPLIT]
    te = [N[b][d][0] - FEE_RT - FUND for d in ds if d >= SPLIT]
    if len(tr) < 180 or len(te) < 180:
        continue
    common = [d for d in ds if d in mu]
    q = {}
    for d in ds:
        q.setdefault((d.year, (d.month - 1) // 3), []).append(N[b][d][0])
    stats.append({'b': b, 'n': len(ds), 'm': m, 't': t, 'mn': mn, 'tn': tn, 'sd': sd, 'win': sum(1 for x in ov if x > 0) / len(ov),
                  'day': sum(dy) / len(dy), 'corr': corr([N[b][d][0] for d in common], [mu[d] for d in common]),
                  'qpos': sum(1 for v in q.values() if sum(v) / len(v) > 0), 'q': len(q),
                  'tr_m': tstat(tr)[0], 'tr_t': tstat(tr)[2], 'te_m': tstat(te)[0], 'te_t': tstat(te)[2]})

print('screened %d tickers with enough history (%d skipped: short history / data problems; flagged for |overnight|>50%%: %s)' %
      (len(stats), len(bases) - len(stats), ', '.join('%s %s' % (b, pct(x, 0)) for b, x in flagged) or 'none'))
print('costs assumed: 0.10%% round-trip fees + 0.02%% funding per night.  With ~%d tickers tested, pure luck alone would produce a best net t-stat around 2.5-2.8.' % len(stats))
H = '  %-6s %5s %9s %6s | %8s %6s | %5s %6s %6s %5s | %14s | %s'
HDR = H % ('ticker', 'nights', 'gross avg', 'gr t', 'NET avg', 'net t', 'win%', 'sd', 'corr MU', 'qtrs+', 'YEAR1 net avg (t)', 'YEAR2 (unseen) net avg (t)')


def line(s):
    return H % (s['b'], s['n'], pct(s['m'], 3), '%.1f' % s['t'], pct(s['mn'], 3), '%.1f' % s['tn'], '%d%%' % round(100 * s['win']),
                pct(s['sd'], 1), '%.2f' % s['corr'], '%d/%d' % (s['qpos'], s['q']),
                '%s (%.1f)' % (pct(s['tr_m'], 2), s['tr_t']), '%s (%.1f)' % (pct(s['te_m'], 2), s['te_t']))


ref = [s for s in stats if s['b'] in ('MU', 'NVDA', 'SPY')]
print('\nREFERENCE'); print(HDR)
for s in ref:
    print(line(s))

print('\n' + '=' * 130)
print('A) PICKED USING YEAR 1 ONLY (top 15 by year-1 net t-stat) -- then judged on year 2, which the ranking never saw')
print(HDR)
sel = sorted(stats, key=lambda s: -s['tr_t'])[:15]
for s in sel:
    print(line(s))
surv = [s for s in sel if s['te_m'] > 0]
print('  -> %d of these 15 were still positive net in year 2; avg year-2 net per night across the 15: %s' %
      (len(surv), pct(sum(s['te_m'] for s in sel) / len(sel), 3)))

print('\n' + '=' * 130)
print('B) WHO BEHAVES MOST LIKE MU?  (among tickers with positive net edge in BOTH years, ranked by correlation of nightly overnight moves with MU)')
print(HDR)
both = [s for s in stats if s['tr_m'] > 0 and s['te_m'] > 0 and s['b'] != 'MU']
for s in sorted(both, key=lambda s: -s['corr'])[:15]:
    print(line(s))

print('\n' + '=' * 130)
print('C) BEST OVERALL BY FULL-PERIOD NET t-STAT (for reference only; in-sample, so optimistic)')
print(HDR)
for s in sorted(stats, key=lambda s: -s['tn'])[:12]:
    print(line(s))

json.dump(stats, open(os.path.join(D, 'screen_stats.json'), 'w'))
