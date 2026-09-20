#!/usr/bin/env python3
import math, statistics
import analyze as A

days = A.trading_days()
nights = [(days[i], days[i + 1]) for i in range(len(days) - 1) if days[i] <= A.LAST_ENTRY_DAY]
data = {s: A.load(s) for s in A.SYMS}


def series(sym, ehm, xhm, sl=True):
    bars, fund = data[sym]
    ts0 = min(bars)
    return [r for r in (A.run_night(bars, fund, a, b, ehm, xhm, use_sl=sl) for (a, b) in nights if A.et_ms(a, 15, 50) >= ts0) if r]


def tstat(xs):
    n, m = len(xs), sum(xs) / len(xs)
    sd = statistics.stdev(xs)
    return m, sd, (m / (sd / math.sqrt(n)) if sd else 0)


print('=' * 96)
print('1) HOW CONCENTRATED IS THE PROFIT?  (buy 16:00 close -> sell 9:30 open, 3.5%% stop, net of fees+funding)')
for s in A.SYMS:
    rows = series(s, (16, 0), (9, 30))
    nets = sorted(r['net'] for r in rows)
    n = len(nets)
    mean = sum(nets) / n
    ex3 = sum(nets[:-3]) / (n - 3)
    print('  %-9s mean %s | MEDIAN night %s | mean WITHOUT the 3 best nights %s | share of total profit from 3 best nights: %.0f%%' %
          (s, A.pct(mean), A.pct(statistics.median(nets)), A.pct(ex3), 100 * sum(nets[-3:]) / sum(nets)))

print('\n' + '=' * 96)
print('2) IS THE EDGE STILL THERE?  net avg per trade in consecutive blocks of ~19 nights (buy 16:00 -> 9:30)')
for s in A.SYMS:
    rows = series(s, (16, 0), (9, 30))
    k = len(rows) // 6
    blocks = [rows[i * k:(i + 1) * k] if i < 5 else rows[5 * k:] for i in range(6)]
    print('  %-9s ' % s + ' | '.join('%s..%s %s' % (b[0]['d'].strftime('%m-%d'), b[-1]['d'].strftime('%m-%d'),
                                                      A.pct(sum(r['net'] for r in b) / len(b))) for b in blocks))
    last = [r['net'] for r in rows[-57:]]
    m, sd, t = tstat(last)
    print('            most recent 57 nights: avg %s, t-stat %.2f  (n=%d)' % (A.pct(m), t, len(last)))

print('\n' + '=' * 96)
print('3) DOES THE 3.5% STOP HELP?  MU, buy 16:00 -> 9:30')
for label, sl in (('with 3.5% stop', True), ('no stop', False)):
    rows = series('MUUSDT', (16, 0), (9, 30), sl=sl)
    nets = [r['net'] for r in rows]
    m, sd, t = tstat(nets)
    print('  %-15s avg %s | median %s | t=%.2f | worst night %s | nights down more than 3.5%%: %d of %d | stopped: %d' %
          (label, A.pct(m), A.pct(statistics.median(nets)), t, A.pct(min(nets)), sum(1 for x in nets if x <= -0.035), len(nets),
           sum(1 for r in rows if r['stopped'])))
rows = series('MUUSDT', (16, 0), (9, 30))
by = {}
for r in rows:
    by.setdefault(r['d'].strftime('%m'), []).append(r['stopped'])
print('  stop-outs by month: ' + ' | '.join('%s: %d/%d' % (k, sum(v), len(v)) for k, v in sorted(by.items())))
print('  typical stop-out cost at the LIVE size ($1,600 notional): about $%.0f per stop-out' % (0.037 * A.NOTIONAL))

print('\n' + '=' * 96)
print('4) IS BUYING 1-5 MINUTES AFTER THE CLOSE *SIGNIFICANTLY* BETTER?  paired, night by night, vs buying at 16:00')
print('   (same nights, same exit at 9:30 open; positive = waiting was better)')
for s in A.SYMS:
    base = series(s, (16, 0), (9, 30))
    out = []
    for lab, hm in (('16:01', (16, 1)), ('16:02', (16, 2)), ('16:05', (16, 5))):
        alt = series(s, hm, (9, 30))
        d = [a['net'] - b['net'] for a, b in zip(alt, base)]
        m, sd, t = tstat(d)
        better = sum(1 for x in d if x > 0)
        out.append('%s: %s/trade (t=%.1f, better on %d of %d nights)' % (lab, A.pct(m, 3), t, better, len(d)))
    print('  %-9s ' % s + ' | '.join(out))
print('   For scale: round-trip fees alone are 0.100% per trade.')

print('\n' + '=' * 96)
print('5) REALISTIC EXECUTION HAIRCUT  (validation on MU\'s 4 real trades: live fills averaged ~0.06%% worse than bar-open prices)')
for s in A.SYMS:
    rows = series(s, (16, 0), (9, 30))
    m = sum(r['net'] for r in rows) / len(rows)
    print('  %-9s backtest net %s  ->  after a 0.06%% execution haircut %s  ->  after a harsher 0.10%% haircut %s' %
          (s, A.pct(m), A.pct(m - 0.0006), A.pct(m - 0.0010)))
