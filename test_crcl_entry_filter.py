#!/usr/bin/env python3
"""Behavioral test of CRCL's 2026-09-21 entry tightening (trend_continuation off, rsi_long_max 63).
Feeds the REAL setups from CRCL's trade log (the losers and the winners) into the real get_decision().
Needs real pandas, so it runs on the droplet:  python3 test_crcl_entry_filter.py  (skips itself where pandas is missing)."""
import logging
import os
import sys
import unittest
from unittest import mock

for _name in ('ta', 'dotenv', 'requests'):
    try:
        __import__(_name)
    except ImportError:
        sys.modules[_name] = mock.MagicMock()
try:
    import pandas as pd
    HAVE_PANDAS = not isinstance(pd, mock.MagicMock)
except ImportError:
    HAVE_PANDAS = False
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import trading_bot_futures as B
logging.disable(logging.CRITICAL)

BULL, BEAR = '4H BULLISH', '4H BEARISH'


def frame(price, ema21, ema50, adx, adx_pos, adx_neg, rsi, atr, vol, vol_ma, prev_close=None, lo=None, hi=None):
    """Two bars (previous, current) with the columns get_decision reads."""
    cur = dict(adx=adx, adx_pos=adx_pos, adx_neg=adx_neg, ema21=ema21, ema50=ema50, rsi=rsi, atr=atr, close=price,
               high=hi if hi is not None else price * 1.001, low=lo if lo is not None else price * 0.997,
               volume=vol, vol_ma=vol_ma, dc_upper=float('nan'), dc_lower=float('nan'))
    prev = dict(cur, close=prev_close if prev_close is not None else price, ema21=ema21, ema50=ema50)
    return pd.DataFrame([prev, cur])


def long_setup(dist_pct, rsi, adx=41.6, atr=1.3958, ema21=93.8911, ema50=92.2979):
    price = ema21 * (1 + dist_pct)
    return frame(price, ema21, ema50, adx, 40.8, 7.8, rsi, atr, 419415, 158601, lo=price * 0.997)


@unittest.skipUnless(HAVE_PANDAS, 'needs real pandas (run on the droplet)')
class CrclEntries(unittest.TestCase):
    def dec(self, sym, df, trend4h):
        return B.get_decision(sym, df, trend4h_override=trend4h)

    def test_config_values(self):
        c = B.SYMBOLS_CONFIG['CRCLUSDT']
        self.assertIs(c['trend_continuation_enabled'], False)
        self.assertEqual(c['rsi_long_max'], 63)
        # nothing else about CRCL changed
        self.assertEqual((c['trade_amount'], c['leverage'], c['max_loss_pct'], c['rsi_short_max'], c['pullback_zone_pct'], c['trail_dist_atr']),
                         (70.0, 40, 0.35, 75, 0.030, 0.20))

    def test_the_three_losing_pullbacks_are_now_blocked(self):
        for name, dist, rsi in (('09-21 10:40', 0.0202, 65.0), ('09-21 11:01', 0.0198, 64.8), ('09-14 10:38', 0.0163, 64.1)):
            d = self.dec('CRCLUSDT', long_setup(dist, rsi), BULL)
            self.assertEqual(d['action'], 'HOLD', (name, d['reason']))
            self.assertIn('RSI', d['reason'])

    def test_the_two_winning_pullbacks_still_enter(self):
        for name, dist, rsi in (('09-21 10:15', 0.0114, 58.7), ('09-14 11:18', 0.0139, 62.1)):
            d = self.dec('CRCLUSDT', long_setup(dist, rsi), BULL)
            self.assertEqual(d['action'], 'LONG', (name, d['reason']))
            self.assertIn('Pullback to EMA21', d['reason'])

    def test_rsi_boundary(self):
        self.assertEqual(self.dec('CRCLUSDT', long_setup(0.012, 63.0), BULL)['action'], 'LONG')
        self.assertEqual(self.dec('CRCLUSDT', long_setup(0.012, 63.2), BULL)['action'], 'HOLD')

    def test_trend_continuation_no_longer_fires_on_crcl(self):
        for name, dist, rsi in (('09-21 09:55', 0.0326, 73.8), ('09-14 14:14', 0.0444, 77.6), ('09-18 11:39', 0.0459, 71.8)):
            d = self.dec('CRCLUSDT', long_setup(dist, rsi, adx=35.0), BULL)
            self.assertEqual(d['action'], 'HOLD', (name, d['reason']))
            self.assertNotIn('TREND CONTINUATION', d['reason'])

    def test_trend_continuation_now_off_bot_wide(self):
        # superseded 2026-09-22: trend_continuation was disabled on every live symbol (not just CRCL) after
        # the same extended-move pattern showed up portfolio-wide. APP used to be the "still enabled" control.
        cfg = B.SYMBOLS_CONFIG['APPUSDT']
        self.assertFalse(cfg.get('trend_continuation_enabled'))
        atr = max(cfg['min_atr'] * 2, 2.0)
        ema21, ema50 = 300.0, 290.0
        price = ema21 * 1.045
        df = frame(price, ema21, ema50, 38.0, 35.0, 8.0, 72.0, atr, 500000, 200000, lo=price * 0.997)
        d = self.dec('APPUSDT', df, BULL)
        self.assertEqual(d['action'], 'HOLD')
        self.assertNotIn('TREND CONTINUATION', d['reason'])

    def test_shorts_on_crcl_are_untouched(self):
        # the winning CRCL pullback SHORT (RSI 36.7, ~2% below EMA21, ADX 47) still enters
        ema21, ema50 = 96.0, 98.0
        price = ema21 * (1 - 0.0198)
        df = frame(price, ema21, ema50, 47.0, 8.0, 40.0, 36.7, 1.4, 300000, 150000, prev_close=ema21 * 1.001, hi=ema21 * 0.999)
        d = self.dec('CRCLUSDT', df, BEAR)
        self.assertEqual(d['action'], 'SHORT', d['reason'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
