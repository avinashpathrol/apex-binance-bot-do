#!/usr/bin/env python3
"""Tests for the SPY 0DTE paper-trading engine (moomoo real quotes + real delta,
GEX-direction entry, interval profit-taking, TWO parallel entry-time variants to
compare timing). Mocks every network boundary (spym_get_combo_quote,
spym_get_real_deltas, spym_get_spot, spym_today_expiry, spym_get_chain,
fetch_cboe_gex) — never hits the real moomoo API, never touches the signed-
request layer. Run: python3 test_spym.py"""
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import spy_options_bot as S

ET = ZoneInfo('America/New_York')


def et(y, m, d, h, mi):
    return datetime(y, m, d, h, mi, tzinfo=ET)


def make_chain():
    chain = []
    for s in range(755, 775):
        chain.append({'strike_price': float(s), 'option_type': 'PUT', 'code': f'US.SPY TEST P{s}'})
        chain.append({'strike_price': float(s), 'option_type': 'CALL', 'code': f'US.SPY TEST C{s}'})
    return chain


class TestResponseEnvelope(unittest.TestCase):
    """Direct coverage for the bug caught live 2026-10-02: moomoo's Quote API
    and Trading API use two different success/error envelopes, and checking
    only one shape made every successful Trading-API call look like an error."""

    def test_passes_through_quote_style_success(self):
        d = {'ret_code': 0, 'ret_msg': 'success', 'data': {'x': 1}}
        self.assertEqual(S._spym_check(d), d)

    def test_raises_on_quote_style_error(self):
        with self.assertRaises(RuntimeError):
            S._spym_check({'ret_code': 1, 'ret_msg': 'boom'})

    def test_passes_through_trading_style_success(self):
        d = {'s': 'ok', 'd': {'accounts': [{'account_id': 123}]}}
        self.assertEqual(S._spym_check(d), d)

    def test_raises_on_trading_style_error(self):
        with self.assertRaises(RuntimeError):
            S._spym_check({'s': 'error', 'errcode': 1, 'errmsg': 'invalid currency'})

    @patch.object(S, 'spym_get')
    def test_get_account_id_parses_real_trading_envelope(self, mock_get):
        S._spym_account_id_cache = None   # reset module-level cache from any earlier test
        mock_get.return_value = {'s': 'ok', 'd': {'accounts': [{'account_id': 286823027570788080}]}}
        self.assertEqual(S.spym_get_account_id(), '286823027570788080')


class TestVariantsConfig(unittest.TestCase):
    def test_two_variants_defined_with_distinct_times(self):
        self.assertEqual(len(S.SPYM_VARIANTS), 2)
        times = {(v['entry_hour'], v['entry_minute']) for v in S.SPYM_VARIANTS.values()}
        self.assertEqual(len(times), 2)   # distinct entry times — that's the whole point of the comparison

    def test_load_state_initializes_all_variants(self):
        state = S.load_spym_state()
        for vkey in S.SPYM_VARIANTS:
            self.assertIn(vkey, state['variants'])
            self.assertIsNone(state['variants'][vkey]['open_position'])
            self.assertEqual(state['variants'][vkey]['trades'], [])

    def test_load_state_initializes_permission_as_pending(self):
        state = S.load_spym_state()
        self.assertEqual(state['options_permission']['status'], 'pending')
        self.assertIsNone(state['options_permission']['activated_date'])


EXPIRATION_RESP = {'data': {'expiration_list': [
    {'strike_time': '2026-10-02', 'option_expiry_date_distance': 1},   # too close -- must be skipped
    {'strike_time': '2026-10-05', 'option_expiry_date_distance': 4},   # first one >= 2 days out -- picked
]}}


class TestOptionsPermissionCheck(unittest.TestCase):
    def _mock_chain_and_quotes(self, mock_chain, mock_spot, mock_post):
        mock_spot.return_value = 764.0
        mock_chain.return_value = [{'strike_price': 761.0, 'option_type': 'PUT', 'code': 'US.SPY TEST P761'}]
        mock_post.return_value = {'data': {'quote_list': [{'code': 'US.SPY TEST P761', 'last_price': 1.1}]}}

    @patch.object(S, 'save_spym_state')
    @patch.object(S, 'notify')
    @patch.object(S, 'spym_get')
    @patch.object(S, 'spym_post')
    @patch.object(S, 'spym_get_spot')
    @patch.object(S, 'spym_get_chain')
    @patch.object(S, 'spym_get_account_id', return_value='ACC1')
    @patch.object(S, 'et_now')
    def test_still_pending_sends_day_1_reminder(self, mock_now, mock_acc, mock_chain,
                                                 mock_spot, mock_post, mock_get, mock_notify, mock_save):
        mock_now.return_value = et(2026, 10, 2, 11, 0)
        self._mock_chain_and_quotes(mock_chain, mock_spot, mock_post)
        mock_get.side_effect = [EXPIRATION_RESP, {'d': {'max_cash_buy': '0', 'max_cash_and_margin_buy': '0'}}]
        state = S.load_spym_state()
        S.spym_check_options_permission(state)
        self.assertEqual(state['options_permission']['status'], 'pending')
        self.assertEqual(state['options_permission']['first_checked_date'], '2026-10-02')
        self.assertEqual(state['options_permission']['last_checked_date'], '2026-10-02')
        self.assertIn('PENDING', mock_notify.call_args[0][0])
        self.assertIn('day 1', mock_notify.call_args[0][0])
        # confirms the fix: the probed expiry must be the safely-future one, never the near one
        probed_path = mock_get.call_args_list[1][0][0]
        self.assertNotIn('2026-10-02', probed_path)

    @patch.object(S, 'save_spym_state')
    @patch.object(S, 'notify')
    @patch.object(S, 'spym_get')
    @patch.object(S, 'spym_post')
    @patch.object(S, 'spym_get_spot')
    @patch.object(S, 'spym_get_chain')
    @patch.object(S, 'spym_get_account_id', return_value='ACC1')
    @patch.object(S, 'et_now')
    def test_day_count_increments_across_days(self, mock_now, mock_acc, mock_chain,
                                               mock_spot, mock_post, mock_get, mock_notify, mock_save):
        self._mock_chain_and_quotes(mock_chain, mock_spot, mock_post)
        mock_get.side_effect = [
            EXPIRATION_RESP, {'d': {'max_cash_buy': '0', 'max_cash_and_margin_buy': '0'}},
            EXPIRATION_RESP, {'d': {'max_cash_buy': '0', 'max_cash_and_margin_buy': '0'}},
        ]
        state = S.load_spym_state()
        mock_now.return_value = et(2026, 10, 2, 11, 0)
        S.spym_check_options_permission(state)
        mock_now.return_value = et(2026, 10, 4, 11, 0)   # two days later
        S.spym_check_options_permission(state)
        self.assertIn('day 3', mock_notify.call_args[0][0])   # Oct 2 (day1) -> Oct 4 (day3) inclusive

    @patch.object(S, 'save_spym_state')
    @patch.object(S, 'notify')
    @patch.object(S, 'spym_get')
    @patch.object(S, 'spym_post')
    @patch.object(S, 'spym_get_spot')
    @patch.object(S, 'spym_get_chain')
    @patch.object(S, 'spym_get_account_id', return_value='ACC1')
    @patch.object(S, 'et_now')
    def test_becomes_active_sends_celebration_and_locks_in(self, mock_now, mock_acc, mock_chain,
                                                             mock_spot, mock_post, mock_get, mock_notify, mock_save):
        mock_now.return_value = et(2026, 10, 2, 11, 0)
        self._mock_chain_and_quotes(mock_chain, mock_spot, mock_post)
        mock_get.side_effect = [EXPIRATION_RESP, {'d': {'max_cash_buy': '0', 'max_cash_and_margin_buy': '0.225'}}]
        state = S.load_spym_state()
        S.spym_check_options_permission(state)
        self.assertEqual(state['options_permission']['status'], 'active')
        self.assertEqual(state['options_permission']['activated_date'], '2026-10-02')
        self.assertIn('ACTIVE', mock_notify.call_args[0][0])

        # once active, must never check or notify again, regardless of day
        mock_notify.reset_mock()
        mock_get.reset_mock()
        mock_now.return_value = et(2026, 10, 3, 11, 0)
        S.spym_check_options_permission(state)
        mock_notify.assert_not_called()
        mock_get.assert_not_called()

    @patch.object(S, 'spym_get')
    @patch.object(S, 'spym_get_account_id')
    @patch.object(S, 'et_now')
    def test_does_not_recheck_same_day(self, mock_now, mock_acc, mock_get):
        mock_now.return_value = et(2026, 10, 2, 11, 0)
        state = S.load_spym_state()
        state['options_permission']['last_checked_date'] = '2026-10-02'
        S.spym_check_options_permission(state)
        mock_acc.assert_not_called()
        mock_get.assert_not_called()


class TestBuildSpread(unittest.TestCase):
    def setUp(self):
        self.chain = make_chain()

    @patch.object(S, 'spym_get_combo_quote')
    @patch.object(S, 'spym_get_real_deltas')
    def test_picks_strike_closest_to_target_delta(self, mock_deltas, mock_combo):
        mock_deltas.return_value = {
            'US.SPY TEST P763': -0.30, 'US.SPY TEST P762': -0.22, 'US.SPY TEST P761': -0.18,
            'US.SPY TEST P760': -0.12, 'US.SPY TEST P759': -0.08,
        }
        mock_combo.return_value = {'price': -0.2, 'bid': -0.22, 'ask': -0.18}
        spread = S.spym_build_spread('BULL_PUT', self.chain, spot=764.0)
        self.assertIsNotNone(spread)
        self.assertEqual(spread['short_strike'], 761.0)
        self.assertEqual(spread['long_strike'], 760.0)
        self.assertAlmostEqual(spread['credit'], 0.18, places=4)
        self.assertEqual(spread['delta_source'], 'real')
        self.assertEqual(spread['contracts'], 1)

    @patch.object(S, 'spym_get_combo_quote')
    @patch.object(S, 'spym_get_real_deltas')
    def test_falls_back_to_pct_otm_when_no_real_delta(self, mock_deltas, mock_combo):
        mock_deltas.return_value = {}
        mock_combo.return_value = {'price': -0.2, 'bid': -0.22, 'ask': -0.18}
        spread = S.spym_build_spread('BULL_PUT', self.chain, spot=764.0)
        self.assertIsNotNone(spread)
        self.assertEqual(spread['delta_source'], 'fallback_pct_otm')
        self.assertIsNone(spread['short_delta'])

    @patch.object(S, 'spym_get_combo_quote')
    @patch.object(S, 'spym_get_real_deltas')
    def test_rejects_spread_below_min_credit_ratio(self, mock_deltas, mock_combo):
        mock_deltas.return_value = {}
        mock_combo.return_value = {'price': -0.05, 'bid': -0.06, 'ask': -0.05}
        spread = S.spym_build_spread('BULL_PUT', self.chain, spot=764.0)
        self.assertIsNone(spread)

    @patch.object(S, 'spym_get_combo_quote')
    @patch.object(S, 'spym_get_real_deltas')
    def test_rejects_non_positive_credit(self, mock_deltas, mock_combo):
        mock_deltas.return_value = {}
        mock_combo.return_value = {'price': 0.05, 'bid': 0.04, 'ask': 0.05}
        spread = S.spym_build_spread('BULL_PUT', self.chain, spot=764.0)
        self.assertIsNone(spread)

    def test_returns_none_when_no_candidates(self):
        spread = S.spym_build_spread('BULL_PUT', [], spot=764.0)
        self.assertIsNone(spread)

    @patch.object(S, 'spym_get_combo_quote')
    @patch.object(S, 'spym_get_real_deltas')
    def test_bear_call_picks_strikes_above_spot(self, mock_deltas, mock_combo):
        mock_deltas.return_value = {'US.SPY TEST C767': 0.18}
        mock_combo.return_value = {'price': -0.2, 'bid': -0.22, 'ask': -0.18}
        spread = S.spym_build_spread('BEAR_CALL', self.chain, spot=764.0)
        self.assertIsNotNone(spread)
        self.assertEqual(spread['short_strike'], 767.0)
        self.assertEqual(spread['long_strike'], 768.0)


class TestOpenPosition(unittest.TestCase):
    @patch.object(S, 'save_spym_state')
    @patch.object(S, 'notify')
    @patch.object(S, 'spym_build_spread')
    @patch.object(S, 'spym_get_chain')
    @patch.object(S, 'fetch_cboe_gex')
    @patch.object(S, 'spym_get_spot')
    @patch.object(S, 'spym_today_expiry')
    @patch.object(S, 'et_now')
    def test_opens_and_marks_entry_date_for_that_variant_only(self, mock_now, mock_expiry, mock_spot, mock_gex,
                                                               mock_chain, mock_build, mock_notify, mock_save):
        mock_now.return_value = et(2026, 10, 2, 9, 45)
        mock_expiry.return_value = '2026-10-02'
        mock_spot.return_value = 764.0
        mock_gex.return_value = {'direction': 'BULL_PUT', 'total_gex_b': 1.0,
                                  'gex_regime': 'positive', 'pin_strike': 760}
        mock_chain.return_value = [{'strike_price': 761.0, 'option_type': 'PUT', 'code': 'X'}]
        mock_build.return_value = {
            'direction': 'BULL_PUT', 'short_code': 'S', 'long_code': 'L',
            'short_strike': 761.0, 'long_strike': 760.0, 'breakeven': 760.8,
            'credit': 0.2, 'width': 1.0, 'contracts': 1, 'max_loss': 80.0, 'max_profit': 20.0,
            'delta_source': 'real', 'short_delta': 0.18,
        }
        state = S.load_spym_state()
        S.spym_open_new_position(state, 'open')
        self.assertIsNotNone(state['variants']['open']['open_position'])
        self.assertEqual(state['variants']['open']['last_entry_date'], '2026-10-02')
        self.assertEqual(state['variants']['open']['open_position']['variant'], 'open')
        # the OTHER variant must be completely untouched
        self.assertIsNone(state['variants']['ten_mst']['open_position'])
        self.assertIsNone(state['variants']['ten_mst']['last_entry_date'])

    @patch.object(S, 'spym_build_spread')
    @patch.object(S, 'spym_get_chain')
    @patch.object(S, 'fetch_cboe_gex')
    @patch.object(S, 'spym_get_spot')
    @patch.object(S, 'spym_today_expiry')
    @patch.object(S, 'et_now')
    def test_does_not_retry_same_day_for_that_variant(self, mock_now, mock_expiry, mock_spot, mock_gex, mock_chain, mock_build):
        mock_now.return_value = et(2026, 10, 2, 11, 0)
        state = S.load_spym_state()
        state['variants']['open']['last_entry_date'] = '2026-10-02'
        S.spym_open_new_position(state, 'open')
        mock_expiry.assert_not_called()
        self.assertIsNone(state['variants']['open']['open_position'])

    @patch.object(S, 'save_spym_state')
    @patch.object(S, 'spym_get_chain')
    @patch.object(S, 'fetch_cboe_gex')
    @patch.object(S, 'spym_get_spot')
    @patch.object(S, 'spym_today_expiry')
    @patch.object(S, 'et_now')
    def test_no_gex_direction_skips_without_marking_entry(self, mock_now, mock_expiry, mock_spot,
                                                            mock_gex, mock_chain, mock_save):
        mock_now.return_value = et(2026, 10, 2, 9, 45)
        mock_expiry.return_value = '2026-10-02'
        mock_spot.return_value = 764.0
        mock_gex.return_value = None
        state = S.load_spym_state()
        S.spym_open_new_position(state, 'open')
        self.assertIsNone(state['variants']['open']['open_position'])
        self.assertIsNone(state['variants']['open']['last_entry_date'])


def make_pos(credit=0.30):
    return {
        'direction': 'BULL_PUT', 'short_code': 'S', 'long_code': 'L',
        'short_strike': 761.0, 'long_strike': 760.0, 'breakeven': 760.7,
        'credit': credit, 'width': 1.0, 'contracts': 1,
        'max_loss': round((1.0 - credit) * 100, 2), 'max_profit': round(credit * 100, 2),
        'delta_source': 'real', 'short_delta': 0.18, 'opened_date': '2026-10-02', 'variant': 'open',
    }


class TestMonitorPosition(unittest.TestCase):
    def _state_with_open(self, credit=0.30):
        state = S.load_spym_state()
        state['variants']['open']['open_position'] = make_pos(credit)
        return state

    @patch.object(S, 'save_spym_state')
    @patch.object(S, 'spym_get_combo_quote')
    @patch.object(S, 'et_now')
    def test_trail_does_not_trigger_on_first_touch_of_activation(self, mock_now, mock_combo, mock_save):
        mock_now.return_value = et(2026, 10, 2, 11, 0)
        state = self._state_with_open(credit=0.30)
        mock_combo.return_value = {'price': -0.10, 'bid': -0.10, 'ask': -0.08}   # pct=(0.30-0.10)/0.30=0.667 (70%)
        S.spym_monitor_position(state, 'open')
        pos = state['variants']['open']['open_position']
        self.assertIsNotNone(pos)   # first touch of a new peak -> gap is 0, no trigger yet
        self.assertAlmostEqual(pos['peak_pct_captured'], 66.7, places=1)
        self.assertEqual(state['variants']['open']['trades'], [])

    @patch.object(S, 'save_spym_state')
    @patch.object(S, 'notify')
    @patch.object(S, 'spym_get_combo_quote')
    @patch.object(S, 'et_now')
    def test_trail_triggers_after_real_pullback_from_peak(self, mock_now, mock_combo, mock_notify, mock_save):
        mock_now.return_value = et(2026, 10, 2, 11, 0)
        state = self._state_with_open(credit=0.30)
        mock_combo.return_value = {'price': -0.09, 'bid': -0.09, 'ask': -0.07}   # pct=(0.30-0.09)/0.30=0.70 (70%)
        S.spym_monitor_position(state, 'open')   # cycle 1: establishes peak=70%, holds
        self.assertIsNotNone(state['variants']['open']['open_position'])

        mock_combo.return_value = {'price': -0.15, 'bid': -0.15, 'ask': -0.13}   # pct=(0.30-0.15)/0.30=0.50 (50%)
        S.spym_monitor_position(state, 'open')   # cycle 2: 20pp back off the 70% peak -> trail fires (>=15pp)
        self.assertIsNone(state['variants']['open']['open_position'])
        trades = state['variants']['open']['trades']
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]['close_reason'], 'trail_stop')
        self.assertAlmostEqual(trades[0]['pnl'], (0.30 - 0.15) * 100, places=2)
        self.assertTrue(trades[0]['win'])

    @patch.object(S, 'save_spym_state')
    @patch.object(S, 'spym_get_combo_quote')
    @patch.object(S, 'et_now')
    def test_trail_does_not_trigger_on_small_pullback(self, mock_now, mock_combo, mock_save):
        mock_now.return_value = et(2026, 10, 2, 11, 0)
        state = self._state_with_open(credit=0.30)
        mock_combo.return_value = {'price': -0.09, 'bid': -0.09, 'ask': -0.07}   # 70% peak
        S.spym_monitor_position(state, 'open')
        mock_combo.return_value = {'price': -0.12, 'bid': -0.12, 'ask': -0.10}   # pct=0.60 (60%) -> only 10pp off peak
        S.spym_monitor_position(state, 'open')
        pos = state['variants']['open']['open_position']
        self.assertIsNotNone(pos)   # 10pp < 15pp giveback -> still holding
        self.assertAlmostEqual(pos['peak_pct_captured'], 70.0, places=1)   # peak itself is unchanged, not reset down
        self.assertEqual(state['variants']['open']['trades'], [])

    @patch.object(S, 'save_spym_state')
    @patch.object(S, 'spym_get_combo_quote')
    @patch.object(S, 'et_now')
    def test_no_trail_activation_below_threshold_regardless_of_movement(self, mock_now, mock_combo, mock_save):
        mock_now.return_value = et(2026, 10, 2, 11, 0)
        state = self._state_with_open(credit=0.30)
        mock_combo.return_value = {'price': -0.22, 'bid': -0.22, 'ask': -0.20}   # pct=(0.30-0.22)/0.30=0.267 (27%)
        S.spym_monitor_position(state, 'open')
        mock_combo.return_value = {'price': -0.25, 'bid': -0.25, 'ask': -0.23}   # pct=0.167 (17%) -- moved against us
        S.spym_monitor_position(state, 'open')
        pos = state['variants']['open']['open_position']
        self.assertIsNotNone(pos)
        self.assertIsNone(pos.get('peak_pct_captured'))   # never activated -- stayed below 50% both times

    @patch.object(S, 'save_spym_state')
    @patch.object(S, 'notify')
    @patch.object(S, 'spym_get_combo_quote')
    @patch.object(S, 'et_now')
    def test_stop_loss_closes(self, mock_now, mock_combo, mock_notify, mock_save):
        mock_now.return_value = et(2026, 10, 2, 11, 0)
        state = self._state_with_open(credit=0.30)
        mock_combo.return_value = {'price': -0.65, 'bid': -0.65, 'ask': -0.60}
        S.spym_monitor_position(state, 'open')
        self.assertIsNone(state['variants']['open']['open_position'])
        trades = state['variants']['open']['trades']
        self.assertEqual(trades[0]['close_reason'], 'stop_loss')
        self.assertLess(trades[0]['pnl'], 0)

    @patch.object(S, 'save_spym_state')
    @patch.object(S, 'notify')
    @patch.object(S, 'spym_get_combo_quote')
    @patch.object(S, 'et_now')
    def test_force_close_at_eod(self, mock_now, mock_combo, mock_notify, mock_save):
        mock_now.return_value = et(2026, 10, 2, 15, 46)
        state = self._state_with_open(credit=0.30)
        mock_combo.return_value = {'price': -0.20, 'bid': -0.20, 'ask': -0.18}
        S.spym_monitor_position(state, 'open')
        self.assertIsNone(state['variants']['open']['open_position'])
        self.assertEqual(state['variants']['open']['trades'][0]['close_reason'], 'force_close_eod')

    @patch.object(S, 'save_spym_state')
    @patch.object(S, 'spym_get_combo_quote')
    @patch.object(S, 'et_now')
    def test_holds_when_no_trigger_met(self, mock_now, mock_combo, mock_save):
        mock_now.return_value = et(2026, 10, 2, 11, 0)
        state = self._state_with_open(credit=0.30)
        mock_combo.return_value = {'price': -0.20, 'bid': -0.20, 'ask': -0.18}
        S.spym_monitor_position(state, 'open')
        self.assertIsNotNone(state['variants']['open']['open_position'])
        self.assertEqual(state['variants']['open']['trades'], [])

    @patch.object(S, 'save_spym_state')
    @patch.object(S, 'notify')
    @patch.object(S, 'spym_get_combo_quote')
    @patch.object(S, 'et_now')
    def test_two_variants_are_independent(self, mock_now, mock_combo, mock_notify, mock_save):
        """Core guarantee of the two-lane design: closing one variant's position
        must never touch the other variant's state."""
        mock_now.return_value = et(2026, 10, 2, 11, 0)
        state = S.load_spym_state()
        state['variants']['open']['open_position'] = make_pos(0.30)
        pos_b = make_pos(0.25)
        pos_b['variant'] = 'ten_mst'
        state['variants']['ten_mst']['open_position'] = pos_b
        mock_combo.return_value = {'price': -0.65, 'bid': -0.65, 'ask': -0.60}  # triggers stop_loss for either
        S.spym_monitor_position(state, 'open')
        self.assertIsNone(state['variants']['open']['open_position'])
        self.assertIsNotNone(state['variants']['ten_mst']['open_position'])   # untouched by the other variant's close
        self.assertEqual(state['variants']['ten_mst']['trades'], [])

    def test_noop_when_nothing_open(self):
        state = S.load_spym_state()
        S.spym_monitor_position(state, 'open')
        self.assertIsNone(state['variants']['open']['open_position'])

    @patch.object(S, 'spym_get_combo_quote', return_value=None)
    def test_holds_when_quote_unavailable(self, mock_combo):
        state = self._state_with_open(credit=0.30)
        S.spym_monitor_position(state, 'open')
        self.assertIsNotNone(state['variants']['open']['open_position'])
        self.assertEqual(state['variants']['open']['trades'], [])


class TestDashboard(unittest.TestCase):
    @patch.object(S, 'spym_get_spot', return_value=764.0)
    def test_dashboard_writes_both_variants(self, mock_spot):
        import tempfile, json, os
        state = S.load_spym_state()
        state['variants']['open']['trades'] = [
            {'date': '2026-10-01', 'pnl': 20.0, 'win': True},
            {'date': '2026-10-02', 'pnl': -30.0, 'win': False},
        ]
        state['variants']['ten_mst']['trades'] = [
            {'date': '2026-10-01', 'pnl': 15.0, 'win': True},
        ]
        fd, path = tempfile.mkstemp(suffix='.json')
        os.close(fd)
        old = S.SPYM_DASHBOARD_FILE
        S.SPYM_DASHBOARD_FILE = path
        try:
            S.spym_write_dashboard(state)
            with open(path) as f:
                d = json.load(f)
        finally:
            S.SPYM_DASHBOARD_FILE = old
            os.remove(path)
        self.assertIn('open', d['variants'])
        self.assertIn('ten_mst', d['variants'])
        self.assertEqual(d['variants']['open']['performance']['total'], 2)
        self.assertEqual(d['variants']['open']['performance']['wins'], 1)
        self.assertAlmostEqual(d['variants']['open']['performance']['net_pnl'], -10.0)
        self.assertEqual(d['variants']['ten_mst']['performance']['total'], 1)
        self.assertAlmostEqual(d['variants']['ten_mst']['performance']['net_pnl'], 15.0)
        self.assertTrue(d['paper_trading'])


class TestRunCycle(unittest.TestCase):
    @patch.object(S, 'spym_write_dashboard')
    @patch.object(S, 'spym_open_new_position')
    @patch.object(S, 'spym_monitor_position')
    @patch.object(S, 'is_market_open', return_value=False)
    def test_noop_outside_market_hours_but_dashboard_still_refreshes(self, mock_open, mock_monitor, mock_openpos, mock_dash):
        state = S.load_spym_state()
        S.run_spym_cycle(state)
        mock_monitor.assert_not_called()
        mock_openpos.assert_not_called()
        mock_dash.assert_called_once()

    @patch.object(S, 'spym_write_dashboard')
    @patch.object(S, 'spym_open_new_position')
    @patch.object(S, 'spym_monitor_position')
    @patch.object(S, 'is_market_open', return_value=True)
    @patch.object(S, 'et_now')
    def test_monitors_open_variant_only(self, mock_now, mock_open, mock_monitor, mock_openpos, mock_dash):
        mock_now.return_value = et(2026, 10, 2, 11, 0)
        state = S.load_spym_state()
        state['variants']['open']['open_position'] = {'short_code': 'S'}
        S.run_spym_cycle(state)
        mock_monitor.assert_called_once_with(state, 'open')
        mock_openpos.assert_not_called()   # 'ten_mst' is flat but it's 11:00, past its own entry time too --
                                            # wait: at 11:00 ET, 'ten_mst' (entry 12:00) is NOT yet in its window

    @patch.object(S, 'spym_write_dashboard')
    @patch.object(S, 'spym_open_new_position')
    @patch.object(S, 'spym_monitor_position')
    @patch.object(S, 'is_market_open', return_value=True)
    @patch.object(S, 'et_now')
    def test_tries_entry_only_for_variant_whose_window_is_open(self, mock_now, mock_open, mock_monitor, mock_openpos, mock_dash):
        mock_now.return_value = et(2026, 10, 2, 10, 0)   # inside 'open' (9:45+) window, before 'ten_mst' (12:00)
        state = S.load_spym_state()
        S.run_spym_cycle(state)
        mock_openpos.assert_called_once_with(state, 'open')

    @patch.object(S, 'spym_write_dashboard')
    @patch.object(S, 'spym_open_new_position')
    @patch.object(S, 'spym_monitor_position')
    @patch.object(S, 'is_market_open', return_value=True)
    @patch.object(S, 'et_now')
    def test_tries_entry_for_both_variants_once_both_windows_open(self, mock_now, mock_open, mock_monitor, mock_openpos, mock_dash):
        mock_now.return_value = et(2026, 10, 2, 12, 30)   # past both 9:45 and 12:00, before the 14:00 cutoff
        state = S.load_spym_state()
        S.run_spym_cycle(state)
        self.assertEqual(mock_openpos.call_count, 2)
        called_variants = {c.args[1] for c in mock_openpos.call_args_list}
        self.assertEqual(called_variants, {'open', 'ten_mst'})

    @patch.object(S, 'spym_write_dashboard')
    @patch.object(S, 'spym_open_new_position')
    @patch.object(S, 'spym_monitor_position')
    @patch.object(S, 'is_market_open', return_value=True)
    @patch.object(S, 'et_now')
    def test_skips_entry_outside_window_when_flat(self, mock_now, mock_open, mock_monitor, mock_openpos, mock_dash):
        mock_now.return_value = et(2026, 10, 2, 15, 0)   # after the 14:00 last-entry cutoff
        state = S.load_spym_state()
        S.run_spym_cycle(state)
        mock_openpos.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
