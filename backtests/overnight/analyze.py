#!/usr/bin/env python3
"""Overnight-hold backtest on Binance TradFi perps (buy near the US close,
sell at the next US open). Pure Python 3.6. All times US/Eastern; the whole
data window (Mar 26 - Sep 20 2026) is EDT (UTC-4), so no DST handling needed."""
import csv, json, math, os, random, statistics
from datetime import date, datetime, timedelta, timezone

D = os.path.dirname(os.path.abspath(__file__))
EDT = timedelta(hours=4)
FEE_RT = 0.0010            # 0.05% taker per side -- what the live bot actually pays
SL_PCT = 0.035             # live MU hard stop
NOTIONAL = 1600.0          # live MU sizing: $40 stake x 40x
HOLIDAYS = {date(2026, 4, 3), date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7)}
LAST_ENTRY_DAY = date(2026, 9, 17)        # Sep 17 close -> Sep 18 open = last COMPLETED night
FIRST_DAY = date(2026, 3, 26)
SYMS = ['MUUSDT', 'NVDAUSDT', 'SPYUSDT']


def et_ms(d, hh, mm):
    dt = datetime(d.year, d.month, d.day, hh, mm, tzinfo=timezone.utc) + EDT
    return int(dt.timestamp() * 1000)


def trading_days():
    out, d = [], FIRST_DAY
    while d <= date(2026, 9, 18):
        if d.weekday() < 5 and d not in HOLIDAYS:
            out.append(d)
        d += timedelta(days=1)
    return out


def load(sym):
    bars = {}
    with open(os.path.join(D, sym + '_1m.csv')) as f:
        r = csv.reader(f)
        next(r)
        for t, o, h, l, c, v in r:
            bars[int(t)] = (float(o), float(h), float(l), float(c))
    fund = [(x['fundingTime'], float(x['fundingRate'])) for x in json.load(open(os.path.join(D, sym + '_funding.json')))]
    return bars, fund


def price_at(bars, ms):
    b = bars.get(ms)
    if b:
        return b[0]
    for k in range(1, 6):
        b = bars.get(ms - 60000 * k)
        if b:
            return b[3]
    return None


def run_night(bars, fund, d_in, d_out, entry_hm, exit_hm, use_sl=True):
    e_ms, x_ms = et_ms(d_in, *entry_hm), et_ms(d_out, *exit_hm)
    pe, px = price_at(bars, e_ms), price_at(bars, x_ms)
    if pe is None or px is None:
        return None
    exit_price, t_exit, stopped = px, x_ms, False
    if use_sl:
        sl, t = pe * (1 - SL_PCT), e_ms + 60000
        while t < x_ms:
            b = bars.get(t)
            if b and b[2] <= sl:
                exit_price, t_exit, stopped = min(sl, b[0]), t, True
                break
            t += 60000
    gross = exit_price / pe - 1
    fnd = sum(rate for (ft, rate) in fund if e_ms < ft <= t_exit)      # longs pay positive rates
    return {'d': d_in, 'gross': gross, 'fund': fnd, 'net': gross - FEE_RT - fnd, 'stopped': stopped,
            'gap': (d_out - d_in).days}


def stats(rets, ci=False):
    n = len(rets)
    mean = sum(rets) / n
    sd = statistics.stdev(rets) if n > 1 else 0.0
    wins, losses = [r for r in rets if r > 0], [r for r in rets if r <= 0]
    cum = peak = dd = 0.0
    for r in rets:
        cum += r
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    out = {'n': n, 'mean': mean, 'wr': len(wins) / n, 't': (mean / (sd / math.sqrt(n))) if sd > 0 else 0.0,
           'worst': min(rets), 'best': max(rets), 'dd': dd,
           'pf': (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float('inf')}
    if ci:
        rnd = random.Random(20260920)
        ms = sorted(sum(rnd.choices(rets, k=n)) / n for _ in range(3000))
        out['lo'], out['hi'] = ms[75], ms[2925]
    return out


def pct(x, d=2):
    return ('%+.' + str(d) + 'f%%') % (x * 100)


def main():
    days = trading_days()
    nights = [(days[i], days[i + 1]) for i in range(len(days) - 1) if days[i] <= LAST_ENTRY_DAY]
    data = {s: load(s) for s in SYMS}
    res = {}

    print('=' * 100)
    print('DATA COVERAGE')
    for s in SYMS:
        bars, fund = data[s]
        ts = sorted(bars)
        first = datetime.fromtimestamp(ts[0] / 1000, tz=timezone.utc)
        n_nights = sum(1 for (a, b) in nights if price_at(bars, et_ms(a, 16, 0)) and price_at(bars, et_ms(b, 9, 30))
                       and et_ms(a, 16, 0) >= ts[0])
        print('  %-9s %7d 1-min bars from %s | %3d funding records | ~%d completed overnight nights' %
              (s, len(bars), first.strftime('%Y-%m-%d'), len(fund), n_nights))

    # ---------------------------------------------------------------- validation
    print('\n' + '=' * 100)
    print('VALIDATION: engine vs MU\'s last 4 LIVE trades (real exchange fills), live timing 15:55 -> 9:28')
    live = [(date(2026, 9, 14), date(2026, 9, 15), 927.54, 937.52), (date(2026, 9, 15), date(2026, 9, 16), 928.30, 937.92),
            (date(2026, 9, 16), date(2026, 9, 17), 926.19, 955.57), (date(2026, 9, 17), date(2026, 9, 18), 979.12, 985.26)]
    bars, fund = data['MUUSDT']
    print('  %-12s %14s %14s' % ('night', 'live fills', 'backtest bars'))
    for a, b, le, lx in live:
        r = run_night(bars, fund, a, b, (15, 55), (9, 28), use_sl=False)
        print('  %s->%s %14s %14s   (gross)' % (a.strftime('%m-%d'), b.strftime('%m-%d'), pct(lx / le - 1), pct(r['gross'])))

    # ---------------------------------------------------------------- main matrix
    ENTRIES = [('15:55 (live bot timing)', (15, 55)), ('16:00 = at the close', (16, 0)), ('16:01 = +1 min', (16, 1)),
               ('16:02 = +2 min', (16, 2)), ('16:05 = +5 min', (16, 5))]
    EXITS = [('9:30 = the open', (9, 30)), ('9:28 (live bot timing)', (9, 28))]
    print('\n' + '=' * 100)
    print('MAIN RESULTS  (per trade, % of notional; net = after 0.10% round-trip fees + real funding; 3.5% stop applied)')
    for s in SYMS:
        bars, fund = data[s]
        ts0 = min(bars)
        print('\n--- %s ---' % s)
        print('  %-24s %-24s %4s %6s %8s %8s %8s %7s %19s %8s %9s' %
              ('entry', 'exit', 'n', 'win%', 'gross', 'funding', 'NET avg', 't-stat', '95% CI of net avg', 'worst', '$/trade*'))
        for en, ehm in ENTRIES:
            for xn, xhm in EXITS:
                if xhm == (9, 28) and ehm not in ((15, 55), (16, 0)):
                    continue
                rows = [r for r in (run_night(bars, fund, a, b, ehm, xhm) for (a, b) in nights if et_ms(a, 15, 50) >= ts0) if r]
                st = stats([r['net'] for r in rows], ci=True)
                gross = sum(r['gross'] for r in rows) / len(rows)
                fnd = sum(r['fund'] for r in rows) / len(rows)
                res[(s, en, xn)] = rows
                print('  %-24s %-24s %4d %5.0f%% %8s %8s %8s %7.2f %19s %8s %9.2f' %
                      (en, xn, st['n'], st['wr'] * 100, pct(gross), pct(fnd, 3), pct(st['mean']), st['t'],
                       '[%s, %s]' % (pct(st['lo']), pct(st['hi'])), pct(st['worst']), st['mean'] * NOTIONAL))
        print('  (* $ per trade on the live MU size: $40 stake x 40x = $1,600 notional)')

    # ---------------------------------------------------------------- dip diagnostic
    print('\n' + '=' * 100)
    print('THE "DIP AFTER THE CLOSE" QUESTION -- average price path, weeknights only, relative to the 16:00 close price')
    CK = [('15:55', 0, 15, 55), ('15:59', 0, 15, 59), ('16:00', 0, 16, 0), ('16:01', 0, 16, 1), ('16:02', 0, 16, 2),
          ('16:05', 0, 16, 5), ('16:10', 0, 16, 10), ('16:30', 0, 16, 30), ('17:00', 0, 17, 0), ('18:00', 0, 18, 0),
          ('20:00', 0, 20, 0), ('22:00', 0, 22, 0), ('00:00', 1, 0, 0), ('04:00', 1, 4, 0), ('08:00', 1, 8, 0),
          ('09:00', 2, 9, 0), ('09:28', 2, 9, 28), ('09:30 open', 2, 9, 30)]
    print('  %-10s' % 'time ET' + ''.join('%14s' % s for s in SYMS))
    path = {s: {} for s in SYMS}
    for s in SYMS:
        bars, fund = data[s]
        ts0 = min(bars)
        for lab, doff, hh, mm in CK:
            vals = []
            for (a, b) in nights:
                if (b - a).days != 1 or et_ms(a, 15, 50) < ts0:
                    continue
                base = price_at(bars, et_ms(a, 16, 0))
                dd_ = a + timedelta(days=1) if doff == 1 else (b if doff == 2 else a)
                p = price_at(bars, et_ms(dd_, hh, mm))
                if base and p:
                    vals.append(p / base - 1)
            path[s][lab] = (sum(vals) / len(vals), len(vals)) if vals else (0, 0)
    for lab, *_ in CK:
        print('  %-10s' % lab + ''.join('%14s' % pct(path[s][lab][0], 3) for s in SYMS))
    print('\n  Best-case bound: if you could buy at the LOWEST price inside the first minute / first 5 minutes after 16:00')
    for s in SYMS:
        bars, fund = data[s]
        ts0 = min(bars)
        m1, m5, up1 = [], [], []
        for (a, b) in nights:
            if et_ms(a, 15, 50) < ts0:
                continue
            base = price_at(bars, et_ms(a, 16, 0))
            lows1 = [bars[et_ms(a, 16, 0)][2]] if et_ms(a, 16, 0) in bars else []
            lows5 = [bars[et_ms(a, 16, k)][2] for k in range(5) if et_ms(a, 16, k) in bars]
            if base and lows1 and lows5:
                m1.append(min(lows1) / base - 1)
                m5.append(min(lows5) / base - 1)
        print('    %-9s avg dip to the lowest print: within 1 min %s | within 5 min %s   (n=%d)   vs. 0.10%% round-trip fees' %
              (s, pct(sum(m1) / len(m1), 3), pct(sum(m5) / len(m5), 3), len(m1)))

    # ---------------------------------------------------------------- splits (16:00 -> 9:30 as the 'at the close' variant)
    print('\n' + '=' * 100)
    print('STABILITY CHECKS -- variant: buy at 16:00 close, sell at 9:30 open (net avg per trade)')
    for s in SYMS:
        rows = res[(s, '16:00 = at the close', '9:30 = the open')]
        wk = [r['net'] for r in rows if r['gap'] == 1]
        we = [r['net'] for r in rows if r['gap'] > 1]
        h = len(rows) // 2
        a_, b_ = [r['net'] for r in rows[:h]], [r['net'] for r in rows[h:]]
        print('\n  %s' % s)
        print('    weeknights (Mon-Thu -> next day): n=%3d  avg %s  win %.0f%%' % (len(wk), pct(sum(wk) / len(wk)), 100 * sum(1 for x in wk if x > 0) / len(wk)))
        print('    weekends/holidays (Fri -> Mon):   n=%3d  avg %s  win %.0f%%' % (len(we), pct(sum(we) / len(we)), 100 * sum(1 for x in we if x > 0) / len(we)))
        print('    first half of history:            n=%3d  avg %s   |  second half: n=%3d  avg %s' % (len(a_), pct(sum(a_) / len(a_)), len(b_), pct(sum(b_) / len(b_))))
        mo = {}
        for r in rows:
            mo.setdefault(r['d'].strftime('%Y-%m'), []).append(r['net'])
        print('    by month: ' + ' | '.join('%s %s (n=%d)' % (k[5:], pct(sum(v) / len(v)), len(v)) for k, v in sorted(mo.items())))
        print('    stopped out by the 3.5%% stop: %d of %d nights' % (sum(1 for r in rows if r['stopped']), len(rows)))
        srt = sorted(rows, key=lambda r: r['net'])
        print('    worst 3 nights: ' + ', '.join('%s %s' % (r['d'].strftime('%m-%d'), pct(r['net'])) for r in srt[:3]) +
              '  | best 3: ' + ', '.join('%s %s' % (r['d'].strftime('%m-%d'), pct(r['net'])) for r in srt[-3:]))

    # ---------------------------------------------------------------- overnight vs intraday
    print('\n' + '=' * 100)
    print('WHERE DOES THE RETURN COME FROM? average gross return, by part of the day (no fees)')
    print('  %-9s %18s %22s %22s' % ('', 'overnight 16:00->9:30', 'daytime 9:30->16:00', 'buy&hold total (sum)'))
    for s in SYMS:
        bars, fund = data[s]
        ts0 = min(bars)
        ov, dy = [], []
        for (a, b) in nights:
            if et_ms(a, 9, 25) < ts0:
                continue
            c0, o1 = price_at(bars, et_ms(a, 16, 0)), price_at(bars, et_ms(b, 9, 30))
            o0 = price_at(bars, et_ms(a, 9, 30))
            if c0 and o1:
                ov.append(o1 / c0 - 1)
            if o0 and c0:
                dy.append(c0 / o0 - 1)
        print('  %-9s %13s (n=%d) %17s (n=%d) %20s' % (s, pct(sum(ov) / len(ov), 3), len(ov), pct(sum(dy) / len(dy), 3), len(dy),
                                                      'overnight %s / day %s' % (pct(sum(ov), 1), pct(sum(dy), 1))))

    json.dump({s + '|' + en + '|' + xn: [{'d': str(r['d']), 'net': r['net'], 'gross': r['gross']} for r in rows]
               for (s, en, xn), rows in res.items()}, open(os.path.join(D, 'results.json'), 'w'))


if __name__ == '__main__':
    main()
