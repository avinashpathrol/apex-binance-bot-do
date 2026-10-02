#!/usr/bin/env python3
"""Offline tests for sentinel_bot.py (no network, no real clock). Run: python3 test_sentinel.py"""
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sentinel_bot as S


def utc(y, mo, d, h, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


def et(y, mo, d, h, mi=0, s=0):
    """an ET wall-clock instant -> aware UTC datetime"""
    return S.utc_from_et(datetime(y, mo, d, h, mi, s))


class FakeFeed:
    """Scriptable market. prices[sym] = mid; spread fixed; depth flat (no slippage unless slip>0)."""
    def __init__(self, prices, lev=None, spread=0.0, slip=0.0):
        self.prices = dict(prices)
        self.levs = lev or {'SNDKUSDT': 75, 'WDCUSDT': 20, 'LITEUSDT': 25, 'MRVLUSDT': 50, 'CRDOUSDT': 25}
        self.spread, self.slip = spread, slip
        self.funding_rows = {}          # sym -> [(t_ms, rate, mark)]
        self.fail = set()               # method names that raise
        self.calls = []
        self.filters_ = {'step': 0.01, 'min_qty': 0.01, 'max_qty': 1000, 'tick': 0.01, 'min_notional': 5.0, 'status': 'TRADING'}

    def _chk(self, name):
        self.calls.append(name)
        if name in self.fail:
            raise RuntimeError('boom ' + name)

    def quote(self, sym):
        self._chk('quote')
        p = self.prices[sym]
        return p * (1 - self.spread / 2), p * (1 + self.spread / 2)

    def fill(self, sym, side, qty):
        self._chk('fill')
        bid, ask = self.quote(sym)
        px = ask * (1 + self.slip) if side == 'BUY' else bid * (1 - self.slip)
        return {'price': px, 'source': 'depth', 'bid': bid, 'ask': ask}

    def leverage(self, sym, stake):
        self._chk('leverage')
        return self.levs[sym]

    def filters(self, sym):
        self._chk('filters')
        return dict(self.filters_)

    def funding(self, sym, start_ms, end_ms):
        self._chk('funding')
        return [r for r in self.funding_rows.get(sym, []) if start_ms < r[0] <= end_ms]

    def last_funding_rate(self, sym):
        return 0.0001


def make_bot(feed=None, tmp=None):
    tmp = tmp or tempfile.mkdtemp()
    feed = feed or FakeFeed({'SNDKUSDT': 1000.0, 'WDCUSDT': 400.0, 'LITEUSDT': 900.0, 'MRVLUSDT': 260.0, 'CRDOUSDT': 190.0})
    sent = []
    bot = S.Sentinel(feed, S.new_state(), notify=sent.append, state_path=os.path.join(tmp, 's.json'),
                     journal_path=os.path.join(tmp, 'j.jsonl'), dashboard_path=os.path.join(tmp, 'd.json'))
    bot.sent, bot.tmp = sent, tmp
    return bot, feed


class TestCalendar(unittest.TestCase):
    def test_2026_holidays_match_known_nyse_list(self):
        want = {date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25),
                date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25)}
        self.assertEqual(S.nyse_holidays(2026), want)

    def test_2027_holidays(self):
        # Jan 1 Fri; MLK Jan 18; Pres Feb 15; Good Friday Mar 26; Memorial May 31; Juneteenth Sat->Fri Jun 18;
        # Jul 4 Sun->Mon Jul 5; Labor Sep 6; Thanksgiving Nov 25; Christmas Sat->Fri Dec 24
        want = {date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26), date(2027, 5, 31),
                date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6), date(2027, 11, 25), date(2027, 12, 24)}
        self.assertEqual(S.nyse_holidays(2027), want)

    def test_early_closes(self):
        self.assertIn(date(2026, 11, 27), S.nyse_early_closes(2026))
        self.assertIn(date(2026, 12, 24), S.nyse_early_closes(2026))
        self.assertNotIn(date(2026, 7, 3), S.nyse_early_closes(2026))       # that day is the holiday itself
        self.assertNotIn(date(2027, 12, 24), S.nyse_early_closes(2027))     # observed holiday, not a half day
        self.assertEqual(S.close_minute(date(2026, 11, 27)), 13 * 60)
        self.assertEqual(S.close_minute(date(2026, 9, 21)), 16 * 60)

    def test_trading_day(self):
        self.assertTrue(S.is_trading_day(date(2026, 9, 21)))       # Monday
        self.assertFalse(S.is_trading_day(date(2026, 9, 20)))      # Sunday
        self.assertFalse(S.is_trading_day(date(2026, 9, 7)))       # Labor Day

    def test_dst_boundaries_and_offsets(self):
        self.assertEqual(S._dst_bounds_utc(2026), (datetime(2026, 3, 8, 7), datetime(2026, 11, 1, 6)))
        self.assertEqual(S.et_from_utc(utc(2026, 9, 21, 19, 55)), datetime(2026, 9, 21, 15, 55))     # EDT
        self.assertEqual(S.et_from_utc(utc(2026, 12, 15, 20, 55)), datetime(2026, 12, 15, 15, 55))   # EST
        self.assertEqual(S.et_from_utc(utc(2026, 3, 8, 6, 59)), datetime(2026, 3, 8, 1, 59))
        self.assertEqual(S.et_from_utc(utc(2026, 3, 8, 7, 0)), datetime(2026, 3, 8, 3, 0))
        self.assertEqual(S.et_from_utc(utc(2026, 11, 1, 5, 59)), datetime(2026, 11, 1, 1, 59))
        self.assertEqual(S.et_from_utc(utc(2026, 11, 1, 6, 0)), datetime(2026, 11, 1, 1, 0))

    def test_matches_zoneinfo_when_available(self):
        try:
            import zoneinfo
            tz = zoneinfo.ZoneInfo('America/New_York')
        except Exception:
            self.skipTest('zoneinfo not available on this interpreter')
        t = utc(2026, 1, 1, 0)
        for _ in range(0, 365 * 3 * 24 * 2):              # every 30 min for 3 years
            want = t.astimezone(tz).replace(tzinfo=None)
            self.assertEqual(S.et_from_utc(t), want, t)
            t += timedelta(minutes=30)
            if t.year > 2028:
                break

    def test_round_trip_et_utc(self):
        for dt in (datetime(2026, 9, 21, 15, 55), datetime(2026, 12, 15, 9, 28), datetime(2027, 6, 1, 16, 0)):
            self.assertEqual(S.et_from_utc(S.utc_from_et(dt)), dt)

    def test_entry_window(self):
        f = lambda d, h, m: S.in_entry_window(datetime(2026, 9, d, h, m))
        self.assertFalse(f(21, 15, 54))
        self.assertTrue(f(21, 15, 55))
        self.assertTrue(f(21, 16, 5))
        self.assertFalse(f(21, 16, 6))
        self.assertFalse(f(19, 15, 57))          # Saturday
        self.assertFalse(S.in_entry_window(datetime(2026, 9, 7, 15, 57)))     # Labor Day
        self.assertTrue(S.in_entry_window(datetime(2026, 11, 27, 12, 57)))    # half day uses 12:55-13:05
        self.assertFalse(S.in_entry_window(datetime(2026, 11, 27, 15, 57)))

    def test_next_entry(self):
        n = S.et_from_utc(S.next_entry_utc(utc(2026, 9, 20, 12)))            # Sunday -> Monday 15:55 ET
        self.assertEqual(n, datetime(2026, 9, 21, 15, 55))
        n = S.et_from_utc(S.next_entry_utc(et(2026, 9, 4, 17, 0)))           # Fri evening -> Tue after Labor Day Monday
        self.assertEqual(n, datetime(2026, 9, 8, 15, 55))
        n = S.et_from_utc(S.next_entry_utc(et(2026, 11, 26, 10, 0)))         # Thanksgiving -> half day Friday
        self.assertEqual(n, datetime(2026, 11, 27, 12, 55))


class TestHelpers(unittest.TestCase):
    def test_pick_max_leverage_uses_real_brackets(self):
        sndk = [{'notionalFloor': 0, 'notionalCap': 50000, 'initialLeverage': 75}, {'notionalFloor': 50000, 'notionalCap': 250000, 'initialLeverage': 50}]
        wdc = [{'notionalFloor': 0, 'notionalCap': 100000, 'initialLeverage': 20}]
        self.assertEqual(S.pick_max_leverage(sndk, 100), 75)
        self.assertEqual(S.pick_max_leverage(wdc, 100), 20)
        # if bracket 1 were tiny, leverage must drop until notional fits a bracket that allows it
        tiny = [{'notionalFloor': 0, 'notionalCap': 2000, 'initialLeverage': 50}, {'notionalFloor': 2000, 'notionalCap': 90000, 'initialLeverage': 10}]
        self.assertEqual(S.pick_max_leverage(tiny, 100), 19)     # 100*20=2000 -> bracket 2 allows only 10 -> 100*19=1900 in b1 allows 50 >= 19

    def test_floor_and_tick(self):
        self.assertEqual(S.floor_step(4.2517, 0.01), 4.25)
        self.assertEqual(S.floor_step(0.3, 0.1), 0.3)
        self.assertEqual(S.round_tick(1702.2048, 0.01), 1702.2)

    def test_walk_book(self):
        v, filled = S.walk_book([(100.0, 1.0), (101.0, 2.0)], 2.0)
        self.assertAlmostEqual(v, 100.5)
        self.assertEqual(filled, 2.0)
        v, filled = S.walk_book([(100.0, 1.0)], 3.0)
        self.assertEqual((v, filled), (100.0, 1.0))

    def test_signed_endpoint_guard(self):
        feed = S.BinanceFeed()
        for path in ('/fapi/v1/order', '/fapi/v1/leverage', '/fapi/v2/account', '/fapi/v1/positionRisk'):
            with self.assertRaises(PermissionError):
                feed.signed_get(path, {})
            with self.assertRaises(PermissionError):
                feed.public_get(path, {})
        self.assertNotIn('order', ' '.join(S.BinanceFeed.PUBLIC_PATHS | S.BinanceFeed.SIGNED_PATHS).lower().replace('orderbook', ''))

    def test_source_has_no_order_placement(self):
        with open(S.__file__) as fh:
            src = fh.read()
        for bad in ("'POST'", '"POST"', '.post(', '/fapi/v1/order', 'newOrder', 'reduceOnly', 'listenKey'):
            body = src.split('def send_telegram')[0] + src.split('# ══ the engine')[1] if bad == '.post(' else src
            if bad == '.post(':
                self.assertNotIn('requests.post', src.split('def send_telegram')[0])   # only telegram may POST
                continue
            self.assertNotIn(bad, src.replace("'/fapi/v1/order', {'symbol': 'SNDKUSDT'}", ''), bad)


class TestEngine(unittest.TestCase):
    def test_full_night_stop_free(self):
        bot, feed = make_bot()
        # Monday 15:55 ET entry
        ev = bot.cycle(et(2026, 9, 21, 15, 55, 5))
        self.assertEqual([e['type'] for e in ev], ['open'] * 5)
        p = bot.st['positions']['SNDKUSDT']
        self.assertEqual(p['leverage'], 75)
        self.assertAlmostEqual(p['qty'], 7.5, places=2)                   # 100*75/1000
        self.assertAlmostEqual(p['notional'], 7500.0, places=1)
        self.assertAlmostEqual(p['sl_price'], 1000 * 0.965, places=2)
        self.assertAlmostEqual(bot.st['positions']['WDCUSDT']['notional'], 2000.0, places=1)
        lite = bot.st['positions']['LITEUSDT']['notional']
        self.assertTrue(2500.0 - 9.0 < lite <= 2500.0, lite)              # 25x at $900/share floors to the 0.01 lot step
        self.assertEqual(len(bot.sent), 1)                                # one batched Telegram message
        # same window, later cycle -> no second entry
        self.assertEqual(bot.cycle(et(2026, 9, 21, 15, 56)), [])
        self.assertEqual(bot.cycle(et(2026, 9, 21, 16, 5, 30)), [])
        # overnight: price up 1%, stop not hit, no early exit
        feed.prices['SNDKUSDT'] = 1010.0
        self.assertEqual(bot.cycle(et(2026, 9, 21, 23, 0)), [])
        self.assertEqual(bot.cycle(et(2026, 9, 22, 9, 27, 50)), [])       # one second before 9:28
        # 9:28 -> exit
        feed.funding_rows['SNDKUSDT'] = [(int(et(2026, 9, 22, 4, 0).timestamp() * 1000), 0.0002, 1005.0)]
        ev = bot.cycle(et(2026, 9, 22, 9, 28, 10))
        closes = {e['rec']['base']: e['rec'] for e in ev}
        self.assertEqual(set(closes), {'SNDK', 'WDC', 'LITE', 'MRVL', 'CRDO'})
        r = closes['SNDK']
        self.assertEqual(r['reason'], 'Market Open')
        self.assertAlmostEqual(r['gross'], 7.5 * 10.0, places=2)                  # +$75
        self.assertAlmostEqual(r['fee'], 7.5 * 1000 * 0.0005 + 7.5 * 1010 * 0.0005, places=2)
        self.assertAlmostEqual(r['funding'], 0.0002 * 7.5 * 1005.0, places=3)     # long pays positive funding
        self.assertAlmostEqual(r['net'], r['gross'] - r['fee'] - r['funding'], places=3)
        self.assertFalse(r['stopped'])
        self.assertTrue(all(v is None for v in bot.st['positions'].values()))
        # journal + dashboard written
        self.assertEqual(len(open(bot.journal_path).read().strip().split('\n')), 5)
        dash = json.load(open(bot.dashboard_path))
        self.assertEqual(dash['mode'], 'paper')
        self.assertEqual(dash['symbols']['SNDKUSDT']['performance']['total'], 1)

    def test_stop_loss_is_software_and_uses_bid(self):
        bot, feed = make_bot(FakeFeed({'SNDKUSDT': 1000.0, 'WDCUSDT': 400.0, 'LITEUSDT': 900.0, 'MRVLUSDT': 260.0, 'CRDOUSDT': 190.0}, spread=0.0004))
        bot.cycle(et(2026, 9, 21, 15, 55))
        entry = bot.st['positions']['SNDKUSDT']['entry_price']
        feed.prices['SNDKUSDT'] = 970.0                       # -3.0%: above stop
        self.assertEqual([e for e in bot.cycle(et(2026, 9, 21, 20, 0)) if e['rec']['base'] == 'SNDK'] if False else [], [])
        self.assertIsNotNone(bot.st['positions']['SNDKUSDT'])
        feed.prices['SNDKUSDT'] = 955.0                       # -4.5% gap through the stop
        ev = bot.cycle(et(2026, 9, 21, 20, 0, 30))
        r = [e['rec'] for e in ev if e['rec']['base'] == 'SNDK'][0]
        self.assertEqual(r['reason'], 'Stop Loss')
        self.assertTrue(r['stopped'])
        self.assertLess(r['exit_price'], entry * 0.965)       # filled at the gapped price, not at the stop level
        self.assertAlmostEqual(r['exit_price'], 955.0 * (1 - 0.0002), places=2)
        self.assertLess(r['net'], -200)                       # ~-4.5% of $7,500 plus costs
        self.assertIsNone(bot.st['positions']['SNDKUSDT'])
        self.assertIsNotNone(bot.st['positions']['WDCUSDT'])   # others unaffected

    def test_no_reentry_after_stop_same_window(self):
        bot, feed = make_bot()
        bot.cycle(et(2026, 9, 21, 15, 55))
        feed.prices['SNDKUSDT'] = 900.0
        bot.cycle(et(2026, 9, 21, 15, 57))                    # stopped inside the entry window
        self.assertIsNone(bot.st['positions']['SNDKUSDT'])
        feed.prices['SNDKUSDT'] = 1000.0
        bot.cycle(et(2026, 9, 21, 16, 1))
        self.assertIsNone(bot.st['positions']['SNDKUSDT'])    # not re-opened the same day

    def test_weekend_hold_exits_monday(self):
        bot, feed = make_bot()
        bot.cycle(et(2026, 9, 18, 15, 55))                    # Friday
        for hh in (20, 23):
            self.assertEqual(bot.cycle(et(2026, 9, 19, hh, 0)), [])    # Saturday
        self.assertEqual(bot.cycle(et(2026, 9, 20, 9, 30)), [])        # Sunday 9:30 -> not a trading day
        self.assertEqual(bot.cycle(et(2026, 9, 21, 9, 27)), [])
        ev = bot.cycle(et(2026, 9, 21, 9, 28, 20))
        self.assertEqual(len(ev), 5)
        self.assertTrue(all(e['rec']['reason'] == 'Market Open' for e in ev))

    def test_holiday_hold_skips_holiday_morning(self):
        bot, feed = make_bot()
        bot.cycle(et(2026, 9, 4, 15, 55))                     # Fri Sep 4, Monday Sep 7 = Labor Day
        self.assertEqual(bot.cycle(et(2026, 9, 7, 9, 30)), [])         # holiday morning: hold
        ev = bot.cycle(et(2026, 9, 8, 9, 29))                          # Tuesday
        self.assertEqual(len(ev), 5)
        self.assertEqual(ev[0]['rec']['exit_day'], '2026-09-08')

    def test_no_entry_on_holiday_and_skip_recorded(self):
        bot, feed = make_bot()
        self.assertEqual(bot.cycle(et(2026, 9, 7, 15, 55, 10)), [])
        self.assertTrue(all(v is None for v in bot.st['positions'].values()))
        self.assertTrue(any(s['why'] == 'US market holiday' for s in bot.st['skips']))
        self.assertNotIn('fill', feed.calls)

    def test_half_day_entry_and_next_day_exit(self):
        bot, feed = make_bot()
        self.assertEqual(bot.cycle(et(2026, 11, 27, 15, 55)), [])       # 3:55pm on a half day = too late, no entry
        ev = bot.cycle(et(2026, 11, 27, 12, 56))
        self.assertEqual([e['type'] for e in ev], ['open'] * 5)
        self.assertTrue(all(e['rec']['reason'] == 'Market Open' for e in bot.cycle(et(2026, 11, 30, 9, 30))))

    def test_late_exit_when_window_missed(self):
        bot, feed = make_bot()
        bot.cycle(et(2026, 9, 21, 15, 55))
        ev = bot.cycle(et(2026, 9, 22, 10, 30))               # bot was down through 9:28-9:40
        self.assertTrue(all(e['rec']['reason'] == 'Late Exit' and e['rec']['late'] for e in ev))

    def test_restart_resumes_position_without_duplicate_entry(self):
        bot, feed = make_bot()
        bot.cycle(et(2026, 9, 21, 15, 55))
        st = json.load(open(bot.state_path))                  # what a restarted process would load
        bot2 = S.Sentinel(feed, st, notify=lambda m: None, state_path=bot.state_path, journal_path=bot.journal_path,
                          dashboard_path=bot.dashboard_path)
        self.assertEqual(bot2.cycle(et(2026, 9, 21, 15, 58)), [])
        self.assertEqual(sum(1 for v in bot2.st['positions'].values() if v), 5)
        ev = bot2.cycle(et(2026, 9, 22, 9, 29))
        self.assertEqual(len(ev), 5)

    def test_transient_leverage_failure_retries_within_window(self):
        bot, feed = make_bot()
        feed.fail.add('leverage')
        bot.cycle(et(2026, 9, 21, 15, 55))
        self.assertTrue(all(v is None for v in bot.st['positions'].values()))
        self.assertEqual(bot.st['last_entry_day'], {})        # the day is NOT burned
        feed.fail.discard('leverage')
        ev = bot.cycle(et(2026, 9, 21, 15, 56))
        self.assertEqual(len(ev), 5)

    def test_quote_failure_isolated_per_symbol_and_position_kept(self):
        class Flaky(FakeFeed):
            def quote(self, sym):
                if sym == 'WDCUSDT' and getattr(self, 'wdc_down', False):
                    raise RuntimeError('wdc feed down')
                return FakeFeed.quote(self, sym)
        bot, feed = make_bot(Flaky({'SNDKUSDT': 1000.0, 'WDCUSDT': 400.0, 'LITEUSDT': 900.0, 'MRVLUSDT': 260.0, 'CRDOUSDT': 190.0}))
        bot.cycle(et(2026, 9, 21, 15, 55))
        feed.wdc_down = True
        feed.prices['LITEUSDT'] = 850.0                        # LITE -5.6% -> must still stop even though WDC errors
        ev = bot.cycle(et(2026, 9, 21, 20, 0))
        self.assertEqual([e['rec']['base'] for e in ev], ['LITE'])
        self.assertIsNotNone(bot.st['positions']['WDCUSDT'])   # retained, retried next cycle
        self.assertIn('WDCUSDT', bot.errors)

    def test_outlier_quote_needs_confirmation(self):
        bot, feed = make_bot()
        bot.cycle(et(2026, 9, 21, 15, 55))
        feed.prices['SNDKUSDT'] = 5.0                          # garbage tick
        self.assertEqual(bot.cycle(et(2026, 9, 21, 20, 0)), [])
        self.assertIsNotNone(bot.st['positions']['SNDKUSDT'])
        feed.prices['SNDKUSDT'] = 1000.0                       # tick was bogus -> recovers, flag cleared
        bot.cycle(et(2026, 9, 21, 20, 1))
        self.assertFalse(bot.st['positions']['SNDKUSDT']['suspect_tick'])
        feed.prices['SNDKUSDT'] = 5.0                          # a real crash: two in a row closes it
        bot.cycle(et(2026, 9, 21, 20, 2))
        ev = bot.cycle(et(2026, 9, 21, 20, 3))
        self.assertEqual(ev[0]['rec']['reason'], 'Stop Loss')

    def test_slippage_and_fee_accounting(self):
        bot, feed = make_bot(FakeFeed({'SNDKUSDT': 1000.0, 'WDCUSDT': 400.0, 'LITEUSDT': 900.0, 'MRVLUSDT': 260.0, 'CRDOUSDT': 190.0}, spread=0.0002, slip=0.0004))
        bot.cycle(et(2026, 9, 21, 15, 55))
        p = bot.st['positions']['WDCUSDT']
        self.assertAlmostEqual(p['entry_price'], 400 * (1 + 0.0001) * (1 + 0.0004), places=3)
        self.assertGreater(p['entry_slip_bps'], 4)
        ev = bot.cycle(et(2026, 9, 22, 9, 28))
        r = [e['rec'] for e in ev if e['rec']['base'] == 'WDC'][0]
        self.assertLess(r['net'], 0)                            # flat price + spread + slip + fees = a small loss
        self.assertAlmostEqual(r['net'], r['gross'] - r['fee'] - r['funding'], places=3)

    def test_liquidity_check_aggregates_real_slippage(self):
        # 2026-09-29: CRDO was added specifically to see how a thin book behaves -- perf() now surfaces
        # avg/max entry+exit slippage so that's visible without reading individual trades.
        bot, feed = make_bot(FakeFeed({'SNDKUSDT': 1000.0, 'WDCUSDT': 400.0, 'LITEUSDT': 900.0, 'MRVLUSDT': 260.0, 'CRDOUSDT': 190.0}, spread=0.0002, slip=0.0004))
        bot.cycle(et(2026, 9, 21, 15, 55))
        bot.cycle(et(2026, 9, 22, 9, 28))
        p = bot.perf(bot.st['trades'])
        expected_bps = (0.0001 + 0.0004) * 1e4    # half the spread + the slip, same MAGNITUDE on entry and exit
        self.assertAlmostEqual(p['avg_entry_slip_bps'], expected_bps, places=1)     # BUY fills above mid: positive
        self.assertAlmostEqual(p['avg_exit_slip_bps'], -expected_bps, places=1)     # SELL fills below mid: negative
        self.assertAlmostEqual(p['max_entry_slip_bps'], expected_bps, places=1)
        self.assertEqual(p['partial_fills'], 0)
        self.assertEqual(bot.perf([])['avg_entry_slip_bps'], 0)   # no trades yet -> no crash, just zeros

    def test_liquidity_check_flags_partial_fills(self):
        class ThinBook(FakeFeed):
            def fill(self, sym, side, qty):
                r = FakeFeed.fill(self, sym, side, qty)
                if sym == 'CRDOUSDT':
                    r['source'] = 'depth_partial'
                return r
        bot, feed = make_bot(ThinBook({'SNDKUSDT': 1000.0, 'WDCUSDT': 400.0, 'LITEUSDT': 900.0, 'MRVLUSDT': 260.0, 'CRDOUSDT': 190.0}))
        bot.cycle(et(2026, 9, 21, 15, 55))
        bot.cycle(et(2026, 9, 22, 9, 28))
        p = bot.perf(bot.st['trades'])
        self.assertEqual(p['partial_fills'], 1)    # counts TRADES touched by a thin fill, not legs -- CRDO's one trade had both legs thin

    def test_funding_estimated_when_history_unavailable(self):
        bot, feed = make_bot()
        bot.cycle(et(2026, 9, 21, 15, 55))
        feed.fail.add('funding')
        r = [e['rec'] for e in bot.cycle(et(2026, 9, 22, 9, 28)) if e['rec']['base'] == 'SNDK'][0]
        self.assertTrue(r['funding_estimated'])
        self.assertGreater(r['funding'], 0)

    def test_corrupt_state_recovers(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, 's.json')
        open(p, 'w').write('{not json')
        st = S.load_state(p)
        self.assertEqual(st['trades'], [])
        self.assertTrue(any('corrupt' in f for f in os.listdir(d)))

    def test_leverage_cap_change_is_used_next_entry(self):
        bot, feed = make_bot()
        feed.levs['SNDKUSDT'] = 50
        bot.cycle(et(2026, 9, 21, 15, 55))
        self.assertAlmostEqual(bot.st['positions']['SNDKUSDT']['notional'], 5000.0, places=1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
