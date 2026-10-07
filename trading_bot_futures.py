#!/usr/bin/env python3
"""
APEX Futures v1 — Binance USDM Perpetuals
Symbol: ETH/USDT Perpetual
Strategy: ADX Regime Filter + EMA21 Pullback (1H)
Both LONG and SHORT. ~0.05% taker fee (2x cheaper than margin).
No borrow/repay. Leverage set via API. ISOLATED margin mode.
"""

import os
import json
import time
import hmac
import hashlib
import logging
import statistics
import requests
import pandas as pd
import ta

from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
from typing import Optional, Tuple
from dotenv import load_dotenv

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_DIR = 'logs'
os.makedirs(LOG_DIR, exist_ok=True)
logger = logging.getLogger('apex_futures')
logger.setLevel(logging.INFO)
fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
fh = logging.FileHandler(os.path.join(LOG_DIR, 'apex_futures.log'), encoding='utf-8')
fh.setFormatter(fmt)
ch = logging.StreamHandler()
ch.setFormatter(fmt)
if not logger.handlers:
    logger.addHandler(fh)
    logger.addHandler(ch)

# ── Environment ───────────────────────────────────────────────────────────────
MST                = timezone(timedelta(hours=-7))
SPOT_BASE_URL      = 'https://api.binance.com'
FUTURES_BASE_URL   = 'https://fapi.binance.com'
BINANCE_API_KEY    = os.environ.get('BINANCE_API_KEY', '').strip()
BINANCE_API_SECRET = os.environ.get('BINANCE_API_SECRET', '').strip()
DISCORD_WEBHOOK_URL = os.environ.get('DISCORD_WEBHOOK_URL', '').strip()
BOT_CONFIG_FILE    = os.environ.get('BOT_CONFIG_FILE', 'bot_config.json').strip()
BOT_STATE_FILE     = os.environ.get('BOT_STATE_FILE_FUTURES', 'bot_state_futures.json').strip()
# Lightweight regime/sentiment journal -- unlike SPY (5 signals/day), Apex
# evaluates all 7 symbols every ~30s, so this is sampled hourly per symbol
# (see _log_apex_regime_snapshot) rather than logged every cycle, to avoid
# ~20k entries/day of noise. Added 2026-09-17 so Hermes's daily analysis has
# a real day-by-day memory of market regime/trend, not just trade P&L.
APEX_REGIME_LOG_FILE     = 'apex_regime_history.jsonl'
APEX_REGIME_LOG_INTERVAL_HOURS = 1
TRADES_LOG_FILE    = os.environ.get('FUTURES_TRADES_LOG', 'trades_log_futures.json').strip()

DEFAULT_TRADE_AMOUNT_USDT = float(os.environ.get('FUTURES_TRADE_AMOUNT_USDT', '20'))
DEFAULT_LEVERAGE          = float(os.environ.get('FUTURES_LEVERAGE', '10'))
CHECK_INTERVAL            = int(os.environ.get('CHECK_INTERVAL', '30'))
ALLOWED_LEVERAGES         = {3.0, 4.0, 5.0, 10.0, 15.0, 20.0, 30.0, 50.0}

# ── Overnight Strategy Config (MU) ────────────────────────────────────────────
OVERNIGHT_CFG = {
    'symbol':   'MUUSDT',
    'amount':   40.0,
    'leverage': 40,
    'sl_pct':   0.035,   # 3.5% hard stop — at 40x this is ~143% of collateral per
                         # stop-out (backtested), not capped at the $40 nominal size
}

# Evidence-gated auto-scaling for MU's size/leverage -- added 2026-09-17 after
# confirming this strategy was on a flat, never-tuned config with zero Hermes
# visibility. Discrete ladder (not a continuous formula) so every step is a
# concrete, auditable before/after: floor is today's real default, ceiling is
# the user-approved $100 collateral cap paired with 50x -- Binance's real
# bracket-1 max leverage for MUUSDT up to $50k notional (confirmed live via
# /fapi/v1/leverageBracket; $100x50=$5,000 notional stays deep in that bracket).
MU_SCALE_LADDER = [
    {'amount': 40.0,  'leverage': 40},   # floor -- never scale below this
    {'amount': 70.0,  'leverage': 45},   # step 1
    {'amount': 100.0, 'leverage': 50},   # ceiling
]
MU_AUTO_SCALE_ENABLED      = True   # live -- enabled 2026-09-18 by user request, after dry-run
                                      # confirmed real evidence tracking favorably (16 trades,
                                      # 50% WR, $4.88 avg net -- both already past the up-bar,
                                      # just short of the 20-trade minimum sample; that gate is
                                      # left in place, not bypassed -- see run_weekly_mu_auto_tune()
MU_MIN_TRADES_FIRST_SCALE  = 20     # ~4 weeks at MU's real ~5 trades/week cadence
MU_ROLLBACK_MIN_TRADES     = 12     # re-check cadence for every level after the first
MU_SCALE_UP_AVG_NET_PCT    = 0.075  # avg net/trade over the window >= 7.5% of that window's collateral
MU_SCALE_UP_MIN_WIN_RATE   = 0.45
MU_SCALE_DOWN_MIN_WIN_RATE = 0.35   # hysteresis gap below the up-bar, avoids flapping at the boundary
MU_SCALE_DOWN_LOSE_STREAK  = 4
MU_AUTO_TUNE_INTERVAL_DAYS = 3   # decision cadence, matches Apex's AUTO_TUNE_INTERVAL_DAYS -- data/
                                  # reporting is daily (run_daily_hermes_sync), decisions stay slower

# Major US market holidays (month, day) — 2026 dates. Shared by is_us_market_open()
# and the MU overnight entry check — MUUSDT keeps trading on Binance 24/7 even when
# NASDAQ is shut, so without this check the overnight strategy would enter/exit
# against a synthetic price with no real underlying market behind it.
US_MARKET_HOLIDAYS = {(1,1),(1,19),(2,16),(4,3),(5,25),(7,4),(9,7),(11,26),(12,25)}

try:
    from zoneinfo import ZoneInfo as _ZI
    _ET_TZ = _ZI('America/New_York')
except Exception:
    _ET_TZ = None

def _et_now() -> datetime:
    if _ET_TZ:
        return datetime.now(_ET_TZ)
    offset = -4 if 3 <= datetime.now(timezone.utc).month <= 11 else -5
    return datetime.now(timezone(timedelta(hours=offset)))

# ── Symbol Config ─────────────────────────────────────────────────────────────
SYMBOLS_CONFIG = {
    'NVDAUSDT': {
        'base': 'NVDA',
        'dashboard_file': 'data_futures_nvda.json',
        'min_atr': 1.0,
        'trade_amount': 30.0,
        'max_loss_pct': 0.30,
        'market_hours_only': True,
        'one_way': True,
        'leverage': 20,
        'skip_margin_type': True,
        'rsi_long_min': 22,
        'rsi_long_max': 70,
        'rsi_short_min': 38,
        'rsi_short_max': 70,
        'trail_dist_atr': 0.15,
        'pullback_zone_pct': 0.035,
        'leverage': 20,
    },
    'AMDUSDT': {
        'base': 'AMD',
        'dashboard_file': 'data_futures_amd.json',
        'min_atr': 1.80,
        'trade_amount': 40.0,
        'max_loss_pct': 0.35,
        'market_hours_only': True,
        'one_way': True,
        'leverage': 25,
        'skip_margin_type': True,
        'daily_profit_lock': 6.0,
        'short_only': True,
        'ema_cross_enabled': True,  # backtested 2026-09-16, 90 days real data -- see get_decision()
        'rsi_long_min': 30,
        'rsi_long_max': 60,
        'rsi_short_min': 35,
        'rsi_short_max': 75,
        'pullback_zone_pct': 0.030,
        'trail_dist_atr': 0.20,
    },
    'APPUSDT': {
        'base': 'APP',
        'dashboard_file': 'data_futures_app.json',
        'min_atr': 1.0,
        'trade_amount': 30.0,
        'max_loss_pct': 0.50,
        'market_hours_only': True,
        'one_way': True,
        'leverage': 25,
        'skip_margin_type': True,
        'daily_profit_lock': 5.0,
        'trend_continuation_enabled': False,  # was True. 2026-09-22: trend-continuation entries (added Sep 13) buy already-extended moves -- pooled across symbols 33% win, -$100 net; August (off) LONG +$21 vs September (on) LONG -$267. Disabled bot-wide per user request.
        'rsi_long_min': 25,
        'rsi_long_max': 65,
        'rsi_short_min': 35,
        'rsi_short_max': 75,
        'pullback_zone_pct': 0.030,
        'trail_dist_atr': 0.20,
    },
    'HOODUSDT': {
        'base': 'HOOD',
        'dashboard_file': 'data_futures_hood.json',
        'min_atr': 0.70,   # real 7-day baseline ATR $0.945 (0.78% of $120.88) checked 2026-09-22 -- 70% pass rate
        'trade_amount': 30.0,  # user request 2026-09-22
        'max_loss_pct': 0.35,
        'market_hours_only': True,
        'one_way': True,
        'leverage': 20,  # max leverage at $30 margin (Binance bracket 1: 0-$100k notional) -- verified live 2026-09-22
        'skip_margin_type': True,
        'daily_profit_lock': 5.0,
        'short_only': True,  # user request 2026-09-22: added as a TSLA replacement, short-only from day one ("I don't want to lose money")
        'rsi_long_min': 25,
        'rsi_long_max': 63,
        'rsi_short_min': 35,
        'rsi_short_max': 75,
        'pullback_zone_pct': 0.030,
        'trail_dist_atr': 0.20,
    },
    'CRCLUSDT': {
        'base': 'CRCL',
        'dashboard_file': 'data_futures_crcl.json',
        'min_atr': 0.75,
        'trade_amount': 70.0,  # raised from 50 -- user request 2026-09-18, for the rest of the month
        'max_loss_pct': 0.35,
        'market_hours_only': True,
        'one_way': True,
        'leverage': 40,
        'skip_margin_type': True,
        'daily_profit_lock': 6.0,
        'short_only': True,  # user request 2026-09-22: LONG net -$144/13 trades vs SHORT +$31/9 trades
        'trend_continuation_enabled': False,  # was True. 2026-09-21: 5 of 6 CRCL trend-continuation LONGs hit the hard stop (-$92); all bought 3-5% above EMA21 at RSI 69-78
        'rsi_long_min': 25,
        'rsi_long_max': 63,  # was 65. 2026-09-21: pullback LONGs entered at RSI >= 63 lost 7 of 7 across symbols (CRCL 3 of 3, -$75); below 63 won 5 of 6
        'rsi_short_min': 35,
        'rsi_short_max': 75,
        'pullback_zone_pct': 0.030,
        'trail_dist_atr': 0.20,
    },
    'SOXLUSDT': {
        'base': 'SOXL',
        'dashboard_file': 'data_futures_soxl.json',
        'min_atr': 0.8,
        'trade_amount': 70.0,  # raised from 40 -- user request 2026-09-18, for the rest of the month
        'max_loss_pct': 0.35,
        'market_hours_only': True,
        'one_way': True,
        'leverage': 50,  # max available at this position size (Binance bracket 1: 0-$50k notional)
        'skip_margin_type': True,
        'daily_profit_lock': 5.0,
        'short_only': True,  # user request 2026-09-22: LONG net -$29/5 trades (20%% win) vs SHORT +$26/5 trades (100%% win)
        'trend_continuation_enabled': False,  # was True. 2026-09-22: trend-continuation entries (added Sep 13) buy already-extended moves -- pooled across symbols 33% win, -$100 net; August (off) LONG +$21 vs September (on) LONG -$267. Disabled bot-wide per user request.
        'rsi_long_min': 25,
        'rsi_long_max': 63,  # was 65. 2026-09-21: pullback LONGs entered at RSI >= 63 lost 7 of 7 across CRCL/SOXL/TSLA (-$116); below 63 won 5 of 6
        'rsi_short_min': 35,
        'rsi_short_max': 75,
        'pullback_zone_pct': 0.030,
        'trail_dist_atr': 0.20,
    },
    'TSLAUSDT': {  # RETIRED 2026-09-22 (removed from TRADING_SYMBOLS) -- small typical moves, wins $3-5 vs losses ~$10, user request. Config kept for reference only, inert.
        'base': 'TSLA',
        'dashboard_file': 'data_futures_tsla.json',
        'min_atr': 0.95,
        'trade_amount': 40.0,
        'max_loss_pct': 0.35,
        'market_hours_only': True,
        'one_way': True,
        'leverage': 25,
        'skip_margin_type': True,
        'daily_profit_lock': 2.5,
        'trend_continuation_enabled': True,
        'breakout_enabled': True,
        'ema_cross_enabled': True,  # backtested 2026-09-16, 90 days real data -- see get_decision()
        'rsi_long_min': 27,
        'rsi_long_max': 63,  # was 66. 2026-09-21: pullback LONGs entered at RSI >= 63 lost 7 of 7 across CRCL/SOXL/TSLA (-$116); below 63 won 5 of 6
        'rsi_short_min': 22,
        'rsi_short_max': 68,
        'pullback_zone_pct': 0.030,
        'trail_dist_atr': 0.20,
        # Backtested 2026-09-16 by replaying all 100 real closed TSLA trades
        # against actual 1-minute Binance price history: raising this from the
        # global default (0.75) consistently reduced losses in every one of
        # the 4 months tested (tightening the hard SL or widening trail_dist
        # instead both made it worse — this was the only direction that
        # helped). TSLA was net roughly flat (-$3.58/100 trades) beforehand;
        # this doesn't make it profitable, just less of a drag. See
        # project_loop_engineering_framework memory for the full analysis.
        'trail_activate_atr': 1.0,
    },
    'NBISUSDT': {
        'base': 'NBIS',
        'dashboard_file': 'data_futures_nbis.json',
        'min_atr': 1.40,
        'trade_amount': 70.0,
        'max_loss_pct': 0.50,
        'market_hours_only': True,
        'one_way': True,
        'leverage': 25,
        'skip_margin_type': True,
        'daily_profit_lock': 7.0,
        'trend_continuation_enabled': False,  # was True. 2026-09-22: trend-continuation entries (added Sep 13) buy already-extended moves -- pooled across symbols 33% win, -$100 net; August (off) LONG +$21 vs September (on) LONG -$267. Disabled bot-wide per user request.
        'ema_cross_enabled': True,  # backtested 2026-09-16, 90 days real data -- see get_decision()
        'rsi_long_min': 25,
        'rsi_long_max': 65,
        'rsi_short_min': 20,
        'rsi_short_max': 70,
        'pullback_zone_pct': 0.042,
        'trail_dist_atr': 0.20,
    },
    'ASTSUSDT': {
        'base': 'ASTS',
        'dashboard_file': 'data_futures_asts.json',
        'min_atr': 0.30,
        'trade_amount': 40.0,
        'max_loss_pct': 0.35,
        'market_hours_only': True,
        'entry_window_et': (9, 45, 11, 0),
        'one_way': True,
        'leverage': 20,
        'skip_margin_type': True,
        'daily_profit_lock': 5.0,
        'short_only': True,  # user request 2026-09-22: LONG net -$24/11 trades vs SHORT +$20/16 trades
        'trend_continuation_enabled': False,  # was True. 2026-09-22: trend-continuation entries (added Sep 13) buy already-extended moves -- pooled across symbols 33% win, -$100 net; August (off) LONG +$21 vs September (on) LONG -$267. Disabled bot-wide per user request.
        'breakout_enabled': True,
        'rsi_long_min': 25,
        'rsi_long_max': 65,
        'rsi_short_min': 25,
        'rsi_short_max': 75,
        'pullback_zone_pct': 0.040,
        'trail_dist_atr': 0.20,
    },
    'ETHUSDT': {
        'base': 'ETH',
        'dashboard_file': 'data_futures_eth.json',
        'min_atr': 8.0,
        'trade_amount': DEFAULT_TRADE_AMOUNT_USDT,
    },
    'BTCUSDT': {
        'base': 'BTC',
        'dashboard_file': 'data_futures_btc.json',
        'min_atr': 200.0,
        'trade_amount': 30.0,
    },
    'SOLUSDT': {
        'base': 'SOL',
        'dashboard_file': 'data_futures_sol.json',
        'min_atr': 1.5,
        'trade_amount': 30.0,
    },
    'AAVEUSDT': {
        'base': 'AAVE',
        'dashboard_file': 'data_futures_aave.json',
        'min_atr': 0.8,
        'trade_amount': 30.0,
    },
    'MUUSDT': {
        'base': 'MU',
        'one_way': True,
        'skip_margin_type': True,
    },
}
TRADING_SYMBOLS = ['NBISUSDT', 'AMDUSDT', 'APPUSDT', 'SOXLUSDT', 'CRCLUSDT', 'ASTSUSDT', 'HOODUSDT']

# Auto-tunable SL/trail parameters live here at runtime, layered over the
# hardcoded SYMBOLS_CONFIG defaults, so run_weekly_auto_tune() can persist a
# change without editing this source file. Always read these three params
# (hard_sl_atr, trail_activate_atr, trail_dist_atr) through get_symbol_cfg(),
# never SYMBOLS_CONFIG directly, so live trading and the auto-tuner always
# agree on which value is actually in effect.
def get_symbol_cfg(symbol: str, key: str, default):
    override = state.get('runtime', {}).get('auto_tune_overrides', {}).get(symbol, {})
    if key in override:
        return override[key]
    return SYMBOLS_CONFIG.get(symbol, {}).get(key, default)

# Same pattern as get_symbol_cfg(), but MUUSDT is deliberately absent from
# SYMBOLS_CONFIG (it's a separate overnight strategy, not one of the 7
# TRADING_SYMBOLS) -- calling get_symbol_cfg('MUUSDT', ...) directly would
# silently always return the default. Falls back to OVERNIGHT_CFG instead.
def get_mu_cfg(key: str, default):
    override = state.get('runtime', {}).get('auto_tune_overrides', {}).get('MUUSDT', {})
    if key in override:
        return override[key]
    return OVERNIGHT_CFG.get(key, default)

# ── Hermes pre-trade veto check ─────────────────────────────────────────────
# Asks a small LLM service (running on a separate droplet, private network
# only -- see check_hermes_veto below) to sanity-check each proposed entry
# against today's macro calendar/sentiment before it fires. Fails open by
# design: any error, timeout, or malformed response is treated as approval,
# so an outage on the Hermes side can never block a trade on its own.
HERMES_VETO_ENABLED = True
HERMES_VETO_URL     = 'http://10.122.0.3:8787/veto_check'
HERMES_VETO_TIMEOUT = 15  # seconds -- matches the service's own internal LLM budget + margin

def check_hermes_veto(symbol: str, dec: dict, price: float) -> Tuple[bool, str]:
    """Ask the Hermes veto service whether to proceed with this entry.
    Fails open (returns True) on any error, timeout, or bad response --
    an outage or slow response on the Hermes side must never block a trade."""
    if not HERMES_VETO_ENABLED:
        return True, ''
    try:
        resp = requests.post(HERMES_VETO_URL, json={
            'symbol':          symbol,
            'action':          dec['action'],
            'confidence':      dec['confidence'],
            'regime':          dec['regime'],
            'trend_direction': dec['trend_direction'],
            'reason':          dec['reason'],
            'price':           price,
        }, timeout=HERMES_VETO_TIMEOUT)
        resp.raise_for_status()
        body     = resp.json()
        decision = (body.get('decision') or 'APPROVE').upper()
        reason   = body.get('reason', '')
        return decision != 'VETO', reason
    except Exception as e:
        logger.info(f'[{symbol}] Hermes veto check unreachable/failed ({e}) — proceeding (fail-open)')
        return True, ''

# Correlated pairs — skip entry in symbol B if symbol A already has an open trade.
# Add pairs here when you observe two symbols that move lockstep and you want to cap
# sector concentration. Empty by default — all symbols trade independently.
CORR_GROUPS: list[set] = []

# ── Strategy Parameters ───────────────────────────────────────────────────────
ADX_MIN           = 25.0
ADX_STRONG        = 30.0
# Floor for the EMA-cross bypass entry below -- added 2026-09-18 after a real
# live loss: NBIS SHORT entered at ADX 12.6 (barely above a flat line, not
# just "not yet confirmed trending") and hit hard SL for -$26.39, the worst
# trade of a 0-for-7 day. The bypass is meant to act a bit AHEAD of ADX_MIN
# confirming (its own two prior live trades entered at ADX 24-25, right next
# to the gate) -- 12.6 is a different case, a genuinely weak/choppy reading,
# not an early one. This floor still lets it fire well before ADX_MIN.
EMA_CROSS_MIN_ADX = 18.0
PULLBACK_ZONE_PCT = 0.018
RSI_LONG_MIN      = 30
RSI_LONG_MAX      = 62
RSI_SHORT_MIN     = 38
RSI_SHORT_MAX     = 70

# Donchian breakout entry — opt-in per symbol via SYMBOLS_CONFIG[symbol]['breakout_enabled'].
# Runs alongside (not instead of) the pullback-to-EMA21 entry above: whichever
# condition is met first opens the trade. Validated by backtest on ASTS/TSLA
# full history (Sep 2026) — similar win rate to pullback with far fewer trades,
# and specifically profitable on TSLA where pullback-only was a net loser.
DC_PERIOD = 20

# ── Daily profit lock ─────────────────────────────────────────────────────────
# Once a symbol has already banked this much realized P&L today, further
# entries that same day require a much stronger signal (confidence >= this
# threshold, which needs every confirmation — volume, RSI sweet spot, and
# EMA position — to align, vs. the ~70 baseline for a plain valid setup).
# Protects a day's gain from being given back on a marginal follow-up trade
# rather than stopping the ticker outright. Per-symbol override via
# SYMBOLS_CONFIG[symbol]['daily_profit_lock'].
DAILY_PROFIT_LOCK_DEFAULT    = 5.0
DAILY_PROFIT_LOCK_CONFIDENCE = 85

# ── Trail Parameters ──────────────────────────────────────────────────────────
TRAIL_ACTIVATE_ATR = 0.75
HARD_SL_ATR        = 1.25
# Near-miss protection (added 2026-10-07): found via a real SOXL trade where peak profit reached
# 97.6% of its (auto-tuned) activation threshold, missed arming the real trail by about $1.30,
# then fully reversed into the hard stop -- the entire ~$52 peak gave back to a -$29 loss with
# zero protection in between, since the trail never technically activated. This catches that
# specific failure mode without touching any symbol's tuned activation threshold: once profit
# gets within NEAR_MISS_ATR_FRAC of activating, protect against giving back more than
# NEAR_MISS_GIVEBACK_FRAC of whatever peak was reached, even though the real trail never armed.
NEAR_MISS_ATR_FRAC      = 0.85
NEAR_MISS_GIVEBACK_FRAC = 0.5
FEE_RATE           = 0.0005   # 0.05% futures taker fee

# ── Per-symbol state ──────────────────────────────────────────────────────────
def _empty_sym_state() -> dict:
    return {
        'position':               None,
        'trail_entry_price':      None,
        'trail_best_price':       None,
        'trail_atr':              None,
        'trade_opened_at':        None,
        'entry_fee_usdt':         0.0,
        'active_qty':             None,
        'active_trade_amount':    None,
        'active_leverage':        None,
        'last_bot_closed_side':   None,
        'last_bot_closed_ts':     0,
        'force_trail_processed':  False,
        'force_trail_active':     False,
        'force_trail_stop_price': None,
        'closed_trades_log':      [],
        'last_hard_sl_ts':        0,
        'consecutive_losses':     0,
        'loss_streak_direction':  None,
        'entry_trend_direction':  None,
    }

state = {
    'symbols': {sym: _empty_sym_state() for sym in SYMBOLS_CONFIG},
    'runtime': {
        'trade_amount_usdt': DEFAULT_TRADE_AMOUNT_USDT,
        'leverage':          DEFAULT_LEVERAGE,
        'source':            'env-defaults',
    },
    'manual_positions': {},
    'overnight_mu': {},
    'overnight_mu_trades': [],
    'hermes_picks': {},
    'hermes_picks_trades': [],
    'hermes_picks_universe': {},
}

run_count       = 0
last_hold_alert: dict = {}


# ── Utilities ─────────────────────────────────────────────────────────────────
def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def safe_float(v, default=0.0) -> float:
    try: return float(v)
    except Exception: return default

def read_json(path: str, default):
    try:
        if not os.path.exists(path): return default
        with open(path, 'r', encoding='utf-8') as f: return json.load(f)
    except Exception as e:
        logger.warning(f'read_json {path}: {e}')
        return default

def write_json(path: str, data) -> None:
    tmp = f'{path}.{os.getpid()}.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f: json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning(f'write_json {path}: {e}')
        try: os.remove(tmp)
        except Exception: pass

def load_state() -> None:
    global state
    loaded = read_json(BOT_STATE_FILE, None)
    if not isinstance(loaded, dict): return
    if 'symbols' in loaded:
        for sym in TRADING_SYMBOLS:
            if sym in loaded['symbols']:
                state['symbols'][sym].update(loaded['symbols'][sym])
        if 'runtime' in loaded:
            state['runtime'].update(loaded['runtime'])
    if 'manual_positions' in loaded:
        state['manual_positions'].update(loaded['manual_positions'])
    elif 'manual_btc' in loaded and loaded['manual_btc'].get('position'):
        state['manual_positions']['BTCUSDT'] = loaded['manual_btc']
    if 'overnight_mu' in loaded:
        state['overnight_mu'].update(loaded['overnight_mu'])
    if 'overnight_mu_trades' in loaded:
        state['overnight_mu_trades'] = loaded['overnight_mu_trades']
    if 'overnight_mu_disabled' in loaded:
        # Bug found 2026-10-05: load_state() is an explicit per-key allowlist and this key was
        # missing from it -- close_overnight_mu() set it correctly in memory and saved it, but a
        # restart silently dropped it (not copied from disk) and the next save_state() call
        # overwrote the file with the fresh default, erasing it. Caught during the MU->Sentinel
        # migration cutover when a manual disk edit reverted right after a restart.
        state['overnight_mu_disabled'] = loaded['overnight_mu_disabled']
    if 'hermes_picks' in loaded:
        state['hermes_picks'].update(loaded['hermes_picks'])
    if 'hermes_picks_trades' in loaded:
        state['hermes_picks_trades'] = loaded['hermes_picks_trades']
    if 'hermes_picks_universe' in loaded:
        state['hermes_picks_universe'] = loaded['hermes_picks_universe']

def save_state() -> None:
    write_json(BOT_STATE_FILE, state)

def sym_state(symbol: str) -> dict:
    if symbol not in state['symbols']:
        state['symbols'][symbol] = _empty_sym_state()
    return state['symbols'][symbol]

def append_trade_log(record: dict) -> None:
    """Append a closed trade to the persistent log file (never overwritten)."""
    try:
        path = os.path.join(os.path.dirname(BOT_STATE_FILE), TRADES_LOG_FILE) \
               if os.path.dirname(BOT_STATE_FILE) else TRADES_LOG_FILE
        existing = read_json(path, [])
        if not isinstance(existing, list):
            existing = []
        existing.append(record)
        write_json(path, existing)
        # Sync to dashboard web root so calendar always shows all trades
        try:
            import shutil
            shutil.copy2(path, '/var/www/apex/trades_log.json')
        except Exception as _ce:
            logger.warning(f'append_trade_log: dashboard sync failed: {_ce}')
    except Exception as e:
        logger.warning(f'append_trade_log: {e}')

def load_trade_log() -> list:
    """Load all trades from the persistent log file."""
    try:
        path = os.path.join(os.path.dirname(BOT_STATE_FILE), TRADES_LOG_FILE) \
               if os.path.dirname(BOT_STATE_FILE) else TRADES_LOG_FILE
        data = read_json(path, [])
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning(f'load_trade_log: {e}')
        return []

def get_daily_realized_pnl(symbol: str) -> float:
    """Sum of this symbol's closed-trade P&L for the current ET calendar day.
    Used to gate further entries once a ticker has already banked a solid
    profit today — protects the day's gain from being given back on a
    marginal follow-up trade instead of stopping the ticker outright."""
    today_et = _et_now().date()
    total = 0.0
    for t in load_trade_log():
        if t.get('symbol') != symbol or t.get('dust'):
            continue
        closed_at = t.get('closed_at')
        if not closed_at:
            continue
        try:
            dt = datetime.fromisoformat(closed_at.replace('Z', '+00:00'))
            dt_et = dt.astimezone(_ET_TZ) if _ET_TZ else dt
            if dt_et.date() == today_et:
                total += float(t.get('pnl', 0))
        except Exception:
            continue
    return round(total, 2)

def mask(v: str, s: int = 6, e: int = 4) -> str:
    if not v: return 'MISSING'
    if len(v) <= s + e: return '*' * len(v)
    return f'{v[:s]}...{v[-e:]}'


# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(msg: str) -> None:
    if not DISCORD_WEBHOOK_URL: return
    import re
    # Convert Telegram HTML tags to Discord markdown
    msg = re.sub(r'<b>(.*?)</b>', r'**\1**', msg, flags=re.DOTALL)
    msg = re.sub(r'<i>(.*?)</i>', r'*\1*',   msg, flags=re.DOTALL)
    msg = re.sub(r'<code>(.*?)</code>', r'`\1`', msg, flags=re.DOTALL)
    msg = re.sub(r'<[^>]+>', '', msg)  # strip any remaining tags
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={'content': msg}, timeout=10)
    except Exception as e:
        logger.warning(f'Discord failed: {e}')

def alert_error(err: str) -> None:
    send_telegram(
        f'❌ <b>APEX FUTURES ERROR</b>\n\n{err}\n\n'
        f'⏰ {datetime.now(MST).strftime("%b %d, %I:%M %p MST")}'
    )


# ── Futures API ───────────────────────────────────────────────────────────────
def _futures_sig(params: dict) -> str:
    return hmac.new(
        BINANCE_API_SECRET.encode('utf-8'),
        urlencode(params).encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()

def binance_futures_public(endpoint: str, params: dict = None) -> dict:
    r = requests.get(f'{FUTURES_BASE_URL}{endpoint}', params=params or {}, timeout=10)
    r.raise_for_status()
    return r.json()

def binance_futures_private(method: str, endpoint: str, params: dict = None) -> dict:
    params = params or {}
    params['timestamp'] = int(time.time() * 1000)
    params['signature'] = _futures_sig(params)
    headers = {'X-MBX-APIKEY': BINANCE_API_KEY}
    url = f'{FUTURES_BASE_URL}{endpoint}'
    r = (requests.get if method.upper() == 'GET' else requests.post)(
        url, params=params, headers=headers, timeout=15
    )
    if not r.ok:
        logger.error(f'FUTURES {method} {endpoint} | {r.status_code} | {r.text}')
    r.raise_for_status()
    return r.json()

def get_futures_balance(asset: str = 'USDT') -> dict:
    try:
        balances = binance_futures_private('GET', '/fapi/v2/balance')
        for b in balances:
            if b['asset'] == asset:
                return {
                    'free':  float(b['availableBalance']),
                    'total': float(b['balance']),
                }
    except Exception as e:
        logger.warning(f'get_futures_balance: {e}')
    return {'free': 0.0, 'total': 0.0}

def get_position_details(symbol: str) -> dict:
    """Returns {side, qty, entry_price, unrealized_pnl} from Binance positionRisk."""
    result = {'side': None, 'qty': 0.0, 'entry_price': None, 'unrealized_pnl': None}
    try:
        positions = binance_futures_private('GET', '/fapi/v2/positionRisk', {'symbol': symbol})
        for p in positions:
            if p['symbol'] != symbol:
                continue
            amt      = float(p['positionAmt'])
            pos_side = p.get('positionSide', 'BOTH')
            side = None
            if pos_side == 'LONG'  and amt >  1e-8: side = 'LONG'
            if pos_side == 'SHORT' and amt < -1e-8: side = 'SHORT'
            if pos_side == 'BOTH':
                if amt >  1e-8: side = 'LONG'
                if amt < -1e-8: side = 'SHORT'
            if side:
                qty = abs(amt)
                if qty < 0.05:  # ignore dust left by close rounding
                    break
                result['side']           = side
                result['qty']            = qty
                result['entry_price']    = safe_float(p.get('entryPrice'), None)
                result['unrealized_pnl'] = safe_float(p.get('unRealizedProfit'), None)
                break
    except Exception as e:
        logger.warning(f'get_position_details [{symbol}]: {e}')
    return result

def detect_futures_position(symbol: str) -> Optional[str]:
    return get_position_details(symbol)['side']

def set_futures_leverage(symbol: str, leverage: int) -> None:
    try:
        binance_futures_private('POST', '/fapi/v1/leverage',
                                {'symbol': symbol, 'leverage': leverage})
        logger.info(f'⚙️ [{symbol}] Leverage set to {leverage}x')
    except Exception as e:
        logger.warning(f'set_futures_leverage [{symbol}]: {e}')

def set_futures_margin_type(symbol: str, margin_type: str = 'ISOLATED') -> None:
    try:
        binance_futures_private('POST', '/fapi/v1/marginType',
                                {'symbol': symbol, 'marginType': margin_type})
        logger.info(f'⚙️ [{symbol}] Margin type → {margin_type}')
    except Exception as e:
        # -4046 = already set to that type
        # -4168 = Multi-Assets Mode active (ISOLATED not supported) — both are fine to ignore
        if '-4046' not in str(e) and '-4168' not in str(e):
            logger.warning(f'set_futures_margin_type [{symbol}]: {e}')

def _with_actual_fill(symbol: str, resp: dict) -> dict:
    """Market-order POST responses usually carry no fill data (avgPrice and
    cumQuote are 0), so get_fill_price() silently fell back to the PRE-ORDER
    ticker quote -- recorded entry/exit prices were quotes, not fills. Found
    2026-09-19: MU entry recorded $1009.98 but the real fill was $1015.17
    (price jumped ~$5 in the same second) -- ~$8 of P&L the books never saw;
    over 4 days the books ran ~$7.74 optimistic vs the exchange's own realized
    P&L. This looks the order up after placement and merges in the real fill.

    Strictly additive and fail-safe: by the time this runs the order has
    ALREADY been placed, so any problem here returns the original response
    unchanged (the old behavior) and never raises -- an exception here would
    make callers believe an executed order had failed."""
    try:
        if float(resp.get('avgPrice') or 0) > 0 or float(resp.get('cumQuote') or 0) > 0:
            return resp
        order_id = resp.get('orderId')
        if not order_id:
            return resp
        for attempt in range(4):
            if attempt:
                time.sleep(0.25)
            o = binance_futures_private('GET', '/fapi/v1/order', {'symbol': symbol, 'orderId': order_id})
            if o.get('status') == 'FILLED' and float(o.get('avgPrice') or 0) > 0:
                merged = dict(resp)
                for k in ('avgPrice', 'executedQty', 'cumQuote', 'status'):
                    if k in o:
                        merged[k] = o[k]
                return merged
        return resp
    except Exception as e:
        logger.warning(f'[{symbol}] actual-fill lookup failed (using order response as-is): {e}')
        return resp

def futures_market_order(symbol: str, side: str, quantity: float,
                         position_side: str = 'LONG', reduce_only: bool = False,
                         close_position: bool = False) -> dict:
    params: dict = {'symbol': symbol, 'side': side, 'type': 'MARKET'}
    one_way = SYMBOLS_CONFIG.get(symbol, {}).get('one_way')
    if close_position and one_way:
        # closePosition=true closes the entire position — no quantity needed, no rounding dust
        params['closePosition'] = 'true'
    else:
        params['quantity'] = str(quantity)
        if one_way:
            if reduce_only:
                params['reduceOnly'] = 'true'
        else:
            params['positionSide'] = position_side
    resp = binance_futures_private('POST', '/fapi/v1/order', params)
    return _with_actual_fill(symbol, resp)

def get_fill_price(resp: dict, fallback: float) -> float:
    """Binance futures market orders return avgPrice='0' — use cumQuote/executedQty instead."""
    try:
        avg = float(resp.get('avgPrice', 0))
        if avg > 0:
            return avg
        cum_quote = float(resp.get('cumQuote', 0))
        exec_qty  = float(resp.get('executedQty', 0))
        if cum_quote > 0 and exec_qty > 0:
            return cum_quote / exec_qty
    except Exception:
        pass
    return fallback

_step_cache: dict = {}

def get_futures_step_size(symbol: str) -> float:
    if symbol in _step_cache:
        return _step_cache[symbol]
    try:
        info = binance_futures_public('/fapi/v1/exchangeInfo')
        for s in info.get('symbols', []):
            if s['symbol'] == symbol:
                for f in s.get('filters', []):
                    if f['filterType'] == 'LOT_SIZE':
                        step = float(f['stepSize'])
                        _step_cache[symbol] = step
                        return step
    except Exception as e:
        logger.warning(f'get_futures_step_size [{symbol}]: {e}')
    return 0.001

def round_step(qty: float, step: float) -> float:
    precision = len(str(step).rstrip('0').split('.')[-1])
    return round(qty - (qty % step), precision)

def get_current_price(symbol: str) -> float:
    # One quick retry on the futures endpoint, then the spot fallback, then a
    # clear error. Added 2026-09-19 after a single 10s futures-API read
    # timeout surfaced as a bare `KeyError: 'price'` and aborted a whole
    # cycle: the spot fallback only works for symbols that ALSO list on spot
    # (BTC/ETH-style) -- the tokenized-stock perps this bot trades don't, so
    # spot returned an error body with no 'price' key, hiding the real cause.
    last_err = None
    for attempt in range(2):
        try:
            return float(binance_futures_public('/fapi/v1/ticker/price', {'symbol': symbol})['price'])
        except Exception as e:
            last_err = e
            if attempt == 0:
                time.sleep(1)
    try:
        r = requests.get(f'{SPOT_BASE_URL}/api/v3/ticker/price',
                         params={'symbol': symbol}, timeout=10)
        return float(r.json()['price'])
    except Exception:
        raise RuntimeError(f'price fetch failed for {symbol}: futures API '
                           f'{type(last_err).__name__}: {last_err}') from None



# ── 4H Trend Confirmation ─────────────────────────────────────────────────────
_4h_cache: dict = {}
H4_CACHE_TTL    = 1800

def _4h_klines_to_df(klines: list) -> pd.DataFrame:
    df4 = pd.DataFrame(klines, columns=[
        'time','open','high','low','close','volume',
        'close_time','quote_volume','trades','taker_base','taker_quote','ignore',
    ])
    for col in ('high','low','close'):
        df4[col] = df4[col].astype(float)
    return df4

def _compute_4h_trend(df4: pd.DataFrame) -> Tuple[str, float, float, float]:
    """Shared by live get_4h_trend() and the auto-tune entry-signal backtest,
    so both use byte-identical logic -- no separate reimplementation to drift
    out of sync. Returns (trend_label, adx, adx_pos, adx_neg) for the LAST
    row of df4, which for the backtest path is a windowed slice ending at
    some historical point in time (rolling indicators are causal, so this
    is a faithful point-in-time value, not a lookahead)."""
    adx_ind        = ta.trend.ADXIndicator(df4['high'], df4['low'], df4['close'], window=14)
    adx_s, pos_s, neg_s = adx_ind.adx(), adx_ind.adx_pos(), adx_ind.adx_neg()
    ema21_s = ta.trend.EMAIndicator(df4['close'], window=21).ema_indicator()
    ema50_s = ta.trend.EMAIndicator(df4['close'], window=50).ema_indicator()
    adx4, adx_pos4, adx_neg4 = float(adx_s.iloc[-1]), float(pos_s.iloc[-1]), float(neg_s.iloc[-1])
    ema21_4, ema50_4 = float(ema21_s.iloc[-1]), float(ema50_s.iloc[-1])
    if adx4 >= 20 and adx_pos4 > adx_neg4 and ema21_4 > ema50_4:
        trend4 = '4H BULLISH'
    elif adx4 >= 20 and adx_neg4 > adx_pos4 and ema21_4 < ema50_4:
        trend4 = '4H BEARISH'
    else:
        trend4 = '4H NEUTRAL'
    return trend4, adx4, adx_pos4, adx_neg4

def get_4h_trend(symbol: str) -> str:
    now = time.time()
    cached = _4h_cache.get(symbol)
    if cached and (now - cached['ts']) < H4_CACHE_TTL:
        return cached['trend']
    try:
        klines = binance_futures_public('/fapi/v1/klines',
                                        {'symbol': symbol, 'interval': '4h', 'limit': 60})
        df4 = _4h_klines_to_df(klines)
        trend4, adx4, adx_pos4, adx_neg4 = _compute_4h_trend(df4)
        _4h_cache[symbol] = {'trend': trend4, 'ts': now}
        logger.info(f'📊 [{symbol}] 4H: {trend4} | ADX={adx4:.1f} +DI={adx_pos4:.1f} -DI={adx_neg4:.1f}')
        return trend4
    except Exception as e:
        logger.warning(f'get_4h_trend [{symbol}] failed: {e}')
        return '4H NEUTRAL'


# ── Market Data & Signal ──────────────────────────────────────────────────────
def _build_1h_indicators(klines: list) -> pd.DataFrame:
    """Shared by live get_market_data() and the auto-tune entry-signal
    backtest -- both call this on a klines list so the indicator math can
    never drift out of sync between live trading and what the backtest
    thinks live trading does."""
    df = pd.DataFrame(klines, columns=[
        'time','open','high','low','close','volume',
        'close_time','quote_volume','trades','taker_base','taker_quote','ignore',
    ])
    for col in ('open','high','low','close','volume'):
        df[col] = df[col].astype(float)
    adx_ind       = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14)
    df['adx']     = adx_ind.adx()
    df['adx_pos'] = adx_ind.adx_pos()
    df['adx_neg'] = adx_ind.adx_neg()
    df['ema21']   = ta.trend.EMAIndicator(df['close'], window=21).ema_indicator()
    df['ema50']   = ta.trend.EMAIndicator(df['close'], window=50).ema_indicator()
    df['rsi']     = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
    df['atr']     = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range()
    df['vol_ma']  = df['volume'].rolling(20).mean()
    # Donchian channel (prior DC_PERIOD bars, excluding the current one) — backs
    # the opt-in breakout entry. shift(1) so today's bar can't see its own high/low.
    df['dc_upper'] = df['high'].rolling(DC_PERIOD).max().shift(1)
    df['dc_lower'] = df['low'].rolling(DC_PERIOD).min().shift(1)
    return df

def get_market_data(symbol: str) -> pd.DataFrame:
    klines = binance_futures_public('/fapi/v1/klines',
                                    {'symbol': symbol, 'interval': '1h', 'limit': 100})
    return _build_1h_indicators(klines)

ATR_HEALTH_INTERVAL_DAYS = 7   # re-check once a week — daily is too noisy/short-window to trust

def _atr_pass_rate_for_symbol(symbol: str) -> dict:
    """One-off diagnostic fetch (7 days of 1H bars) — read-only, never used
    for trading decisions. Reports what fraction of the last week's hourly
    windows would have cleared this symbol's configured min_atr, so a stale
    threshold (the NBIS problem — 2.5 configured while real ATR settled near
    1.5, blocking most hours) shows up as a flag instead of silently
    starving the ticker of trades until someone happens to dig in manually."""
    try:
        klines = binance_futures_public('/fapi/v1/klines',
                                        {'symbol': symbol, 'interval': '1h', 'limit': 24 * 7 + 14})
        df = pd.DataFrame(klines, columns=[
            'time','open','high','low','close','volume',
            'close_time','quote_volume','trades','taker_base','taker_quote','ignore',
        ])
        for col in ('high','low','close'):
            df[col] = df[col].astype(float)
        atr_series = ta.volatility.AverageTrueRange(
            df['high'], df['low'], df['close'], window=14).average_true_range().dropna()
        if len(atr_series) < 20:
            return {'pass_rate': None, 'baseline_atr': None, 'status': 'unknown'}

        static_value = SYMBOLS_CONFIG[symbol]['min_atr']
        baseline  = round(float(atr_series.mean()), 4)
        pass_rate = round(float((atr_series >= static_value).mean() * 100), 1)
        status    = 'warning' if pass_rate < 50 else 'watch' if pass_rate < 65 else 'ok'
        return {'pass_rate': pass_rate, 'baseline_atr': baseline,
                'configured_min_atr': static_value, 'status': status,
                'checked_at': now_utc_iso()}
    except Exception as e:
        logger.warning(f'atr_health [{symbol}]: {e}')
        return {'pass_rate': None, 'baseline_atr': None, 'status': 'unknown'}

def run_weekly_atr_health_check() -> None:
    """Gate: only actually runs once every ATR_HEALTH_INTERVAL_DAYS. Safe to
    call every main-loop cycle. Never touches trading logic — just refreshes
    state['runtime']['atr_health'][symbol] and pings Discord with a summary
    when any ticker needs a manual min_atr review."""
    last = state['runtime'].get('last_atr_health_check')
    if last:
        try:
            age_days = (datetime.now(timezone.utc) -
                        datetime.fromisoformat(last.replace('Z', '+00:00'))).days
        except Exception:
            age_days = ATR_HEALTH_INTERVAL_DAYS
        if age_days < ATR_HEALTH_INTERVAL_DAYS:
            return

    results = {}
    flagged = []
    for symbol in TRADING_SYMBOLS:
        health = _atr_pass_rate_for_symbol(symbol)
        results[symbol] = health
        if health['status'] in ('warning', 'watch'):
            base = SYMBOLS_CONFIG[symbol]['base']
            flagged.append(f"{'🔴' if health['status']=='warning' else '🟡'} {base}: "
                            f"{health['pass_rate']:.0f}% pass rate "
                            f"(configured {health['configured_min_atr']:.2f}, "
                            f"weekly avg ATR {health['baseline_atr']:.2f})")

    state['runtime']['atr_health'] = results
    state['runtime']['last_atr_health_check'] = now_utc_iso()
    save_state()

    if flagged:
        send_telegram(
            '📊 <b>Weekly ATR Review</b>\n' + '\n'.join(flagged) +
            '\n\nThese tickers\' min_atr may be stale — worth a manual look.'
        )
    else:
        logger.info('📊 Weekly ATR review: all tickers healthy, no flags')


# ── Weekly trade review — read-only self-analysis, no auto-tuning ──────────
# Same idea as the ATR health check above and the equivalent review already
# running on Sentinel: bucket every closed trade (now carrying rich entry
# context since record_closed_trade was enriched) by symbol/entry-type/
# side/exit-reason/hour/weekday/hold-time and flag whichever conditions are
# dragging down that symbol's win rate or losing money outright. Purely
# informational — a human decides what, if anything, to change.
TRADE_REVIEW_INTERVAL_DAYS = 7
TRADE_REVIEW_MIN_TRADES    = 10
TRADE_REVIEW_MIN_BUCKET_N  = 5

def _apex_bucket_stats(records: list, key_fn, min_n: int = TRADE_REVIEW_MIN_BUCKET_N) -> dict:
    buckets = {}
    for r in records:
        k = key_fn(r)
        if k is None:
            continue
        buckets.setdefault(k, []).append(r)
    stats = {}
    for k, rs in buckets.items():
        if len(rs) < min_n:
            continue
        n = len(rs)
        wins = len([r for r in rs if r.get('win')])
        net = sum(float(r.get('pnl', 0)) for r in rs)
        r_mults = [r['r_multiple'] for r in rs if r.get('r_multiple') is not None]
        avg_r = sum(r_mults) / len(r_mults) if r_mults else None
        stats[k] = {'n': n, 'win_rate': round(wins / n * 100, 1), 'net_pnl': round(net, 2),
                    'avg_r': round(avg_r, 2) if avg_r is not None else None}
    return stats

def _apex_hour_bucket(r):
    h = r.get('entry_hour_utc')
    if h is None: return None
    if 0 <= h < 6:   return 'night (00-06 UTC)'
    if 6 <= h < 12:  return 'morning (06-12 UTC)'
    if 12 <= h < 18: return 'afternoon (12-18 UTC)'
    return 'evening (18-24 UTC)'

def _apex_hold_bucket(r):
    hm = r.get('hold_minutes')
    if hm is None: return None
    if hm < 60:   return 'quick (<1h)'
    if hm < 240:  return 'medium (1-4h)'
    return 'long (4h+)'

def run_weekly_trade_review() -> None:
    last = state['runtime'].get('last_trade_review')
    if last:
        try:
            age_days = (datetime.now(timezone.utc) -
                        datetime.fromisoformat(last.replace('Z', '+00:00'))).days
        except Exception:
            age_days = TRADE_REVIEW_INTERVAL_DAYS
        if age_days < TRADE_REVIEW_INTERVAL_DAYS:
            return

    state['runtime']['last_trade_review'] = now_utc_iso()
    save_state()

    records = [t for t in load_trade_log() if not t.get('dust')]
    if not records:
        logger.info('📋 Weekly trade review: no trades logged yet')
        return

    for symbol in sorted(set(TRADING_SYMBOLS) & {r['symbol'] for r in records if r.get('symbol')}):
        sym_records = [r for r in records if r['symbol'] == symbol]
        n = len(sym_records)
        base = SYMBOLS_CONFIG.get(symbol, {}).get('base', symbol)
        if n < TRADE_REVIEW_MIN_TRADES:
            logger.info(f'📋 [{base}] Weekly trade review skipped — only {n} trades logged so far')
            continue

        overall_wr  = len([r for r in sym_records if r.get('win')]) / n * 100
        overall_net = sum(float(r.get('pnl', 0)) for r in sym_records)

        bucket_groups = {
            'Entry type':  _apex_bucket_stats(sym_records, lambda r: r.get('entry_type')),
            'Hour of day': _apex_bucket_stats(sym_records, _apex_hour_bucket),
            'Day of week': _apex_bucket_stats(sym_records, lambda r: r.get('entry_weekday')),
            'Side':        _apex_bucket_stats(sym_records, lambda r: r.get('side')),
            'Exit reason': _apex_bucket_stats(sym_records, lambda r: (r.get('exit_reason') or r.get('note') or '')[:20] or None),
            'Hold time':   _apex_bucket_stats(sym_records, _apex_hold_bucket),
        }

        flags = []
        for group_name, stats in bucket_groups.items():
            for bucket_name, s in stats.items():
                if s['win_rate'] < overall_wr - 15 or s['net_pnl'] < 0:
                    r_note = f", avgR={s['avg_r']:+.2f}" if s['avg_r'] is not None else ''
                    flags.append(f"  • {group_name} = {bucket_name}: {s['n']} trades, "
                                 f"{s['win_rate']}%W, net ${s['net_pnl']:+.2f}{r_note}")

        if flags:
            msg = (f'📊 <b>Weekly Trade Review — {base}</b>\n\n'
                   f'Overall: {n} trades, {overall_wr:.1f}% win rate, net ${overall_net:+.2f}\n\n'
                   f'Underperforming conditions worth a manual look:\n' + '\n'.join(flags))
        else:
            msg = (f'📊 <b>Weekly Trade Review — {base}</b>\n\n'
                   f'Overall: {n} trades, {overall_wr:.1f}% win rate, net ${overall_net:+.2f}\n'
                   f'No condition stands out as a consistent drag — looks healthy.')
        send_telegram(msg)
        logger.info(f'📋 [{base}] Weekly trade review sent ({len(flags)} flags)')


# ── Monthly consistency review — is the edge real or noise? ────────────────
# The weekly review above answers "which conditions are dragging on a
# symbol." This answers a different, coarser question: bucketing each
# symbol's trades by calendar month and checking whether net P&L is stable
# across months (a real edge) or wildly inconsistent (probably noise) --
# same idea as the Information Coefficient / ICIR concept from quant
# research (mean of a period metric divided by its stdev across periods).
# Below 0.3 is the same "probably noise" bar that framework uses. Also
# flags the specific pattern a bucketed weekly review can miss: a symbol
# with a large trade count and decent win rate that still nets near zero,
# which usually means the exit logic (trail stop / profit target) is
# giving back on winners what it saves on losers. Purely informational --
# no auto-tuning, a human decides what to change.
MONTHLY_REVIEW_INTERVAL_DAYS = 7   # check-in cadence is weekly; buckets are still calendar months
MONTHLY_REVIEW_MIN_MONTHS    = 2
MONTHLY_REVIEW_MIN_BUCKET_N  = 5
MONTHLY_REVIEW_MIN_TRADES    = 15
MONTHLY_REVIEW_ICIR_WEAK     = 0.3
MONTHLY_REVIEW_FLAT_MIN_N    = 30      # "large sample" threshold for the flat-despite-volume flag
MONTHLY_REVIEW_FLAT_PER_TRADE = 0.50   # avg $/trade below this (in absolute value) counts as "flat"

def run_monthly_strategy_review() -> None:
    last = state['runtime'].get('last_monthly_review')
    if last:
        try:
            age_days = (datetime.now(timezone.utc) -
                        datetime.fromisoformat(last.replace('Z', '+00:00'))).days
        except Exception:
            age_days = MONTHLY_REVIEW_INTERVAL_DAYS
        if age_days < MONTHLY_REVIEW_INTERVAL_DAYS:
            return

    state['runtime']['last_monthly_review'] = now_utc_iso()
    save_state()

    records = [t for t in load_trade_log() if not t.get('dust') and t.get('closed_at')]
    if not records:
        logger.info('📆 Monthly strategy review: no trades logged yet')
        return

    lines = ['📆 <b>Monthly Strategy Review</b>', '']
    any_symbol_reported = False

    for symbol in TRADING_SYMBOLS:
        sym_records = [r for r in records if r.get('symbol') == symbol]
        n = len(sym_records)
        base = SYMBOLS_CONFIG.get(symbol, {}).get('base', symbol)
        if n < MONTHLY_REVIEW_MIN_TRADES:
            continue

        by_month = {}
        for r in sym_records:
            by_month.setdefault(r['closed_at'][:7], []).append(r)
        month_nets = []
        for month, rs in sorted(by_month.items()):
            if len(rs) < MONTHLY_REVIEW_MIN_BUCKET_N:
                continue
            month_nets.append(sum(float(r.get('pnl', 0)) for r in rs))

        total_net  = sum(float(r.get('pnl', 0)) for r in sym_records)
        wins       = len([r for r in sym_records if r.get('win')])
        wr         = wins / n * 100
        per_trade  = total_net / n

        flags = []
        if total_net < 0:
            flags.append(f'🔴 Losing money overall: net ${total_net:+.2f} across {n} trades')

        if len(month_nets) >= MONTHLY_REVIEW_MIN_MONTHS:
            mean_net = statistics.mean(month_nets)
            stdev_net = statistics.stdev(month_nets) if len(month_nets) > 1 else 0
            icir = (mean_net / stdev_net) if stdev_net else None
            if icir is not None and icir < MONTHLY_REVIEW_ICIR_WEAK and total_net >= 0:
                flags.append(f'🟡 Inconsistent month to month (consistency score {icir:+.2f}, '
                             f'below {MONTHLY_REVIEW_ICIR_WEAK} = likely noise, not a stable edge)')

        if n >= MONTHLY_REVIEW_FLAT_MIN_N and abs(per_trade) < MONTHLY_REVIEW_FLAT_PER_TRADE and total_net >= 0:
            flags.append(f'🟠 Flat despite volume: {n} trades, {wr:.0f}% win rate, but only '
                         f'${per_trade:+.3f}/trade average — check whether exits are giving back '
                         f'winners\' gains (trail stop / profit target may need review)')

        if flags:
            any_symbol_reported = True
            lines.append(f'<b>{base}</b> — {n} trades, {wr:.1f}% WR, net ${total_net:+.2f}')
            lines.extend(f'  {f}' for f in flags)
            lines.append('')

    if not any_symbol_reported:
        logger.info('📆 Monthly strategy review: no symbol flagged, or not enough history yet')
        return

    send_telegram('\n'.join(lines))
    logger.info('📆 Monthly strategy review sent')


# ── Weekly auto-tune — same rigor as the manual TSLA fix, automated ────────
# The monthly review above only flags problems for a human to look at. This
# closes the loop: when a symbol is flagged, replay its real closed trades
# against actual historical Binance price data (same method used to validate
# the TSLA trail-activation fix on 2026-09-16), test candidate values for the
# three SL/trail parameters one at a time, and only deploy a change if it
# improves total P&L AND holds up in a strict majority of individual monthly
# buckets -- never react to one bad week or a single aggregate number, which
# is exactly the "confirm your bias" failure mode a single backtest produces.
# Because this applies without human sign-off, it carries a real safety net:
# every change is logged in full via Telegram, and a later run checks real
# (not replayed) performance since the change and auto-reverts if it's
# clearly worse -- an out-of-sample gate against genuinely new trades, not
# just the historical data the change was chosen on.
ENTRY_TUNE_ENABLED            = False  # gate: entry-signal backtest currently only checks
                                         # conditions at bar CLOSE, missing intra-hour polling
                                         # live trading actually does (confirmed on CRCL: found only
                                         # 3 of 14 real signals). Flip True once upgraded to sample
                                         # within each hour, not just at the close.
AUTO_TUNE_INTERVAL_DAYS       = 3   # decision cadence -- deliberately slower than the daily data/
                                     # report sync below, so one day's trades can't swing a real
                                     # parameter change (changed from 7->3 on user's explicit request
                                     # 2026-09-17, keeping decisions decoupled from daily reporting)
HERMES_SYNC_INTERVAL_DAYS     = 1   # push snapshot + pull/log Hermes's narrative report daily --
                                     # this is data/memory only, never applies a trading change itself
AUTO_TUNE_MIN_TRADES          = 10   # lowered from 50 -- don't let a new ticker bleed for months untuned
AUTO_TUNE_MIN_BUCKETS         = 3    # chronological chunks, NOT calendar months -- see _autotune_time_buckets
AUTO_TUNE_MIN_IMPROVEMENT_USD = 10.0
AUTO_TUNE_COOLDOWN_DAYS       = 21
AUTO_TUNE_ROLLBACK_MIN_TRADES = 10   # lowered from 15, in step with the lower min-trades bar -- a change
                                      # made on thinner data should be re-checked sooner, not later
AUTO_TUNE_CACHE_FILE          = os.path.join(
    os.path.dirname(BOT_STATE_FILE) or '.', 'auto_tune_replay_cache.json')

HARD_SL_CANDIDATES       = [0.75, 0.90, 1.00, 1.10, 1.25, 1.50]
TRAIL_ACTIVATE_CANDIDATES = [0.50, 0.60, 0.75, 0.90, 1.00, 1.15, 1.30, 1.50]
TRAIL_DIST_CANDIDATES     = [0.15, 0.20, 0.25, 0.30, 0.40]

# ── Hermes weekly bot-review: candidate suggestions, not direct config writes ──
# Hermes analyzes trade history + config + its own veto-call history once a
# week and proposes specific parameter values worth trying. Those proposals
# are added to the candidate lists above/below, NOT applied directly -- they
# still have to pass the exact same _autotune_qualifies() backtest-and-consistency
# bar as every hardcoded candidate before anything reaches live config. This
# is the whole point: an LLM's qualitative read can surface ideas the fixed
# grid wouldn't have tried, but the existing statistical validation (and the
# rollback gate after) stays the sole gate on what actually gets applied.
HERMES_SUGGESTIONS_URL = 'http://10.122.0.3:8787/suggestions'
HERMES_REPORT_URL      = 'http://10.122.0.3:8787/report'
HERMES_SNAPSHOT_URL    = 'http://10.122.0.3:8787/ingest_snapshot'
HERMES_REPORT_HISTORY_MAX = 60   # ~2 months of daily reports (analysis now runs daily, not weekly)
VALID_TUNABLE_PARAMS = {  # param: (min, max) -- sanity bounds, reject anything outside
    'hard_sl_atr':        (0.3, 3.0),
    'trail_activate_atr': (0.3, 3.0),
    'trail_dist_atr':     (0.1, 1.0),
    'rsi_long_min':       (10, 50),
    'rsi_long_max':       (50, 90),
    'rsi_short_min':      (10, 50),
    'rsi_short_max':      (50, 90),
    'pullback_zone_pct':  (0.005, 0.05),
}

def push_hermes_snapshot() -> None:
    """Best-effort push of pre-aggregated performance summaries (not raw trade
    dumps) + current effective config + auto-tune history to the Hermes
    droplet, so its weekly analysis has real data. Failure here must never
    affect trading -- log and move on.

    Sends SUMMARIES, not the full trade log: an earlier version pushed up to
    300 raw trade records (~120KB) and the analysis failed outright --
    "conversation context exceeded model limits" -- on the very first real
    test. Reuses the same _apex_bucket_stats() aggregation the existing
    weekly trade review already computes, which is both far smaller and
    better signal than raw JSON for an LLM to reason about."""
    try:
        records = [t for t in load_trade_log() if not t.get('dust')]
        symbols_snapshot = {}
        for symbol in TRADING_SYMBOLS:
            sym_records = [r for r in records if r.get('symbol') == symbol]
            cfg = {param: get_symbol_cfg(symbol, param, None) for param in VALID_TUNABLE_PARAMS}
            cfg.update({
                'leverage':          SYMBOLS_CONFIG[symbol].get('leverage'),
                'trade_amount':      SYMBOLS_CONFIG[symbol].get('trade_amount'),
                'ema_cross_enabled': SYMBOLS_CONFIG[symbol].get('ema_cross_enabled', False),
                'short_only':        SYMBOLS_CONFIG[symbol].get('short_only', False),
                'long_only':         SYMBOLS_CONFIG[symbol].get('long_only', False),
            })
            symbols_snapshot[symbol] = {
                'config': cfg,
                'performance': summarize_performance(sym_records) if sym_records else None,
                'by_entry_type': _apex_bucket_stats(sym_records, lambda r: r.get('entry_type')),
                'by_exit_reason': _apex_bucket_stats(
                    sym_records, lambda r: (r.get('exit_reason') or r.get('note') or '')[:20] or None),
                'by_hour':  _apex_bucket_stats(sym_records, _apex_hour_bucket),
                'by_hold':  _apex_bucket_stats(sym_records, _apex_hold_bucket),
                # A handful of concrete recent examples, not the full log --
                # trimmed to the fields actually useful for pattern-spotting.
                'recent_trades': [
                    {k: t.get(k) for k in ('opened_at', 'closed_at', 'side', 'entry_type',
                                            'exit_reason', 'pnl', 'win', 'r_multiple')}
                    for t in sorted(sym_records, key=lambda t: t.get('closed_at', ''), reverse=True)[:12]
                ],
            }
        mu_trades = state.get('overnight_mu_trades', [])
        payload = {
            'generated_at':         now_utc_iso(),
            'symbols':              symbols_snapshot,
            'auto_tune_history':    state['runtime'].get('auto_tune_history', [])[-30:],
            'auto_tune_overrides':  state['runtime'].get('auto_tune_overrides', {}),
            'recent_market_sentiment': _summarize_apex_regime_sentiment(days=7),
            'mu_overnight': {
                'config': {
                    'amount':   get_mu_cfg('amount', OVERNIGHT_CFG['amount']),
                    'leverage': get_mu_cfg('leverage', OVERNIGHT_CFG['leverage']),
                    'sl_pct':   OVERNIGHT_CFG['sl_pct'],
                },
                'performance': _overnight_mu_perf(mu_trades),
                'recent_trades': [
                    {k: t.get(k) for k in ('opened_at', 'closed_at', 'entry_price', 'exit_price',
                                            'qty', 'net', 'reason')}
                    for t in sorted(mu_trades, key=lambda t: t.get('closed_at', ''), reverse=True)[:12]
                ],
            },
        }
        resp = requests.post(HERMES_SNAPSHOT_URL, json=payload, timeout=15)
        resp.raise_for_status()
        logger.info(f'📤 Pushed snapshot to Hermes ({len(records)} trades summarized)')
    except Exception as e:
        logger.info(f'Hermes snapshot push failed (non-critical): {e}')

def pull_hermes_suggestions() -> dict:
    """Returns {symbol: [{'param', 'value', 'reasoning'}, ...]} from Hermes's most
    recent weekly analysis. Empty dict on any failure, missing data, or a
    suggestion that fails basic sanity checks -- fail-open, same as everywhere
    else in this integration: a broken/unreachable Hermes just means the
    auto-tuner runs exactly as it did before this feature existed."""
    out: dict = {}
    try:
        resp = requests.get(HERMES_SUGGESTIONS_URL, timeout=10)
        resp.raise_for_status()
        for s in resp.json().get('suggestions', []):
            symbol, param, value = s.get('symbol'), s.get('param'), s.get('value')
            if symbol not in TRADING_SYMBOLS or param not in VALID_TUNABLE_PARAMS:
                continue
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            lo, hi = VALID_TUNABLE_PARAMS[param]
            if not (lo <= value <= hi):
                logger.info(f'🤖 [{symbol}] Hermes suggested {param}={value}, outside sane bounds '
                            f'[{lo},{hi}] -- ignored')
                continue
            out.setdefault(symbol, []).append(
                {'param': param, 'value': value, 'reasoning': s.get('reasoning', '')})
    except Exception as e:
        logger.info(f'Hermes suggestions pull failed (non-critical): {e}')
    return out

def pull_and_log_hermes_report() -> None:
    """Pulls Hermes's plain-English weekly narrative (the same underlying
    analysis pull_hermes_suggestions() already reads for structured
    candidates, which has always discarded the narrative text). Appends to a
    capped local history so the dashboard can show it -- dedup'd by
    generated_at so re-polling the same not-yet-refreshed report doesn't
    create duplicate log entries. Fail-open: any failure just means no new
    entry this week, never a trading impact."""
    try:
        resp = requests.get(HERMES_REPORT_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        report, generated_at = data.get('report_summary'), data.get('generated_at')
        if not report:
            return
        history = state['runtime'].setdefault('hermes_report_history', [])
        if history and history[-1].get('generated_at') == generated_at:
            return  # already logged this exact report
        history.append({'generated_at': generated_at, 'report_summary': report})
        del history[:-HERMES_REPORT_HISTORY_MAX]
        save_state()
    except Exception as e:
        logger.info(f'Hermes report pull failed (non-critical): {e}')

def write_hermes_log_dashboard() -> None:
    """What Hermes has analyzed and what it's changed, for the dashboard's
    Hermes Log tab. 'changes' doubles as the health check the user asked
    for: every 'applied' entry is auto-re-checked against new real trades
    (see run_weekly_auto_tune's rollback logic) and shows up here as a
    'validated' (held up) or 'reverted' (didn't, auto-corrected) follow-up
    entry -- that before/after check IS the health check, just made visible."""
    try:
        write_json(os.path.join(WEB_ROOT, 'data_hermes_log.json'), {
            'updated_at':        now_utc_iso(),
            'report_history':    list(reversed(state['runtime'].get('hermes_report_history', []))),
            'changes':           list(reversed(state['runtime'].get('auto_tune_history', [])[-40:])),
            'current_overrides': state['runtime'].get('auto_tune_overrides', {}),
        })
    except Exception as e:
        logger.warning(f'write_hermes_log_dashboard: {e}')

def _autotune_fetch_klines(symbol: str, interval: str, start_ms: int, end_ms: int, limit: int = 1500) -> list:
    """Fetching a symbol's full trade history (dozens to 100+ trades, each
    needing an ATR window + a price-path window) can burst well past
    Binance's public rate limit -- caught in testing when TSLA's fetch got
    429'd into returning nothing right after NBIS's fetch used up the
    budget. Retry with backoff specifically on 429 rather than treating it
    like any other failure, since it's transient and recoverable."""
    out, cursor = [], start_ms
    while cursor < end_ms:
        batch = None
        for attempt in range(4):
            try:
                batch = binance_futures_public('/fapi/v1/klines', {
                    'symbol': symbol, 'interval': interval, 'limit': limit,
                    'startTime': cursor, 'endTime': end_ms,
                })
                break
            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code == 429 and attempt < 3:
                    time.sleep(3 * (attempt + 1))
                    continue
                raise
        if not batch:
            break
        out.extend(batch)
        cursor = batch[-1][0] + 1
        if len(batch) < limit:
            break
        time.sleep(0.25)
    return out

def _autotune_atr_at_entry(symbol: str, opened_at_iso: str) -> Optional[float]:
    try:
        entry_dt = datetime.fromisoformat(opened_at_iso.replace('Z', '+00:00'))
        entry_ms = int(entry_dt.timestamp() * 1000)
        klines = _autotune_fetch_klines(symbol, '1h', entry_ms - 16 * 3600 * 1000, entry_ms)
        klines = [k for k in klines if k[0] < entry_ms]
        if len(klines) < 14:
            return None
        ranges = [float(k[2]) - float(k[3]) for k in klines[-14:]]
        return sum(ranges) / len(ranges)
    except Exception as e:
        logger.warning(f'autotune_atr [{symbol}]: {e}')
        return None

def _autotune_price_path(symbol: str, opened_at_iso: str, closed_at_iso: str) -> list:
    try:
        od = datetime.fromisoformat(opened_at_iso.replace('Z', '+00:00'))
        cd = datetime.fromisoformat(closed_at_iso.replace('Z', '+00:00'))
        start_ms = int(od.timestamp() * 1000)
        end_ms   = int(cd.timestamp() * 1000) + 60000
        klines = _autotune_fetch_klines(symbol, '1m', start_ms, end_ms)
        return [[k[0], float(k[2]), float(k[3]), float(k[4])] for k in klines]  # time, high, low, close
    except Exception as e:
        logger.warning(f'autotune_path [{symbol}]: {e}')
        return []

def _autotune_load_cache() -> dict:
    return read_json(AUTO_TUNE_CACHE_FILE, {})

def _autotune_save_cache(cache: dict) -> None:
    write_json(AUTO_TUNE_CACHE_FILE, cache)

def _autotune_enrich_trades(symbol: str, trades: list) -> Tuple[list, int]:
    """Attach {atr, path} to each trade, fetching only what isn't already
    cached from a previous week's run -- avoids re-fetching a growing
    history's full price data every single week. Returns (enriched, failed)
    -- failed is tracked explicitly so a rate-limit storm or API outage
    shows up as a visible skip rather than silently shrinking the sample
    and letting the candidate search run on a truncated, non-random subset."""
    cache = _autotune_load_cache()
    sym_cache = cache.setdefault(symbol, {})
    enriched = []
    fetched_new = 0
    failed = 0
    for r in trades:
        # closed_at, not opened_at -- partial closes/scale-outs of the same
        # position share one opened_at, which caused an 8-trade cache
        # collision in testing (siblings silently reused each other's price
        # path). closed_at is unique per real trade record.
        key = r['closed_at']
        cached = sym_cache.get(key)
        if cached and cached.get('atr') is not None and cached.get('path'):
            enriched.append({'side': r['side'], 'entry_price': r['entry_price'],
                             'closed_at': r['closed_at'], 'atr': cached['atr'], 'path': cached['path']})
            continue
        atr = _autotune_atr_at_entry(symbol, r['opened_at'])
        if atr is None:
            failed += 1
            continue
        path = _autotune_price_path(symbol, r['opened_at'], r['closed_at'])
        if not path:
            failed += 1
            continue
        sym_cache[key] = {'atr': atr, 'path': path}
        enriched.append({'side': r['side'], 'entry_price': r['entry_price'],
                         'closed_at': r['closed_at'], 'atr': atr, 'path': path})
        fetched_new += 1
        time.sleep(0.3)  # throttle between trades, not just between pages within one trade's fetch
    if fetched_new:
        _autotune_save_cache(cache)
        logger.info(f'🔧 [{symbol}] auto-tune: fetched {fetched_new} new trade(s) of replay data'
                    + (f', {failed} failed' if failed else ''))
    return enriched, failed

def _autotune_simulate_exit(side: str, entry_price: float, atr: float, path: list,
                             hard_sl_mult: float, trail_activate_mult: float,
                             trail_dist_mult: float, max_loss_pct: float,
                             collateral: float, leverage: float) -> float:
    """Bar-by-bar replay matching check_sl_trail()'s exact math. Returns the
    simulated exit price."""
    is_long = side == 'LONG'
    qty = (collateral * leverage * 0.995) / entry_price if entry_price else 0
    hard_sl_dist = atr * hard_sl_mult
    max_loss_dollar = collateral * max_loss_pct
    max_loss_dist = (max_loss_dollar / qty) if qty > 0 else hard_sl_dist
    sl_dist = min(hard_sl_dist, max_loss_dist)
    sl = entry_price - sl_dist if is_long else entry_price + sl_dist
    activate_dist = atr * trail_activate_mult

    best = entry_price
    trail_active = False
    for _, hi, lo, close in path:
        if not trail_active:
            if (lo <= sl) if is_long else (hi >= sl):
                return sl
        fav = hi if is_long else lo
        if (is_long and fav > best) or (not is_long and fav < best):
            best = fav
        profit_dist = (best - entry_price) if is_long else (entry_price - best)
        if not trail_active and profit_dist >= activate_dist:
            trail_active = True
        if trail_active:
            dyn_dist = max(atr * trail_dist_mult, profit_dist * 0.18)
            trail_stop = best - dyn_dist if is_long else best + dyn_dist
            trail_stop = max(trail_stop, entry_price) if is_long else min(trail_stop, entry_price)
            adverse = lo if is_long else hi
            if (adverse <= trail_stop) if is_long else (adverse >= trail_stop):
                return trail_stop
    return path[-1][3] if path else entry_price

def _autotune_candidate_pnl(symbol: str, enriched: list, hard_sl_mult: float,
                             trail_activate_mult: float, trail_dist_mult: float) -> Tuple[float, dict]:
    """Returns (total_pnl, {bucket_index: pnl}) for this symbol's real trades
    replayed under the given candidate parameter set. Buckets are
    chronological chunks of the trade sequence, NOT calendar months -- a
    ticker with only 10-30 trades might all fall in the same 1-2 real
    calendar months, which would make a months-based consistency check
    impossible to pass regardless of how good the candidate is. Chunking by
    trade order instead scales down gracefully: 3-6 buckets regardless of
    how much wall-clock time the trades span."""
    collateral = SYMBOLS_CONFIG.get(symbol, {}).get('trade_amount', 30.0)
    leverage   = SYMBOLS_CONFIG.get(symbol, {}).get('leverage', 30)
    max_loss_pct = SYMBOLS_CONFIG.get(symbol, {}).get('max_loss_pct', 0.30)
    ordered = sorted(enriched, key=lambda d: d['closed_at'])
    n = len(ordered)
    num_buckets = min(6, max(AUTO_TUNE_MIN_BUCKETS, n // 10)) if n else 1
    bucket_size = max(1, n // num_buckets) if num_buckets else max(1, n)

    total = 0.0
    buckets: dict = {}
    for idx, d in enumerate(ordered):
        qty = (collateral * leverage * 0.995) / d['entry_price']
        exit_price = _autotune_simulate_exit(
            d['side'], d['entry_price'], d['atr'], d['path'],
            hard_sl_mult, trail_activate_mult, trail_dist_mult, max_loss_pct, collateral, leverage)
        fee = (d['entry_price'] + exit_price) * qty * FEE_RATE
        pnl = ((exit_price - d['entry_price']) if d['side'] == 'LONG' else (d['entry_price'] - exit_price)) * qty - fee
        total += pnl
        b = min(idx // bucket_size, num_buckets - 1)
        buckets[b] = buckets.get(b, 0.0) + pnl
    return total, buckets

def _autotune_qualifies(baseline_total: float, baseline_buckets: dict,
                         cand_total: float, cand_buckets: dict) -> bool:
    if cand_total < baseline_total + AUTO_TUNE_MIN_IMPROVEMENT_USD:
        return False
    buckets = sorted(set(baseline_buckets) & set(cand_buckets))
    if len(buckets) < AUTO_TUNE_MIN_BUCKETS:
        return False
    improved = sum(1 for b in buckets if cand_buckets[b] >= baseline_buckets[b])
    return improved > len(buckets) / 2


# ── Entry-signal auto-tune — a bigger, riskier problem than exit tuning ────
# Exit tuning replays REAL trades (fixed, known entries) -- a well-anchored
# question. Entry tuning has to ask "what NEW trades would have fired under
# a different threshold," which requires re-running the actual entry
# decision tree (get_decision) against historical bars, not just replaying
# known outcomes. Built to reuse the exact same indicator/decision code the
# live bot runs (_build_1h_indicators, _compute_4h_trend, get_decision
# itself) rather than a separate reimplementation, specifically to avoid
# the kind of drift bug that would be invisible until it silently produced
# wrong backtests.
ENTRY_BACKTEST_WARMUP_BARS   = 100   # matches production's get_market_data() window exactly
ENTRY_BACKTEST_4H_WINDOW     = 60    # matches production's get_4h_trend() window exactly
ENTRY_BACKTEST_MAX_HOLD_HRS  = 72    # cap a hypothetical trade's search window; real trades rarely run longer
ENTRY_BACKTEST_MAX_TRADES    = 150   # safety cap -- a badly-loosened candidate could otherwise fire constantly

RSI_LONG_MIN_CANDIDATES  = [25, 30, 35]
RSI_LONG_MAX_CANDIDATES  = [58, 62, 66]
RSI_SHORT_MIN_CANDIDATES = [34, 38, 42]
RSI_SHORT_MAX_CANDIDATES = [66, 70, 74]
PULLBACK_ZONE_CANDIDATES = [0.012, 0.018, 0.024]

def _autotune_entry_history(symbol: str, since_ms: int) -> Optional[Tuple[list, list, dict]]:
    """Fetch 1H, 4H, and 1-minute klines covering since_ms through now, with
    enough 1H/4H lookback before since_ms for indicator warmup. The 1-minute
    data only needs to cover the actually-tested window (since_ms onward,
    plus a small buffer for boundary alignment), not the warmup period --
    it's used to reconstruct the still-forming candle at each intra-hour
    checkpoint. Returns (klines_1h, klines_4h, klines_1m_by_hour) or None."""
    now_ms = int(time.time() * 1000)
    start_1h = since_ms - ENTRY_BACKTEST_WARMUP_BARS * 3600 * 1000
    start_4h = since_ms - ENTRY_BACKTEST_4H_WINDOW * 4 * 3600 * 1000
    start_1m = since_ms - 3600 * 1000
    try:
        klines_1h = _autotune_fetch_klines(symbol, '1h', start_1h, now_ms)
        klines_4h = _autotune_fetch_klines(symbol, '4h', start_4h, now_ms)
        if len(klines_1h) < ENTRY_BACKTEST_WARMUP_BARS + 10 or len(klines_4h) < ENTRY_BACKTEST_4H_WINDOW:
            return None
        klines_1m = _autotune_fetch_klines(symbol, '1m', start_1m, now_ms)
        klines_1m_by_hour = _autotune_group_1m_by_hour(klines_1m, klines_1h)
        return klines_1h, klines_4h, klines_1m_by_hour
    except Exception as e:
        logger.warning(f'autotune_entry_history [{symbol}]: {e}')
        return None

def _autotune_4h_trend_series(klines_4h: list) -> list:
    """(close_time_ms, trend_label) for every 4H bar with enough history
    behind it, each computed from a trailing ENTRY_BACKTEST_4H_WINDOW-bar
    slice ending at that bar -- same rolling window production uses, so
    each value is a faithful point-in-time trend, not a lookahead."""
    out = []
    for j in range(ENTRY_BACKTEST_4H_WINDOW - 1, len(klines_4h)):
        window = klines_4h[j - ENTRY_BACKTEST_4H_WINDOW + 1: j + 1]
        df4 = _4h_klines_to_df(window)
        try:
            trend4, _, _, _ = _compute_4h_trend(df4)
        except Exception:
            trend4 = '4H NEUTRAL'
        out.append((klines_4h[j][6], trend4))  # index 6 = close_time
    return out

def _autotune_lookup_4h_trend(trend_series: list, ts_ms: int) -> str:
    result = '4H NEUTRAL'
    for close_time, trend in trend_series:
        if close_time > ts_ms:
            break
        result = trend
    return result

def _autotune_group_1m_by_hour(klines_1m: list, klines_1h: list) -> dict:
    """hour_open_time_ms -> sorted list of 1m klines within that hour. Single
    pass; both inputs are already chronologically sorted."""
    hour_opens = [k[0] for k in klines_1h]
    grouped = {h: [] for h in hour_opens}
    hi, n_hours = 0, len(hour_opens)
    for k in klines_1m:
        t = k[0]
        while hi + 1 < n_hours and hour_opens[hi + 1] <= t:
            hi += 1
        if hour_opens[hi] <= t < hour_opens[hi] + 3600000:
            grouped[hour_opens[hi]].append(k)
    return grouped

def _autotune_synthetic_bar(bars_in_window: list, open_time: int, open_price: float) -> Optional[list]:
    """Aggregate 1-minute bars seen so far within an hour into what a live
    poll's still-forming 1H candle would show at that moment -- same shape
    Binance's own klines return, so _build_1h_indicators can't tell the
    difference. This is the core of the intra-hour fix: production polls
    continuously and reacts to this partial candle; the original replay only
    ever saw the fully-closed bar, missing most of what live trading catches."""
    if not bars_in_window:
        return None
    highs = [float(k[2]) for k in bars_in_window]
    lows  = [float(k[3]) for k in bars_in_window]
    close = float(bars_in_window[-1][4])
    vol   = sum(float(k[5]) for k in bars_in_window)
    return [open_time, open_price, max(highs), min(lows), close, vol, bars_in_window[-1][0], 0, 0, 0, 0, 0]

ENTRY_BACKTEST_CHECKPOINTS_MIN = list(range(5, 61, 5))  # every 5 min into the hour to
    # poll -- mimics live trading's continuous polling reacting to the still-
    # forming candle, instead of only ever checking the fully-closed bar. 60
    # uses the real closed bar directly (exact match to the original method).

def _autotune_replay_entries(symbol: str, klines_1h: list, klines_1m_by_hour: dict,
                              trend4h_series: list, entry_overrides: dict, hard_sl_mult: float,
                              trail_activate_mult: float, trail_dist_mult: float) -> list:
    """Walk the 1H bars, running the REAL get_decision() with entry_overrides
    temporarily applied via the SAME runtime-override layer get_symbol_cfg()
    reads (NOT SYMBOLS_CONFIG directly -- get_symbol_cfg always prefers a
    live runtime override over SYMBOLS_CONFIG, so mutating SYMBOLS_CONFIG
    here would be silently ignored for any symbol that already has an
    earlier auto-tuned entry override in effect).

    Within each hour, checks several intra-hour checkpoints (a synthetic
    still-forming candle built from 1-minute data) before falling back to
    the fully-closed bar -- the original bar-close-only version was tested
    on CRCL and found only 3 of 14 real signals, because live trading polls
    continuously within the hour and this didn't. Takes the FIRST checkpoint
    that fires, matching how live trading would have caught it as soon as
    the condition was true, not only once the hour fully closed.

    Enforces one-position-at-a-time, same as live trading. Returns a list of
    {side, entry_price, pnl, closed_at} dicts."""
    live_overrides = state['runtime'].setdefault('auto_tune_overrides', {}).setdefault(symbol, {})
    had_key = {k: (k in live_overrides) for k in entry_overrides}
    original = {k: live_overrides.get(k) for k in entry_overrides}
    live_overrides.update(entry_overrides)
    collateral = SYMBOLS_CONFIG[symbol].get('trade_amount', 30.0)
    leverage   = SYMBOLS_CONFIG[symbol].get('leverage', 30)
    max_loss_pct = SYMBOLS_CONFIG[symbol].get('max_loss_pct', 0.30)

    trades = []
    try:
        i = ENTRY_BACKTEST_WARMUP_BARS
        n = len(klines_1h)
        while i < n and len(trades) < ENTRY_BACKTEST_MAX_TRADES:
            hour_open_time  = klines_1h[i][0]
            hour_open_price = float(klines_1h[i][1])
            bars_this_hour  = klines_1m_by_hour.get(hour_open_time, [])
            prior_closed    = klines_1h[i - ENTRY_BACKTEST_WARMUP_BARS + 1: i]

            fired = None
            for cp_min in ENTRY_BACKTEST_CHECKPOINTS_MIN:
                if cp_min == 60:
                    synth = klines_1h[i]
                    cp_time = klines_1h[i][6]
                else:
                    cp_time = hour_open_time + cp_min * 60000
                    bars_upto = [b for b in bars_this_hour if b[0] < cp_time]
                    synth = _autotune_synthetic_bar(bars_upto, hour_open_time, hour_open_price)
                    if synth is None:
                        continue
                window = prior_closed + [synth]
                if len(window) < ENTRY_BACKTEST_WARMUP_BARS:
                    continue
                df = _build_1h_indicators(window)
                if df['atr'].isna().iloc[-1] or df['rsi'].isna().iloc[-1]:
                    continue
                trend4h = _autotune_lookup_4h_trend(trend4h_series, cp_time)
                try:
                    decision = get_decision(symbol, df, trend4h_override=trend4h)
                except Exception:
                    continue
                if decision['action'] in ('LONG', 'SHORT'):
                    # get_decision() itself doesn't gate on market hours --
                    # production checks this separately before ACTING on a
                    # decision (see run_symbol()). Replicate that gate here,
                    # or a hypothetical "entry" outside real trading hours
                    # blocks the one-position slot for however long it takes
                    # to resolve, starving genuine market-hours opportunities
                    # the real bot would have caught instead. This was the
                    # actual cause of the 3-vs-14 gap found testing on CRCL,
                    # not checkpoint granularity.
                    cp_dt = datetime.fromtimestamp(cp_time / 1000, tz=timezone.utc)
                    if SYMBOLS_CONFIG[symbol].get('market_hours_only') and not is_us_market_open(cp_dt):
                        continue
                    if not in_entry_window(SYMBOLS_CONFIG[symbol], cp_dt):
                        continue
                    fired = (decision, float(synth[4]), float(df['atr'].iloc[-1]), cp_time)
                    break

            if fired is None:
                i += 1
                continue

            decision, entry_price, atr, entry_time_ms = fired
            entry_dt = datetime.fromtimestamp(entry_time_ms / 1000, tz=timezone.utc)
            exit_search_end = entry_dt + timedelta(hours=ENTRY_BACKTEST_MAX_HOLD_HRS)
            path = _autotune_price_path(symbol, entry_dt.isoformat(), exit_search_end.isoformat())
            if not path:
                i += 1
                continue
            exit_price = _autotune_simulate_exit(
                decision['action'], entry_price, atr, path,
                hard_sl_mult, trail_activate_mult, trail_dist_mult, max_loss_pct, collateral, leverage)
            qty = (collateral * leverage * 0.995) / entry_price
            fee = (entry_price + exit_price) * qty * FEE_RATE
            pnl = ((exit_price - entry_price) if decision['action'] == 'LONG'
                   else (entry_price - exit_price)) * qty - fee
            exit_time_ms = path[-1][0]
            for t, hi, lo, close in path:
                is_long = decision['action'] == 'LONG'
                # cheap re-check: first bar whose extreme matches the recorded
                # exit price approximates when the exit actually happened
                if abs((hi if is_long else lo) - exit_price) < 1e-9 or abs((lo if is_long else hi) - exit_price) < 1e-9:
                    exit_time_ms = t
                    break
            trades.append({
                'side': decision['action'], 'entry_price': entry_price, 'pnl': pnl,
                'closed_at': datetime.fromtimestamp(exit_time_ms / 1000, tz=timezone.utc).isoformat(),
            })
            # advance i to the first 1H bar at/after the exit -- don't scan for a
            # new entry while "in" this hypothetical position
            while i < n and klines_1h[i][6] < exit_time_ms:
                i += 1
        return trades
    finally:
        for k in entry_overrides:
            if had_key[k]:
                live_overrides[k] = original[k]
            else:
                live_overrides.pop(k, None)

def _autotune_entry_candidate_pnl(trades: list) -> Tuple[float, dict]:
    """Same chronological-chunk bucketing as _autotune_candidate_pnl, applied
    to a list of hypothetical entry-replay trades instead of real ones."""
    ordered = sorted(trades, key=lambda d: d['closed_at'])
    n = len(ordered)
    num_buckets = min(6, max(AUTO_TUNE_MIN_BUCKETS, n // 10)) if n else 1
    bucket_size = max(1, n // num_buckets) if num_buckets else max(1, n)
    total = 0.0
    buckets: dict = {}
    for idx, d in enumerate(ordered):
        total += d['pnl']
        b = min(idx // bucket_size, num_buckets - 1)
        buckets[b] = buckets.get(b, 0.0) + d['pnl']
    return total, buckets

def run_daily_hermes_sync() -> None:
    """Daily data/memory sync -- push a fresh snapshot and log whatever
    narrative report Hermes's most recent (now-daily) analysis produced.
    Deliberately never applies a trading change itself; that stays on
    run_weekly_auto_tune()'s slower, evidence-gated cadence. Split out
    2026-09-17 so 'Hermes remembers market sentiment/signal behavior daily'
    doesn't also mean 'trading parameters change daily' -- those are
    different risk profiles and were kept decoupled on purpose."""
    last = state['runtime'].get('last_hermes_sync')
    if last:
        try:
            age_days = (datetime.now(timezone.utc) -
                        datetime.fromisoformat(last.replace('Z', '+00:00'))).days
        except Exception:
            age_days = HERMES_SYNC_INTERVAL_DAYS
        if age_days < HERMES_SYNC_INTERVAL_DAYS:
            return
    state['runtime']['last_hermes_sync'] = now_utc_iso()
    save_state()
    try:
        push_hermes_snapshot()
        pull_and_log_hermes_report()
        write_hermes_log_dashboard()
    except Exception as e:
        logger.warning(f'run_daily_hermes_sync failed (non-critical): {e}')

def run_weekly_auto_tune() -> None:
    last = state['runtime'].get('last_auto_tune')
    if last:
        try:
            age_days = (datetime.now(timezone.utc) -
                        datetime.fromisoformat(last.replace('Z', '+00:00'))).days
        except Exception:
            age_days = AUTO_TUNE_INTERVAL_DAYS
        if age_days < AUTO_TUNE_INTERVAL_DAYS:
            return

    # This runs synchronously inside run_once(), and fetching a symbol's full
    # trade history (dozens to 100+ trades) can take minutes even with
    # rate-limit backoff -- measured ~3 min end-to-end in testing. That would
    # leave any currently-open position unmonitored (no SL/trail checks) for
    # the whole duration. Defer to the next cycle (~30s later) rather than
    # consume this week's slot -- cheap to retry, and it'll run as soon as
    # everything is flat again.
    if any(sym_state(sym).get('position') for sym in TRADING_SYMBOLS):
        logger.info('🔧 auto-tune: deferred -- a position is open, will retry next cycle')
        return

    state['runtime']['last_auto_tune'] = now_utc_iso()
    overrides = state['runtime'].setdefault('auto_tune_overrides', {})
    history    = state['runtime'].setdefault('auto_tune_history', [])
    save_state()

    # Pull whatever Hermes's MOST RECENT analysis produced for use in THIS
    # decision -- the snapshot push and narrative-report logging now happen
    # daily via run_daily_hermes_sync(), decoupled from this slower decision
    # cadence (see that function's docstring for why).
    hermes_suggestions = pull_hermes_suggestions()
    if hermes_suggestions:
        logger.info(f'🤖 Hermes suggestions available for: {", ".join(hermes_suggestions)}')

    all_records = [t for t in load_trade_log() if not t.get('dust') and t.get('closed_at')
                   and t.get('opened_at') and t.get('entry_price') and t.get('side')]

    # ── Rollback check: does an existing override hold up on new real trades? ──
    # overrides[symbol] is {param_name: value, '_meta': {...}} -- the param_name
    # key is what get_symbol_cfg() actually reads at runtime; '_meta' is
    # bookkeeping only. (Storing param/value as literal dict keys 'param' and
    # 'value' instead of the real param name was a real bug found 2026-09-16:
    # get_symbol_cfg's `if key in override` could never match, so an "applied"
    # change silently never took effect. Fixed here; see
    # project_loop_engineering_framework memory for the incident.)
    for symbol, ov in list(overrides.items()):
        meta = ov.get('_meta', {})
        applied_at = meta.get('applied_at')
        if not applied_at:
            continue
        new_trades = [r for r in all_records if r['symbol'] == symbol and r['opened_at'] > applied_at]
        if len(new_trades) < AUTO_TUNE_ROLLBACK_MIN_TRADES:
            continue
        new_avg = sum(r['pnl'] for r in new_trades) / len(new_trades)
        baseline_avg = meta.get('baseline_avg_pnl', 0)
        new_total = sum(r['pnl'] for r in new_trades)
        base = SYMBOLS_CONFIG.get(symbol, {}).get('base', symbol)
        if new_total < 0 and new_avg < baseline_avg:
            reverted = overrides.pop(symbol)
            reverted_meta = reverted.get('_meta', {})
            history.append({'symbol': symbol, 'action': 'reverted', 'params': reverted,
                            'at': now_utc_iso(), 'reason': f'{len(new_trades)} new trades averaged '
                            f'${new_avg:+.2f}/trade (worse than ${baseline_avg:+.2f}/trade before the change)'})
            save_state()
            send_telegram(
                f'↩️ <b>Auto-Tune Reverted — {base}</b>\n\n'
                f'The {reverted_meta.get("param")}={reverted_meta.get("value")} change from '
                f'{applied_at[:10]} did not hold up: {len(new_trades)} real trades since then '
                f'averaged ${new_avg:+.2f}/trade (net ${new_total:+.2f}), worse than the '
                f'${baseline_avg:+.2f}/trade baseline it was meant to beat. Reverted to the prior value.'
            )
            logger.info(f'↩️ [{base}] auto-tune reverted — did not hold up out-of-sample')
        else:
            history.append({'symbol': symbol, 'action': 'validated', 'params': ov,
                            'at': now_utc_iso(), 'note': f'{len(new_trades)} new trades averaged ${new_avg:+.2f}/trade'})
            save_state()
            logger.info(f'✅ [{symbol}] auto-tune change validated on {len(new_trades)} new real trades')

    # ── Look for a new change to make ───────────────────────────────────────
    for symbol in TRADING_SYMBOLS:
        time.sleep(2)  # extra spacing between symbols on top of the per-trade throttle in enrich
        base = SYMBOLS_CONFIG.get(symbol, {}).get('base', symbol)
        existing = overrides.get(symbol)
        if existing:
            existing = existing.get('_meta', existing)  # tolerate either shape defensively
            applied_days_ago = (datetime.now(timezone.utc) -
                                 datetime.fromisoformat(existing['applied_at'].replace('Z', '+00:00'))).days
            if applied_days_ago < AUTO_TUNE_COOLDOWN_DAYS:
                continue  # give a recent change time to be evaluated before touching this symbol again

        sym_records = [r for r in all_records if r['symbol'] == symbol]
        if len(sym_records) < AUTO_TUNE_MIN_TRADES:
            continue

        cur_hard_sl   = get_symbol_cfg(symbol, 'hard_sl_atr', HARD_SL_ATR)
        cur_activate  = get_symbol_cfg(symbol, 'trail_activate_atr', TRAIL_ACTIVATE_ATR)
        cur_trail_dst = get_symbol_cfg(symbol, 'trail_dist_atr', 0.25)
        cur_rsi_lmin  = get_symbol_cfg(symbol, 'rsi_long_min', RSI_LONG_MIN)
        cur_rsi_lmax  = get_symbol_cfg(symbol, 'rsi_long_max', RSI_LONG_MAX)
        cur_rsi_smin  = get_symbol_cfg(symbol, 'rsi_short_min', RSI_SHORT_MIN)
        cur_rsi_smax  = get_symbol_cfg(symbol, 'rsi_short_max', RSI_SHORT_MAX)
        cur_pullback  = get_symbol_cfg(symbol, 'pullback_zone_pct', PULLBACK_ZONE_PCT)
        cur_values = {'hard_sl_atr': cur_hard_sl, 'trail_activate_atr': cur_activate,
                     'trail_dist_atr': cur_trail_dst, 'rsi_long_min': cur_rsi_lmin,
                     'rsi_long_max': cur_rsi_lmax, 'rsi_short_min': cur_rsi_smin,
                     'rsi_short_max': cur_rsi_smax, 'pullback_zone_pct': cur_pullback}

        enriched, failed = _autotune_enrich_trades(symbol, sym_records)
        if failed > 0 and failed >= len(sym_records) * 0.2:
            # A high failure rate (rate limiting, API outage) would make the
            # replay run on a small, non-random subset -- skip this symbol
            # this cycle rather than draw a conclusion from bad data, and
            # say so out loud instead of silently doing nothing.
            base = SYMBOLS_CONFIG.get(symbol, {}).get('base', symbol)
            logger.warning(f'🔧 [{base}] auto-tune: {failed}/{len(sym_records)} trades failed to '
                          f'fetch replay data -- skipping this cycle, will retry next week')
            send_telegram(f'⚠️ <b>Auto-Tune Skipped — {base}</b>\n'
                         f'{failed}/{len(sym_records)} trades failed to fetch price history '
                         f'(likely rate-limited) -- skipping this week, will retry next cycle.')
            continue
        if len(enriched) < AUTO_TUNE_MIN_TRADES:
            continue

        baseline_total, baseline_buckets = _autotune_candidate_pnl(
            symbol, enriched, cur_hard_sl, cur_activate, cur_trail_dst)
        if len(baseline_buckets) < AUTO_TUNE_MIN_BUCKETS:
            continue
        if baseline_total >= 0 and len(baseline_buckets) >= AUTO_TUNE_MIN_BUCKETS:
            bucket_vals = sorted(baseline_buckets.values())
            mean_b = statistics.mean(bucket_vals)
            stdev_b = statistics.stdev(bucket_vals) if len(bucket_vals) > 1 else 0
            icir = (mean_b / stdev_b) if stdev_b else None
            if icir is not None and icir >= MONTHLY_REVIEW_ICIR_WEAK:
                continue  # not flagged -- profitable and consistent, leave it alone

        sym_hermes_sugg = hermes_suggestions.get(symbol, [])
        hermes_reasoning_by_key = {(s['param'], s['value']): s['reasoning'] for s in sym_hermes_sugg}
        hermes_exit_extra = (
            [('hard_sl_atr', s['value'], s['value'], cur_activate, cur_trail_dst)
             for s in sym_hermes_sugg if s['param'] == 'hard_sl_atr' and s['value'] != cur_hard_sl] +
            [('trail_activate_atr', s['value'], cur_hard_sl, s['value'], cur_trail_dst)
             for s in sym_hermes_sugg if s['param'] == 'trail_activate_atr' and s['value'] != cur_activate] +
            [('trail_dist_atr', s['value'], cur_hard_sl, cur_activate, s['value'])
             for s in sym_hermes_sugg if s['param'] == 'trail_dist_atr' and s['value'] != cur_trail_dst]
        )
        candidates = (
            [('hard_sl_atr', v, v, cur_activate, cur_trail_dst) for v in HARD_SL_CANDIDATES if v != cur_hard_sl] +
            [('trail_activate_atr', v, cur_hard_sl, v, cur_trail_dst) for v in TRAIL_ACTIVATE_CANDIDATES if v != cur_activate] +
            [('trail_dist_atr', v, cur_hard_sl, cur_activate, v) for v in TRAIL_DIST_CANDIDATES if v != cur_trail_dst] +
            hermes_exit_extra
        )
        best = None
        for param_name, value, hs, ta, td in candidates:
            cand_total, cand_buckets = _autotune_candidate_pnl(symbol, enriched, hs, ta, td)
            if _autotune_qualifies(baseline_total, baseline_buckets, cand_total, cand_buckets):
                improvement = cand_total - baseline_total
                if best is None or improvement > best['improvement']:
                    best = {'param': param_name, 'value': value, 'improvement': improvement,
                            'cand_total': cand_total, 'cand_buckets': cand_buckets,
                            'hermes_reasoning': hermes_reasoning_by_key.get((param_name, value))}

        entry_search_ran = False
        if best is None and ENTRY_TUNE_ENABLED:
            logger.info(f'🔧 [{base}] auto-tune: reviewed {len(enriched)} trades, no consistent exit '
                       f'improvement found across {len(HARD_SL_CANDIDATES)+len(TRAIL_ACTIVATE_CANDIDATES)+len(TRAIL_DIST_CANDIDATES)} candidates -- '
                       f'trying entry-signal parameters (bigger overfitting risk, only reached when exit tuning finds nothing)')
            entry_search_ran = True
            since_ms = int(datetime.fromisoformat(min(r['opened_at'] for r in sym_records).replace('Z', '+00:00')).timestamp() * 1000)
            hist = _autotune_entry_history(symbol, since_ms)
            if hist is None:
                logger.info(f'🔧 [{base}] auto-tune: not enough price history for entry-signal backtest, skipping')
                continue
            klines_1h, klines_4h, klines_1m_by_hour = hist
            trend4h_series = _autotune_4h_trend_series(klines_4h)
            entry_baseline_trades = _autotune_replay_entries(
                symbol, klines_1h, klines_1m_by_hour, trend4h_series, {}, cur_hard_sl, cur_activate, cur_trail_dst)
            entry_baseline_total, entry_baseline_buckets = _autotune_entry_candidate_pnl(entry_baseline_trades)
            if len(entry_baseline_trades) >= AUTO_TUNE_MIN_TRADES and len(entry_baseline_buckets) >= AUTO_TUNE_MIN_BUCKETS:
                hermes_entry_extra = [
                    (s['param'], s['value'], {s['param']: s['value']}) for s in sym_hermes_sugg
                    if s['param'] in ('rsi_long_min', 'rsi_long_max', 'rsi_short_min',
                                      'rsi_short_max', 'pullback_zone_pct')
                ]
                entry_candidates = (
                    [('rsi_long_min', v, {'rsi_long_min': v}) for v in RSI_LONG_MIN_CANDIDATES if v != cur_rsi_lmin] +
                    [('rsi_long_max', v, {'rsi_long_max': v}) for v in RSI_LONG_MAX_CANDIDATES if v != cur_rsi_lmax] +
                    [('rsi_short_min', v, {'rsi_short_min': v}) for v in RSI_SHORT_MIN_CANDIDATES if v != cur_rsi_smin] +
                    [('rsi_short_max', v, {'rsi_short_max': v}) for v in RSI_SHORT_MAX_CANDIDATES if v != cur_rsi_smax] +
                    [('pullback_zone_pct', v, {'pullback_zone_pct': v}) for v in PULLBACK_ZONE_CANDIDATES if v != cur_pullback] +
                    hermes_entry_extra
                )
                for param_name, value, ov in entry_candidates:
                    cand_trades = _autotune_replay_entries(
                        symbol, klines_1h, klines_1m_by_hour, trend4h_series, ov, cur_hard_sl, cur_activate, cur_trail_dst)
                    cand_total, cand_buckets = _autotune_entry_candidate_pnl(cand_trades)
                    if _autotune_qualifies(entry_baseline_total, entry_baseline_buckets, cand_total, cand_buckets):
                        improvement = cand_total - entry_baseline_total
                        if best is None or improvement > best['improvement']:
                            best = {'param': param_name, 'value': value, 'improvement': improvement,
                                    'cand_total': cand_total, 'cand_buckets': cand_buckets, 'is_entry': True,
                                    'baseline_total': entry_baseline_total, 'baseline_buckets': entry_baseline_buckets,
                                    'n_trades': len(entry_baseline_trades),
                                    'hermes_reasoning': hermes_reasoning_by_key.get((param_name, value))}
            else:
                logger.info(f'🔧 [{base}] auto-tune: only {len(entry_baseline_trades)} hypothetical entries in '
                           f'backtest, not enough to evaluate entry-signal candidates')

        if best is None:
            logger.info(f'🔧 [{base}] auto-tune: no consistent improvement found in exit'
                       f'{" or entry" if entry_search_ran else ""} parameters')
            continue

        is_entry = best.get('is_entry', False)
        is_hermes = bool(best.get('hermes_reasoning'))
        eff_baseline_total = best.get('baseline_total', baseline_total)
        eff_baseline_buckets = best.get('baseline_buckets', baseline_buckets)
        eff_n = best.get('n_trades', len(enriched))

        applied_at = now_utc_iso()
        baseline_avg_pnl = eff_baseline_total / eff_n if eff_n else 0
        overrides[symbol] = {
            best['param']: best['value'],
            '_meta': {'param': best['param'], 'value': best['value'], 'applied_at': applied_at,
                      'baseline_avg_pnl': baseline_avg_pnl, 'is_entry_param': is_entry,
                      'source': 'hermes' if is_hermes else 'grid'},
        }
        history.append({'symbol': symbol, 'action': 'applied', 'params': overrides[symbol],
                        'at': applied_at, 'baseline_total': round(eff_baseline_total, 2),
                        'candidate_total': round(best['cand_total'], 2),
                        'source': 'hermes' if is_hermes else 'grid'})
        save_state()

        bucket_lines = '\n'.join(
            f'    chunk {b+1}: ${eff_baseline_buckets.get(b,0):+.2f} → ${best["cand_buckets"].get(b,0):+.2f}'
            for b in sorted(set(eff_baseline_buckets) | set(best['cand_buckets'])))
        n_trades_note = f' ({eff_n} trades -- thin sample, watch the rollback check closely)' \
            if eff_n < 50 else ''
        kind_note = '🎯 <b>Entry-signal change</b> (bigger overfitting risk than exit tuning)' if is_entry \
            else '🛑 Exit-parameter change'
        hermes_note = f'\n🤖 <b>Hermes-suggested candidate</b> — {best["hermes_reasoning"]}\n' if is_hermes else ''
        send_telegram(
            f'🔧 <b>Auto-Tune Applied — {base}</b>{n_trades_note}\n\n'
            f'{kind_note}{hermes_note}\n'
            f'Replayed {eff_n} {"hypothetical entries from a historical bar-by-bar re-run of the entry logic" if is_entry else "real trades"} '
            f'against actual price history. '
            f'Changing <b>{best["param"]}</b>: {cur_values.get(best["param"])} → <b>{best["value"]}</b>\n\n'
            f'Replayed P&L: ${eff_baseline_total:+.2f} → ${best["cand_total"]:+.2f} '
            f'(+${best["improvement"]:.2f}), improved in a majority of chronological chunks tested:\n{bucket_lines}\n\n'
            f'⚠️ This is a replay estimate, not a guarantee — real performance since this change will be '
            f'checked automatically after {AUTO_TUNE_ROLLBACK_MIN_TRADES} new trades, and reverted if it '
            f"doesn't hold up."
        )
        logger.info(f'🔧 [{base}] auto-tune applied: {best["param"]}={best["value"]} '
                    f'(replayed +${best["improvement"]:.2f}, entry_param={is_entry}, '
                    f'source={"hermes" if is_hermes else "grid"})')

    write_hermes_log_dashboard()


def get_decision(symbol: str, df: pd.DataFrame, trend4h_override: Optional[str] = None) -> dict:
    c = df.iloc[-1]
    p = df.iloc[-2]
    adx     = float(c['adx'])
    adx_pos = float(c['adx_pos'])
    adx_neg = float(c['adx_neg'])
    ema21   = float(c['ema21'])
    ema50   = float(c['ema50'])
    rsi     = float(c['rsi'])
    atr     = float(c['atr'])
    price   = float(c['close'])
    hi      = float(c['high'])
    lo      = float(c['low'])
    vol     = float(c['volume'])
    vol_ma  = float(c['vol_ma']) if not pd.isna(c['vol_ma']) else vol
    dc_upper = float(c['dc_upper']) if not pd.isna(c['dc_upper']) else None
    dc_lower = float(c['dc_lower']) if not pd.isna(c['dc_lower']) else None
    min_atr = SYMBOLS_CONFIG[symbol]['min_atr']
    cfg = SYMBOLS_CONFIG[symbol]
    # Routed through get_symbol_cfg (not cfg.get directly) so a live
    # auto-tune override actually takes effect here -- these are the entry
    # parameters run_weekly_auto_tune() can tune, same mechanism as the
    # SL/trail params in check_sl_trail().
    rsi_long_min      = get_symbol_cfg(symbol, 'rsi_long_min',      RSI_LONG_MIN)
    rsi_long_max      = get_symbol_cfg(symbol, 'rsi_long_max',      RSI_LONG_MAX)
    rsi_short_min     = get_symbol_cfg(symbol, 'rsi_short_min',     RSI_SHORT_MIN)
    rsi_short_max     = get_symbol_cfg(symbol, 'rsi_short_max',     RSI_SHORT_MAX)
    pullback_zone_pct = get_symbol_cfg(symbol, 'pullback_zone_pct', PULLBACK_ZONE_PCT)

    snap = {
        'adx':     round(adx, 2),
        'adx_pos': round(adx_pos, 2),
        'adx_neg': round(adx_neg, 2),
        'ema21':   round(ema21, 4),
        'ema50':   round(ema50, 4),
        'rsi':     round(rsi, 2),
        'atr':     round(atr, 4),
    }

    def hold(reason, trend='NEUTRAL'):
        return {'action': 'HOLD', 'confidence': 0,
                'regime': 'CHOPPY' if adx < ADX_MIN else 'TRENDING',
                'trend_direction': trend, 'reason': reason, 'indicators': snap}

    if atr < min_atr:
        return hold(f'ATR {atr:.4f} too low (min {min_atr}) — not enough movement to cover fees')

    vol_ok = vol >= vol_ma * 0.9   # require ≥90% of 20-bar average to enter
    trend4h = trend4h_override if trend4h_override is not None else get_4h_trend(symbol)

    # ── EMA21/50 cross entry — opt-in per symbol, runs BEFORE the ADX gate ────
    # ADX is a Wilder-smoothed lagging indicator by construction. Backtested
    # 2026-09-16 against 90 days of real price history + our actual live exit
    # mechanics on TSLA/NBIS/AMD: waiting for ADX >= ADX_MIN (the gate below)
    # lost to entering right at the EMA21/50 cross in 11 of 12 symbol-months
    # tested -- a median 22-70 hours pass (NBIS worst: median 70h, 8% of the
    # move already gone) before ADX confirms what the EMA cross already
    # showed. This does NOT wait for that gate, but still requires the same
    # 4H trend filter and volume confirmation the other entry types use.
    if cfg.get('ema_cross_enabled') and adx >= EMA_CROSS_MIN_ADX \
            and not pd.isna(p['ema21']) and not pd.isna(p['ema50']):
        p_ema21, p_ema50 = float(p['ema21']), float(p['ema50'])
        crossed_up   = p_ema21 <= p_ema50 and ema21 > ema50
        crossed_down = p_ema21 >= p_ema50 and ema21 < ema50
        if (crossed_up or crossed_down) and vol_ok:
            cross_dir = 'BULLISH' if crossed_up else 'BEARISH'
            counter_trend = (cross_dir == 'BULLISH' and trend4h == '4H BEARISH') or \
                            (cross_dir == 'BEARISH' and trend4h == '4H BULLISH')
            if not counter_trend:
                conf = 62
                if adx > 18:             conf += 5   # some trend strength already, even pre-confirmation
                if vol >= vol_ma * 1.2:  conf += 8
                return {
                    'action': 'LONG' if crossed_up else 'SHORT', 'confidence': min(conf, 85),
                    'regime': 'TRENDING', 'trend_direction': cross_dir,
                    'reason': (f'EMA21/50 cross {"up" if crossed_up else "down"} | ADX {adx:.1f} '
                               f'(pre-confirmation) | RSI {rsi:.1f} | vol {vol:.0f}/{vol_ma:.0f}'),
                    'indicators': snap,
                }

    if adx < ADX_MIN:
        return hold(f'ADX {adx:.1f} < {ADX_MIN} — market choppy, no entry')

    di_bullish  = adx_pos > adx_neg
    ema_bullish = ema21 > ema50

    if di_bullish and ema_bullish:
        trend = 'BULLISH'
    elif not di_bullish and not ema_bullish:
        trend = 'BEARISH'
    else:
        return hold(
            f'ADX {adx:.1f} trending but +DI/{adx_pos:.1f} vs -DI/{adx_neg:.1f} '
            f'conflicts with EMA — waiting for alignment', 'MIXED'
        )

    # ── 4H trend filter — only trade in direction of higher timeframe ────────────
    if trend == 'BULLISH' and trend4h == '4H BEARISH':
        return hold(f'BULLISH on 1H but 4H is BEARISH — counter-trend, skipping', trend)
    if trend == 'BEARISH' and trend4h == '4H BULLISH':
        return hold(f'BEARISH on 1H but 4H is BULLISH — counter-trend, skipping', trend)

    if trend == 'BULLISH':
        dist_pct      = (price - ema21) / ema21
        in_zone       = 0 <= dist_pct <= pullback_zone_pct
        candle_dipped = lo <= ema21 * (1 + pullback_zone_pct)
        rsi_ok        = rsi_long_min <= rsi <= rsi_long_max
        if in_zone and candle_dipped and rsi_ok and vol_ok:
            conf = 70
            if adx > ADX_STRONG:             conf += 7
            if vol >= vol_ma:                conf += 5   # above-average volume = stronger signal
            if 40 <= rsi <= 55:              conf += 5
            if float(p['close']) < ema21:    conf += 5
            return {
                'action': 'LONG', 'confidence': min(conf, 90),
                'regime': 'TRENDING', 'trend_direction': trend,
                'reason': (f'Pullback to EMA21 in uptrend | ADX {adx:.1f} | RSI {rsi:.1f} | '
                           f'dist {dist_pct*100:.2f}% above EMA21 | vol {vol:.0f}/{vol_ma:.0f}'),
                'indicators': snap,
            }
        # ── Trend-continuation entry — opt-in per symbol ──────────────────────
        # For stocks that run hard without ever pulling back to EMA21 (e.g. NBIS
        # in a parabolic move), the pullback entry above can sit out an entire
        # trending leg. This buys strength instead of dips: only fires when the
        # clean pullback failed, but the trend is genuinely strong (ADX well
        # above minimum, RSI showing real momentum, real volume behind it).
        # Higher risk than a pullback entry (buying into strength, not a dip),
        # so it's capped short of the most extreme overbought readings and
        # tagged distinctly in the reason string for visibility.
        if cfg.get('trend_continuation_enabled') and adx >= ADX_STRONG and vol_ok \
                and 65 <= rsi <= 88 and dist_pct > pullback_zone_pct:
            conf = 65
            if adx > ADX_STRONG + 10:  conf += 10
            if vol >= vol_ma * 1.2:    conf += 10
            return {
                'action': 'LONG', 'confidence': min(conf, 90),
                'regime': 'TRENDING', 'trend_direction': trend,
                'reason': (f'TREND CONTINUATION — no pullback, buying strength | ADX {adx:.1f} | '
                           f'RSI {rsi:.1f} | dist {dist_pct*100:.2f}% above EMA21 | vol {vol:.0f}/{vol_ma:.0f}'),
                'indicators': snap,
            }

        # ── Donchian breakout entry — opt-in per symbol, runs alongside the
        # pullback entry above (see DC_PERIOD comment). Independent trigger:
        # doesn't require a pullback at all, just a genuine close beyond the
        # prior DC_PERIOD-bar range while the trend/ADX/4H filters already
        # passed above.
        if cfg.get('breakout_enabled') and dc_upper is not None and price > dc_upper and vol_ok:
            conf = 68
            if adx > ADX_STRONG:      conf += 10
            if vol >= vol_ma * 1.2:   conf += 10
            return {
                'action': 'LONG', 'confidence': min(conf, 88),
                'regime': 'TRENDING', 'trend_direction': trend,
                'reason': (f'Donchian breakout above {DC_PERIOD}h high ${dc_upper:.4f} | ADX {adx:.1f} | '
                           f'RSI {rsi:.1f} | vol {vol:.0f}/{vol_ma:.0f}'),
                'indicators': snap,
            }

        parts = []
        if not in_zone:       parts.append(f'price {dist_pct*100:.2f}% from EMA21 (need 0–1.8%)')
        if not candle_dipped: parts.append('candle low not near EMA21')
        if not rsi_ok:        parts.append(f'RSI {rsi:.1f} outside [{rsi_long_min}–{rsi_long_max}]')
        if not vol_ok:        parts.append(f'vol {vol:.0f} < 90% of avg {vol_ma:.0f} — low-volume move')
        return hold(f'BULLISH — waiting: {", ".join(parts)}', trend)

    if trend == 'BEARISH':
        dist_pct      = (ema21 - price) / ema21
        in_zone       = 0 <= dist_pct <= pullback_zone_pct
        candle_tapped = hi >= ema21 * (1 - pullback_zone_pct)
        rsi_ok        = rsi_short_min <= rsi <= rsi_short_max
        if in_zone and candle_tapped and rsi_ok and vol_ok:
            conf = 70
            if adx > ADX_STRONG:             conf += 7
            if vol >= vol_ma:                conf += 5   # above-average volume = stronger signal
            if 45 <= rsi <= 60:              conf += 5
            if float(p['close']) > ema21:    conf += 5
            return {
                'action': 'SHORT', 'confidence': min(conf, 90),
                'regime': 'TRENDING', 'trend_direction': trend,
                'reason': (f'Bounce to EMA21 in downtrend | ADX {adx:.1f} | RSI {rsi:.1f} | '
                           f'dist {dist_pct*100:.2f}% below EMA21 | vol {vol:.0f}/{vol_ma:.0f}'),
                'indicators': snap,
            }

        # ── Donchian breakout entry — opt-in per symbol, same as the BULLISH
        # side above: independent trigger, no pullback required.
        if cfg.get('breakout_enabled') and dc_lower is not None and price < dc_lower and vol_ok:
            conf = 68
            if adx > ADX_STRONG:      conf += 10
            if vol >= vol_ma * 1.2:   conf += 10
            return {
                'action': 'SHORT', 'confidence': min(conf, 88),
                'regime': 'TRENDING', 'trend_direction': trend,
                'reason': (f'Donchian breakout below {DC_PERIOD}h low ${dc_lower:.4f} | ADX {adx:.1f} | '
                           f'RSI {rsi:.1f} | vol {vol:.0f}/{vol_ma:.0f}'),
                'indicators': snap,
            }

        parts = []
        if not in_zone:       parts.append(f'price {dist_pct*100:.2f}% from EMA21 (need 0–1.8%)')
        if not candle_tapped: parts.append('candle high not near EMA21')
        if not rsi_ok:        parts.append(f'RSI {rsi:.1f} outside [{rsi_short_min}–{rsi_short_max}]')
        if not vol_ok:        parts.append(f'vol {vol:.0f} < 90% of avg {vol_ma:.0f} — low-volume move')
        return hold(f'BEARISH — waiting: {", ".join(parts)}', trend)

    return hold('No actionable setup')


# ── Trade Recording ───────────────────────────────────────────────────────────
def _classify_entry_type(entry_reason: str) -> str:
    r = (entry_reason or '').lower()
    if 'donchian breakout' in r:    return 'breakout'
    if 'trend continuation' in r:   return 'trend_continuation'
    if 'ema21/50 cross' in r:       return 'ema_cross'
    if 'pullback to ema21' in r or 'bounce to ema21' in r: return 'pullback'
    return 'other'

def record_closed_trade(symbol: str, side: str, entry_price: float, exit_price: float,
                        qty: float, reason: str, actual_fee: float = None) -> None:
    ss  = sym_state(symbol)
    fee = actual_fee if actual_fee is not None else (entry_price + exit_price) * qty * FEE_RATE
    pnl = round((exit_price - entry_price) * qty - fee, 6) if side == 'LONG' \
        else round((entry_price - exit_price) * qty - fee, 6)
    is_dust = qty < 0.1

    opened_at = ss.get('trade_opened_at')
    closed_at = now_utc_iso()
    hold_minutes = None
    try:
        if opened_at:
            od = datetime.fromisoformat(opened_at.replace('Z', '+00:00'))
            cd = datetime.fromisoformat(closed_at.replace('Z', '+00:00'))
            hold_minutes = round((cd - od).total_seconds() / 60, 1)
    except Exception:
        pass

    collateral = safe_float(ss.get('active_trade_amount'), None)
    max_loss_pct = SYMBOLS_CONFIG.get(symbol, {}).get('max_loss_pct')
    r_multiple = None
    if collateral and max_loss_pct:
        max_loss_dollar = collateral * max_loss_pct
        if max_loss_dollar > 0:
            r_multiple = round(pnl / max_loss_dollar, 3)

    entry_reason = ss.get('entry_reason', '')
    record = {
        'source':      'bot',
        'symbol':      symbol,
        'side':        side,
        'entry_price': round(entry_price, 6),
        'exit_price':  round(exit_price, 6),
        'qty':         round(qty, 6),
        'fee':         round(fee, 6),
        'pnl':         pnl,
        'win':         pnl > 0,
        'dust':        is_dust,
        'opened_at':   opened_at,
        'closed_at':   closed_at,
        'note':        reason[:120],
        # entry context — carried from open_long/open_short, lets losing
        # patterns be analyzed later (which entry type, indicators, time)
        'entry_type':       _classify_entry_type(entry_reason),
        'entry_reason':      entry_reason[:160],
        'entry_confidence':  ss.get('entry_confidence'),
        'entry_indicators':  ss.get('entry_indicators') or {},
        'entry_hour_utc':    ss.get('entry_hour_utc'),
        'entry_weekday':     ss.get('entry_weekday'),
        'exit_reason':       reason[:120],
        'hold_minutes':      hold_minutes,
        'r_multiple':        r_multiple,
    }
    log = ss.get('closed_trades_log') or []
    log.append(record)
    ss['closed_trades_log'] = log[-200:]
    append_trade_log(record)   # persist to append-only file (survives restarts)
    save_state()
    logger.info(f'📋 [{symbol}] Trade | {side} | pnl={pnl:+.4f} USDT | win={pnl > 0}')

def summarize_performance(trades: list) -> dict:
    real   = [t for t in trades if not t.get('dust')]
    total  = len(real)
    wins   = sum(1 for t in real if t.get('win'))
    net    = round(sum(float(t.get('pnl', 0)) for t in trades), 4)  # net includes dust fees
    pnls   = [float(t.get('pnl', 0)) for t in real]
    return {
        'closed_trades': len(trades), 'wins': wins, 'losses': total - wins,
        'win_rate':      round(wins / total * 100, 1) if total else 0.0,
        'net_profit':    net,
        'avg_pnl':       round(net / total, 4) if total else 0.0,
        'best_trade':    round(max(pnls, default=0.0), 4),
        'worst_trade':   round(min(pnls, default=0.0), 4),
    }


# ── Trailing Stop ─────────────────────────────────────────────────────────────
def get_atr_1h(symbol: str, period: int = 14) -> float:
    try:
        klines = binance_futures_public('/fapi/v1/klines',
                                        {'symbol': symbol, 'interval': '1h', 'limit': period + 1})
        ranges = [float(k[2]) - float(k[3]) for k in klines]
        return sum(ranges[-period:]) / period
    except Exception as e:
        logger.warning(f'get_atr_1h [{symbol}]: {e}')
        return 0.0

def init_trail(symbol: str, entry_price: float) -> None:
    ss  = sym_state(symbol)
    atr = get_atr_1h(symbol)
    ss['trail_entry_price'] = entry_price
    ss['trail_best_price']  = entry_price
    ss['trail_atr']         = atr
    save_state()
    logger.info(f'📐 [{symbol}] Trail init | entry={entry_price:.4f} ATR={atr:.4f} '
                f'hardSL±{atr*HARD_SL_ATR:.4f}')

def clear_trail(symbol: str) -> None:
    ss = sym_state(symbol)
    ss['trail_entry_price']   = None
    ss['trail_best_price']    = None
    ss['trail_atr']           = None
    ss['force_trail_active']     = False
    ss['force_trail_processed']  = False
    ss['force_trail_stop_price'] = None
    save_state()

def check_sl_trail(symbol: str, position: str, price: float) -> Tuple[bool, str]:
    ss    = sym_state(symbol)
    entry = safe_float(ss.get('trail_entry_price'), None)
    best  = safe_float(ss.get('trail_best_price'),  None)
    atr   = safe_float(ss.get('trail_atr'),         None)
    if entry is None or atr is None or atr <= 0:
        return False, ''
    is_long            = position == 'LONG'
    sl_atr_mult        = get_symbol_cfg(symbol, 'hard_sl_atr', HARD_SL_ATR)
    hard_sl_dist       = atr * sl_atr_mult
    trail_activate_mult = get_symbol_cfg(symbol, 'trail_activate_atr', TRAIL_ACTIVATE_ATR)
    activate_dist      = atr * trail_activate_mult
    force_trail_active = ss.get('force_trail_active', False)
    profit_so_far      = (best - entry) if is_long else (entry - best)

    trail_active = profit_so_far >= activate_dist

    if not trail_active and not force_trail_active:
        # Per-symbol dollar cap: max_loss_pct of collateral (wider for volatile symbols)
        collateral   = safe_float(sym_state(symbol).get('active_trade_amount'), 20.0)
        leverage     = safe_float(sym_state(symbol).get('active_leverage'), 30.0)
        qty          = (collateral * leverage * 0.995) / entry if entry else 0
        loss_pct     = SYMBOLS_CONFIG.get(symbol, {}).get('max_loss_pct', 0.30)
        max_loss_dollar = collateral * loss_pct
        max_loss_dist = (max_loss_dollar / qty) if qty > 0 else hard_sl_dist
        sl_dist    = min(hard_sl_dist, max_loss_dist)
        sl = entry - sl_dist if is_long else entry + sl_dist
        if (is_long and price <= sl) or (not is_long and price >= sl):
            return True, f'🛑 Hard SL hit | entry={entry:.4f} sl={sl:.4f} price={price:.4f}'

    # Natural trail activation: graduate out of force trail mode
    if force_trail_active and trail_active:
        ss['force_trail_active']     = False
        ss['force_trail_stop_price'] = None
        force_trail_active = False
        save_state()

    if is_long and price > best:
        ss['trail_best_price'] = price; best = price; save_state()
    elif not is_long and price < best:
        ss['trail_best_price'] = price; best = price; save_state()

    profit_dist = (best - entry) if is_long else (entry - best)

    if force_trail_active:
        # $0.50 floor at click, widens as best rises: 20% of gain above click price
        click_price     = safe_float(ss.get('force_trail_stop_price'), best)
        gain_from_click = max((best - click_price) if is_long else (click_price - best), 0)
        dyn_dist        = max(gain_from_click * 0.15, 0.50)  # lock 85% of gain from click, min $0.50
        trail_stop      = best - dyn_dist if is_long else best + dyn_dist
        if (is_long and price <= trail_stop) or (not is_long and price >= trail_stop):
            return True, (f'🔒 Force trail stop hit | best={best:.4f} stop={trail_stop:.4f} '
                          f'price={price:.4f}')
        logger.info(f'🔒 Force-trail [{symbol}] best={best:.4f} stop={trail_stop:.4f} dist={dyn_dist:.2f}')
        return False, ''

    if profit_dist < activate_dist:
        near_miss_dist = activate_dist * NEAR_MISS_ATR_FRAC
        if profit_dist >= near_miss_dist:
            # Peak profit got close to the real activation threshold but never quite reached it --
            # protect against giving back more than half of that peak instead of leaving the
            # position fully unprotected until the hard stop (see NEAR_MISS_ATR_FRAC comment).
            near_miss_giveback = profit_dist * NEAR_MISS_GIVEBACK_FRAC
            near_miss_stop = best - near_miss_giveback if is_long else best + near_miss_giveback
            if (is_long and price <= near_miss_stop) or (not is_long and price >= near_miss_stop):
                locked_pct = round((1 - NEAR_MISS_GIVEBACK_FRAC) * 100)
                return True, (f'⚠️ Near-miss trail hit | best={best:.4f} stop={near_miss_stop:.4f} '
                              f'price={price:.4f} | never fully armed ({profit_dist:.4f} < {activate_dist:.4f}) '
                              f'but protected ~{locked_pct}% of the peak')
        logger.info(f'📐 [{symbol}] Trail not active | profit={profit_dist:.4f} < {activate_dist:.4f}')
        return False, ''

    trail_atr_mult = get_symbol_cfg(symbol, 'trail_dist_atr', 0.25)
    dyn_dist   = max(atr * trail_atr_mult, profit_dist * 0.18)
    trail_stop = best - dyn_dist if is_long else best + dyn_dist
    # Breakeven floor: once trail activates never stop out at a loss
    if is_long:
        trail_stop = max(trail_stop, entry)
    else:
        trail_stop = min(trail_stop, entry)
    locked_pct = round((profit_dist - dyn_dist) / profit_dist * 100) if profit_dist > 0 else 0
    if (is_long and price <= trail_stop) or (not is_long and price >= trail_stop):
        return True, (f'📐 Trail stop hit | best={best:.4f} stop={trail_stop:.4f} '
                      f'price={price:.4f} | locked {locked_pct:.0f}%')
    logger.info(f'📐 [{symbol}] Trail active | best={best:.4f} stop={trail_stop:.4f} locked={locked_pct:.0f}%')
    return False, ''

def build_trail_info(symbol: str, position: Optional[str]) -> dict:
    ss  = sym_state(symbol)
    ep  = ss.get('trail_entry_price')
    bp  = ss.get('trail_best_price')
    atr = ss.get('trail_atr')
    info = {'entry_price': ep, 'best_price': bp, 'atr': atr,
            'sl': None, 'trail_stop': None, 'active': False}
    if ep and atr:
        collateral = safe_float(ss.get('active_trade_amount'), 30.0)
        leverage   = safe_float(ss.get('active_leverage'), 30.0)
        qty        = (collateral * leverage * 0.995) / ep if ep else 0
        sl_atr_mult = get_symbol_cfg(symbol, 'hard_sl_atr', HARD_SL_ATR)
        max_loss_dist = (12.0 / qty) if qty > 0 else atr * sl_atr_mult
        sl_dist    = min(atr * sl_atr_mult, max_loss_dist)
        info['sl'] = round(ep - sl_dist, 4) if position == 'LONG' \
                else round(ep + sl_dist, 4)
    if ep and bp and atr:
        profit = (bp - ep) if position == 'LONG' else (ep - bp)
        trail_activate_mult = get_symbol_cfg(symbol, 'trail_activate_atr', TRAIL_ACTIVATE_ATR)
        if profit >= atr * trail_activate_mult:
            trail_atr_mult = get_symbol_cfg(symbol, 'trail_dist_atr', 0.25)
            dyn = max(atr * trail_atr_mult, profit * 0.20)
            info['trail_stop'] = round(bp - dyn, 4) if position == 'LONG' else round(bp + dyn, 4)
            info['active'] = True
    return info


# ── Trade Execution ───────────────────────────────────────────────────────────
SAME_DIR_COOLDOWN = 900  # 15 minutes (was 600 = 10 min until 2026-09-21; raised on user request)
# Consecutive-loss circuit breaker -- added 2026-09-18 after a real incident:
# SOXL re-entered LONG three times in one session (119.35 -> 118.81 -> 118.48),
# losing every time, because the flat same-direction cooldown above has no memory
# of a losing streak -- it resets the instant the timer runs out, regardless
# of what just happened. This tracks consecutive losses per symbol; once a
# symbol hits the threshold, new entries are blocked NOT for a fixed duration
# but until trend_direction actually reads differently than it did during the
# losing streak -- tied to evidence the regime changed, not a guessed
# cooldown length that might expire while the same stale trend read is still
# in effect (which is exactly what let SOXL back in each time today).
CONSECUTIVE_LOSS_FREEZE_THRESHOLD = 2

def _update_loss_streak(ss: dict, symbol: str, net: float) -> None:
    if net > 0:
        if ss.get('consecutive_losses'):
            logger.info(f'🧊 [{symbol}] Win — consecutive-loss streak reset (was {ss["consecutive_losses"]})')
        ss['consecutive_losses']    = 0
        ss['loss_streak_direction'] = None
        return
    ss['consecutive_losses'] = ss.get('consecutive_losses', 0) + 1
    if ss['consecutive_losses'] >= CONSECUTIVE_LOSS_FREEZE_THRESHOLD:
        ss['loss_streak_direction'] = ss.get('entry_trend_direction')
        logger.info(f'🧊 [{symbol}] {ss["consecutive_losses"]} consecutive losses — standing down while '
                    f'trend_direction stays {ss["loss_streak_direction"]}')

def open_long(symbol: str, price: float, confidence: int, reason: str, indicators: dict = None,
              trend_direction: str = None) -> bool:
    ss   = sym_state(symbol)
    base = SYMBOLS_CONFIG[symbol]['base']
    cfg  = SYMBOLS_CONFIG[symbol]
    # Block re-entry in same direction within SAME_DIR_COOLDOWN (15 min) of last close
    if ss.get('last_bot_closed_side') == 'LONG':
        elapsed = time.time() - int(ss.get('last_bot_closed_ts', 0))
        if elapsed < SAME_DIR_COOLDOWN:
            remaining = int((SAME_DIR_COOLDOWN - elapsed) / 60)
            logger.info(f'⏳ [{symbol}] LONG cooldown — last LONG closed {int(elapsed/60)}m ago, waiting {remaining}m more')
            return False
    # Consecutive-loss circuit breaker — see CONSECUTIVE_LOSS_FREEZE_THRESHOLD
    if ss.get('consecutive_losses', 0) >= CONSECUTIVE_LOSS_FREEZE_THRESHOLD \
            and trend_direction is not None and ss.get('loss_streak_direction') == trend_direction:
        logger.info(f'🧊 [{symbol}] Circuit breaker — {ss["consecutive_losses"]} losses in a row while '
                    f'trend_direction stayed {trend_direction}; standing down until this reads differently')
        return False
    try:
        collateral = float(cfg.get('trade_amount') or state['runtime']['trade_amount_usdt'])
        leverage   = int(cfg.get('leverage') or state['runtime']['leverage'])
        if not cfg.get('skip_margin_type'):
            set_futures_margin_type(symbol, 'ISOLATED')
        set_futures_leverage(symbol, leverage)

        balance = get_futures_balance('USDT')
        if balance['free'] < collateral:
            logger.warning(f'[{symbol}] Insufficient USDT: need ${collateral:.2f} have ${balance["free"]:.2f}')
            return False

        step       = get_futures_step_size(symbol)
        gross_usdt = collateral * leverage
        quantity   = round_step((gross_usdt * 0.995) / price, step)
        if quantity < step:
            return False

        resp         = futures_market_order(symbol, 'BUY', quantity, position_side='LONG')
        actual_price = get_fill_price(resp, price)
        qty_filled   = float(resp.get('executedQty') or 0) or quantity
        fee_usdt     = qty_filled * actual_price * FEE_RATE

        ss['trade_opened_at']     = now_utc_iso()
        ss['entry_fee_usdt']      = fee_usdt
        ss['active_trade_amount'] = collateral
        ss['active_leverage']     = leverage
        ss['active_qty']          = qty_filled
        init_trail(symbol, actual_price)
        atr = safe_float(ss.get('trail_atr'), 0)

        # Entry context — carried through to the trade journal on close, so
        # losing patterns can be analyzed later (which entry type, what the
        # indicators looked like, what time it was).
        now_ = datetime.now(timezone.utc)
        ss['entry_reason']          = reason
        ss['entry_confidence']      = confidence
        ss['entry_indicators']      = indicators or {}
        ss['entry_hour_utc']        = now_.hour
        ss['entry_weekday']         = now_.strftime('%A')
        ss['entry_trend_direction'] = trend_direction

        logger.info(f'✅ [{symbol}] LONG OPEN {qty_filled:.4f} {base} @ ${actual_price:.4f} fee=${fee_usdt:.4f}')
        send_telegram(
            f'🟢 <b>APEX FUTURES — LONG {base}/USDT ({leverage}x)</b>\n\n'
            f'💰 Entry: ${actual_price:,.4f}\n'
            f'💵 Collateral: ${collateral:.2f} | Effective: ${gross_usdt:.2f}\n'
            f'🪙 Qty: {qty_filled:.4f} {base}\n'
            f'🛑 Hard SL: ${actual_price - atr*HARD_SL_ATR:,.4f}\n'
            f'🎯 Confidence: {confidence}%\n'
            f'📊 {reason}'
        )
        return True
    except Exception as e:
        logger.error(f'open_long [{symbol}] failed: {e}')
        alert_error(f'open_long {symbol}: {e}')
        return False

def close_long(symbol: str, price: float, reason: str) -> bool:
    ss   = sym_state(symbol)
    base = SYMBOLS_CONFIG[symbol]['base']
    try:
        positions = binance_futures_private('GET', '/fapi/v2/positionRisk', {'symbol': symbol})
        qty_held  = 0.0
        for p in positions:
            if p['symbol'] == symbol and p.get('positionSide', 'BOTH') in ('LONG', 'BOTH'):
                qty_held = abs(float(p['positionAmt']))
                if qty_held > 1e-8: break
        step     = get_futures_step_size(symbol)
        quantity = round_step(qty_held, step)
        if quantity < step:
            logger.warning(f'[{symbol}] No {base} long position to close')
            return False

        entry_price = safe_float(ss.get('trail_entry_price'), price)
        entry_fee   = safe_float(ss.get('entry_fee_usdt'), 0.0)

        resp         = futures_market_order(symbol, 'SELL', quantity, position_side='LONG', reduce_only=True)
        actual_close = get_fill_price(resp, price)
        exit_fee     = quantity * actual_close * FEE_RATE
        total_fee    = round(entry_fee + exit_fee, 6)

        ss['last_bot_closed_side'] = 'LONG'
        ss['last_bot_closed_ts']   = int(time.time())
        record_closed_trade(symbol, 'LONG', entry_price, actual_close, quantity, reason, total_fee)
        gross = (actual_close - entry_price) * quantity
        net   = gross - total_fee
        _update_loss_streak(ss, symbol, net)
        logger.info(f'✅ [{symbol}] LONG CLOSE {quantity:.4f} {base} @ ${actual_close:.4f} net={net:+.4f}')
        pnl_banner = f'🟢 +${net:.2f}' if net >= 0 else f'🔴 -${abs(net):.2f}'
        send_telegram(
            f'{pnl_banner}\n'
            f'🔴 <b>APEX FUTURES — LONG {base}/USDT CLOSED</b>\n\n'
            f'💰 Exit: ${actual_close:,.4f} | Entry: ${entry_price:,.4f}\n'
            f'🪙 Qty: {quantity:.4f} {base}\n'
            f'✅ Gross: {gross:+.4f} USDT\n💸 Fees: -{total_fee:.4f} USDT\n'
            f'🏦 Net P&L: {net:+.4f} USDT\n📉 {reason}'
        )
        return True
    except Exception as e:
        logger.error(f'close_long [{symbol}] failed: {e}')
        alert_error(f'close_long {symbol}: {e}')
        return False

def open_short(symbol: str, price: float, confidence: int, reason: str, indicators: dict = None,
               trend_direction: str = None) -> bool:
    ss   = sym_state(symbol)
    base = SYMBOLS_CONFIG[symbol]['base']
    cfg  = SYMBOLS_CONFIG[symbol]
    # Block re-entry in same direction within SAME_DIR_COOLDOWN (15 min) of last close
    if ss.get('last_bot_closed_side') == 'SHORT':
        elapsed = time.time() - int(ss.get('last_bot_closed_ts', 0))
        if elapsed < SAME_DIR_COOLDOWN:
            remaining = int((SAME_DIR_COOLDOWN - elapsed) / 60)
            logger.info(f'⏳ [{symbol}] SHORT cooldown — last SHORT closed {int(elapsed/60)}m ago, waiting {remaining}m more')
            return False
    # Consecutive-loss circuit breaker — see CONSECUTIVE_LOSS_FREEZE_THRESHOLD
    if ss.get('consecutive_losses', 0) >= CONSECUTIVE_LOSS_FREEZE_THRESHOLD \
            and trend_direction is not None and ss.get('loss_streak_direction') == trend_direction:
        logger.info(f'🧊 [{symbol}] Circuit breaker — {ss["consecutive_losses"]} losses in a row while '
                    f'trend_direction stayed {trend_direction}; standing down until this reads differently')
        return False
    try:
        collateral = float(cfg.get('trade_amount') or state['runtime']['trade_amount_usdt'])
        leverage   = int(cfg.get('leverage') or state['runtime']['leverage'])
        if not cfg.get('skip_margin_type'):
            set_futures_margin_type(symbol, 'ISOLATED')
        set_futures_leverage(symbol, leverage)

        balance = get_futures_balance('USDT')
        if balance['free'] < collateral:
            logger.warning(f'[{symbol}] Insufficient USDT: need ${collateral:.2f} have ${balance["free"]:.2f}')
            return False

        step       = get_futures_step_size(symbol)
        gross_usdt = collateral * leverage
        quantity   = round_step((gross_usdt * 0.995) / price, step)
        if quantity < step:
            return False

        resp         = futures_market_order(symbol, 'SELL', quantity, position_side='SHORT')
        actual_price = get_fill_price(resp, price)
        qty_filled   = float(resp.get('executedQty') or 0) or quantity
        fee_usdt     = qty_filled * actual_price * FEE_RATE

        ss['trade_opened_at']     = now_utc_iso()
        ss['entry_fee_usdt']      = fee_usdt
        ss['active_trade_amount'] = collateral
        ss['active_leverage']     = leverage
        ss['active_qty']          = qty_filled
        init_trail(symbol, actual_price)
        atr = safe_float(ss.get('trail_atr'), 0)

        now_ = datetime.now(timezone.utc)
        ss['entry_reason']          = reason
        ss['entry_confidence']      = confidence
        ss['entry_indicators']      = indicators or {}
        ss['entry_hour_utc']        = now_.hour
        ss['entry_weekday']         = now_.strftime('%A')
        ss['entry_trend_direction'] = trend_direction

        logger.info(f'✅ [{symbol}] SHORT OPEN {qty_filled:.4f} {base} @ ${actual_price:.4f} fee=${fee_usdt:.4f}')
        send_telegram(
            f'🔴 <b>APEX FUTURES — SHORT {base}/USDT ({leverage}x)</b>\n\n'
            f'💰 Entry: ${actual_price:,.4f}\n'
            f'💵 Collateral: ${collateral:.2f} | Effective: ${gross_usdt:.2f}\n'
            f'🪙 Qty: {qty_filled:.4f} {base}\n'
            f'🛑 Hard SL: ${actual_price + atr*HARD_SL_ATR:,.4f}\n'
            f'🎯 Confidence: {confidence}%\n'
            f'📊 {reason}'
        )
        return True
    except Exception as e:
        logger.error(f'open_short [{symbol}] failed: {e}')
        alert_error(f'open_short {symbol}: {e}')
        return False

def close_short(symbol: str, price: float, reason: str) -> bool:
    ss   = sym_state(symbol)
    base = SYMBOLS_CONFIG[symbol]['base']
    try:
        positions = binance_futures_private('GET', '/fapi/v2/positionRisk', {'symbol': symbol})
        qty_held  = 0.0
        for p in positions:
            if p['symbol'] == symbol and p.get('positionSide', 'BOTH') in ('SHORT', 'BOTH'):
                qty_held = abs(float(p['positionAmt']))
                if qty_held > 1e-8: break
        step     = get_futures_step_size(symbol)
        quantity = round_step(qty_held, step)
        if quantity < step:
            logger.warning(f'[{symbol}] No {base} short position to close')
            return False

        entry_price = safe_float(ss.get('trail_entry_price'), price)
        entry_fee   = safe_float(ss.get('entry_fee_usdt'), 0.0)

        resp         = futures_market_order(symbol, 'BUY', quantity, position_side='SHORT', reduce_only=True)
        actual_close = get_fill_price(resp, price)
        exit_fee     = quantity * actual_close * FEE_RATE
        total_fee    = round(entry_fee + exit_fee, 6)

        ss['last_bot_closed_side'] = 'SHORT'
        ss['last_bot_closed_ts']   = int(time.time())
        record_closed_trade(symbol, 'SHORT', entry_price, actual_close, quantity, reason, total_fee)
        gross = (entry_price - actual_close) * quantity
        net   = gross - total_fee
        _update_loss_streak(ss, symbol, net)
        logger.info(f'✅ [{symbol}] SHORT CLOSE {quantity:.4f} {base} @ ${actual_close:.4f} net={net:+.4f}')
        pnl_banner = f'🟢 +${net:.2f}' if net >= 0 else f'🔴 -${abs(net):.2f}'
        send_telegram(
            f'{pnl_banner}\n'
            f'🟢 <b>APEX FUTURES — SHORT {base}/USDT CLOSED</b>\n\n'
            f'💰 Exit: ${actual_close:,.4f} | Entry: ${entry_price:,.4f}\n'
            f'🪙 Qty: {quantity:.4f} {base}\n'
            f'✅ Gross: {gross:+.4f} USDT\n💸 Fees: -{total_fee:.4f} USDT\n'
            f'🏦 Net P&L: {net:+.4f} USDT\n📈 {reason}'
        )
        return True
    except Exception as e:
        logger.error(f'close_short [{symbol}] failed: {e}')
        alert_error(f'close_short {symbol}: {e}')
        return False


# ── Dashboard ─────────────────────────────────────────────────────────────────
WEB_ROOT = os.environ.get('WEB_ROOT', '/var/www/apex').strip()

def push_dashboard_data(data: dict, dashboard_file: str) -> None:
    try:
        path = os.path.join(WEB_ROOT, dashboard_file)
        write_json(path, data)
        logger.info(f'✅ Dashboard written ({path})')
    except Exception as e:
        logger.warning(f'Dashboard write error ({dashboard_file}): {e}')

def fetch_dashboard_config() -> dict:
    default = {
        'trade_amount_usdt': DEFAULT_TRADE_AMOUNT_USDT, 'leverage': DEFAULT_LEVERAGE,
        'close_requested': False, 'close_requested_at': None,
        'bot_paused': False, 'force_trail': False, 'force_trail_at': None,
        'close_symbol': None, 'updated_at': None,
    }
    try:
        cfg = read_json(os.path.join(WEB_ROOT, BOT_CONFIG_FILE), None)
        if not isinstance(cfg, dict): return default
        # Read futures-specific keys, fall back to shared keys
        amt = safe_float(cfg.get('futures_trade_amount_usdt') or cfg.get('trade_amount_usdt'),
                         DEFAULT_TRADE_AMOUNT_USDT)
        lev = safe_float(cfg.get('futures_leverage') or cfg.get('leverage'), DEFAULT_LEVERAGE)
        if lev not in ALLOWED_LEVERAGES: lev = DEFAULT_LEVERAGE
        if amt <= 0: amt = DEFAULT_TRADE_AMOUNT_USDT
        return {
            'trade_amount_usdt':  amt,
            'leverage':           lev,
            'close_requested':    bool(cfg.get('futures_close_requested', False)),
            'close_requested_at': cfg.get('futures_close_requested_at'),
            'close_symbol':       cfg.get('futures_close_symbol'),
            'bot_paused':         bool(cfg.get('futures_bot_paused', False)),
            'force_trail':        bool(cfg.get('futures_force_trail', False)),
            'force_trail_at':     cfg.get('futures_force_trail_at'),
            'force_trail_symbol': cfg.get('futures_force_trail_symbol'),
            'mu_close_requested':    bool(cfg.get('futures_mu_close_requested', False)),
            'mu_close_requested_at': cfg.get('futures_mu_close_requested_at'),
            'mu_levels_request':  cfg.get('futures_mu_levels_request') if isinstance(cfg.get('futures_mu_levels_request'), dict) else None,
            'manual_trade':       cfg.get('manual_trade') if isinstance(cfg.get('manual_trade'), dict) else None,
            'updated_at':         cfg.get('updated_at'),
        }
    except Exception as e:
        logger.warning(f'Dashboard config read failed: {e}')
        return default

def apply_runtime_settings(cfg: dict) -> None:
    amt = cfg['trade_amount_usdt']
    lev = cfg['leverage']
    state['runtime'].update({'trade_amount_usdt': amt, 'leverage': lev, 'source': 'dashboard-config'})
    logger.info(f'⚙️ Futures runtime | ${amt:.2f} @ {lev:.0f}x')

def clear_flag(flag_name: str) -> None:
    try:
        path = os.path.join(WEB_ROOT, BOT_CONFIG_FILE)
        cfg  = read_json(path, {})
        cfg[flag_name] = False
        write_json(path, cfg)
        logger.info(f'✅ Cleared flag: {flag_name}')
    except Exception as e:
        logger.warning(f'clear_flag({flag_name}) failed: {e}')


# ── Manual BTC Short ──────────────────────────────────────────────────────────
# ── Manual Trades (any symbol, any direction, configurable size/leverage) ──────
MANUAL_SYMBOLS = {
    'BTCUSDT':  {'skip_margin_type': False},
    'NBISUSDT': {'skip_margin_type': True},
}

def _manual_pos(symbol: str) -> dict:
    mp = state.setdefault('manual_positions', {})
    if symbol not in mp:
        mp[symbol] = {'position': None, 'entry_price': None, 'qty': None,
                      'opened_at': None, 'entry_fee': 0.0, 'leverage': 5, 'amount_usdt': 100.0}
    return mp[symbol]

def write_manual_dashboard() -> None:
    positions = {}
    for sym in MANUAL_SYMBOLS:
        mp    = _manual_pos(sym)
        pos   = mp.get('position')
        price = 0.0
        pnl   = 0.0
        try:
            price = get_current_price(sym)
            if pos and mp.get('entry_price') and mp.get('qty'):
                ep    = mp['entry_price']
                qty   = mp['qty']
                gross = (ep - price) * qty if pos == 'SHORT' else (price - ep) * qty
                pnl   = round(gross - safe_float(mp.get('entry_fee')), 4)
        except Exception:
            pass
        positions[sym] = {
            'position':      pos,
            'entry_price':   mp.get('entry_price'),
            'qty':           mp.get('qty'),
            'opened_at':     mp.get('opened_at'),
            'current_price': price,
            'pnl':           pnl,
            'leverage':      mp.get('leverage', 5),
            'amount_usdt':   mp.get('amount_usdt', 100.0),
        }
    write_json(os.path.join(WEB_ROOT, 'data_futures_manual.json'), {
        'positions':  positions,
        'updated_at': now_utc_iso(),
    })

def open_manual_position(symbol: str, side: str, amount: float, leverage: int) -> bool:
    mp  = _manual_pos(symbol)
    cfg = MANUAL_SYMBOLS.get(symbol, {})
    if mp.get('position'):
        logger.info(f'[MANUAL {symbol}] Already in {mp["position"]}')
        return False
    try:
        if not cfg.get('skip_margin_type'):
            set_futures_margin_type(symbol, 'ISOLATED')
        set_futures_leverage(symbol, leverage)
        balance = get_futures_balance('USDT')
        if balance['free'] < amount:
            logger.warning(f'[MANUAL {symbol}] Insufficient balance: need ${amount} have ${balance["free"]:.2f}')
            return False
        price      = get_current_price(symbol)
        step       = get_futures_step_size(symbol)
        gross_usdt = amount * leverage
        quantity   = round_step((gross_usdt * 0.995) / price, step)
        order_side = 'BUY' if side == 'LONG' else 'SELL'
        resp = binance_futures_private('POST', '/fapi/v1/order', {
            'symbol': symbol, 'side': order_side, 'type': 'MARKET', 'quantity': str(quantity),
        })
        actual_price = get_fill_price(resp, price)
        qty_filled   = float(resp.get('executedQty') or 0) or quantity
        fee_usdt     = qty_filled * actual_price * FEE_RATE
        mp.update({'position': side, 'entry_price': actual_price, 'qty': qty_filled,
                   'opened_at': now_utc_iso(), 'entry_fee': fee_usdt,
                   'leverage': leverage, 'amount_usdt': amount})
        save_state()
        base  = symbol.replace('USDT', '')
        emoji = '🟢' if side == 'LONG' else '🔴'
        logger.info(f'✅ [MANUAL {symbol}] {side} OPEN {qty_filled:.6f} {base} @ ${actual_price:,.4f}')
        send_telegram(
            f'{emoji} <b>MANUAL {side} {base}/USDT OPENED</b>\n\n'
            f'💰 Entry: ${actual_price:,.4f}\n'
            f'💵 Collateral: ${amount:.0f} | Effective: ${gross_usdt:.0f}\n'
            f'🪙 Qty: {qty_filled:.6f} {base}\n'
            f'⚡ {leverage}× leverage | Manual trade'
        )
        write_manual_dashboard()
        return True
    except Exception as e:
        logger.error(f'open_manual_position [{symbol} {side}] failed: {e}')
        alert_error(f'Manual {side} {symbol}: {e}')
        return False

def close_manual_position(symbol: str) -> bool:
    mp  = _manual_pos(symbol)
    pos = mp.get('position')
    if not pos:
        logger.info(f'[MANUAL {symbol}] No position to close')
        return False
    try:
        positions = binance_futures_private('GET', '/fapi/v2/positionRisk', {'symbol': symbol})
        qty_held  = 0.0
        for p in positions:
            if p['symbol'] == symbol:
                qty_held = abs(float(p['positionAmt']))
                if qty_held > 1e-8: break
        step     = get_futures_step_size(symbol)
        quantity = round_step(qty_held, step)
        if quantity < step:
            logger.warning(f'[MANUAL {symbol}] No position on Binance — clearing state')
            mp.update({'position': None, 'entry_price': None, 'qty': None, 'opened_at': None, 'entry_fee': 0.0})
            save_state()
            return False
        entry_price = safe_float(mp.get('entry_price'))
        entry_fee   = safe_float(mp.get('entry_fee'))
        price       = get_current_price(symbol)
        close_side  = 'SELL' if pos == 'LONG' else 'BUY'
        resp = binance_futures_private('POST', '/fapi/v1/order', {
            'symbol': symbol, 'side': close_side, 'type': 'MARKET',
            'quantity': str(quantity), 'reduceOnly': 'true',
        })
        actual_close = get_fill_price(resp, price)
        exit_fee     = quantity * actual_close * FEE_RATE
        total_fee    = entry_fee + exit_fee
        gross        = (entry_price - actual_close) * quantity if pos == 'SHORT' else (actual_close - entry_price) * quantity
        net          = gross - total_fee
        mp.update({'position': None, 'entry_price': None, 'qty': None, 'opened_at': None, 'entry_fee': 0.0})
        save_state()
        base  = symbol.replace('USDT', '')
        emoji = '✅' if net >= 0 else '🔴'
        logger.info(f'{emoji} [MANUAL {symbol}] {pos} CLOSED {quantity:.6f} {base} @ ${actual_close:,.4f} net={net:+.4f}')
        send_telegram(
            f'{emoji} <b>MANUAL {pos} {base}/USDT CLOSED</b>\n\n'
            f'💰 Exit: ${actual_close:,.4f} | Entry: ${entry_price:,.4f}\n'
            f'🪙 Qty: {quantity:.6f} {base}\n'
            f'✅ Gross: {gross:+.4f} USDT\n💸 Fees: -{total_fee:.4f} USDT\n'
            f'🏦 Net P&L: {net:+.4f} USDT'
        )
        write_manual_dashboard()
        return True
    except Exception as e:
        logger.error(f'close_manual_position [{symbol}] failed: {e}')
        alert_error(f'Manual close {symbol}: {e}')
        return False

def process_manual_trade(cfg: dict) -> None:
    trade = cfg.get('manual_trade')
    if not isinstance(trade, dict):
        write_manual_dashboard()
        return
    action   = trade.get('action')
    symbol   = trade.get('symbol', '')
    side     = trade.get('side', '').upper()
    amount   = max(10.0, safe_float(trade.get('amount_usdt'), 100.0))
    leverage = max(1, min(20, int(safe_float(trade.get('leverage'), 5))))
    if symbol not in MANUAL_SYMBOLS:
        logger.warning(f'[MANUAL] Unknown symbol: {symbol}')
        clear_flag('manual_trade')
        return
    if action == 'open' and side in ('LONG', 'SHORT'):
        open_manual_position(symbol, side, amount, leverage)
    elif action == 'close':
        close_manual_position(symbol)
    clear_flag('manual_trade')


def _et_tz():
    try:
        import zoneinfo
        return zoneinfo.ZoneInfo('America/New_York')
    except Exception:
        from datetime import timezone, timedelta
        month = __import__('datetime').datetime.utcnow().month
        offset = -4 if 3 <= month <= 11 else -5
        return timezone(timedelta(hours=offset))

def is_us_market_open(at: Optional[datetime] = None) -> bool:
    """True during regular US market hours Mon-Fri 9:45am-4:00pm ET (handles
    DST). `at` (an aware datetime) checks that point in time instead of now
    -- used by the auto-tune entry-signal backtest to faithfully replicate
    this same gate historically; defaults to real "now" for live trading,
    unchanged from before."""
    tz = _et_tz()
    now_et = at.astimezone(tz) if at is not None else datetime.now(tz)
    if now_et.weekday() >= 5:
        return False
    if (now_et.month, now_et.day) in US_MARKET_HOLIDAYS:
        return False
    # Skip first 15 min after open (whipsaw period)
    open_time  = now_et.replace(hour=9,  minute=45, second=0, microsecond=0)
    close_time = now_et.replace(hour=16, minute=0,  second=0, microsecond=0)
    return open_time <= now_et < close_time


def in_entry_window(cfg: dict, at: Optional[datetime] = None) -> bool:
    """True if `at` (ET, defaults to now) falls inside the symbol's optional
    entry_window_et, a (start_h, start_m, end_h, end_m) tuple restricting new
    entries to a sub-range of market hours. Symbols without this key trade
    all session."""
    window = cfg.get('entry_window_et')
    if not window:
        return True
    tz = _et_tz()
    now_et = at.astimezone(tz) if at is not None else datetime.now(tz)
    sh, sm, eh, em = window
    start = now_et.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end   = now_et.replace(hour=eh, minute=em, second=0, microsecond=0)
    return start <= now_et < end


# ── Monthly PnL Calendar ──────────────────────────────────────────────────────
def send_monthly_summary():
    """Send ASCII monthly PnL calendar to Discord after market close."""
    try:
        import calendar as cal_mod
        try:
            import zoneinfo
            tz = zoneinfo.ZoneInfo('America/New_York')
        except Exception:
            from datetime import timezone, timedelta
            month = datetime.now(timezone.utc).month
            tz = timezone(timedelta(hours=-4 if 3 <= month <= 11 else -5))

        now_et = datetime.now(tz)
        year, month = now_et.year, now_et.month

        path = os.path.join(os.path.dirname(BOT_STATE_FILE), TRADES_LOG_FILE) \
               if os.path.dirname(BOT_STATE_FILE) else TRADES_LOG_FILE
        trades = json.load(open(path)) if os.path.exists(path) else []

        by_day = {}
        for t in trades:
            raw = t.get('closed_at') or t.get('opened_at')
            if not raw: continue
            dt = datetime.fromisoformat(raw).astimezone(tz)
            if dt.year != year or dt.month != month: continue
            by_day.setdefault(dt.day, []).append(t)

        MONTH_NAMES = ['Jan','Feb','Mar','Apr','May','Jun',
                       'Jul','Aug','Sep','Oct','Nov','Dec']

        def dot(val): return '🟢' if val >= 0 else '🔴'

        # Calendar grid in plain code block (emoji breaks alignment)
        lines = [f'📅 **{MONTH_NAMES[month-1]} {year} — Daily P&L**', '```']
        lines.append('Mon   Tue   Wed   Thu   Fri')
        lines.append('─────────────────────────────')

        for week in cal_mod.monthcalendar(year, month):
            day_row, pnl_row = '', ''
            has_pnl = False
            for dow in range(5):
                day = week[dow]
                if day == 0:
                    day_row += '      '; pnl_row += '      '
                else:
                    day_row += f'  {day:2d}  '
                    if day in by_day:
                        net = sum(float(t.get('pnl', 0)) for t in by_day[day])
                        pnl_row += f'{net:+.2f}'.center(6)
                        has_pnl = True
                    else:
                        pnl_row += '  --  '
            lines.append(day_row)
            if has_pnl:
                lines.append(pnl_row)

        lines.append('─────────────────────────────')
        lines.append('```')

        # Summary + per-symbol outside code block — emoji + bold render on mobile
        all_month = [t for trades in by_day.values() for t in trades]
        total = len(all_month)
        net   = sum(float(t.get('pnl', 0)) for t in all_month)
        wins  = sum(1 for t in all_month if t.get('win') or float(t.get('pnl', 0)) > 0)
        wr    = wins / total * 100 if total else 0
        lines.append(f'{dot(net)} **Total** · {total} trades · {wr:.0f}%W · **{net:+.2f} USDT**')
        lines.append('')

        by_sym = {}
        for t in all_month:
            base = SYMBOLS_CONFIG.get(t.get('symbol', ''), {}).get('base') or t.get('symbol', '?')
            by_sym.setdefault(base, []).append(t)
        for base in sorted(by_sym.keys()):
            ts = by_sym.get(base)
            if not ts: continue
            s_net  = sum(float(t.get('pnl', 0)) for t in ts)
            s_wins = sum(1 for t in ts if t.get('win') or float(t.get('pnl', 0)) > 0)
            s_wr   = s_wins / len(ts) * 100
            lines.append(f'{dot(s_net)} **{base}** · {len(ts)} trades · {s_wr:.0f}%W · {s_net:+.2f} USDT')


        send_telegram('\n'.join(lines))
        logger.info('📅 Monthly summary sent to Discord')
    except Exception as e:
        logger.warning(f'send_monthly_summary: {e}')


def _log_apex_regime_snapshot(symbol: str, ss: dict, dec: dict) -> None:
    """Appends one regime/sentiment record per symbol, at most once per
    APEX_REGIME_LOG_INTERVAL_HOURS -- gate lives on the symbol's own state
    dict so a restart doesn't cause a burst of duplicate entries right at
    the top of the hour. Never raises -- a logging failure must never
    affect trading."""
    try:
        now_ts = time.time()
        last_ts = ss.get('last_regime_log_ts', 0)
        if now_ts - last_ts < APEX_REGIME_LOG_INTERVAL_HOURS * 3600:
            return
        ss['last_regime_log_ts'] = now_ts
        record = {
            'ts': now_utc_iso(), 'symbol': symbol, 'action': dec.get('action'),
            'confidence': dec.get('confidence'), 'regime': dec.get('regime'),
            'trend_direction': dec.get('trend_direction'),
            'adx': (dec.get('indicators') or {}).get('adx'),
        }
        with open(APEX_REGIME_LOG_FILE, 'a') as f:
            f.write(json.dumps(record) + '\n')
    except Exception as e:
        logger.warning(f'_log_apex_regime_snapshot: {e}')


def _summarize_apex_regime_sentiment(days: int = 7) -> dict:
    """Aggregates apex_regime_history.jsonl into a compact market-sentiment
    summary -- the Apex-side counterpart to SPY's recent_market_sentiment
    (added the same day, on request, for the same reason: give Hermes's now-
    daily analysis a real memory of conditions, not just trade P&L)."""
    try:
        if not os.path.exists(APEX_REGIME_LOG_FILE):
            return {}
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        records = []
        with open(APEX_REGIME_LOG_FILE) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get('ts', '') >= cutoff:
                    records.append(r)
        if not records:
            return {}
        adx_vals = [r['adx'] for r in records if r.get('adx') is not None]
        regimes, trends, actions = {}, {}, {}
        for r in records:
            for bucket, key in ((regimes, 'regime'), (trends, 'trend_direction'), (actions, 'action')):
                v = r.get(key)
                if v: bucket[v] = bucket.get(v, 0) + 1
        by_symbol = {}
        for sym in TRADING_SYMBOLS:
            sym_records = [r for r in records if r.get('symbol') == sym]
            if not sym_records:
                continue
            sym_trends = {}
            for r in sym_records:
                t = r.get('trend_direction')
                if t: sym_trends[t] = sym_trends.get(t, 0) + 1
            by_symbol[sym] = {'n_samples': len(sym_records), 'trend_counts': sym_trends}
        return {
            'window_days': days,
            'n_samples': len(records),
            'adx': {'min': round(min(adx_vals), 1), 'max': round(max(adx_vals), 1),
                    'avg': round(sum(adx_vals) / len(adx_vals), 1)} if adx_vals else None,
            'regime_counts': regimes,
            'trend_counts': trends,
            'action_counts': actions,
            'by_symbol': by_symbol,
        }
    except Exception as e:
        logger.info(f'_summarize_apex_regime_sentiment failed (non-critical): {e}')
        return {}


# ── Per-symbol cycle ──────────────────────────────────────────────────────────
def run_symbol(symbol: str, cfg: dict, allow_new_entry: bool = True) -> dict:
    global last_hold_alert
    ss        = sym_state(symbol)
    base      = SYMBOLS_CONFIG[symbol]['base']
    dash_file = SYMBOLS_CONFIG[symbol]['dashboard_file']

    try:
        position = detect_futures_position(symbol)
        ss['position'] = position

        df    = get_market_data(symbol)
        price = get_current_price(symbol)
        dec   = get_decision(symbol, df)
        action     = dec['action']
        confidence = dec['confidence']
        regime     = dec['regime']
        trend      = dec['trend_direction']
        reason     = dec['reason']
        indicators = dec['indicators']

        logger.info(f'💰 [{symbol}] ${price:.4f} | signal={action}({confidence}%) | pos={position} | {regime}/{trend}')

        _log_apex_regime_snapshot(symbol, ss, dec)
        run_paper_offhours(symbol, price, action, confidence, reason)

        # ── Dashboard close request ───────────────────────────────────────────
        dashboard_close_executed = False
        close_sym = cfg.get('close_symbol')
        if cfg.get('close_requested') and position and (close_sym == symbol or close_sym is None):
            req_at = cfg.get('close_requested_at')
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(
                    req_at.replace('Z', '+00:00'))).total_seconds() if req_at else 999
            except Exception:
                age = 999
            if age < 300:
                send_telegram(f'📱 <b>Dashboard Close</b>\nClosing {position} {base}/USDT Futures @ ${price:,.4f}')
                closed = close_long(symbol, price, 'Dashboard close') if position == 'LONG' \
                    else close_short(symbol, price, 'Dashboard close')
                if closed:
                    dashboard_close_executed = True
                    clear_trail(symbol)
                    position = None
                    clear_flag('futures_close_requested')

        # ── Force trail ───────────────────────────────────────────────────────
        ft_sym = cfg.get('force_trail_symbol')
        if not cfg.get('force_trail'):
            ss['force_trail_processed'] = False
        if cfg.get('force_trail') and position and not ss.get('force_trail_processed') \
                and (ft_sym == symbol or ft_sym is None):
            if not ss.get('trail_entry_price'):
                init_trail(symbol, price)
            initial_stop = price - 0.50 if position == 'LONG' else price + 0.50
            ss['force_trail_stop_price'] = price   # click price — used to measure gain from click
            ss['force_trail_active']     = True
            ss['force_trail_processed']  = True
            save_state()
            logger.info(f'🔒 [{symbol}] Force trail locked | click={price:.4f} initial_stop={initial_stop:.4f}')
            send_telegram(f'🔒 <b>Force Trail {base}/USDT Futures</b>\nPrice: ${price:,.4f}\nInitial stop: ${initial_stop:,.4f} (widens as price rises)')

        # ── Trail sync ────────────────────────────────────────────────────────
        if position and ss.get('trail_entry_price') is None:
            # Restore entry price and qty from Binance (e.g. after bot restart)
            pos_details = get_position_details(symbol)
            restored_ep = pos_details['entry_price']
            if restored_ep:
                init_trail(symbol, restored_ep)
                logger.info(f'🔄 [{symbol}] Restored entry from Binance: ${restored_ep:.4f}')
            else:
                init_trail(symbol, price)
            if pos_details['qty'] and not ss.get('active_qty'):
                ss['active_qty'] = pos_details['qty']
                logger.info(f'🔄 [{symbol}] Restored qty from Binance: {pos_details["qty"]}')
        if not position and ss.get('trail_entry_price') is not None:
            clear_trail(symbol)

        # ── SL / Trail check ──────────────────────────────────────────────────
        sl_close = False
        status   = ''
        if position and not dashboard_close_executed:
            sl_hit, sl_reason = check_sl_trail(symbol, position, price)
            if sl_hit:
                send_telegram(f'🛑 <b>SL/Trail {base}/USDT Futures</b>\n{sl_reason}\nClosing @ ${price:,.4f}')
                closed_ok = close_long(symbol, price, sl_reason) if position == 'LONG' \
                            else close_short(symbol, price, sl_reason)
                sl_close = True
                clear_trail(symbol)
                if 'Hard SL' in sl_reason:
                    ss['last_hard_sl_ts'] = int(time.time())
                # Trust the close — don't re-query Binance (residual dust would re-trigger)
                position = None if closed_ok else detect_futures_position(symbol)

        # ── Entry / management ────────────────────────────────────────────────
        bot_paused = cfg.get('bot_paused', False)
        if dashboard_close_executed or sl_close:
            action = 'HOLD'
            status = 'CLOSED ✅'
        elif not bot_paused:
            if position is None and action in ('LONG', 'SHORT') and not allow_new_entry:
                status = f'HOLD — other symbol has better setup right now'
            elif position is None and action in ('LONG', 'SHORT') and \
                    SYMBOLS_CONFIG[symbol].get('market_hours_only') and not is_us_market_open():
                logger.info(f'⏰ [{symbol}] Market closed — skipping entry')
                status = 'HOLD — market closed'
            elif position is None and action in ('LONG', 'SHORT') and not in_entry_window(SYMBOLS_CONFIG[symbol]):
                logger.info(f'⏰ [{symbol}] Outside entry window — skipping entry')
                status = 'HOLD — outside entry window'
            elif position is None and action in ('LONG', 'SHORT') and \
                    get_daily_realized_pnl(symbol) >= SYMBOLS_CONFIG[symbol].get('daily_profit_lock', DAILY_PROFIT_LOCK_DEFAULT) and \
                    confidence < DAILY_PROFIT_LOCK_CONFIDENCE:
                _daily_pnl  = get_daily_realized_pnl(symbol)
                _lock_level = SYMBOLS_CONFIG[symbol].get('daily_profit_lock', DAILY_PROFIT_LOCK_DEFAULT)
                logger.info(f'🔒 [{symbol}] Daily profit lock — ${_daily_pnl:.2f} banked today '
                            f'(≥${_lock_level:.2f}), need confidence≥{DAILY_PROFIT_LOCK_CONFIDENCE} got {confidence}%')
                status = f'HOLD — daily profit lock (${_daily_pnl:.2f} today, need confidence≥{DAILY_PROFIT_LOCK_CONFIDENCE}%)'
            elif position is None and action in ('LONG', 'SHORT'):
                # Correlation guard — skip entry if a highly correlated symbol is already in trade
                _corr_blocked = False
                for _grp in CORR_GROUPS:
                    if symbol in _grp:
                        _busy = [s for s in _grp if s != symbol and sym_state(s).get('position')]
                        if _busy:
                            _busy_names = ', '.join(s.replace('USDT', '') for s in _busy)
                            status = f'HOLD — correlated with {_busy_names} (same-sector guard)'
                            logger.info(f'[{symbol}] Correlation block: {_busy_names} already in trade')
                            _corr_blocked = True
                            break

                if _corr_blocked:
                    pass  # status already set above
                elif action == 'SHORT' and SYMBOLS_CONFIG[symbol].get('long_only'):
                    status = 'HOLD — long only mode'
                elif action == 'LONG' and SYMBOLS_CONFIG[symbol].get('short_only'):
                    status = 'HOLD — short only mode'
                else:
                    veto_ok, veto_reason = check_hermes_veto(symbol, dec, price)
                    if not veto_ok:
                        status = f'HOLD — Hermes veto: {veto_reason}'
                        logger.info(f'🛑 [{symbol}] Hermes vetoed {action} entry: {veto_reason}')
                        send_telegram(f'🛑 <b>Hermes Veto</b> {base}/USDT Futures\n'
                                      f'Blocked {action} @ ${price:,.4f} (confidence {confidence}%)\n'
                                      f'Technical reason: {reason}\nHermes: {veto_reason}')
                    elif action == 'LONG':
                        ok = open_long(symbol, price, confidence, reason, indicators, dec['trend_direction'])
                        status = 'LONG OPENED ✅' if ok else 'LONG FAILED ❌'
                        if ok: position = 'LONG'
                    else:
                        ok = open_short(symbol, price, confidence, reason, indicators, dec['trend_direction'])
                        status = 'SHORT OPENED ✅' if ok else 'SHORT FAILED ❌'
                        if ok: position = 'SHORT'
            elif position == 'LONG':
                ind_ = dec.get('indicators', {})
                trend4h_ = get_4h_trend(symbol)
                # Early exit: 4H flipped bearish OR strong opposing indicators while LONG
                early_exit_long = (
                    trend4h_ == '4H BEARISH' or
                    (action == 'SHORT') or
                    (action == 'HOLD' and safe_float(ind_.get('rsi'), 50) > 68 and
                     safe_float(ind_.get('adx_neg'), 0) > safe_float(ind_.get('adx_pos'), 0))
                )
                if early_exit_long and action in ('SHORT', 'HOLD'):
                    exit_reason = 'Signal reversed to SHORT' if action == 'SHORT' else f'Early exit — 4H bearish or momentum fading'
                    if close_long(symbol, price, exit_reason):
                        clear_trail(symbol); position = None
                        status = f'LONG CLOSED — {exit_reason} ✅'
                    else:
                        status = 'LONG CLOSE FAILED ❌'
                else:
                    status = f'HOLDING LONG | {reason}'
            elif position == 'SHORT':
                ind_ = dec.get('indicators', {})
                trend4h_ = get_4h_trend(symbol)
                # Early exit: 4H flipped bullish OR strong opposing indicators while SHORT
                early_exit_short = (
                    trend4h_ == '4H BULLISH' or
                    (action == 'LONG') or
                    (action == 'HOLD' and safe_float(ind_.get('rsi'), 50) < 32 and
                     safe_float(ind_.get('adx_pos'), 0) > safe_float(ind_.get('adx_neg'), 0))
                )
                if early_exit_short and action in ('LONG', 'HOLD'):
                    exit_reason = 'Signal reversed to LONG' if action == 'LONG' else f'Early exit — 4H bullish or momentum fading'
                    if close_short(symbol, price, exit_reason):
                        clear_trail(symbol); position = None
                        status = f'SHORT CLOSED — {exit_reason} ✅'
                    else:
                        status = 'SHORT CLOSE FAILED ❌'
                else:
                    status = f'HOLDING SHORT | {reason}'

        if bot_paused and not status:
            status = '⏸️ BOT PAUSED'
        status = status or f'HOLD — {reason}'

        # ── Final state ───────────────────────────────────────────────────────
        final_pos_details = get_position_details(symbol)
        latest_pos = final_pos_details['side']
        ss['position'] = latest_pos
        save_state()

        # Read from persistent log (survives restarts); filter to this symbol
        all_logged    = load_trade_log()
        closed_trades = [t for t in all_logged if t.get('symbol') == symbol]
        performance   = summarize_performance(closed_trades)
        trail_info    = build_trail_info(symbol, latest_pos)
        balance       = get_futures_balance('USDT')

        payload = {
            'generated_at':    now_utc_iso(),
            'symbol':          symbol,
            'exchange':        'Binance Futures',
            'price':           price,
            'usdt_balance':    balance['free'],
            'leverage':        SYMBOLS_CONFIG[symbol].get('leverage') or state['runtime']['leverage'],
            'trade_amount':    SYMBOLS_CONFIG[symbol].get('trade_amount') or state['runtime']['trade_amount_usdt'],
            'atr_health':      state['runtime'].get('atr_health', {}).get(symbol),
            'position':        latest_pos,
            'active_qty':      ss.get('active_qty') or final_pos_details['qty'] or None,
            'unrealized_pnl':  final_pos_details['unrealized_pnl'],
            'trade_opened_at': ss.get('trade_opened_at'),
            'action':          action,
            'confidence':      confidence,
            'status':          status,
            'bot_paused':      bot_paused,
            'regime':          regime,
            'trend_direction': trend,
            'reason':          reason,
            'indicators':      indicators,

            'closed_trades':   closed_trades,
            'performance':     performance,
            'trail':           trail_info,
            'run_count':       run_count,
        }
        push_dashboard_data(payload, dash_file)
        return payload

    except Exception as e:
        logger.error(f'run_symbol [{symbol}] error: {e}', exc_info=True)
        alert_error(f'{symbol}: {e}')
        return {}


# ── Dust Cleanup ──────────────────────────────────────────────────────────────
DUST_USDT_THRESHOLD = 5.0  # close residual positions worth less than $5

def cleanup_dust():
    """Close tiny leftover positions that are below the dust threshold."""
    for symbol in TRADING_SYMBOLS:
        try:
            positions = binance_futures_private('GET', '/fapi/v2/positionRisk', {'symbol': symbol})
            for p in positions:
                if p['symbol'] != symbol: continue
                amt      = float(p.get('positionAmt', 0))
                entry    = safe_float(p.get('entryPrice'), 0)
                notional = abs(amt) * entry
                if abs(amt) < 1e-8 or notional >= DUST_USDT_THRESHOLD: continue
                # This is dust — bot state says no position but Binance has a residual
                ss = sym_state(symbol)
                if ss.get('position'): continue  # bot thinks it's in a real trade, skip
                pos_side = p.get('positionSide', 'BOTH')
                side     = 'LONG' if (pos_side == 'LONG' or (pos_side == 'BOTH' and amt > 0)) else 'SHORT'
                close_side = 'SELL' if side == 'LONG' else 'BUY'
                step     = get_futures_step_size(symbol)
                quantity = round_step(abs(amt), step)
                if quantity < step: continue
                logger.warning(f'🧹 [{symbol}] Dust detected: {amt} {side} (${notional:.2f}) — closing')
                futures_market_order(symbol, close_side, quantity, position_side=pos_side if pos_side != 'BOTH' else None)
                send_telegram(f'🧹 <b>Dust Cleaned [{symbol}]</b>\nResidual {side} {abs(amt):.4f} (${notional:.2f}) auto-closed')
        except Exception as e:
            logger.warning(f'cleanup_dust [{symbol}]: {e}')


# ── Overnight MU Strategy ─────────────────────────────────────────────────────
def open_overnight_mu() -> bool:
    on = state.get('overnight_mu', {})
    if on.get('position'):
        return False
    sym    = OVERNIGHT_CFG['symbol']
    amount = get_mu_cfg('amount', OVERNIGHT_CFG['amount'])
    lev    = get_mu_cfg('leverage', OVERNIGHT_CFG['leverage'])
    override_active = 'MUUSDT' in state.get('runtime', {}).get('auto_tune_overrides', {})
    logger.info(f'[OVERNIGHT] MU sizing this open: ${amount:.0f}/{lev}x (override_active={override_active})')
    try:
        price    = get_current_price(sym)
        set_futures_leverage(sym, lev)
        step     = get_futures_step_size(sym)
        quantity = round_step((amount * lev * 0.995) / price, step)
        if quantity < step:
            logger.warning('[OVERNIGHT] MU quantity too small')
            return False
        resp         = futures_market_order(sym, 'BUY', quantity, position_side='LONG')
        actual_price = get_fill_price(resp, price)
        qty_filled   = float(resp.get('executedQty') or 0) or quantity
        sl_price     = round(actual_price * (1 - OVERNIGHT_CFG['sl_pct']), 4)
        state['overnight_mu'] = {
            'position':    'LONG',
            'entry_price': actual_price,
            'qty':         qty_filled,
            'entry_fee':   qty_filled * actual_price * FEE_RATE,
            'sl_price':    sl_price,
            'opened_at':   now_utc_iso(),
        }
        save_state()
        logger.info(f'🌙 [OVERNIGHT] MU OPEN {qty_filled:.4f} @ ${actual_price:.4f} SL=${sl_price:.4f}')
        send_telegram(
            f'🌙 <b>OVERNIGHT — MU LONG OPEN ({lev}x)</b>\n\n'
            f'💰 Entry: ${actual_price:,.4f}\n'
            f'💵 Collateral: ${amount:.2f} | Notional: ${amount*lev:.2f}\n'
            f'🪙 Qty: {qty_filled:.4f} MU\n'
            f'🛑 Stop Loss: ${sl_price:,.4f} (3.5%)\n'
            f'⏰ Exit at market open ~9:30 AM ET'
        )
        write_overnight_dashboard()
        return True
    except Exception as e:
        logger.error(f'[OVERNIGHT] open failed: {e}')
        alert_error(f'Overnight MU open: {e}')
        return False

def close_overnight_mu(reason: str) -> bool:
    on = state.get('overnight_mu', {})
    if not on.get('position'):
        return False
    sym = OVERNIGHT_CFG['symbol']
    try:
        positions = binance_futures_private('GET', '/fapi/v2/positionRisk', {'symbol': sym})
        qty_held  = 0.0
        for p in positions:
            if p['symbol'] == sym and p.get('positionSide', 'BOTH') in ('LONG', 'BOTH'):
                qty_held = abs(float(p['positionAmt']))
                if qty_held > 1e-8: break
        step     = get_futures_step_size(sym)
        quantity = round_step(qty_held, step)
        if quantity < step:
            state['overnight_mu'] = {}
            save_state()
            return False
        price        = get_current_price(sym)
        resp         = futures_market_order(sym, 'SELL', quantity, position_side='LONG', reduce_only=True)
        actual_close = get_fill_price(resp, price)
        entry_price  = on['entry_price']
        total_fee    = on.get('entry_fee', 0.0) + quantity * actual_close * FEE_RATE
        gross        = (actual_close - entry_price) * quantity
        net          = gross - total_fee
        pnl_banner   = f'🟢 +${net:.2f}' if net >= 0 else f'🔴 -${abs(net):.2f}'
        logger.info(f'🌅 [OVERNIGHT] MU CLOSE @ ${actual_close:.4f} net={net:+.4f} reason={reason}')
        send_telegram(
            f'{pnl_banner}\n'
            f'🌅 <b>OVERNIGHT — MU CLOSED ({reason})</b>\n\n'
            f'💰 Exit: ${actual_close:,.4f} | Entry: ${entry_price:,.4f}\n'
            f'🪙 Qty: {quantity:.4f} MU\n'
            f'✅ Gross: {gross:+.4f} USDT\n💸 Fees: -{total_fee:.4f} USDT\n'
            f'🏦 Net P&L: {net:+.4f} USDT'
        )
        state.setdefault('overnight_mu_trades', []).append({
            'opened_at':   on.get('opened_at'),
            'closed_at':   now_utc_iso(),
            'entry_price': entry_price,
            'exit_price':  actual_close,
            'qty':         quantity,
            'gross':       round(gross, 4),
            'fee':         round(total_fee, 4),
            'net':         round(net, 4),
            'reason':      reason,
        })
        state['overnight_mu'] = {}
        clear_trail(sym)
        # One-time migration cutover -- user request 2026-10-03: MU's real
        # trading is moving to Sentinel (separation of concerns, one bot one
        # strategy). Rather than rely on flipping a switch at the right
        # moment, this disables Apex's own entry path the instant MU next
        # closes for ANY reason (this normal exit, a stop-loss, a manual
        # close) -- Sentinel's replica engine picks up the next entry window
        # from here on. Does not touch the close that's already in progress
        # above; only prevents a FUTURE entry. Remove this block (and the
        # guard in run_overnight_strategy's entry section) once Apex's MU
        # code is fully retired.
        state['overnight_mu_disabled'] = True
        save_state()
        logger.info('[OVERNIGHT] MU disabled on Apex after this close -- Sentinel takes over entries from here')
        send_telegram('🔁 <b>MU migration cutover</b>\nApex will not open another MU position. Sentinel takes the next entry window.')
        write_overnight_dashboard()
        return True
    except Exception as e:
        logger.error(f'[OVERNIGHT] close failed: {e}')
        alert_error(f'Overnight MU close: {e}')
        return False

def _overnight_mu_perf(trades: list) -> dict:
    wins   = [t for t in trades if t.get('net', 0) > 0]
    losses = [t for t in trades if t.get('net', 0) <= 0]
    total  = len(trades)
    return {
        'total':      total,
        'wins':       len(wins),
        'losses':     len(losses),
        'win_rate':   round(len(wins) / total * 100, 1) if total else 0,
        'win_pnl':    round(sum(t['net'] for t in wins), 2),
        'loss_pnl':   round(sum(t['net'] for t in losses), 2),
        'total_fees': round(sum(t.get('fee', 0) for t in trades), 2),
        'net_pnl':    round(sum(t.get('net', 0) for t in trades), 2),
    }

def write_overnight_dashboard(price: float = None) -> None:
    on     = state.get('overnight_mu', {})
    trades = state.get('overnight_mu_trades', [])
    current_price = unrealized_pnl = None
    if on.get('position'):
        try:
            current_price  = price if price is not None else get_current_price(OVERNIGHT_CFG['symbol'])
            unrealized_pnl = round((current_price - on['entry_price']) * on.get('qty', 0), 4)
        except Exception:
            pass
        # Prefer Binance's own unrealized P&L (marked at the exchange's mark
        # price) so this matches what the trader sees in the Binance app --
        # same source the 7 futures symbols' dashboards already use.
        try:
            ex = get_position_details(OVERNIGHT_CFG['symbol'])
            if ex.get('side') and ex.get('unrealized_pnl') is not None:
                unrealized_pnl = round(ex['unrealized_pnl'], 4)
        except Exception:
            pass
    payload = {
        'generated_at':  now_utc_iso(),
        'position':      on.get('position'),
        'entry_price':   on.get('entry_price'),
        'sl_price':      on.get('sl_price'),
        'sl_custom':     bool(on.get('sl_custom')),
        'sl_default':    round(on['entry_price'] * (1 - OVERNIGHT_CFG['sl_pct']), 4) if on.get('entry_price') else None,
        'target_price':  on.get('target_price'),
        'entry_fee':     on.get('entry_fee'),
        'fee_rate':      FEE_RATE,
        'levels_status': on.get('levels_status'),
        'qty':           on.get('qty'),
        'opened_at':     on.get('opened_at'),
        'current_price': current_price,
        'unrealized_pnl': unrealized_pnl,
        # Current per-trade stake -- the base the dashboard's "% return" is
        # measured against (same base the auto-scaler's avg-net bar uses).
        'amount':        get_mu_cfg('amount', OVERNIGHT_CFG['amount']),
        'leverage':      get_mu_cfg('leverage', OVERNIGHT_CFG['leverage']),
        'performance':   _overnight_mu_perf(trades),
        'trades': list(reversed(trades)),
    }
    write_json(os.path.join(WEB_ROOT, 'data_overnight_mu.json'), payload)


def run_weekly_mu_auto_tune() -> None:
    """Evidence-gated size/leverage scaling for the MU overnight strategy,
    mirroring run_weekly_auto_tune()'s gate -> evaluate -> apply -> log ->
    notify shape -- but deterministic and self-contained, not a price-replay
    backtest (size/leverage don't change which trades win or lose, just the
    dollar/notional scale, so a live rolling-window check on real trades is
    the right tool here, not a kline replay)."""
    last = state['runtime'].get('last_mu_auto_tune')
    if last:
        try:
            age_days = (datetime.now(timezone.utc) -
                        datetime.fromisoformat(last.replace('Z', '+00:00'))).days
        except Exception:
            age_days = MU_AUTO_TUNE_INTERVAL_DAYS
        if age_days < MU_AUTO_TUNE_INTERVAL_DAYS:
            return
    if state.get('overnight_mu', {}).get('position'):
        logger.info('🔧 MU auto-scale: deferred -- position open, retry next cycle')
        return

    state['runtime']['last_mu_auto_tune'] = now_utc_iso()
    overrides = state['runtime'].setdefault('auto_tune_overrides', {})
    history   = state['runtime'].setdefault('auto_tune_history', [])
    save_state()

    try:
        trades = [t for t in state.get('overnight_mu_trades', [])
                  if t.get('opened_at') and t.get('net') is not None]
        cur_amount = get_mu_cfg('amount', OVERNIGHT_CFG['amount'])
        cur_lev    = get_mu_cfg('leverage', OVERNIGHT_CFG['leverage'])
        cur_idx = next((i for i, lvl in enumerate(MU_SCALE_LADDER)
                         if lvl['amount'] == cur_amount and lvl['leverage'] == cur_lev), 0)

        existing   = overrides.get('MUUSDT')
        applied_at = existing.get('_meta', {}).get('applied_at') if existing else None
        window     = [t for t in trades if not applied_at or t['opened_at'] > applied_at]
        min_needed = MU_MIN_TRADES_FIRST_SCALE if not applied_at else MU_ROLLBACK_MIN_TRADES
        if len(window) < min_needed:
            logger.info(f'🔧 MU auto-scale: {len(window)}/{min_needed} trades since last check -- not enough yet')
            return

        wins     = [t for t in window if t['net'] > 0]
        win_rate = len(wins) / len(window)
        avg_net  = sum(t['net'] for t in window) / len(window)
        streak = 0
        for t in sorted(window, key=lambda t: t['opened_at'], reverse=True):
            if t['net'] <= 0: streak += 1
            else: break

        decision = None
        if (win_rate < MU_SCALE_DOWN_MIN_WIN_RATE or avg_net < 0
                or streak >= MU_SCALE_DOWN_LOSE_STREAK) and cur_idx > 0:
            decision = 'down'
        elif (win_rate >= MU_SCALE_UP_MIN_WIN_RATE
                and avg_net >= MU_SCALE_UP_AVG_NET_PCT * cur_amount) and cur_idx < len(MU_SCALE_LADDER) - 1:
            decision = 'up'

        reason = (f'{len(window)} trades at ${cur_amount:.0f}/{cur_lev}x averaged ${avg_net:+.2f}/trade '
                  f'({win_rate*100:.0f}% WR, {streak}-loss streak)')

        if decision is None:
            history.append({'symbol': 'MUUSDT', 'action': 'validated',
                             'params': overrides.get('MUUSDT', {}), 'at': now_utc_iso(), 'note': reason})
            save_state()
            write_hermes_log_dashboard()
            logger.info(f'🔧 MU auto-scale: no change -- {reason}')
            return

        new_idx = cur_idx + (1 if decision == 'up' else -1)
        new_lvl = MU_SCALE_LADDER[new_idx]

        if not MU_AUTO_SCALE_ENABLED:
            send_telegram(
                f'🧪 <b>MU Auto-Scale DRY RUN</b>\n\n'
                f'Would {"scale UP" if decision=="up" else "scale DOWN"} '
                f'${cur_amount:.0f}/{cur_lev}x → ${new_lvl["amount"]:.0f}/{new_lvl["leverage"]}x\n\n'
                f'{reason}\n\nNot applied — dry-run mode.'
            )
            logger.info(f'🧪 [MU] auto-scale dry-run: → ${new_lvl["amount"]:.0f}/{new_lvl["leverage"]}x — {reason}')
            return  # dry-run: deliberately not written to auto_tune_history/overrides

        if new_idx == 0:
            overrides.pop('MUUSDT', None)
        else:
            overrides['MUUSDT'] = {
                'amount': new_lvl['amount'], 'leverage': new_lvl['leverage'],
                '_meta': {'param': 'mu_scale', 'value': f'${new_lvl["amount"]:.0f}/{new_lvl["leverage"]}x',
                          'applied_at': now_utc_iso(), 'baseline_avg_pnl': avg_net,
                          'step_index': new_idx, 'source': 'mu_auto_scale'},
            }
        # 'reverted' means back at the floor with no override active; a scale-
        # down that lands on an intermediate rung is still an elevated
        # override, not a reversion to baseline -- distinct action so the
        # dashboard badge doesn't overstate how conservative it went.
        if decision == 'up':
            mu_action = 'applied'
        elif new_idx == 0:
            mu_action = 'reverted'
        else:
            mu_action = 'scaled_down'
        history.append({
            'symbol': 'MUUSDT', 'action': mu_action,
            'params': overrides.get('MUUSDT', {'amount': new_lvl['amount'], 'leverage': new_lvl['leverage']}),
            'at': now_utc_iso(), 'baseline_total': round(avg_net * len(window), 2),
            'reason': reason, 'source': 'mu_auto_scale',
        })
        save_state()
        send_telegram(
            f'{"⬆️" if decision=="up" else "⬇️"} <b>MU Auto-Scale {"Up" if decision=="up" else "Down"}</b>\n\n'
            f'${cur_amount:.0f}/{cur_lev}x → ${new_lvl["amount"]:.0f}/{new_lvl["leverage"]}x\n\n{reason}'
        )
        write_hermes_log_dashboard()
        logger.info(f'🔧 MU auto-scale {decision}: ${cur_amount:.0f}/{cur_lev}x → '
                    f'${new_lvl["amount"]:.0f}/{new_lvl["leverage"]}x -- {reason}')
    except Exception as e:
        logger.warning(f'run_weekly_mu_auto_tune failed (non-critical, MU keeps trading at current size): {e}')


# ── Hermes Daily Picks (paper only) ────────────────────────────────────────────
# Added 2026-09-18, on explicit request: let Hermes scan Binance's real
# tokenized-stock/ETF perpetuals daily, pick its own trade, enter/manage/close
# it with zero human involvement, and explain its reasoning at both ends --
# but PAPER ONLY, no real orders anywhere in this feature, so a genuine track
# record can build before any real capital is ever considered. This is
# deliberately a separate universe/pool from MU's off-hours pilot and from
# TRADING_SYMBOLS -- it includes the 8 real-money tickers too (on purpose, so
# a Hermes pick landing on one of them is an implicit, free comparison).
HERMES_PICK_TOP_N                 = 8
HERMES_PICK_CHECK_INTERVAL_HOURS  = 1
HERMES_PICK_DISCOVERY_ET_HOUR     = 10   # ~30 min after US market open, past opening-auction noise
HERMES_PICK_MAX_HOLD_MULTIPLIER   = 3    # deterministic backstop: force-close at 3x expected_hold_days
                                          # even if /pick_checkin stays unreachable -- mirrors MU's hard-SL backstop
HERMES_PICK_URL    = 'http://10.122.0.3:8787/pick_trade'
HERMES_CHECKIN_URL = 'http://10.122.0.3:8787/pick_checkin'
EQUITY_UNIVERSE_CACHE_TTL_HOURS   = 24

def discover_equity_perpetuals(force: bool = False) -> list:
    """USDT-margined tokenized-equity perpetuals (underlyingType == 'EQUITY',
    status TRADING) -- confirmed live against Binance's real exchangeInfo,
    ~155 real stock/ETF tickers (AAPL, NVDA, TSLA, SPY, QQQ, the 8 this bot
    already trades, and many more), not a guessed/curated list. Cached in
    state for 24h since the roster doesn't change daily. Fail-open: any
    error returns the last good cache (even stale), or [] if never cached --
    caller treats [] as 'skip discovery this cycle', never raises."""
    cached = state.get('hermes_picks_universe', {})
    if not force and cached.get('symbols'):
        try:
            age_h = (datetime.now(timezone.utc) -
                      datetime.fromisoformat(cached['discovered_at'].replace('Z', '+00:00'))).total_seconds() / 3600
            if age_h < EQUITY_UNIVERSE_CACHE_TTL_HOURS:
                return cached['symbols']
        except Exception:
            pass
    try:
        info = binance_futures_public('/fapi/v1/exchangeInfo')
        symbols = [s['symbol'] for s in info.get('symbols', [])
                   if s.get('underlyingType') == 'EQUITY'
                   and s.get('quoteAsset') == 'USDT'
                   and s.get('status') == 'TRADING']
        state['hermes_picks_universe'] = {'symbols': symbols, 'discovered_at': now_utc_iso()}
        save_state()
        return symbols
    except Exception as e:
        logger.warning(f'discover_equity_perpetuals failed: {e}')
        return cached.get('symbols', [])

def score_equity_candidate(symbol: str) -> Optional[dict]:
    """Cheap momentum/breakout score, 0-100, from the SAME indicators
    get_decision() already uses for real trading (get_market_data() ->
    _build_1h_indicators()) -- no separate indicator math to keep in sync.
    Returns None on any fetch/compute failure (thin data, newly-listed
    symbol) -- caller just drops it, never raises."""
    try:
        df = get_market_data(symbol)
        p = df.iloc[-1]
        adx, adx_pos, adx_neg = float(p['adx']), float(p['adx_pos']), float(p['adx_neg'])
        ema21, ema50, rsi, atr = float(p['ema21']), float(p['ema50']), float(p['rsi']), float(p['atr'])
        price, vol, vol_ma = float(p['close']), float(p['volume']), float(p['vol_ma'])
        if pd.isna(adx) or pd.isna(ema21) or pd.isna(vol_ma) or vol_ma <= 0:
            return None
        trend = 'BULLISH' if (adx_pos > adx_neg and ema21 > ema50) else \
                'BEARISH' if (adx_neg > adx_pos and ema21 < ema50) else 'NEUTRAL'
        vol_ratio = vol / vol_ma
        dc_upper, dc_lower = p.get('dc_upper'), p.get('dc_lower')
        breakout = 'UP'   if (dc_upper is not None and not pd.isna(dc_upper) and price > dc_upper) else \
                   'DOWN' if (dc_lower is not None and not pd.isna(dc_lower) and price < dc_lower) else None
        score = min(adx, 60) / 60 * 40 + min(vol_ratio, 2.5) / 2.5 * 25 + \
                (25 if breakout else 0) + (10 if trend != 'NEUTRAL' else 0)
        return {'symbol': symbol, 'score': round(score, 2), 'price': price, 'adx': round(adx, 2),
                'trend': trend, 'rsi': round(rsi, 2), 'atr': round(atr, 4),
                'breakout': breakout, 'vol_ratio': round(vol_ratio, 2)}
    except Exception:
        return None

def rank_hermes_candidates(top_n: int = HERMES_PICK_TOP_N) -> list:
    scored = []
    for sym in discover_equity_perpetuals():
        s = score_equity_candidate(sym)
        if s:
            scored.append(s)
    scored.sort(key=lambda x: x['score'], reverse=True)
    return scored[:top_n]

def get_max_bracket_leverage(symbol: str, notional: float = 50.0) -> int:
    """Real bracket max leverage for a given notional, queried live (same
    /fapi/v1/leverageBracket API already confirmed for MUUSDT tonight --
    bracket 1 = 50x up to $50k). Used ONLY for simulated paper-trade sizing
    math here -- never to set leverage on a real order. Fails to a
    conservative 5x on any error."""
    try:
        for entry in binance_futures_private('GET', '/fapi/v1/leverageBracket', {'symbol': symbol}):
            if entry.get('symbol') != symbol:
                continue
            for b in entry.get('brackets', []):
                if notional <= float(b.get('notionalCap', 0)):
                    return int(b['initialLeverage'])
            return int(entry['brackets'][0]['initialLeverage'])
    except Exception as e:
        logger.warning(f'get_max_bracket_leverage[{symbol}]: {e}')
    return 5

def log_hermes_pick_event(action: str, record: dict) -> None:
    """Reuses the SAME auto_tune_history feed the Hermes Log dashboard tab
    already reads, tagged source='hermes_pick' -- proven escape hatch
    (renderHermesLog() already special-cases non-Apex sources for MU's
    ladder). Also means push_hermes_snapshot() gives Hermes automatic memory
    of its own past picks, with zero extra plumbing."""
    try:
        history = state['runtime'].setdefault('auto_tune_history', [])
        history.append({
            'symbol': record.get('symbol'), 'action': action, 'source': 'hermes_pick',
            'at': now_utc_iso(), 'reason': record.get('exit_reasoning') or record.get('reasoning'),
            'params': {k: record.get(k) for k in
                       ('direction', 'entry_price', 'target', 'stop', 'exit_price', 'pnl_usdt')},
        })
        save_state()
        write_hermes_log_dashboard()
    except Exception as e:
        logger.warning(f'log_hermes_pick_event failed: {e}')

def write_hermes_picks_dashboard() -> None:
    try:
        trades = state.get('hermes_picks_trades', [])
        wins = [t for t in trades if t.get('win')]
        write_json(os.path.join(WEB_ROOT, 'data_hermes_picks.json'), {
            'generated_at': now_utc_iso(),
            'position': state.get('hermes_picks') or None,
            'performance': {
                'total': len(trades), 'wins': len(wins),
                'win_rate': round(len(wins) / len(trades) * 100, 1) if trades else 0,
                'net_pnl': round(sum(t.get('pnl_usdt', 0) for t in trades), 2),
            },
            'trades': list(reversed(trades[-30:])),
        })
    except Exception as e:
        logger.warning(f'write_hermes_picks_dashboard: {e}')

def open_hermes_pick(pick: dict, candidates: list) -> bool:
    if state.get('hermes_picks', {}).get('symbol'):
        return False   # one open pick at a time -- simplest, safest
    try:
        symbol = pick['symbol']
        price  = get_current_price(symbol)
        lev    = get_max_bracket_leverage(symbol)
        qty    = 50.0 * lev * 0.995 / price
        snapshot = next((c for c in candidates if c['symbol'] == symbol), {})
        record = {
            'symbol': symbol, 'direction': pick['direction'], 'entry_price': price,
            'entry_zone': [pick['entry_low'], pick['entry_high']], 'target': pick['target'],
            'stop': pick['stop'], 'expected_hold_days': pick['expected_hold_days'],
            'collateral_usdt': 50.0, 'leverage': lev, 'qty': qty,
            'reasoning': pick['reasoning'], 'candidate_snapshot': snapshot,
            'opened_at': now_utc_iso(), 'provider': pick.get('provider'), 'checkins': [],
        }
        state['hermes_picks'] = record
        save_state()
        base = symbol.replace('USDT', '')
        is_apex_ticker = base in {SYMBOLS_CONFIG[s]['base'] for s in SYMBOLS_CONFIG} or symbol == OVERNIGHT_CFG['symbol']
        note = ('\n\n📌 Note: this is one of Apex\'s own live-traded tickers — an implicit comparison.'
                 if is_apex_ticker else '')
        send_telegram(
            f"🔮 <b>Hermes Daily Pick — {pick['direction']} {base}/USDT (PAPER)</b>\n\n"
            f"💰 Entry: ${price:,.4f} (LLM zone ${pick['entry_low']:.4f}-${pick['entry_high']:.4f})\n"
            f"🎯 Target: ${pick['target']:,.4f} | 🛑 Stop: ${pick['stop']:,.4f}\n"
            f"⏱ Expected hold: {pick['expected_hold_days']} days\n"
            f"💵 $50 @ {lev}x (simulated — no real funds)\n\n"
            f"📊 {pick['reasoning']}{note}"
        )
        log_hermes_pick_event('opened', record)
        write_hermes_picks_dashboard()
        return True
    except Exception as e:
        logger.warning(f'open_hermes_pick failed: {e}')
        return False

def close_hermes_pick(exit_price: float, exit_reason: str, exit_reasoning: str) -> bool:
    pick = state.get('hermes_picks') or {}
    if not pick.get('symbol'):
        return False
    try:
        entry, qty, is_long = pick['entry_price'], pick['qty'], pick['direction'] == 'LONG'
        gross = (exit_price - entry) * qty if is_long else (entry - exit_price) * qty
        fee   = qty * (entry + exit_price) * FEE_RATE
        net   = gross - fee
        days_held = (datetime.now(timezone.utc) -
                     datetime.fromisoformat(pick['opened_at'].replace('Z', '+00:00'))).total_seconds() / 86400
        trade = {**pick, 'exit_price': exit_price, 'closed_at': now_utc_iso(), 'exit_reason': exit_reason,
                  'exit_reasoning': exit_reasoning, 'pnl_usdt': round(net, 4),
                  'pnl_pct': round(net / pick['collateral_usdt'] * 100, 2), 'win': net > 0,
                  'days_held': round(days_held, 1)}
        state.setdefault('hermes_picks_trades', []).append(trade)
        state['hermes_picks'] = {}
        save_state()
        base, emoji = pick['symbol'].replace('USDT', ''), ('🟢' if net >= 0 else '🔴')
        label = {'target_hit': 'Target Hit ✅', 'stop_hit': 'Stop Hit 🛑',
                 'time_exit': 'Time Exit ⏰', 'time_exit_forced': 'Forced Time Exit ⚠️'}[exit_reason]
        send_telegram(
            f"{emoji} <b>Hermes Daily Pick CLOSED — {label} ({base}/USDT)</b>\n\n"
            f"Entry: ${entry:,.4f} → Exit: ${exit_price:,.4f} | Held {days_held:.1f}d\n"
            f"Net P&L: {net:+.2f} USDT ({trade['pnl_pct']:+.1f}%) (simulated — no real funds)\n\n"
            f"📊 {exit_reasoning}"
        )
        log_hermes_pick_event(f'closed_{exit_reason}', trade)
        write_hermes_picks_dashboard()
        return True
    except Exception as e:
        logger.warning(f'close_hermes_pick failed: {e}')
        return False

def _ask_hermes_checkin(pick: dict, price: float, days_held: float) -> Optional[dict]:
    try:
        resp = requests.post(HERMES_CHECKIN_URL, json={
            'symbol': pick['symbol'], 'direction': pick['direction'], 'entry_price': pick['entry_price'],
            'target': pick['target'], 'stop': pick['stop'], 'entry_reasoning': pick['reasoning'],
            'opened_at': pick['opened_at'], 'expected_hold_days': pick['expected_hold_days'],
            'days_held': round(days_held, 1), 'current_price': price,
        }, timeout=30)
        resp.raise_for_status()
        body = resp.json()
        if body.get('decision') in ('HOLD', 'EXIT'):
            return {'decision': body['decision'], 'reasoning': body.get('reasoning', '')}
    except Exception as e:
        logger.info(f'[HERMES-PICK] check-in unreachable (non-critical): {e}')
    return None

def _monitor_open_hermes_pick(pick: dict) -> None:
    try:
        price = get_current_price(pick['symbol'])
        is_long = pick['direction'] == 'LONG'
        if (is_long and price >= pick['target']) or (not is_long and price <= pick['target']):
            close_hermes_pick(price, 'target_hit', pick['reasoning'] + ' — hit target as expected.')
            return
        if (is_long and price <= pick['stop']) or (not is_long and price >= pick['stop']):
            close_hermes_pick(price, 'stop_hit', pick['reasoning'] + ' — hit stop; setup invalidated.')
            return
        days_held = (datetime.now(timezone.utc) -
                     datetime.fromisoformat(pick['opened_at'].replace('Z', '+00:00'))).total_seconds() / 86400
        if days_held <= pick['expected_hold_days']:
            return
        if days_held > pick['expected_hold_days'] * HERMES_PICK_MAX_HOLD_MULTIPLIER:
            close_hermes_pick(price, 'time_exit_forced',
                               'Exceeded max hold safety window with no usable check-in response — closing defensively.')
            return
        checkin = _ask_hermes_checkin(pick, price, days_held)
        if checkin is None:
            return   # try again next hourly tick -- no forced action on a single failed call
        pick.setdefault('checkins', []).append(
            {'at': now_utc_iso(), 'decision': checkin['decision'], 'reasoning': checkin['reasoning'],
             'days_held': round(days_held, 1)})
        save_state()
        if checkin['decision'] == 'EXIT':
            close_hermes_pick(price, 'time_exit', checkin['reasoning'])
    except Exception as e:
        logger.warning(f'_monitor_open_hermes_pick failed: {e}')

def _run_daily_hermes_pick_discovery() -> None:
    candidates = rank_hermes_candidates()
    if not candidates:
        logger.info('[HERMES-PICK] No candidates today -- skipping')
        return
    payload = [dict(c, max_leverage=get_max_bracket_leverage(c['symbol'])) for c in candidates]
    try:
        resp = requests.post(HERMES_PICK_URL, json={'candidates': payload}, timeout=30)
        resp.raise_for_status()
        result = resp.json()
    except Exception as e:
        logger.info(f'[HERMES-PICK] pick_trade unreachable (non-critical): {e}')
        return
    if result.get('decision') == 'TRADE':
        open_hermes_pick(result, candidates)
    else:
        reason = result.get('reasoning') or result.get('reason') or 'No reason given'
        logger.info(f'[HERMES-PICK] No trade today: {reason}')
        send_telegram(f"🔮 <b>Hermes Daily Pick — No Trade Today</b>\n\n{reason}")

def run_hermes_picks_cycle() -> None:
    """Self-gated hourly check, same pattern as run_weekly_mu_auto_tune --
    safe to call every main-loop cycle. If a pick is already open, monitors
    it; otherwise runs discovery once/day after US market open. Fail-open at
    every layer: never raises into run_once(), never blocks real trading."""
    try:
        last = state['runtime'].get('last_hermes_pick_check')
        if last:
            age_h = (datetime.now(timezone.utc) -
                      datetime.fromisoformat(last.replace('Z', '+00:00'))).total_seconds() / 3600
            if age_h < HERMES_PICK_CHECK_INTERVAL_HOURS:
                return
        state['runtime']['last_hermes_pick_check'] = now_utc_iso()
        save_state()

        pick = state.get('hermes_picks') or {}
        if pick.get('symbol'):
            _monitor_open_hermes_pick(pick)
            return

        today = _et_now().date().isoformat()
        if state['runtime'].get('last_hermes_pick_attempt_date') == today or \
           _et_now().hour < HERMES_PICK_DISCOVERY_ET_HOUR:
            return
        state['runtime']['last_hermes_pick_attempt_date'] = today
        save_state()
        _run_daily_hermes_pick_discovery()
    except Exception as e:
        logger.warning(f'run_hermes_picks_cycle failed (non-critical): {e}')


# ── Off-hours/weekend PAPER trading (test ticker only) ────────────────────────
# NBIS backtested as the single best candidate for allowing off-hours/weekend
# entries (see session notes: weekend-inclusive backtest showed +$968 delta
# over market-hours-only, the largest of all 7 tickers) — but real weekend
# liquidity on these tokens is only ~8% of market-hours volume, so real
# execution risk is unverified. This runs the SAME live strategy signal
# during off-hours as a PAPER position (no real order, fully separate state
# from the real position) so we can validate against real live quotes before
# ever considering real money here. During real market hours every symbol
# trades exactly as before, unaffected by any of this.
# Expanded from NBIS-only to all 7 symbols 2026-09-17, on Hermes's recommendation
# (asked whether to relax market_hours_only; verdict was "no real off-hours
# evidence yet for any symbol -- expand this same paper pilot to all symbols,
# run 4-6 weeks / ~30-50 signals each, then revisit with real data").
PAPER_OFFHOURS_SYMBOLS = set(TRADING_SYMBOLS)

def paper_state(symbol: str) -> dict:
    state.setdefault('paper', {})
    state['paper'].setdefault(symbol, {'position': None, 'trades': []})
    return state['paper'][symbol]

def open_paper_position(symbol: str, side: str, price: float, confidence: int, reason: str) -> bool:
    cfg = SYMBOLS_CONFIG[symbol]
    ps  = paper_state(symbol)
    if ps.get('position'):
        return False
    amount   = float(cfg.get('trade_amount') or state['runtime']['trade_amount_usdt'])
    leverage = int(cfg.get('leverage') or state['runtime']['leverage'])
    qty      = (amount * leverage * 0.995) / price
    atr      = get_atr_1h(symbol)
    ps['position'] = {
        'side': side, 'entry_price': price, 'qty': qty, 'amount': amount, 'leverage': leverage,
        'trail_best_price': price, 'trail_atr': atr,
        'opened_at': now_utc_iso(), 'confidence': confidence, 'reason': reason,
    }
    save_state()
    base = cfg['base']
    logger.info(f'📝 [PAPER:{base}] {side} opened @ ${price:.4f} (off-hours/weekend, simulated)')
    send_telegram(
        f'📝 <b>PAPER — {side} {base}/USDT (off-hours test)</b>\n\n'
        f'💰 Entry: ${price:,.4f}\n'
        f'💵 ${amount:.0f} @ {leverage}x (simulated — no real order placed)\n'
        f'🎯 Confidence: {confidence}%\n📊 {reason}'
    )
    write_paper_dashboard(symbol)
    return True

def check_paper_trail(symbol: str, price: float) -> Tuple[bool, str]:
    """Standalone reimplementation of check_sl_trail's math, reading/writing
    the paper position's own stored fields instead of sym_state() — kept
    fully separate so nothing here can ever touch real trading state."""
    cfg = SYMBOLS_CONFIG[symbol]
    ps  = paper_state(symbol)
    pos = ps.get('position')
    if not pos:
        return False, ''
    is_long = pos['side'] == 'LONG'
    entry   = pos['entry_price']
    atr     = pos.get('trail_atr')
    best    = pos.get('trail_best_price', entry)
    if not atr or atr <= 0:
        return False, ''
    hard_sl_dist  = atr * get_symbol_cfg(symbol, 'hard_sl_atr', HARD_SL_ATR)
    activate_dist = atr * get_symbol_cfg(symbol, 'trail_activate_atr', TRAIL_ACTIVATE_ATR)
    profit_so_far = (best - entry) if is_long else (entry - best)
    trail_active  = profit_so_far >= activate_dist

    if not trail_active:
        max_loss_dollar = pos['amount'] * cfg.get('max_loss_pct', 0.30)
        max_loss_dist   = (max_loss_dollar / pos['qty']) if pos['qty'] else hard_sl_dist
        sl_dist = min(hard_sl_dist, max_loss_dist)
        sl = entry - sl_dist if is_long else entry + sl_dist
        if (is_long and price <= sl) or (not is_long and price >= sl):
            return True, f'Paper Hard SL | entry={entry:.4f} sl={sl:.4f} price={price:.4f}'

    if is_long and price > best:
        pos['trail_best_price'] = price; best = price
    elif not is_long and price < best:
        pos['trail_best_price'] = price; best = price

    profit_dist = (best - entry) if is_long else (entry - best)
    if profit_dist < activate_dist:
        return False, ''

    dyn_dist   = max(atr * get_symbol_cfg(symbol, 'trail_dist_atr', 0.25), profit_dist * 0.18)
    trail_stop = best - dyn_dist if is_long else best + dyn_dist
    trail_stop = max(trail_stop, entry) if is_long else min(trail_stop, entry)
    if (is_long and price <= trail_stop) or (not is_long and price >= trail_stop):
        return True, f'Paper Trail Stop | best={best:.4f} stop={trail_stop:.4f} price={price:.4f}'
    return False, ''

def close_paper_position(symbol: str, price: float, reason: str) -> bool:
    cfg = SYMBOLS_CONFIG[symbol]
    ps  = paper_state(symbol)
    pos = ps.get('position')
    if not pos:
        return False
    entry, qty = pos['entry_price'], pos['qty']
    gross = (price - entry) * qty if pos['side'] == 'LONG' else (entry - price) * qty
    fee   = qty * (entry + price) * FEE_RATE
    net   = gross - fee
    trade = {
        'side': pos['side'], 'entry_price': entry, 'exit_price': price, 'qty': qty,
        'pnl': round(net, 4), 'win': net > 0, 'reason': reason,
        'opened_at': pos['opened_at'], 'closed_at': now_utc_iso(),
    }
    ps.setdefault('trades', []).append(trade)
    ps['position'] = None
    save_state()
    base  = cfg['base']
    emoji = '🟢' if net >= 0 else '🔴'
    logger.info(f'📝 [PAPER:{base}] Closed {pos["side"]} @ {price:.4f} net={net:+.4f} reason={reason}')
    send_telegram(
        f'{emoji} <b>PAPER — {pos["side"]} {base}/USDT CLOSED ({reason})</b>\n\n'
        f'Exit: ${price:,.4f} | Entry: ${entry:,.4f}\n'
        f'Net P&L: {net:+.2f} USDT (simulated — no real funds)'
    )
    write_paper_dashboard(symbol)
    return True

def write_paper_dashboard(symbol: str) -> None:
    cfg    = SYMBOLS_CONFIG[symbol]
    ps     = paper_state(symbol)
    trades = ps.get('trades', [])
    wins   = [t for t in trades if t['win']]
    total  = len(trades)
    daily_pnl = {}
    for t in trades:
        d = (t.get('closed_at') or '')[:10]
        if d:
            daily_pnl[d] = round(daily_pnl.get(d, 0) + t['pnl'], 2)
    payload = {
        'generated_at': now_utc_iso(), 'symbol': symbol, 'paper_trading': True,
        'position': ps.get('position'),
        'performance': {
            'total': total, 'wins': len(wins), 'losses': total - len(wins),
            'win_rate': round(len(wins) / total * 100, 1) if total else 0,
            'net_pnl': round(sum(t['pnl'] for t in trades), 2),
        },
        'daily_pnl': daily_pnl,
        'trades': list(reversed(trades[-30:])),
    }
    write_json(os.path.join(WEB_ROOT, f'data_paper_offhours_{cfg["base"].lower()}.json'), payload)

def run_paper_offhours(symbol: str, price: float, action: str, confidence: int, reason: str) -> None:
    if symbol not in PAPER_OFFHOURS_SYMBOLS:
        return
    ps = paper_state(symbol)
    if ps.get('position'):
        hit, hit_reason = check_paper_trail(symbol, price)
        if hit:
            close_paper_position(symbol, price, hit_reason)
        return
    cfg = SYMBOLS_CONFIG[symbol]
    market_closed = cfg.get('market_hours_only') and not is_us_market_open()
    if market_closed and action in ('LONG', 'SHORT'):
        open_paper_position(symbol, action, price, confidence, reason)

# ── MU exit levels set from the dashboard ─────────────────────────────────────
# The dashboard's "Stop at price" / "Take profit at price" controls write ONE request
# (futures_mu_levels_request) into bot_config.json. It is bound to the specific open
# position (for_opened_at), validated here, applied once, then cleared. A custom stop can
# only sit between the default 3.5% stop and the current price: it can tighten protection
# or relax it back toward the default, never loosen it below the default. Enforcement is
# the same software check as the default stop (polled once per cycle, ~40s), so a fast
# drop can gap past it -- the level is a trigger, not a guaranteed fill price.
MU_LEVEL_MIN_GAP = 0.0005      # a requested stop/target must be >= 0.05% away from the current price

def _mu_net_at(on: dict, px: float) -> float:
    """Net P&L (entry fee already paid + exit fee) if the MU long were closed at px."""
    qty = on.get('qty', 0.0)
    return (px - on['entry_price']) * qty - on.get('entry_fee', 0.0) - qty * px * FEE_RATE

def apply_mu_levels_request(req: dict, price: float) -> None:
    on = state.get('overnight_mu', {})
    if not on.get('position'):
        clear_flag('futures_mu_levels_request')
        return
    applied, rejected = [], []
    try:
        if req.get('for_opened_at') != on.get('opened_at'):
            rejected.append('ignored: the request was for a different position')
        else:
            default_sl = round(on['entry_price'] * (1 - OVERNIGHT_CFG['sl_pct']), 4)
            if req.get('reset_stop'):
                on['sl_price'] = default_sl
                on.pop('sl_custom', None)
                applied.append(f'stop reset to the default ${default_sl:,.2f}')
            if req.get('stop') is not None:
                stop = safe_float(req.get('stop'), 0.0)
                if stop <= default_sl:
                    rejected.append(f'stop ${stop:,.2f} is not above the default stop ${default_sl:,.2f}')
                elif stop >= price * (1 - MU_LEVEL_MIN_GAP):
                    rejected.append(f'stop ${stop:,.2f} is at or above the current price ${price:,.2f} - it would close immediately (use Close instead)')
                else:
                    on['sl_price'] = round(stop, 4)
                    on['sl_custom'] = True
                    applied.append(f'stop set to ${stop:,.2f} (about {_mu_net_at(on, stop):+.2f} USDT net if it fills there)')
            if req.get('clear_target'):
                if on.pop('target_price', None) is not None:
                    applied.append('take-profit target cleared')
            if req.get('target') is not None:
                target = safe_float(req.get('target'), 0.0)
                if target <= price * (1 + MU_LEVEL_MIN_GAP):
                    rejected.append(f'target ${target:,.2f} is at or below the current price ${price:,.2f} - it would close immediately (use Close instead)')
                else:
                    on['target_price'] = round(target, 4)
                    applied.append(f'take-profit set to ${target:,.2f} (about {_mu_net_at(on, target):+.2f} USDT net)')
    except Exception as e:
        rejected.append(f'error while applying: {e}')
    msg = '; '.join(applied + rejected) or 'no change'
    on['levels_status'] = {'at': now_utc_iso(), 'ok': bool(applied) and not rejected, 'msg': msg}
    state['overnight_mu'] = on
    save_state()
    logger.info(f'[OVERNIGHT] MU exit levels request: {msg}')
    send_telegram(('🛡 <b>MU exit levels updated</b>\n' if not rejected else '⚠️ <b>MU exit levels request</b>\n')
                  + '\n'.join(f'• {m}' for m in applied + rejected))
    clear_flag('futures_mu_levels_request')
    try:
        write_overnight_dashboard(price)
    except Exception as e:
        logger.warning(f'[OVERNIGHT] dashboard refresh after levels request failed (non-critical): {e}')


def run_overnight_strategy() -> None:
    et      = _et_now()
    weekday = et.weekday()   # 0=Mon … 4=Fri
    hour    = et.hour
    minute  = et.minute
    on      = state.get('overnight_mu', {})
    has_pos = bool(on.get('position'))
    sym     = OVERNIGHT_CFG['symbol']

    # SL check — runs any time there's an open overnight position
    if has_pos:
        price = get_current_price(sym)
        # A dashboard request to set/adjust the exit levels is applied BEFORE the checks, so a new
        # stop takes effect this very cycle. Never lets a bad request break the cycle.
        try:
            lv_req = fetch_dashboard_config().get('mu_levels_request')
            if lv_req:
                apply_mu_levels_request(lv_req, price)
                on = state.get('overnight_mu', {})
        except Exception as e:
            logger.warning(f'[OVERNIGHT] exit-levels request failed (ignored): {e}')
        sl_price = on.get('sl_price', 0)
        if sl_price and price <= sl_price:
            reason = 'Custom Stop' if on.get('sl_custom') else 'Stop Loss'
            logger.info(f'[OVERNIGHT] {reason} hit @ ${price:.4f} (sl={sl_price:.4f})')
            close_overnight_mu(reason)
            return
        tp_price = on.get('target_price')
        if tp_price and price >= tp_price:
            logger.info(f'[OVERNIGHT] Take-profit hit @ ${price:.4f} (target={tp_price:.4f})')
            close_overnight_mu('Target Hit')
            return
        # Keep the dashboard's live P&L fresh while a position is open. This
        # file used to be written only on open/close, so a weekend hold showed
        # the entry-moment P&L for ~2 days (found 2026-09-19: +$3.25 displayed
        # vs ~-$9 real). Never lets a display write affect trading.
        try:
            write_overnight_dashboard(price)
        except Exception as e:
            logger.warning(f'[OVERNIGHT] dashboard refresh failed (non-critical): {e}')

    # Manual close — dashboard "Close" button
    if has_pos:
        cfg = fetch_dashboard_config()
        if cfg.get('mu_close_requested'):
            req_at = cfg.get('mu_close_requested_at')
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(
                    req_at.replace('Z', '+00:00'))).total_seconds() if req_at else 999
            except Exception:
                age = 999
            if age < 300:
                logger.info('[OVERNIGHT] Manual close requested from dashboard')
                send_telegram('📱 <b>Dashboard Close</b>\nClosing MU overnight position at market')
                closed = close_overnight_mu('Dashboard Close')
                if closed:
                    clear_flag('futures_mu_close_requested')
                    return
            else:
                clear_flag('futures_mu_close_requested')

    # Entry: 3:55–4:05 PM ET, Mon–Fri (Friday included — backtested holding
    # through the weekend to Monday's real market open: 22 trades, 63.6% WR,
    # +$463.70, meaningfully better per-trade than the weekday-only average.
    # Never on a US market holiday — MUUSDT keeps printing on Binance even
    # when NASDAQ is closed, so entering then means trading a synthetic price
    # with no real market behind it.
    is_holiday = (et.month, et.day) in US_MARKET_HOLIDAYS
    mu_disabled = state.get('overnight_mu_disabled', False)   # see close_overnight_mu: set once MU migrates to Sentinel
    if mu_disabled and not has_pos and weekday < 5 and not is_holiday and hour == 15 and minute == 55:
        logger.info('[OVERNIGHT] MU entry skipped -- disabled, Sentinel owns entries now')
    elif not mu_disabled and not has_pos and weekday < 5 and not is_holiday:
        if hour == 15 and minute >= 55:
            logger.info('[OVERNIGHT] Entry window — opening MU')
            open_overnight_mu()
        elif hour == 16 and minute <= 5:
            logger.info('[OVERNIGHT] Entry window (just after close) — opening MU')
            open_overnight_mu()
    elif not mu_disabled and not has_pos and weekday < 5 and is_holiday and hour == 15 and minute == 55:
        logger.info('[OVERNIGHT] Skipping MU entry — US market holiday today')

    # Exit: 9:28–9:40 AM ET any weekday — always close flat at market open.
    # (Backtested the extend+trail variant against always-closing-flat over 5
    # months / 89 trades: the trail added ~$7 total across 21 trades that would
    # have extended — statistically noise, never once saved a trade that would
    # have lost, never once meaningfully added. Removed for simplicity.)
    if has_pos and weekday < 5 and not on.get('exit_decided'):
        if hour == 9 and 28 <= minute <= 40:
            on['exit_decided'] = True
            state['overnight_mu'] = on
            save_state()
            logger.info('[OVERNIGHT] Exit window — closing MU at market open')
            close_overnight_mu('Market Open')


# ── Weekly Summary ────────────────────────────────────────────────────────────
def send_weekly_summary() -> None:
    """Every Friday ~4 PM ET — per-symbol win rate and P&L for the past 7 days."""
    try:
        try:
            import zoneinfo
            tz = zoneinfo.ZoneInfo('America/New_York')
        except Exception:
            tz = timezone(timedelta(hours=-4 if 3 <= datetime.now(timezone.utc).month <= 11 else -5))

        cutoff_ts = time.time() - 7 * 86400
        path = os.path.join(os.path.dirname(BOT_STATE_FILE), TRADES_LOG_FILE) \
               if os.path.dirname(BOT_STATE_FILE) else TRADES_LOG_FILE
        all_trades = json.load(open(path)) if os.path.exists(path) else []

        def dot(v): return '🟢' if v >= 0 else '🔴'

        total_pnl   = 0.0
        total_wins  = 0
        total_count = 0
        sym_lines   = []

        for sym in TRADING_SYMBOLS:
            base = SYMBOLS_CONFIG.get(sym, {}).get('base', sym.replace('USDT', ''))
            week_trades = []
            for t in all_trades:
                if t.get('symbol') != sym: continue
                raw = t.get('closed_at') or t.get('opened_at')
                if not raw: continue
                try:
                    ts = datetime.fromisoformat(raw).timestamp()
                except Exception:
                    continue
                if ts >= cutoff_ts:
                    week_trades.append(t)

            if not week_trades:
                sym_lines.append(f'  {base}: no trades')
                continue

            wins = sum(1 for t in week_trades if t.get('win') or float(t.get('pnl', 0)) > 0)
            losses = len(week_trades) - wins
            net    = sum(float(t.get('pnl', 0)) for t in week_trades)
            wr     = wins / len(week_trades) * 100 if week_trades else 0
            sym_lines.append(
                f'{dot(net)} <b>{base}</b>: {wins}W/{losses}L ({wr:.0f}%W) · {net:+.2f} USDT'
            )
            total_pnl   += net
            total_wins  += wins
            total_count += len(week_trades)

        overall_wr = total_wins / total_count * 100 if total_count else 0
        header = (
            f'📊 <b>Weekly Summary — '
            f'{datetime.now(tz).strftime("%b %d, %Y")}</b>\n'
            f'{dot(total_pnl)} {total_count} trades · {overall_wr:.0f}%W · '
            f'<b>{total_pnl:+.2f} USDT</b>\n'
        )
        send_telegram(header + '\n'.join(sym_lines))
        logger.info('📊 Weekly summary sent')
    except Exception as e:
        logger.warning(f'send_weekly_summary: {e}')


# ── Main Loop ─────────────────────────────────────────────────────────────────
def run_once():
    global run_count
    run_count += 1
    logger.info('=' * 60)
    logger.info(f'  APEX Futures — Run #{run_count}')
    logger.info('=' * 60)
    try:
        cfg = fetch_dashboard_config()
        apply_runtime_settings(cfg)
        process_manual_trade(cfg)
        run_overnight_strategy()
        run_weekly_atr_health_check()
        run_weekly_trade_review()
        run_monthly_strategy_review()
        run_daily_hermes_sync()
        run_weekly_auto_tune()
        run_weekly_mu_auto_tune()
        run_hermes_picks_cycle()

        # ── Find which symbols already have open positions ────────────────────
        open_syms = set()
        for sym in TRADING_SYMBOLS:
            if detect_futures_position(sym) is not None:
                open_syms.add(sym)

        # ── BTC + ETH trade fully independently ──────────────────────────────
        logger.info(f'📊 Open positions: {open_syms}')

        # ── Run each symbol ───────────────────────────────────────────────────
        closed_this_cycle = set()
        for symbol in TRADING_SYMBOLS:
            was_in_trade = symbol in open_syms
            allow_entry  = True  # both BTC and ETH always allowed to enter independently
            # Per-symbol isolation (added 2026-09-19): one symbol's failure used
            # to abort the cycle for ALL symbols -- including trail/SL checks on
            # any other open position. Now it's logged/alerted and skipped.
            try:
                result = run_symbol(symbol, cfg, allow_new_entry=allow_entry)
            except Exception as e:
                logger.error(f'[{symbol}] run_symbol error (other symbols unaffected): {e}', exc_info=True)
                alert_error(f'{symbol}: {e}')
                continue
            if was_in_trade and result and not result.get('position'):
                closed_this_cycle.add(symbol)

        # ── Dust cleanup immediately after any close ──────────────────────────
        if closed_this_cycle:
            cleanup_dust()

        # ── Daily end-of-day summary at 4:05 PM ET ───────────────────────────
        try:
            import zoneinfo
            _tz = zoneinfo.ZoneInfo('America/New_York')
        except Exception:
            _tz = timezone(timedelta(hours=-4 if 3 <= datetime.now(timezone.utc).month <= 11 else -5))
        _et = datetime.now(_tz)
        _today = _et.date().isoformat()
        if (_et.weekday() < 5 and _et.hour == 16 and 5 <= _et.minute < 15
                and state.get('last_summary_date') != _today):
            state['last_summary_date'] = _today
            save_state()
            send_monthly_summary()

        # Weekly summary every Friday at ~4:05 PM ET
        if (_et.weekday() == 4 and _et.hour == 16 and 5 <= _et.minute < 15
                and state.get('last_weekly_date') != _today):
            state['last_weekly_date'] = _today
            save_state()
            send_weekly_summary()

        if cfg.get('force_trail'):
            clear_flag('futures_force_trail')
    except Exception as e:
        logger.error(f'run_once error: {e}', exc_info=True)
        alert_error(str(e))


def main():
    load_state()
    for sym in TRADING_SYMBOLS:
        sym_state(sym)['force_trail_processed'] = False
    save_state()

    startup_cfg = fetch_dashboard_config()
    apply_runtime_settings(startup_cfg)
    pull_and_log_hermes_report()  # capture whatever report already exists, don't wait for next weekly run
    write_hermes_log_dashboard()
    write_hermes_picks_dashboard()

    roster_line = ' | '.join(
        f"{SYMBOLS_CONFIG[sym]['base']}=${SYMBOLS_CONFIG[sym].get('trade_amount', DEFAULT_TRADE_AMOUNT_USDT)}"
        f"@{SYMBOLS_CONFIG[sym].get('leverage', DEFAULT_LEVERAGE):.0f}x"
        for sym in TRADING_SYMBOLS
    )
    roster_names = ' + '.join(SYMBOLS_CONFIG[sym]['base'] for sym in TRADING_SYMBOLS)

    logger.info(f'🚀 APEX Futures v1 — {roster_names} Perpetuals')
    logger.info(f'   {roster_line} | API: {mask(BINANCE_API_KEY)}')

    send_telegram(
        f'🚀 <b>APEX Futures Started — {roster_names} Perps</b>\n\n'
        f'📌 {roster_line}\n'
        f'🎯 ADX Regime + EMA21 Pullback (1H) + 4H Trend Filter\n'
        f'↕️ Long + Short | Fee: 0.05% taker\n'
        f'⏱ Cycle: every {CHECK_INTERVAL}s'
    )

    while True:
        try:
            run_once()
        except Exception as e:
            logger.error(f'Main loop error: {e}', exc_info=True)
            alert_error(f'Main loop: {e}')

        logger.info(f'⏳ Sleeping {CHECK_INTERVAL}s...')
        for _ in range(CHECK_INTERVAL // 2):
            time.sleep(2)
            try:
                quick_cfg = fetch_dashboard_config()
                if (quick_cfg.get('close_requested') or quick_cfg.get('force_trail')
                        or isinstance(quick_cfg.get('manual_trade'), dict)):
                    logger.info('⚡ Flag detected mid-sleep — waking up')
                    break
                wake = False
                for sym in TRADING_SYMBOLS:
                    ss    = sym_state(sym)
                    pos   = ss.get('position')
                    _price = get_current_price(sym)
                    # Live price update every 5s
                    dash_path = os.path.join(WEB_ROOT, SYMBOLS_CONFIG[sym]['dashboard_file'])
                    try:
                        existing = read_json(dash_path, {})
                        if existing:
                            existing['price'] = _price
                            existing['generated_at'] = now_utc_iso()
                            write_json(dash_path, existing)
                    except Exception:
                        pass
                    # Mid-sleep SL check — close immediately, don't just wake
                    if pos and ss.get('trail_entry_price') and ss.get('trail_atr'):
                        _hit, _reason = check_sl_trail(sym, pos, _price)
                        if _hit:
                            logger.info(f'⚡ [{sym}] Trail/SL hit mid-sleep — closing NOW @ ${_price}')
                            base_ = SYMBOLS_CONFIG[sym]['base']
                            send_telegram(f'🛑 <b>SL/Trail {base_}/USDT Futures</b>\n{_reason}\nClosing @ ${_price:,.4f}')
                            try:
                                if pos == 'LONG':
                                    close_long(sym, _price, _reason)
                                else:
                                    close_short(sym, _price, _reason)
                                clear_trail(sym)
                                ss['position'] = None
                            except Exception as _ce:
                                logger.error(f'⚡ [{sym}] Mid-sleep close failed: {_ce}')
                            wake = True
                # Mid-sleep overnight SL check
                _on = state.get('overnight_mu', {})
                if _on.get('position') and _on.get('sl_price'):
                    _mu_price = get_current_price(OVERNIGHT_CFG['symbol'])
                    if _mu_price <= _on['sl_price']:
                        logger.info(f'⚡ [OVERNIGHT] SL hit mid-sleep @ ${_mu_price:.4f}')
                        close_overnight_mu('Stop Loss')
                        wake = True
                if wake:
                    break
            except Exception:
                pass


if __name__ == '__main__':
    main()
