#!/usr/bin/env python3
"""Live dry run: REAL Binance quotes/depth/brackets/filters/funding, FAKE clock, nothing written, nothing sent."""
import sys, time, logging
sys.path.insert(0, '/root/binance-bot/apex-binance-bot-do')
from datetime import datetime
import sentinel_bot as S
logging.getLogger('sentinel').setLevel(logging.WARNING)
feed = S.BinanceFeed()
msgs = []
bot = S.Sentinel(feed, S.new_state(), notify=msgs.append, write_files=False)

t_in = S.utc_from_et(datetime(2026, 9, 21, 15, 55, 5))
ev = bot.cycle(t_in)
print('ENTRY cycle (fake Mon 15:55:05 ET, real market data): %d event(s)' % len(ev))
for e in ev:
    p = e['pos']
    print('  %-4s lev %2dx qty %-8s fill %-10.2f notional $%-9.2f stop %-10.2f slip %+.2fbp  src=%s' % (
        p['base'], p['leverage'], p['qty'], p['entry_price'], p['notional'], p['sl_price'], p['entry_slip_bps'], p['entry_fill_source']))
print('  telegram (not sent) ->', msgs[0].replace('\n', ' | ')[:230] if msgs else None)
print('  errors:', bot.errors or 'none')

# an overnight stop check with real quotes should NOT trigger (prices are unchanged)
print('OVERNIGHT cycle:', len(bot.cycle(S.utc_from_et(datetime(2026, 9, 21, 23, 0)))), 'events (expect 0)')

t_out = S.utc_from_et(datetime(2026, 9, 22, 9, 28, 10))
ev = bot.cycle(t_out)
print('EXIT cycle (fake Tue 9:28:10 ET): %d event(s)' % len(ev))
for e in ev:
    r = e['rec']
    print('  %-4s %-11s gross %+8.2f fee %6.2f funding %6.3f net %+8.2f (%+.3f%% notional) exit_src=%s' % (
        r['base'], r['reason'], r['gross'], r['fee'], r['funding'], r['net'], r['net_pct_notional'], r['exit_fill_source']))
print('  (real spread+slippage round trip on flat prices is the only P&L here, plus fees)')

now_ms = int(time.time() * 1000)
for sym in ('SNDKUSDT', 'WDCUSDT', 'LITEUSDT'):
    rows = feed.funding(sym, now_ms - 3 * 86400000, now_ms)
    print('funding %-9s last 3 days: %d settlements, sample %s' % (sym, len(rows), [(time.strftime('%m-%d %H:%M', time.gmtime(t / 1000)), r, m) for (t, r, m) in rows[-2:]]))
print('lev cache:', bot.st['leverage'])
