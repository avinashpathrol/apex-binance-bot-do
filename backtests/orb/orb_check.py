#!/usr/bin/env python3
"""Signal + fee-drag check of the 'first 5-minute candle vs 12 EMA' opening strategy on Binance TradFi perps.
Entry: open of the 9:35 ET bar, direction = close of the 9:30-9:35 candle vs EMA12 of 5-min closes (long above, short below).
Exits tested (the ad's trailing logic is not public, so it cannot be replicated): (A) hold to 15:55 ET;
(B) same, with a stop at the far end of the first candle (the usual ORB stop).  Costs: 0.10% round trip (0.05% taker/side).
Pure Python 3.6.  Run from the backtest folder."""
import io, math, statistics, sys
sys.stdout, _o = io.StringIO(), sys.stdout
import analyze as A
sys.stdout = _o

FEE_RT = 0.0010
SYMS = ['SPYUSDT', 'NVDAUSDT', 'MUUSDT', 'SNDKUSDT']


def bar(bars, ms):
    return bars.get(ms)


def five_closes(bars, end_ms, n):
    """closes of the last n completed 5-min bars ending at end_ms (exclusive), oldest first (None-safe)."""
    out = []
    t = end_ms - 5 * 60000
    for _ in range(n):
        b = bars.get(t + 4 * 60000)            # close of the 5th minute bar in that 5-min bucket
        if b is None:
            for k in range(1, 4):
                b = bars.get(t + (4 - k) * 60000)
                if b:
                    break
        if b:
            out.append(b[3])
        t -= 5 * 60000
    return out[::-1]


def ema(xs, n):
    a, e = 2.0 / (n + 1), xs[0]
    for x in xs[1:]:
        e = a * x + (1 - a) * e
    return e


def tstat(xs):
    n = len(xs)
    if n < 3:
        return 0.0
    sd = statistics.stdev(xs)
    return (sum(xs) / n) / (sd / math.sqrt(n)) if sd else 0.0


days = A.trading_days()
print('First-5-minute-candle vs EMA12 opening strategy on Binance perps (entry 9:35 ET, fees 0.10%% round trip)')
print('=' * 118)
print('%-9s %4s | %6s %8s | %-31s | %-47s' % ('', 'days', 'long%', '1st-cand', 'A) hold to 15:55, no stop', 'B) stop at far end of the first candle, exit 15:55'))
print('%-9s %4s | %6s %8s | %6s %8s %8s %6s | %6s %8s %8s %6s %8s %7s' %
      ('symbol', '', '', 'range', 'win%', 'gross', 'NET', 't', 'win%', 'gross', 'NET', 't', 'fee in R', 'stop%'))
for sym in SYMS:
    bars, fund = A.load(sym)
    ts0 = min(bars)
    A_g, B_g, B_r, rng, longs, nB, stopped = [], [], [], [], 0, 0, 0
    for d in days:
        s = A.et_ms(d, 9, 30)
        if s - 3 * 3600 * 1000 < ts0:
            continue
        c1 = [bars.get(s + i * 60000) for i in range(5)]
        if any(x is None for x in c1):
            continue
        o, h, l, c = c1[0][0], max(x[1] for x in c1), min(x[2] for x in c1), c1[4][3]
        hist = five_closes(bars, s, 60) + [c]
        if len(hist) < 40:
            continue
        long_ = c > ema(hist, 12)
        e_bar, x_bar = bars.get(s + 5 * 60000), bars.get(A.et_ms(d, 15, 55))
        if not e_bar or not x_bar:
            continue
        entry, exit_px = e_bar[0], x_bar[0]
        sgn = 1 if long_ else -1
        longs += long_
        rng.append((h - l) / entry)
        A_g.append(sgn * (exit_px / entry - 1))
        # B) stop at the far end of the first candle (long: candle low; short: candle high)
        stop = l if long_ else h
        risk = abs(entry - stop) / entry
        if risk <= 0:
            continue
        px, hit = exit_px, False
        t = s + 5 * 60000
        end = A.et_ms(d, 15, 55)
        while t < end:
            b = bars.get(t)
            if b and ((long_ and b[2] <= stop) or ((not long_) and b[1] >= stop)):
                px, hit = stop, True
                break
            t += 60000
        g = sgn * (px / entry - 1)
        B_g.append(g); B_r.append((g - FEE_RT) / risk); nB += 1; stopped += hit
    n = len(A_g)
    netA = [g - FEE_RT for g in A_g]
    netB = [g - FEE_RT for g in B_g]
    avg_risk = sum(rng) / len(rng)
    print('%-9s %4d | %5.0f%% %7.3f%% | %5.0f%% %+7.3f%% %+7.3f%% %6.2f | %5.0f%% %+7.3f%% %+7.3f%% %6.2f %7.2fR %6.0f%%' % (
        sym[:-4], n, 100 * longs / n, 100 * avg_risk,
        100 * sum(1 for x in netA if x > 0) / n, 100 * sum(A_g) / n, 100 * sum(netA) / n, tstat(netA),
        100 * sum(1 for x in netB if x > 0) / nB, 100 * sum(B_g) / nB, 100 * sum(netB) / nB, tstat(netB),
        FEE_RT / avg_risk, 100 * stopped / nB))
print('\nfee in R = 0.10%% round-trip fee divided by the average first-candle range (the stop distance).  The ad\'s claimed edge is ~+0.12R per trade.')
