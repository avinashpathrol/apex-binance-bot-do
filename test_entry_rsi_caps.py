#!/usr/bin/env python3
"""Behavioral test of the SOXL and TSLA rsi_long_max 63 change (2026-09-21), using the REAL setups from their trade logs.
Runs the real get_decision(). Needs real pandas, so it runs on the droplet:  python3 test_entry_rsi_caps.py"""
import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_crcl_entry_filter as T          # reuses its stubs and frame() helper
B, HAVE_PANDAS = T.B, T.HAVE_PANDAS
logging.disable(logging.CRITICAL)

BULL, BEAR = '4H BULLISH', '4H BEARISH'


def setup(dist_pct, ema21, ema50, adx, pdi, mdi, rsi, atr, vol, vol_ma):
    price = ema21 * (1 + dist_pct)
    return T.frame(price, ema21, ema50, adx, pdi, mdi, rsi, atr, vol, vol_ma, lo=price * 0.997)


# (label, dist as logged, ema21, ema50, adx, +DI, -DI, rsi, atr, vol, vol_ma)  -- copied from trades_log_futures.json
SOXL_LOSING_PULLBACKS = [('09-18 10:44 -$16.04', 0.0243, 115.9909, 112.7447, 35.40, 34.15, 17.56, 64.24, 1.6980, 281234, 304569),
                         ('09-18 11:41  -$9.71', 0.0221, 115.9700, 112.7356, 35.40, 34.09, 17.53, 63.59, 1.7008, 603319, 320673)]
SOXL_TREND_CONT = [('09-18 09:51 -$15.97', 0.0313, 115.7941, 112.5763, 35.49, 36.49, 19.24, 65.77, 1.6696, 1198178, 281869),
                   ('09-21 10:21 +$20.47', 0.0409, 128.4310, 124.7505, 48.36, 33.72, 18.79, 85.64, 1.5492, 1690062, 260185)]
TSLA_LOSING_PULLBACKS = [('09-17 22:16  -$9.25', 0.0104, 364.1456, 361.9162, 28.06, 36.33, 16.66, 65.88, 2.5024, 13238, 13179),
                         ('09-18 09:50  -$6.03', 0.0076, 367.7682, 365.3253, 34.26, 30.93, 12.39, 65.93, 1.6769, 55605, 5989)]
TSLA_WINNING_PULLBACK = ('09-17 11:45  +$4.06', 0.0053, 363.3827, 361.5064, 27.70, 39.24, 16.64, 57.58, 2.4229, 10586, 11585)


@unittest.skipUnless(HAVE_PANDAS, 'needs real pandas (run on the droplet)')
class RsiCaps(unittest.TestCase):
    def dec(self, sym, args, rsi=None):
        a = list(args[1:])
        if rsi is not None:
            a[6] = rsi
        return B.get_decision(sym, setup(*a), trend4h_override=BULL)

    def test_config_values(self):
        C = B.SYMBOLS_CONFIG
        self.assertEqual((C['SOXLUSDT']['rsi_long_max'], C['TSLAUSDT']['rsi_long_max']), (63, 63))
        # everything else about SOXL / TSLA is unchanged
        s, t = C['SOXLUSDT'], C['TSLAUSDT']
        self.assertEqual((s['trade_amount'], s['leverage'], s['max_loss_pct'], s['rsi_short_max'], s['pullback_zone_pct'], s['trail_dist_atr'], s['trend_continuation_enabled']),
                         (70.0, 50, 0.35, 75, 0.030, 0.20, False))   # trend_continuation disabled bot-wide 2026-09-22 (was True)
        self.assertEqual((t['trade_amount'], t['leverage'], t['max_loss_pct'], t['rsi_short_max'], t['pullback_zone_pct'], t['trail_dist_atr'], t['trend_continuation_enabled']),
                         (40.0, 25, 0.35, 68, 0.030, 0.20, True))
        # other symbols keep their own caps; CRCL keeps last week's change
        self.assertEqual([C[k]['rsi_long_max'] for k in ('NVDAUSDT', 'AMDUSDT', 'APPUSDT', 'NBISUSDT', 'ASTSUSDT')], [70, 60, 65, 65, 65])
        self.assertEqual((C['CRCLUSDT']['rsi_long_max'], C['CRCLUSDT']['trend_continuation_enabled']), (63, False))

    def test_soxl_losing_pullbacks_are_blocked(self):
        for args in SOXL_LOSING_PULLBACKS:
            d = self.dec('SOXLUSDT', args)
            self.assertEqual(d['action'], 'HOLD', (args[0], d['reason']))
            self.assertIn('RSI', d['reason'])

    def test_tsla_losing_pullbacks_are_blocked(self):
        for args in TSLA_LOSING_PULLBACKS:
            d = self.dec('TSLAUSDT', args)
            self.assertEqual(d['action'], 'HOLD', (args[0], d['reason']))

    def test_tsla_winning_pullback_still_enters(self):
        d = self.dec('TSLAUSDT', TSLA_WINNING_PULLBACK)
        self.assertEqual(d['action'], 'LONG', d['reason'])
        self.assertIn('Pullback to EMA21', d['reason'])

    def test_boundary_at_63(self):
        for sym, args in (('SOXLUSDT', SOXL_LOSING_PULLBACKS[0]), ('TSLAUSDT', TSLA_LOSING_PULLBACKS[0])):
            self.assertEqual(self.dec(sym, args, rsi=63.0)['action'], 'LONG', sym)
            self.assertEqual(self.dec(sym, args, rsi=63.2)['action'], 'HOLD', sym)

    def test_soxl_trend_continuation_now_disabled(self):
        # superseded 2026-09-22: trend_continuation turned off bot-wide (these two setups used to be SOXL's
        # only trend-continuation winners; they no longer fire at all, by design).
        for args in SOXL_TREND_CONT:
            d = self.dec('SOXLUSDT', args)
            self.assertEqual(d['action'], 'HOLD', (args[0], d['reason']))
            self.assertNotIn('TREND CONTINUATION', d['reason'])

    def test_shorts_are_untouched(self):
        for sym, ema21, ema50, atr in (('SOXLUSDT', 120.0, 123.0, 1.7), ('TSLAUSDT', 370.0, 374.0, 2.4)):
            price = ema21 * (1 - 0.012)
            df = T.frame(price, ema21, ema50, 42.0, 9.0, 36.0, 42.0, atr, 300000, 150000, prev_close=ema21 * 1.001, hi=ema21 * 0.999)
            d = B.get_decision(sym, df, trend4h_override=BEAR)
            self.assertEqual(d['action'], 'SHORT', (sym, d['reason']))


if __name__ == '__main__':
    unittest.main(verbosity=2)
