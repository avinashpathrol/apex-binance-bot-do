#!/usr/bin/env python3
"""Offline test of the same-direction re-entry cooldown (SAME_DIR_COOLDOWN) in trading_bot_futures.py.
The real open_long / open_short run; the exchange is stubbed and the network is disabled. "Reached the order path"
is detected by set_futures_leverage being called -- the first exchange action after the cooldown gate.
Run: python3 test_same_dir_cooldown.py   (needs Python >= 3.9, like the bot itself)"""
import logging
import os
import sys
import time
import unittest
from unittest import mock

for _name in ('pandas', 'ta', 'dotenv', 'numpy', 'requests'):
    try:
        __import__(_name)
    except ImportError:
        sys.modules[_name] = mock.MagicMock()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import trading_bot_futures as B
logging.disable(logging.CRITICAL)

SYM = 'NBISUSDT'


class NoNetwork:
    def __getattr__(self, name):
        raise RuntimeError('network disabled in this test')


class Cooldown(unittest.TestCase):
    def setUp(self):
        self.reached, self.logs = [], []
        self._saved = {k: getattr(B, k) for k in ('set_futures_margin_type', 'set_futures_leverage', 'send_telegram', 'alert_error', 'requests')}

        def stop_here(symbol, *a, **k):
            self.reached.append(symbol)
            raise RuntimeError('stop: reached the order path')
        B.set_futures_margin_type = lambda *a, **k: None
        B.set_futures_leverage = stop_here
        B.send_telegram = lambda m: None
        B.alert_error = lambda m: None
        B.requests = NoNetwork()
        B.logger.info = lambda msg, *a, **k: self.logs.append(str(msg))
        ss = B.sym_state(SYM)
        for k in ('last_bot_closed_side', 'last_bot_closed_ts', 'consecutive_losses', 'loss_streak_direction'):
            ss.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(B, k, v)
        del B.logger.info

    def closed(self, side, seconds_ago):
        ss = B.sym_state(SYM)
        ss['last_bot_closed_side'], ss['last_bot_closed_ts'] = side, time.time() - seconds_ago

    def attempt(self, side):
        fn = B.open_long if side == 'LONG' else B.open_short
        try:
            return fn(SYM, 100.0, 5, 'test')
        except RuntimeError:
            return 'reached-order-path'

    def test_constant_is_fifteen_minutes(self):
        self.assertEqual(B.SAME_DIR_COOLDOWN, 900)

    def test_same_side_blocked_for_the_full_15_minutes(self):
        for side in ('LONG', 'SHORT'):
            for ago in (60, 599, 601, 720, 899):              # 601-899 s would have been allowed under the old 10-minute rule
                self.reached.clear(); self.logs.clear()
                self.closed(side, ago)
                self.assertIs(self.attempt(side), False, (side, ago))
                self.assertEqual(self.reached, [], (side, ago))
                self.assertTrue(any('%s cooldown' % side in m for m in self.logs), (side, ago, self.logs))

    def test_same_side_allowed_after_15_minutes(self):
        for side in ('LONG', 'SHORT'):
            for ago in (901, 960, 3600):
                self.reached.clear(); self.logs.clear()
                self.closed(side, ago)
                self.attempt(side)
                self.assertEqual(self.reached, [SYM], (side, ago))
                self.assertFalse(any('cooldown' in m for m in self.logs), (side, ago))

    def test_opposite_side_is_never_delayed(self):
        for entering, closed in (('LONG', 'SHORT'), ('SHORT', 'LONG')):
            self.reached.clear()
            self.closed(closed, 30)
            self.attempt(entering)
            self.assertEqual(self.reached, [SYM], (entering, closed))

    def test_no_previous_close_is_not_delayed(self):
        for side in ('LONG', 'SHORT'):
            self.reached.clear()
            self.attempt(side)
            self.assertEqual(self.reached, [SYM], side)

    def test_log_message_reports_remaining_minutes(self):
        self.closed('LONG', 720)                                    # 12 min ago -> ~3 min left of 15 (the log rounds the remainder down)
        self.attempt('LONG')
        self.assertTrue(any('closed 12m ago, waiting 2m more' in m or 'closed 12m ago, waiting 3m more' in m for m in self.logs), self.logs)


if __name__ == '__main__':
    unittest.main(verbosity=2)
