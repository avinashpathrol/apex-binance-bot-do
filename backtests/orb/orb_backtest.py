#!/usr/bin/env python3
"""Backtest of the advertised "first 5-minute candle vs 12 EMA" opening strategy.

Rule (from the ad): at the New York open (9:30 ET) watch the FIRST 5-minute candle; if it closes above the 12 EMA -> long,
below -> short.  Position then "managed with a trailing stop, held while momentum continues".  The ad does not disclose the
stop/trail, so several natural exits are tested and ALL are reported:
  V0  no stop, exit at the 16:00 close                     (pure signal test)
  V1  stop at the far end of the first candle, exit 16:00   (classic opening-range stop)
  V2  V1 + exit when a 5-min bar CLOSES on the wrong side of the 12 EMA ("stay while momentum continues")
  V3  V1 + trailing stop under the previous 5-min bar's low / over its high
Entry = OPEN of the 9:35 bar (first tradable price after the signal candle closes).  Stops are touched-price fills (gap => open).
Controls on the same days: always long, always short, candle colour (paper's rule), reversed EMA signal, random direction.
Pure Python (3.6 ok).  Usage: python3 orb_backtest.py duka <dir> | binance <csv> [label]"""
import csv, glob, io, math, os, random, statistics, sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, '/Users/avinash/Downloads/bot code/apex-binance-bot-do')
import logging
import sentinel_bot as SB                       # NYSE holidays / early closes / ET conversion (unit-tested)
logging.getLogger('sentinel').setLevel(logging.ERROR)

EMA_N = 12
ALPHA = 2.0 / (EMA_N + 1)
RISK_FLOOR = 0.0003                             # R is computed on at least a 0.03% stop distance (avoids absurd R on tiny candles)
VARIANTS = ['V0', 'V1', 'V2', 'V3']
COSTS = [('gross 0%', 0.0), ('0.02% (futures/QQQ-like)', 0.0002), ('0.10% (Binance fees)', 0.0010)]


# ── data ──────────────────────────────────────────────────────────────────────
def load_duka(dirpath):
    days = {}
    for f in glob.glob(os.path.join(dirpath, '*.csv')):
        b = os.path.basename(f)
        d = date(int(b[:4]), int(b[5:7]), int(b[8:10]))
        rows = []
        with open(f) as fh:
            for line in fh:
                p = line.strip().split(',')
                if len(p) == 6:
                    rows.append((int(p[0]), float(p[1]), float(p[2]), float(p[3]), float(p[4])))
        if rows:
            days[d] = rows
    return days


def load_binance(path):
    days = {}
    with open(path) as fh:
        r = csv.reader(fh)
        next(r)
        for t, o, h, l, c, v in r:
            ts = int(t) // 1000
            dt = datetime.fromtimestamp(ts, timezone.utc)
            sec = dt.hour * 3600 + dt.minute * 60
            if 6 * 3600 <= sec <= 21 * 3600 + 15 * 60:
                days.setdefault(dt.date(), []).append((ts, float(o), float(h), float(l), float(c)))
    return days


def five_min(rows):
    out, cur = [], None
    for ts, o, h, l, c in sorted(rows):
        k = ts - ts % 300
        if cur is None or cur[0] != k:
            if cur:
                out.append(cur)
            cur = [k, o, h, l, c]
        else:
            cur[2] = max(cur[2], h); cur[3] = min(cur[3], l); cur[4] = c
    if cur:
        out.append(cur)
    return out                                    # [start, open, high, low, close]


# ── one day ───────────────────────────────────────────────────────────────────
def prepare(D, rows):
    """-> dict with the 5-min bars, the first candle, EMA at its close, or None if the day is unusable."""
    if not SB.is_trading_day(D) or D in SB.nyse_early_closes(D.year):
        return None
    t0 = int(SB.utc_from_et(datetime(D.year, D.month, D.day, 9, 30)).timestamp())
    b5 = five_min(rows)
    idx = {b[0]: i for i, b in enumerate(b5)}
    last_start = t0 + 6 * 3600 + 25 * 60          # 15:55 bar
    if t0 not in idx or last_start not in idx or idx[last_start] - idx[t0] != 77:
        return None                                # missing bars inside the session
    i0 = idx[t0]
    if i0 < 30:
        return None                                # need >= 2.5h of warm-up for the EMA
    ema = b5[0][4]
    for b in b5[1:i0 + 1]:
        ema = ALPHA * b[4] + (1 - ALPHA) * ema
    return {'D': D, 'b5': b5, 'i0': i0, 'ema0': ema, 'first': b5[i0]}


def simulate(day, direction, variant):
    b5, i0, first = day['b5'], day['i0'], day['first']
    entry = b5[i0 + 1][1]
    stop0 = first[3] if direction > 0 else first[2]
    risk = abs(entry - stop0) / entry
    last = len(b5) - 1
    while last > 0 and b5[last][0] > first[0] + 6 * 3600 + 25 * 60:
        last -= 1
    stop, ema, exit_px, reason = stop0, day['ema0'], None, 'EOD'
    # the EMA state after the signal candle (ema0 already includes it)
    for j in range(i0 + 1, last + 1):
        o, h, l, c = b5[j][1], b5[j][2], b5[j][3], b5[j][4]
        if variant != 'V0':
            if direction > 0:
                if o <= stop:
                    exit_px, reason = o, 'stop(gap)'; break
                if l <= stop:
                    exit_px, reason = stop, 'stop'; break
            else:
                if o >= stop:
                    exit_px, reason = o, 'stop(gap)'; break
                if h >= stop:
                    exit_px, reason = stop, 'stop'; break
        ema = ALPHA * c + (1 - ALPHA) * ema
        if variant == 'V2' and j < last and ((direction > 0 and c < ema) or (direction < 0 and c > ema)):
            exit_px, reason = b5[j + 1][1], 'ema-trail'; break
        if variant == 'V3':
            stop = max(stop, l) if direction > 0 else min(stop, h)
    if exit_px is None:
        exit_px = b5[last][4]
    gross = direction * (exit_px / entry - 1)
    return {'gross': gross, 'risk': max(risk, RISK_FLOOR), 'reason': reason, 'dir': direction, 'D': day['D']}


def signals(day):
    f, e = day['first'], day['ema0']
    ema_dir = 1 if f[4] > e else (-1 if f[4] < e else 0)
    col = 1 if f[4] > f[1] else (-1 if f[4] < f[1] else 0)
    return {'EMA12 (the ad)': ema_dir, 'candle colour (paper)': col, 'always long': 1, 'always short': -1, 'reversed EMA12': -ema_dir}


# ── stats ─────────────────────────────────────────────────────────────────────
def tstat(xs):
    n = len(xs)
    if n < 3:
        return 0.0
    sd = statistics.stdev(xs)
    return (sum(xs) / n) / (sd / math.sqrt(n)) if sd else 0.0


def summarize(trs, cost, stop_variant):
    net = [t['gross'] - cost for t in trs]
    n = len(net)
    R = [(t['gross'] - cost) / t['risk'] for t in trs]
    out = {'n': n, 'win': 100.0 * sum(1 for x in net if x > 0) / n, 'avg': 100 * sum(net) / n, 't': tstat(net),
           'avgR': sum(R) / n if stop_variant else None}
    pos, neg = sum(x for x in (R if stop_variant else net) if x > 0), -sum(x for x in (R if stop_variant else net) if x < 0)
    out['pf'] = pos / neg if neg > 0 else float('inf')
    out['R'] = R
    return out


def boot_equity(R, risk_frac, n_trades=1448, sims=2000, seed=7):
    rng = random.Random(seed)
    tot, dd = [], []
    for _ in range(sims):
        eq, peak, mdd = 1.0, 1.0, 0.0
        for _ in range(n_trades):
            eq *= max(0.0, 1 + risk_frac * rng.choice(R))
            peak = max(peak, eq)
            mdd = max(mdd, 1 - eq / peak)
        tot.append(eq - 1); dd.append(mdd)
    tot.sort(); dd.sort()
    return tot[len(tot) // 2], dd[len(dd) // 2], sum(1 for x in tot if x >= 9.82) / sims


def main():
    kind, path = sys.argv[1], sys.argv[2]
    label = sys.argv[3] if len(sys.argv) > 3 else os.path.basename(path.rstrip('/'))
    raw = load_duka(path) if kind == 'duka' else load_binance(path)
    days = [d for d in (prepare(D, rows) for D, rows in sorted(raw.items())) if d]
    if not days:
        print('no usable days'); return
    both = {v: [(simulate(d, 1, v), simulate(d, -1, v)) for d in days] for v in VARIANTS}       # per day: (long trade, short trade)
    sig = [signals(d) for d in days]
    names = list(sig[0].keys())

    def pick(name, v):
        out = []
        for (lt, st), sg in zip(both[v], sig):
            dr = sg[name]
            if dr:
                out.append(lt if dr > 0 else st)
        return out

    yrs = sorted(set(d['D'].year for d in days))
    print('=' * 122)
    print('%s   |   %d usable trading days   %s -> %s   (years: %s)' % (label, len(days), days[0]['D'], days[-1]['D'], ', '.join(map(str, yrs))))
    fr = [d['first'] for d in days]
    print('average first-candle range (= stop distance): %.3f%% of price | EMA12 says long on %.0f%% of days' % (
        100 * sum((f[2] - f[3]) / f[1] for f in fr) / len(fr), 100 * sum(1 for d in days if d['first'][4] > d['ema0']) / len(days)))
    for cname, cost in COSTS:
        print('\n--- round-trip cost: %s ' % cname + '-' * 60)
        print('%-24s %-4s %5s %6s %10s %6s %8s %6s' % ('signal', 'exit', 'n', 'win%', 'avg/trade', 't', 'avg R', 'PF'))
        rows = [('EMA12 (the ad)', v) for v in VARIANTS] + [(nm, v) for nm in names[1:] for v in ('V0', 'V1')]
        for name, v in rows:
            s_ = summarize(pick(name, v), cost, v != 'V0')
            print('%-24s %-4s %5d %5.1f%% %+9.3f%% %6.2f %8s %6.2f' % (
                name, v, s_['n'], s_['win'], s_['avg'], s_['t'], ('%+.3f' % s_['avgR']) if s_['avgR'] is not None else '   -', s_['pf']))
        for v in ('V0', 'V1'):                                     # random-direction control (500 draws)
            avgs = []
            for seed in range(500):
                rr = random.Random(seed)
                g = [(lt if rr.random() < 0.5 else st)['gross'] - cost for (lt, st) in both[v]]
                avgs.append(100 * sum(g) / len(g))
            avgs.sort()
            print('%-24s %-4s %5d %6s %+9.3f%%   (95%% of random-direction draws land in %+.3f%% .. %+.3f%%)' % (
                'random direction', v, len(days), '', sum(avgs) / len(avgs), avgs[12], avgs[487]))
    print("\n--- does the EMA12 signal add anything beyond simply being long a rising market?  (paired per-day difference vs 'always long', gross) ---")
    for v in ('V0', 'V1'):
        diffs = [((lt if sg['EMA12 (the ad)'] > 0 else st)['gross'] - lt['gross']) if sg['EMA12 (the ad)'] else 0.0 for (lt, st), sg in zip(both[v], sig)]
        gl = [lt['gross'] for (lt, st) in both[v]]
        ge = [((lt if sg['EMA12 (the ad)'] > 0 else st)['gross']) if sg['EMA12 (the ad)'] else 0.0 for (lt, st), sg in zip(both[v], sig)]
        print('%s: EMA12 mean %+.3f%% | always-long mean %+.3f%% | signal adds %+.3f%% per trade (paired t = %.2f, n=%d) -> long-bias explains %.0f%% of the gross edge' % (
            v, 100 * sum(ge) / len(ge), 100 * sum(gl) / len(gl), 100 * sum(diffs) / len(diffs), tstat(diffs), len(diffs),
            100 * (sum(gl) / len(gl)) / (sum(ge) / len(ge)) if sum(ge) > 0 else float('nan')))
    print("\n--- the ad's signal by year (net of 0.02%% costs)   V1 = candle stop, V2 = EMA-trail ---")
    for v in ('V1', 'V2'):
        trs = pick('EMA12 (the ad)', v)
        line = []
        for y in yrs:
            sub = [t for t in trs if t['D'].year == y]
            if len(sub) >= 5:
                line.append('%d n=%d %+.3f%% R%+.2f' % (y, len(sub), 100 * sum(t['gross'] - 0.0002 for t in sub) / len(sub),
                                                        sum((t['gross'] - 0.0002) / t['risk'] for t in sub) / len(sub)))
        print('%s: %s' % (v, ' | '.join(line)))
    print("\n--- bootstrap to the ad's 1,448 trades from THIS data's R-multiples (0.02%% cost).  Ad claims +982%% and ~-20%% max drawdown ---")
    for v in ('V1', 'V2', 'V3'):
        R = summarize(pick('EMA12 (the ad)', v), 0.0002, True)['R']
        cells = []
        for rf in (0.005, 0.01, 0.02):
            med, mdd, p982 = boot_equity(R, rf, sims=600)
            cells.append('%.1f%%/trade -> median %+.0f%%, DD %.0f%%, P(>=982%%) %.0f%%' % (100 * rf, 100 * med, 100 * mdd, 100 * p982))
        print('%s avg R %+.3f | %s' % (v, sum(R) / len(R), ' || '.join(cells)))
    extra(days, both, sig)




def extra(days, both, sig):
    """robustness add-ons: R-tail check, V0 leverage curve, by-year V0, halves."""
    ad = lambda v: [(lt if sg['EMA12 (the ad)'] > 0 else st) for (lt, st), sg in zip(both[v], sig) if sg['EMA12 (the ad)']]
    print('\n' + '=' * 122)
    print('ROBUSTNESS (the ad\'s EMA12 signal, costs 0.02%)')
    R = sorted(summarize(ad('V1'), 0.0002, True)['R'])
    n = len(R)
    top = R[int(n * 0.98):]
    print('V1 R-multiples: median %+.2f | 5th pct %+.2f | 95th pct %+.2f | max %+.1f | best 2%% of trades (n=%d) contribute %.0f%% of total R; avg R without them %+.3f' % (
        R[n // 2], R[int(n * 0.05)], R[int(n * 0.95)], R[-1], len(top), 100 * sum(top) / sum(R) if sum(R) else float('nan'), (sum(R) - sum(top)) / (n - len(top))))
    g = [t['gross'] - 0.0002 for t in ad('V0')]
    print('V0 (enter 9:35, hold to the close, no stop): avg %+.3f%%/trade, daily sd %.2f%%, win %.1f%%' % (
        100 * sum(g) / len(g), 100 * statistics.pstdev(g), 100 * sum(1 for x in g if x > 0) / len(g)))
    rng = random.Random(3)
    print('  bootstrap to 1,448 trades at a fixed leverage on V0 (no dependence on R tails):')
    for lev in (1.0, 1.5, 2.0, 3.0):
        tot, dd = [], []
        for _ in range(600):
            eq, peak, mdd = 1.0, 1.0, 0.0
            for _ in range(1448):
                eq *= max(0.0, 1 + lev * rng.choice(g))
                peak = max(peak, eq); mdd = max(mdd, 1 - eq / peak)
            tot.append(eq - 1); dd.append(mdd)
        tot.sort(); dd.sort()
        print('    %.1fx leverage: median total return %+.0f%%, median max drawdown %.0f%%, 10th-90th pct of total return %+.0f%% .. %+.0f%%, P(>=+982%%) = %.0f%%' % (
            lev, 100 * tot[300], 100 * dd[300], 100 * tot[60], 100 * tot[540], 100 * sum(1 for x in tot if x >= 9.82) / 600))
    print('  by year (V0 avg per trade / win%, net 0.02%):')
    trs = ad('V0')
    cells = []
    for y in sorted(set(t['D'].year for t in trs)):
        sub = [t['gross'] - 0.0002 for t in trs if t['D'].year == y]
        cells.append('%d: %+.3f%% (%.0f%%, n=%d)' % (y, 100 * sum(sub) / len(sub), 100 * sum(1 for x in sub if x > 0) / len(sub), len(sub)))
    print('   ' + ' | '.join(cells))
    a = [t['gross'] - 0.0002 for t in trs if t['D'].year <= 2022]
    b = [t['gross'] - 0.0002 for t in trs if t['D'].year >= 2023]
    print('  2019-2022: %+.3f%%/trade (t=%.2f, n=%d)  |  2023-2026: %+.3f%%/trade (t=%.2f, n=%d)' % (100 * sum(a) / len(a), tstat(a), len(a), 100 * sum(b) / len(b), tstat(b), len(b)))
    for lab, sel in (('longs', 1), ('shorts', -1)):
        x = [t['gross'] - 0.0002 for t in trs if t['dir'] == sel]
        print('  EMA12 %s: %+.3f%%/trade (t=%.2f, win %.1f%%, n=%d)' % (lab, 100 * sum(x) / len(x), tstat(x), 100 * sum(1 for z in x if z > 0) / len(x), len(x)))


if __name__ == '__main__':
    main()
