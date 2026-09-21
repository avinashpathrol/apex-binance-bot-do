#!/usr/bin/env python3
"""Offline tests for the MU exit-level requests (custom stop / take-profit) in trading_bot_futures.py.
Runs the REAL bot functions against a temporary config folder; only the exchange and Telegram are stubbed.
Run: python3 test_mu_levels.py"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

for _name in ('pandas', 'ta', 'dotenv', 'numpy', 'requests'):          # heavy deps the bot imports but these tests never use
    try:
        __import__(_name)
    except ImportError:
        sys.modules[_name] = mock.MagicMock()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import logging
import trading_bot_futures as B
logging.disable(logging.CRITICAL)

OPENED = '2026-09-18T19:55:06.751650+00:00'


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._saved = {k: getattr(B, k) for k in ('WEB_ROOT', 'get_current_price', 'save_state', 'send_telegram', 'close_overnight_mu',
                                                    '_et_now', 'get_position_details')}
        B.WEB_ROOT = self.tmp
        self.price, self.closed, self.sent, self.saves = 1025.0, [], [], [0]
        B.get_current_price = lambda sym: self.price
        B.save_state = lambda: self.saves.__setitem__(0, self.saves[0] + 1)
        B.send_telegram = lambda m: self.sent.append(m)
        B.get_position_details = lambda sym: {}
        B._et_now = lambda: datetime(2026, 9, 22, 22, 0)         # Tuesday 10 PM ET: no entry/exit window is active

        def fake_close(reason):
            self.closed.append(reason)
            B.state['overnight_mu'] = {}
            return True
        B.close_overnight_mu = fake_close
        B.state['overnight_mu'] = {'position': 'LONG', 'entry_price': 1000.0, 'qty': 1.5, 'entry_fee': 0.75,
                                   'sl_price': round(1000.0 * (1 - B.OVERNIGHT_CFG['sl_pct']), 4), 'opened_at': OPENED}
        self.default_sl = B.state['overnight_mu']['sl_price']
        self.cfg({})

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(B, k, v)
        B.state['overnight_mu'] = {}

    def cfg(self, extra):
        base = {'futures_trade_amount_usdt': 40, 'futures_leverage': 40, 'futures_bot_paused': False}
        base.update(extra)
        with open(os.path.join(self.tmp, B.BOT_CONFIG_FILE), 'w') as f:
            json.dump(base, f)

    def req(self, **kw):
        r = {'for_opened_at': OPENED, 'requested_at': '2026-09-21T05:00:00Z'}
        r.update(kw)
        self.cfg({'futures_mu_levels_request': r})

    def on(self):
        return B.state['overnight_mu']

    def flag(self):
        return json.load(open(os.path.join(self.tmp, B.BOT_CONFIG_FILE))).get('futures_mu_levels_request')


class TestConfigReading(Base):
    def test_request_is_read_and_defaults_stay_none(self):
        self.assertIsNone(B.fetch_dashboard_config()['mu_levels_request'])
        self.req(stop=1010.0)
        self.assertEqual(B.fetch_dashboard_config()['mu_levels_request']['stop'], 1010.0)
        self.cfg({'futures_mu_levels_request': False})                # what clear_flag leaves behind
        self.assertIsNone(B.fetch_dashboard_config()['mu_levels_request'])
        self.cfg({'futures_mu_levels_request': 'garbage'})
        self.assertIsNone(B.fetch_dashboard_config()['mu_levels_request'])


class TestRequestRules(Base):
    def test_valid_stop_is_applied_and_flag_cleared(self):
        self.req(stop=1015.0)
        B.run_overnight_strategy()
        self.assertEqual(self.on()['sl_price'], 1015.0)
        self.assertTrue(self.on()['sl_custom'])
        self.assertTrue(self.on()['levels_status']['ok'])
        self.assertIs(self.flag(), False)                              # one-shot: cleared after applying
        self.assertEqual(self.closed, [])                              # price 1025 is above the new stop
        self.assertTrue(any('MU exit levels updated' in m for m in self.sent))

    def test_stop_at_or_above_price_rejected(self):
        for stop in (1025.0, 1024.6, 1100.0):                          # 1024.6 is inside the 0.05% minimum gap
            self.req(stop=stop)
            B.run_overnight_strategy()
            self.assertEqual(self.on()['sl_price'], self.default_sl, stop)
            self.assertFalse(self.on()['levels_status']['ok'])
            self.assertNotIn('sl_custom', self.on())
        self.assertEqual(self.closed, [])

    def test_stop_at_or_below_default_rejected(self):
        self.req(stop=self.default_sl)
        B.run_overnight_strategy()
        self.assertEqual(self.on()['sl_price'], self.default_sl)
        self.assertFalse(self.on()['levels_status']['ok'])
        self.req(stop=900.0)
        B.run_overnight_strategy()
        self.assertEqual(self.on()['sl_price'], self.default_sl)

    def test_custom_stop_can_be_relaxed_but_not_below_default(self):
        self.req(stop=1015.0); B.run_overnight_strategy()
        self.req(stop=990.0); B.run_overnight_strategy()               # looser than before, still above the default
        self.assertEqual(self.on()['sl_price'], 990.0)
        self.req(stop=960.0); B.run_overnight_strategy()               # below the 3.5% default -> refused
        self.assertEqual(self.on()['sl_price'], 990.0)

    def test_stale_request_for_another_position_ignored(self):
        self.cfg({'futures_mu_levels_request': {'for_opened_at': '2026-09-17T19:55:00+00:00', 'stop': 1015.0}})
        B.run_overnight_strategy()
        self.assertEqual(self.on()['sl_price'], self.default_sl)
        self.assertIn('different position', self.on()['levels_status']['msg'])
        self.assertIs(self.flag(), False)

    def test_reset_returns_to_default(self):
        self.req(stop=1015.0); B.run_overnight_strategy()
        self.req(reset_stop=True); B.run_overnight_strategy()
        self.assertEqual(self.on()['sl_price'], self.default_sl)
        self.assertNotIn('sl_custom', self.on())

    def test_target_set_clear_and_reject(self):
        self.req(target=1040.0); B.run_overnight_strategy()
        self.assertEqual(self.on()['target_price'], 1040.0)
        self.req(target=1020.0); B.run_overnight_strategy()            # at/below price -> refused, old target kept
        self.assertEqual(self.on()['target_price'], 1040.0)
        self.assertFalse(self.on()['levels_status']['ok'])
        self.req(clear_target=True); B.run_overnight_strategy()
        self.assertNotIn('target_price', self.on())

    def test_stop_and_target_together(self):
        self.req(stop=1015.0, target=1050.0); B.run_overnight_strategy()
        self.assertEqual((self.on()['sl_price'], self.on()['target_price']), (1015.0, 1050.0))
        self.assertTrue(self.on()['levels_status']['ok'])

    def test_no_position_just_clears_the_flag(self):
        B.state['overnight_mu'] = {}
        B.apply_mu_levels_request({'for_opened_at': OPENED, 'stop': 1015.0}, 1025.0)
        self.assertEqual(B.state['overnight_mu'], {})
        self.assertEqual(self.sent, [])

    def test_garbage_values_never_raise(self):
        self.req(stop='abc', target='xyz')
        B.run_overnight_strategy()                                     # safe_float -> 0 -> both refused
        self.assertEqual(self.on()['sl_price'], self.default_sl)
        self.assertNotIn('target_price', self.on())
        self.assertFalse(self.on()['levels_status']['ok'])


class TestEnforcement(Base):
    def test_custom_stop_closes_with_its_own_reason(self):
        self.req(stop=1015.0); B.run_overnight_strategy()
        self.price = 1014.9
        B.run_overnight_strategy()
        self.assertEqual(self.closed, ['Custom Stop'])

    def test_price_above_custom_stop_holds(self):
        self.req(stop=1015.0); B.run_overnight_strategy()
        for px in (1030.0, 1016.0, 1015.01):
            self.price = px
            B.run_overnight_strategy()
        self.assertEqual(self.closed, [])

    def test_target_hit_closes(self):
        self.req(target=1040.0); B.run_overnight_strategy()
        self.price = 1041.0
        B.run_overnight_strategy()
        self.assertEqual(self.closed, ['Target Hit'])

    def test_default_stop_unchanged_and_labelled_stop_loss(self):
        self.price = self.default_sl - 0.5
        B.run_overnight_strategy()
        self.assertEqual(self.closed, ['Stop Loss'])

    def test_new_stop_applies_in_the_same_cycle(self):
        self.req(stop=1015.0)
        self.price = 1025.0
        B.run_overnight_strategy()
        self.assertEqual(self.closed, [])                              # applied, price still above it
        # a request whose price is already below the market's real price is rejected, not fired
        self.req(stop=1030.0)
        B.run_overnight_strategy()
        self.assertEqual(self.closed, [])

    def test_manual_close_still_works(self):
        self.cfg({'futures_mu_close_requested': True, 'futures_mu_close_requested_at': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.000Z')})
        B.run_overnight_strategy()
        self.assertEqual(self.closed, ['Dashboard Close'])

    def test_bad_request_does_not_break_the_stop_check(self):
        self.cfg({'futures_mu_levels_request': {'for_opened_at': OPENED, 'stop': {'not': 'a number'}}})
        self.price = self.default_sl - 1
        B.run_overnight_strategy()
        self.assertEqual(self.closed, ['Stop Loss'])


class TestDashboardPayload(Base):
    def test_payload_publishes_levels_and_fee_inputs(self):
        self.req(stop=1015.0, target=1050.0); B.run_overnight_strategy()
        d = json.load(open(os.path.join(self.tmp, 'data_overnight_mu.json')))
        self.assertEqual((d['sl_price'], d['sl_custom'], d['target_price']), (1015.0, True, 1050.0))
        self.assertEqual(d['sl_default'], self.default_sl)
        self.assertEqual((d['entry_fee'], d['fee_rate']), (0.75, B.FEE_RATE))
        self.assertTrue(d['levels_status']['ok'])

    def test_net_at_matches_the_bots_own_close_math(self):
        on = self.on()
        px = 1030.0
        expected = (px - 1000.0) * 1.5 - 0.75 - 1.5 * px * B.FEE_RATE          # same formula close_overnight_mu uses
        self.assertAlmostEqual(B._mu_net_at(on, px), expected, places=9)


if __name__ == '__main__':
    unittest.main(verbosity=2)
