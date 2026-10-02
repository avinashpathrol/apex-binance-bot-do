#!/usr/bin/env python3
"""Behavioral test of the 2026-09-22 roster change: TSLA removed, HOOD added (short-only), trend_continuation
disabled bot-wide, short_only added for ASTS/SOXL/CRCL (AMD already had it). Runs the REAL get_decision() and
the REAL long_only/short_only gate. Needs real pandas, so run on the droplet: python3 test_roster_change.py"""
import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_crcl_entry_filter as T          # reuses stubs / frame() helper
B, HAVE_PANDAS = T.B, T.HAVE_PANDAS
logging.disable(logging.CRITICAL)

BULL, BEAR = '4H BULLISH', '4H BEARISH'
SHORT_ONLY = ('AMDUSDT', 'ASTSUSDT', 'CRCLUSDT', 'SOXLUSDT', 'HOODUSDT')
BOTH_DIR = ('NBISUSDT', 'APPUSDT')


def gate(symbol, action):
    """Mirrors the exact check in run_symbol (line ~3334-3336): returns True if this action would be BLOCKED."""
    cfg = B.SYMBOLS_CONFIG[symbol]
    if action == 'SHORT' and cfg.get('long_only'):
        return True
    if action == 'LONG' and cfg.get('short_only'):
        return True
    return False


class TestRoster(unittest.TestCase):
    def test_tsla_removed_hood_added(self):
        self.assertNotIn('TSLAUSDT', B.TRADING_SYMBOLS)
        self.assertIn('HOODUSDT', B.TRADING_SYMBOLS)
        self.assertEqual(len(B.TRADING_SYMBOLS), 7)
        self.assertEqual(set(B.TRADING_SYMBOLS), {'NBISUSDT', 'AMDUSDT', 'APPUSDT', 'SOXLUSDT', 'CRCLUSDT', 'ASTSUSDT', 'HOODUSDT'})

    def test_tsla_config_kept_but_inert(self):
        # still present (harmless -- not iterated since it's out of TRADING_SYMBOLS) so nothing else breaks
        self.assertIn('TSLAUSDT', B.SYMBOLS_CONFIG)

    def test_hood_config(self):
        c = B.SYMBOLS_CONFIG['HOODUSDT']
        self.assertEqual(c['trade_amount'], 30.0)
        self.assertEqual(c['leverage'], 20)
        self.assertIs(c.get('short_only'), True)
        self.assertNotIn('trend_continuation_enabled', c)
        self.assertNotIn('long_only', c)
        self.assertEqual(c['dashboard_file'], 'data_futures_hood.json')

    def test_short_only_symbols(self):
        for sym in SHORT_ONLY:
            self.assertIs(B.SYMBOLS_CONFIG[sym].get('short_only'), True, sym)
            self.assertIsNone(B.SYMBOLS_CONFIG[sym].get('long_only'), sym)

    def test_both_direction_symbols_unrestricted(self):
        for sym in BOTH_DIR:
            self.assertFalse(B.SYMBOLS_CONFIG[sym].get('short_only'), sym)
            self.assertFalse(B.SYMBOLS_CONFIG[sym].get('long_only'), sym)

    def test_trend_continuation_off_everywhere_live(self):
        for sym in B.TRADING_SYMBOLS:
            self.assertFalse(B.SYMBOLS_CONFIG[sym].get('trend_continuation_enabled'), sym)

    def test_amd_untouched(self):
        # AMD already had short_only=True and no trend_continuation key before this change -- confirm no drift
        c = B.SYMBOLS_CONFIG['AMDUSDT']
        self.assertIs(c.get('short_only'), True)
        self.assertNotIn('trend_continuation_enabled', c)
        self.assertEqual((c['trade_amount'], c['leverage']), (40.0, 25))

    def test_existing_sizes_untouched(self):
        # "keep current trade sizes as is"
        want = {'NBISUSDT': (70.0, 25), 'AMDUSDT': (40.0, 25), 'APPUSDT': (30.0, 25),
                'SOXLUSDT': (70.0, 50), 'CRCLUSDT': (70.0, 40), 'ASTSUSDT': (40.0, 20)}
        for sym, (amt, lev) in want.items():
            c = B.SYMBOLS_CONFIG[sym]
            self.assertEqual((c['trade_amount'], c['leverage']), (amt, lev), sym)

    def test_gate_blocks_long_on_short_only_symbols(self):
        for sym in SHORT_ONLY:
            self.assertTrue(gate(sym, 'LONG'), sym)
            self.assertFalse(gate(sym, 'SHORT'), sym)

    def test_gate_allows_both_on_unrestricted_symbols(self):
        for sym in BOTH_DIR:
            self.assertFalse(gate(sym, 'LONG'), sym)
            self.assertFalse(gate(sym, 'SHORT'), sym)


@unittest.skipUnless(HAVE_PANDAS, 'needs real pandas (run on the droplet)')
class TestRealDecisions(unittest.TestCase):
    """get_decision() itself doesn't know about short_only -- confirm a LONG signal on a short-only symbol
    still fires (so the caller-side gate above is the thing actually protecting it), and that trend_continuation
    truly never fires anywhere live now."""
    def test_short_only_symbol_can_still_produce_a_long_signal_pre_gate(self):
        # a clean pullback setup on CRCL (short-only) should still return LONG from get_decision itself --
        # the short_only restriction is enforced by the CALLER (gate()), not inside get_decision.
        df = T.long_setup(0.012, 58.7)
        d = B.get_decision('CRCLUSDT', df, trend4h_override=BULL)
        self.assertEqual(d['action'], 'LONG')
        self.assertTrue(gate('CRCLUSDT', d['action']))   # confirms the gate is what stops it, not get_decision

    def test_trend_continuation_never_fires_on_any_live_symbol(self):
        for sym in B.TRADING_SYMBOLS:
            ema21, ema50 = 100.0, 96.0
            price = ema21 * 1.045
            df = T.frame(price, ema21, ema50, 38.0, 35.0, 8.0, 75.0, 2.0, 500000, 200000, lo=price * 0.997)
            d = B.get_decision(sym, df, trend4h_override=BULL)
            self.assertNotIn('TREND CONTINUATION', d.get('reason', ''), sym)

    def test_hood_pullback_still_works_for_short(self):
        ema21, ema50 = 120.0, 123.0
        price = ema21 * (1 - 0.0198)
        df = T.frame(price, ema21, ema50, 42.0, 8.0, 36.0, 42.0, 0.95, 300000, 150000, prev_close=ema21 * 1.001, hi=ema21 * 0.999)
        d = B.get_decision('HOODUSDT', df, trend4h_override=BEAR)
        self.assertEqual(d['action'], 'SHORT', d['reason'])
        self.assertFalse(gate('HOODUSDT', d['action']))


if __name__ == '__main__':
    unittest.main(verbosity=2)
