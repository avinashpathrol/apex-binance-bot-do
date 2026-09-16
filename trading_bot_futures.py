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
        'trend_continuation_enabled': True,
        'rsi_long_min': 25,
        'rsi_long_max': 65,
        'rsi_short_min': 35,
        'rsi_short_max': 75,
        'pullback_zone_pct': 0.030,
        'trail_dist_atr': 0.20,
    },
    'CRCLUSDT': {
        'base': 'CRCL',
        'dashboard_file': 'data_futures_crcl.json',
        'min_atr': 0.75,
        'trade_amount': 50.0,
        'max_loss_pct': 0.35,
        'market_hours_only': True,
        'one_way': True,
        'leverage': 40,
        'skip_margin_type': True,
        'daily_profit_lock': 6.0,
        'trend_continuation_enabled': True,
        'rsi_long_min': 25,
        'rsi_long_max': 65,
        'rsi_short_min': 35,
        'rsi_short_max': 75,
        'pullback_zone_pct': 0.030,
        'trail_dist_atr': 0.20,
    },
    'SOXLUSDT': {
        'base': 'SOXL',
        'dashboard_file': 'data_futures_soxl.json',
        'min_atr': 0.8,
        'trade_amount': 20.0,
        'max_loss_pct': 0.35,
        'market_hours_only': True,
        'one_way': True,
        'leverage': 15,
        'skip_margin_type': True,
        'daily_profit_lock': 5.0,
        'trend_continuation_enabled': True,
        'rsi_long_min': 25,
        'rsi_long_max': 65,
        'rsi_short_min': 35,
        'rsi_short_max': 75,
        'pullback_zone_pct': 0.030,
        'trail_dist_atr': 0.20,
    },
    'TSLAUSDT': {
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
        'rsi_long_min': 27,
        'rsi_long_max': 66,
        'rsi_short_min': 22,
        'rsi_short_max': 68,
        'pullback_zone_pct': 0.030,
        'trail_dist_atr': 0.20,
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
        'trend_continuation_enabled': True,
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
        'trend_continuation_enabled': True,
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
TRADING_SYMBOLS = ['NBISUSDT', 'AMDUSDT', 'APPUSDT', 'SOXLUSDT', 'CRCLUSDT', 'ASTSUSDT', 'TSLAUSDT']

# Correlated pairs — skip entry in symbol B if symbol A already has an open trade.
# Add pairs here when you observe two symbols that move lockstep and you want to cap
# sector concentration. Empty by default — all symbols trade independently.
CORR_GROUPS: list[set] = []

# ── Strategy Parameters ───────────────────────────────────────────────────────
ADX_MIN           = 25.0
ADX_STRONG        = 30.0
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
    return binance_futures_private('POST', '/fapi/v1/order', params)

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
    try:
        return float(binance_futures_public('/fapi/v1/ticker/price', {'symbol': symbol})['price'])
    except Exception:
        r = requests.get(f'{SPOT_BASE_URL}/api/v3/ticker/price',
                         params={'symbol': symbol}, timeout=10)
        return float(r.json()['price'])



# ── 4H Trend Confirmation ─────────────────────────────────────────────────────
_4h_cache: dict = {}
H4_CACHE_TTL    = 1800

def get_4h_trend(symbol: str) -> str:
    now = time.time()
    cached = _4h_cache.get(symbol)
    if cached and (now - cached['ts']) < H4_CACHE_TTL:
        return cached['trend']
    try:
        klines = binance_futures_public('/fapi/v1/klines',
                                        {'symbol': symbol, 'interval': '4h', 'limit': 60})
        df4 = pd.DataFrame(klines, columns=[
            'time','open','high','low','close','volume',
            'close_time','quote_volume','trades','taker_base','taker_quote','ignore',
        ])
        for col in ('high','low','close'):
            df4[col] = df4[col].astype(float)
        adx_ind       = ta.trend.ADXIndicator(df4['high'], df4['low'], df4['close'], window=14)
        df4['adx']     = adx_ind.adx()
        df4['adx_pos'] = adx_ind.adx_pos()
        df4['adx_neg'] = adx_ind.adx_neg()
        df4['ema21']   = ta.trend.EMAIndicator(df4['close'], window=21).ema_indicator()
        df4['ema50']   = ta.trend.EMAIndicator(df4['close'], window=50).ema_indicator()
        c4 = df4.iloc[-1]
        adx4     = float(c4['adx'])
        adx_pos4 = float(c4['adx_pos'])
        adx_neg4 = float(c4['adx_neg'])
        ema21_4  = float(c4['ema21'])
        ema50_4  = float(c4['ema50'])
        if adx4 >= 20 and adx_pos4 > adx_neg4 and ema21_4 > ema50_4:
            trend4 = '4H BULLISH'
        elif adx4 >= 20 and adx_neg4 > adx_pos4 and ema21_4 < ema50_4:
            trend4 = '4H BEARISH'
        else:
            trend4 = '4H NEUTRAL'
        _4h_cache[symbol] = {'trend': trend4, 'ts': now}
        logger.info(f'📊 [{symbol}] 4H: {trend4} | ADX={adx4:.1f} +DI={adx_pos4:.1f} -DI={adx_neg4:.1f}')
        return trend4
    except Exception as e:
        logger.warning(f'get_4h_trend [{symbol}] failed: {e}')
        return '4H NEUTRAL'


# ── Market Data & Signal ──────────────────────────────────────────────────────
def get_market_data(symbol: str) -> pd.DataFrame:
    klines = binance_futures_public('/fapi/v1/klines',
                                    {'symbol': symbol, 'interval': '1h', 'limit': 100})
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
MONTHLY_REVIEW_INTERVAL_DAYS = 30
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


def get_decision(symbol: str, df: pd.DataFrame) -> dict:
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
    rsi_long_min      = cfg.get('rsi_long_min',      RSI_LONG_MIN)
    rsi_long_max      = cfg.get('rsi_long_max',      RSI_LONG_MAX)
    rsi_short_min     = cfg.get('rsi_short_min',     RSI_SHORT_MIN)
    rsi_short_max     = cfg.get('rsi_short_max',     RSI_SHORT_MAX)
    pullback_zone_pct = cfg.get('pullback_zone_pct', PULLBACK_ZONE_PCT)

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

    if adx < ADX_MIN:
        return hold(f'ADX {adx:.1f} < {ADX_MIN} — market choppy, no entry')
    if atr < min_atr:
        return hold(f'ATR {atr:.4f} too low (min {min_atr}) — not enough movement to cover fees')

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
    trend4h = get_4h_trend(symbol)
    if trend == 'BULLISH' and trend4h == '4H BEARISH':
        return hold(f'BULLISH on 1H but 4H is BEARISH — counter-trend, skipping', trend)
    if trend == 'BEARISH' and trend4h == '4H BULLISH':
        return hold(f'BEARISH on 1H but 4H is BULLISH — counter-trend, skipping', trend)

    vol_ok = vol >= vol_ma * 0.9   # require ≥90% of 20-bar average to enter

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
    sl_atr_mult        = SYMBOLS_CONFIG.get(symbol, {}).get('hard_sl_atr', HARD_SL_ATR)
    hard_sl_dist       = atr * sl_atr_mult
    activate_dist      = atr * TRAIL_ACTIVATE_ATR
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
        logger.info(f'📐 [{symbol}] Trail not active | profit={profit_dist:.4f} < {activate_dist:.4f}')
        return False, ''

    trail_atr_mult = SYMBOLS_CONFIG.get(symbol, {}).get('trail_dist_atr', 0.25)
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
        max_loss_dist = (12.0 / qty) if qty > 0 else atr * HARD_SL_ATR
        sl_dist    = min(atr * HARD_SL_ATR, max_loss_dist)
        info['sl'] = round(ep - sl_dist, 4) if position == 'LONG' \
                else round(ep + sl_dist, 4)
    if ep and bp and atr:
        profit = (bp - ep) if position == 'LONG' else (ep - bp)
        if profit >= atr * TRAIL_ACTIVATE_ATR:
            trail_atr_mult = SYMBOLS_CONFIG.get(symbol, {}).get('trail_dist_atr', 0.25)
            dyn = max(atr * trail_atr_mult, profit * 0.20)
            info['trail_stop'] = round(bp - dyn, 4) if position == 'LONG' else round(bp + dyn, 4)
            info['active'] = True
    return info


# ── Trade Execution ───────────────────────────────────────────────────────────
SAME_DIR_COOLDOWN = 600  # 10 minutes

def open_long(symbol: str, price: float, confidence: int, reason: str, indicators: dict = None) -> bool:
    ss   = sym_state(symbol)
    base = SYMBOLS_CONFIG[symbol]['base']
    cfg  = SYMBOLS_CONFIG[symbol]
    # Block re-entry in same direction within 10 min of last close
    if ss.get('last_bot_closed_side') == 'LONG':
        elapsed = time.time() - int(ss.get('last_bot_closed_ts', 0))
        if elapsed < SAME_DIR_COOLDOWN:
            remaining = int((SAME_DIR_COOLDOWN - elapsed) / 60)
            logger.info(f'⏳ [{symbol}] LONG cooldown — last LONG closed {int(elapsed/60)}m ago, waiting {remaining}m more')
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
        ss['entry_reason']     = reason
        ss['entry_confidence'] = confidence
        ss['entry_indicators'] = indicators or {}
        ss['entry_hour_utc']   = now_.hour
        ss['entry_weekday']    = now_.strftime('%A')

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

def open_short(symbol: str, price: float, confidence: int, reason: str, indicators: dict = None) -> bool:
    ss   = sym_state(symbol)
    base = SYMBOLS_CONFIG[symbol]['base']
    cfg  = SYMBOLS_CONFIG[symbol]
    # Block re-entry in same direction within 10 min of last close
    if ss.get('last_bot_closed_side') == 'SHORT':
        elapsed = time.time() - int(ss.get('last_bot_closed_ts', 0))
        if elapsed < SAME_DIR_COOLDOWN:
            remaining = int((SAME_DIR_COOLDOWN - elapsed) / 60)
            logger.info(f'⏳ [{symbol}] SHORT cooldown — last SHORT closed {int(elapsed/60)}m ago, waiting {remaining}m more')
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
        ss['entry_reason']     = reason
        ss['entry_confidence'] = confidence
        ss['entry_indicators'] = indicators or {}
        ss['entry_hour_utc']   = now_.hour
        ss['entry_weekday']    = now_.strftime('%A')

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


def is_us_market_open() -> bool:
    """True during regular US market hours Mon-Fri 9:45am-4:00pm ET (handles DST)."""
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo('America/New_York')
    except Exception:
        from datetime import timezone, timedelta
        # Rough DST fallback: EDT Mar-Nov = UTC-4, EST Nov-Mar = UTC-5
        month = __import__('datetime').datetime.utcnow().month
        offset = -4 if 3 <= month <= 11 else -5
        tz = timezone(timedelta(hours=offset))
    now_et = __import__('datetime').datetime.now(tz)
    if now_et.weekday() >= 5:
        return False
    if (now_et.month, now_et.day) in US_MARKET_HOLIDAYS:
        return False
    # Skip first 15 min after open (whipsaw period)
    open_time  = now_et.replace(hour=9,  minute=45, second=0, microsecond=0)
    close_time = now_et.replace(hour=16, minute=0,  second=0, microsecond=0)
    return open_time <= now_et < close_time


def in_entry_window(cfg: dict) -> bool:
    """True if now (ET) falls inside the symbol's optional entry_window_et,
    a (start_h, start_m, end_h, end_m) tuple restricting new entries to a
    sub-range of market hours. Symbols without this key trade all session."""
    window = cfg.get('entry_window_et')
    if not window:
        return True
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo('America/New_York')
    except Exception:
        from datetime import timezone, timedelta
        month = __import__('datetime').datetime.utcnow().month
        offset = -4 if 3 <= month <= 11 else -5
        tz = timezone(timedelta(hours=offset))
    now_et = __import__('datetime').datetime.now(tz)
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
                elif action == 'LONG':
                    ok = open_long(symbol, price, confidence, reason, indicators)
                    status = 'LONG OPENED ✅' if ok else 'LONG FAILED ❌'
                    if ok: position = 'LONG'
                else:
                    ok = open_short(symbol, price, confidence, reason, indicators)
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
    amount = OVERNIGHT_CFG['amount']
    lev    = OVERNIGHT_CFG['leverage']
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
        save_state()
        write_overnight_dashboard()
        return True
    except Exception as e:
        logger.error(f'[OVERNIGHT] close failed: {e}')
        alert_error(f'Overnight MU close: {e}')
        return False

def write_overnight_dashboard() -> None:
    on     = state.get('overnight_mu', {})
    trades = state.get('overnight_mu_trades', [])
    wins   = [t for t in trades if t['net'] > 0]
    losses = [t for t in trades if t['net'] <= 0]
    total  = len(trades)
    current_price = unrealized_pnl = None
    if on.get('position'):
        try:
            current_price  = get_current_price(OVERNIGHT_CFG['symbol'])
            unrealized_pnl = round((current_price - on['entry_price']) * on.get('qty', 0), 4)
        except Exception:
            pass
    payload = {
        'generated_at':  now_utc_iso(),
        'position':      on.get('position'),
        'entry_price':   on.get('entry_price'),
        'sl_price':      on.get('sl_price'),
        'qty':           on.get('qty'),
        'opened_at':     on.get('opened_at'),
        'current_price': current_price,
        'unrealized_pnl': unrealized_pnl,
        'performance': {
            'total':      total,
            'wins':       len(wins),
            'losses':     len(losses),
            'win_rate':   round(len(wins) / total * 100, 1) if total else 0,
            'win_pnl':    round(sum(t['net'] for t in wins), 2),
            'loss_pnl':   round(sum(t['net'] for t in losses), 2),
            'total_fees': round(sum(t['fee'] for t in trades), 2),
            'net_pnl':    round(sum(t['net'] for t in trades), 2),
        },
        'trades': list(reversed(trades)),
    }
    write_json(os.path.join(WEB_ROOT, 'data_overnight_mu.json'), payload)


# ── Off-hours/weekend PAPER trading (test ticker only) ────────────────────────
# NBIS backtested as the single best candidate for allowing off-hours/weekend
# entries (see session notes: weekend-inclusive backtest showed +$968 delta
# over market-hours-only, the largest of all 7 tickers) — but real weekend
# liquidity on these tokens is only ~8% of market-hours volume, so real
# execution risk is unverified. This runs the SAME live strategy signal
# during off-hours as a PAPER position (no real order, fully separate state
# from the real position) so we can validate against real live quotes before
# ever considering real money here. During real market hours NBIS trades
# exactly as before, unaffected by any of this.
PAPER_OFFHOURS_SYMBOLS = {'NBISUSDT'}

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
    hard_sl_dist  = atr * cfg.get('hard_sl_atr', HARD_SL_ATR)
    activate_dist = atr * TRAIL_ACTIVATE_ATR
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

    dyn_dist   = max(atr * cfg.get('trail_dist_atr', 0.25), profit_dist * 0.18)
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
        price    = get_current_price(sym)
        sl_price = on.get('sl_price', 0)
        if sl_price and price <= sl_price:
            logger.info(f'[OVERNIGHT] SL hit @ ${price:.4f} (sl={sl_price:.4f})')
            close_overnight_mu('Stop Loss')
            return

    # Entry: 3:55–4:05 PM ET, Mon–Fri (Friday included — backtested holding
    # through the weekend to Monday's real market open: 22 trades, 63.6% WR,
    # +$463.70, meaningfully better per-trade than the weekday-only average.
    # Never on a US market holiday — MUUSDT keeps printing on Binance even
    # when NASDAQ is closed, so entering then means trading a synthetic price
    # with no real market behind it.
    is_holiday = (et.month, et.day) in US_MARKET_HOLIDAYS
    if not has_pos and weekday < 5 and not is_holiday:
        if hour == 15 and minute >= 55:
            logger.info('[OVERNIGHT] Entry window — opening MU')
            open_overnight_mu()
        elif hour == 16 and minute <= 5:
            logger.info('[OVERNIGHT] Entry window (just after close) — opening MU')
            open_overnight_mu()
    elif not has_pos and weekday < 5 and is_holiday and hour == 15 and minute == 55:
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
            result       = run_symbol(symbol, cfg, allow_new_entry=allow_entry)
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
