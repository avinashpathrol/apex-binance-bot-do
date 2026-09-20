#!/usr/bin/env python3
"""Replay the REAL Sentinel engine over the historical 1-minute perp data used in the overnight backtest and compare
night-by-night with analyze.run_night (buy 15:55 ET / sell 9:28 ET / 3.5% stop / 0.10% fees / real funding).
Nothing here touches the network or writes state.  Usage: python3 replay_check.py [SNDKUSDT|MUUSDT]"""
import os, sys, time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
BT = os.path.join(HERE, '..', 'backtest')
sys.path.insert(0, BT)
sys.path.insert(0, '/Users/avinash/Downloads/bot code/apex-binance-bot-do')
import io
_o = sys.stdout; sys.stdout = io.StringIO()
import analyze as A                     # scratchpad copy (has the CSVs next to it)
sys.stdout = _o
import sentinel_bot as S
import logging
logging.getLogger('sentinel').setLevel(logging.ERROR)

SYM = sys.argv[1] if len(sys.argv) > 1 else 'SNDKUSDT'
BARS, FUND = A.load(SYM)
TS0 = min(BARS)


class ReplayFeed:
    """mode 'open': one quote per minute = that minute's open (no wick information).
       mode 'wick': also a :30 quote = the bar's LOW if it pierces the stop-relevant range, else the close.
       Fill = the quote itself (no spread, no slippage) so results are directly comparable with the backtest."""
    def __init__(self, mode):
        self.mode, self.now_ms = mode, 0
        self.lev = 75
    def _px(self):
        m = (self.now_ms // 60000) * 60000
        b = BARS.get(m)
        if b is None:
            for k in range(1, 6):
                b = BARS.get(m - 60000 * k)
                if b:
                    return b[3]
            raise RuntimeError('no bar')
        if self.now_ms % 60000 < 30000 or self.mode == 'open':
            return b[0]
        return b[2] if self.mode == 'wick' else b[3]          # :30 sample = the bar's low (wick-aware) -- optimistic detection
    def quote(self, sym):
        p = self._px(); return p, p
    def fill(self, sym, side, qty):
        p = self._px(); return {'price': p, 'source': 'depth', 'bid': p, 'ask': p}
    def leverage(self, sym, stake): return self.lev
    def filters(self, sym): return {'step': 0.01, 'min_qty': 0.01, 'max_qty': 1e9, 'tick': 0.01, 'min_notional': 5.0, 'status': 'TRADING'}
    def funding(self, sym, s_ms, e_ms): return [(t, r, None) for (t, r) in FUND if s_ms < t <= e_ms]
    def last_funding_rate(self, sym): return 0.0


def replay(mode, step_s):
    S.SYMBOLS = {SYM: {'base': SYM[:-4], 'name': SYM, 'note': ''}}
    feed = ReplayFeed(mode)
    st = S.new_state(); st['positions'] = {SYM: None}
    bot = S.Sentinel(feed, st, notify=lambda m: None, write_files=False)
    t = max(TS0 // 1000 + 3600, int(datetime(2026, 4, 8, tzinfo=timezone.utc).timestamp()))
    end = A.et_ms(A.date(2026, 9, 18), 9, 45) // 1000
    t0 = time.time(); n = 0
    while t <= end:
        now = datetime.fromtimestamp(t, timezone.utc)
        et = S.et_from_utc(now)
        if bot.st['positions'][SYM] or S.in_entry_window(et):
            feed.now_ms = t * 1000
            bot.cycle(now); n += 1
        t += step_s
    return bot.st['trades'], n, time.time() - t0


days = A.trading_days()
nights = [(days[i], days[i + 1]) for i in range(len(days) - 1) if days[i] <= A.LAST_ENTRY_DAY and A.et_ms(days[i], 15, 50) >= TS0
          and days[i] >= A.date(2026, 4, 8)]
ref = {}
for a, b in nights:
    r = A.run_night(BARS, FUND, a, b, (15, 55), (9, 28), use_sl=True)
    if r: ref[a.isoformat()] = r

print('=' * 110)
print('REPLAY of the real Sentinel engine on %s 1-min data vs analyze.run_night (same nights, buy 15:55 / sell 9:28 ET, 3.5%% stop, 0.10%% fees, real funding)' % SYM)
print('=' * 110)
for mode, step in (('open', 60), ('wick', 30)):
    trades, n, secs = replay(mode, step)
    mine = {t['entry_day']: t for t in trades}
    common = sorted(set(mine) & set(ref))
    ns_c = [d for d in common if not ref[d]['stopped'] and not mine[d]['stopped']]
    diffs = [abs(mine[d]['net'] / mine[d]['notional'] - ref[d]['net']) for d in ns_c]
    ref_stops = sum(1 for d in common if ref[d]['stopped']); my_stops = sum(1 for d in common if mine[d]['stopped'])
    both = [d for d in common if ref[d]['stopped'] and mine[d]['stopped']]
    m_net = sum(mine[d]['net'] / mine[d]['notional'] for d in common) / len(common)
    r_net = sum(ref[d]['net'] for d in common) / len(common)
    print('\nmode=%-5s (%d polls, %.0fs)   nights: engine %d | backtest %d | matched %d' % (mode, n, secs, len(mine), len(ref), len(common)))
    print('  nights with NO stop in either:  %d   max |net diff| = %.6f%% of notional   (0 = identical P&L math)' % (len(ns_c), 100 * max(diffs) if diffs else 0))
    print('  stop-outs: engine %d | backtest %d | both %d' % (my_stops, ref_stops, len(both)))
    if both:
        d_stop = [mine[d]['net'] / mine[d]['notional'] - ref[d]['net'] for d in both]
        print('  on nights both stopped: engine exit is worse than the backtest by avg %.3f%% of notional (poll/gap slippage the backtest ignores)' % (-100 * sum(d_stop) / len(d_stop)))
    print('  mean net per night:  engine %+.3f%%   backtest %+.3f%%   (difference %+.3f%%)' % (100 * m_net, 100 * r_net, 100 * (m_net - r_net)))
    late = sum(1 for d in common if mine[d]['late'])
    print('  late exits: %d | reasons: %s' % (late, {k: sum(1 for d in common if mine[d]['reason'] == k) for k in set(mine[d]['reason'] for d in common)}))
