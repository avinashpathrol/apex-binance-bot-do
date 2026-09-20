#!/usr/bin/env python3
import io, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'screen'))
import analyze as A
_o = sys.stdout; sys.stdout = io.StringIO()
import screen as SC            # stock-data nights for the fidelity check
sys.stdout = _o

days = A.trading_days()
nights = [(days[i], days[i + 1]) for i in range(len(days) - 1) if days[i] <= A.LAST_ENTRY_DAY]
data = {s: A.load(s) for s in ('SNDKUSDT', 'MUUSDT')}


def series(sym, ehm, xhm, sl=True):
    bars, fund = data[sym]
    ts0 = min(bars)
    return [r for r in (A.run_night(bars, fund, a, b, ehm, xhm, use_sl=sl) for (a, b) in nights if A.et_ms(a, 15, 50) >= ts0) if r]


print('=' * 104)
print('SANDISK (SNDKUSDT) ON THE REAL BINANCE PERP  vs  MU, same engine, same nights  (fees 0.10% + real funding, live 3.5% stop)')
print('=' * 104)

# fidelity
bars, fund = data['SNDKUSDT']
st, pp = [], []
for a, b in nights:
    if A.et_ms(a, 15, 50) < min(bars) or a not in SC.N['SNDK']:
        continue
    r = A.run_night(bars, fund, a, b, (16, 0), (9, 30), use_sl=False)
    if r:
        st.append(SC.N['SNDK'][a][0]); pp.append(r['gross'])
print('\nFIDELITY: SNDK perp vs the real stock overnight move (%d nights): stock avg %s | perp avg %s | correlation %.3f' %
      (len(st), A.pct(sum(st) / len(st), 3), A.pct(sum(pp) / len(pp), 3), SC.corr(st, pp)))

print('\n%-8s %-26s %-22s %4s %5s %8s %8s %7s %19s %8s %9s %s' %
      ('', 'entry', 'exit', 'n', 'win%', 'gross', 'NET avg', 't-stat', '95% CI of net avg', 'worst', '$/trade*', 'stopped'))
for sym in ('SNDKUSDT', 'MUUSDT'):
    for en, ehm in (('15:55 (live timing)', (15, 55)), ('16:00 at the close', (16, 0)), ('16:01 (+1 min)', (16, 1)), ('16:05 (+5 min)', (16, 5))):
        for xn, xhm in (('9:30 open', (9, 30)), ('9:28 (live timing)', (9, 28))):
            if xhm == (9, 28) and ehm not in ((15, 55), (16, 0)):
                continue
            rows = series(sym, ehm, xhm)
            st_ = A.stats([r['net'] for r in rows], ci=True)
            gross = sum(r['gross'] for r in rows) / len(rows)
            print('%-8s %-26s %-22s %4d %4.0f%% %8s %8s %7.2f %19s %8s %9.2f %d' %
                  (sym[:-4], en, xn, st_['n'], st_['wr'] * 100, A.pct(gross), A.pct(st_['mean']), st_['t'],
                   '[%s, %s]' % (A.pct(st_['lo']), A.pct(st_['hi'])), A.pct(st_['worst']), st_['mean'] * A.NOTIONAL,
                   sum(1 for r in rows if r['stopped'])))
print('  (* $ per trade at the live MU size: $1,600 notional)')

print('\n' + '=' * 104)
print('STABILITY (buy 16:00 -> sell 9:30, net):')
for sym in ('SNDKUSDT', 'MUUSDT'):
    rows = series(sym, (16, 0), (9, 30))
    h = len(rows) // 2
    a_, b_ = rows[:h], rows[h:]
    wk = [r['net'] for r in rows if r['gap'] == 1]; we = [r['net'] for r in rows if r['gap'] > 1]
    mo = {}
    for r in rows:
        mo.setdefault(r['d'].strftime('%m'), []).append(r['net'])
    srt = sorted(rows, key=lambda r: r['net'])
    print('\n  %s' % sym[:-4])
    print('    first half %s | second half %s | weeknights %s (n=%d) | weekends %s (n=%d)' %
          (A.pct(sum(r['net'] for r in a_) / len(a_)), A.pct(sum(r['net'] for r in b_) / len(b_)), A.pct(sum(wk) / len(wk)), len(wk), A.pct(sum(we) / len(we)), len(we)))
    print('    by month: ' + ' | '.join('%s %s (n=%d)' % (k, A.pct(sum(v) / len(v)), len(v)) for k, v in sorted(mo.items())))
    print('    stop-outs %d of %d nights | worst 3: %s | best 3: %s' %
          (sum(1 for r in rows if r['stopped']), len(rows), ', '.join(A.pct(r['net']) for r in srt[:3]), ', '.join(A.pct(r['net']) for r in srt[-3:])))

# concentration with MU
s1 = {r['d']: r['gross'] for r in series('SNDKUSDT', (16, 0), (9, 30), sl=False)}
m1 = {r['d']: r['gross'] for r in series('MUUSDT', (16, 0), (9, 30), sl=False)}
common = sorted(set(s1) & set(m1))
both_up = sum(1 for d in common if s1[d] > 0 and m1[d] > 0); both_dn = sum(1 for d in common if s1[d] <= 0 and m1[d] <= 0)
big_s = [d for d in common if s1[d] <= -0.035]
print('\nHOW MUCH WOULD SNDK ADD ON TOP OF MU?  overnight correlation (perp, %d nights): %.2f | both up %d / both down %d of %d nights (%.0f%% same direction)' %
      (len(common), SC.corr([s1[d] for d in common], [m1[d] for d in common]), both_up, both_dn, len(common), 100 * (both_up + both_dn) / len(common)))
print('  nights where SNDK fell 3.5%%+ overnight: %d -- MU also fell on %d of them' % (len(big_s), sum(1 for d in big_s if m1[d] < 0)))
