#!/usr/bin/env python3
"""
APEX v2 — Binance Cross-Margin SOL/USDT Bot
Strategy: ADX Regime Filter + EMA21 Pullback (1H timeframe)

Entry logic:
  LONG:  ADX >= 22, +DI > -DI, EMA21 > EMA50, price pulls back within 1.8% above EMA21,
         candle low touched EMA21 area, RSI 30-62
  SHORT: ADX >= 22, -DI > +DI, EMA21 < EMA50, price bounces within 1.8% below EMA21,
         candle high touched EMA21 area, RSI 38-70
  HOLD:  ADX < 22 (choppy), conflicting DI/EMA structure, price not at EMA21 yet

Stop loss:  1.5x ATR(14) from entry (1H ATR)
Trail stop: activates at 0.75x ATR profit, trails best price by dynamic dist (65% of gains locked)
"""

import os
import json
import time
import hmac
import hashlib
import base64
import logging
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
logger = logging.getLogger('apex')
logger.setLevel(logging.INFO)
fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
fh = logging.FileHandler(os.path.join(LOG_DIR, 'apex_bot.log'), encoding='utf-8')
fh.setFormatter(fmt)
ch = logging.StreamHandler()
ch.setFormatter(fmt)
if not logger.handlers:
    logger.addHandler(fh)
    logger.addHandler(ch)

# ── Environment & Constants ───────────────────────────────────────────────────
MST = timezone(timedelta(hours=-7))
BINANCE_BASE_URL   = 'https://api.binance.com'
BINANCE_API_KEY    = os.environ.get('BINANCE_API_KEY', '').strip()
BINANCE_API_SECRET = os.environ.get('BINANCE_API_SECRET', '').strip()
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
TELEGRAM_CHAT_ID   = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
GH_TOKEN           = os.environ.get('GH_TOKEN', '').strip()
DASHBOARD_REPO     = os.environ.get('DASHBOARD_REPO', 'avinashpathrol/apex-dashboard').strip()
DASHBOARD_FILE     = 'data_binance_sol.json'
BOT_CONFIG_FILE    = os.environ.get('BOT_CONFIG_FILE', 'bot_config.json').strip()
BOT_STATE_FILE     = os.environ.get('BOT_STATE_FILE', 'bot_state.json').strip()

SYMBOL      = 'SOLUSDT'
BASE_ASSET  = 'SOL'
QUOTE_ASSET = 'USDT'

DEFAULT_TRADE_AMOUNT_USDT = float(os.environ.get('TRADE_AMOUNT_USDT', '60'))
DEFAULT_LEVERAGE          = float(os.environ.get('LEVERAGE', '5'))
CHECK_INTERVAL            = int(os.environ.get('CHECK_INTERVAL', '60'))
ALLOWED_LEVERAGES         = {3.0, 4.0, 5.0}

# ── Strategy Parameters ───────────────────────────────────────────────────────
ADX_MIN           = 22.0   # below this = choppy market, no entries
ADX_STRONG        = 30.0   # above this = strong trend, +7% confidence bonus
PULLBACK_ZONE_PCT = 0.018  # price must be within 1.8% of EMA21 to qualify as pullback
RSI_LONG_MIN      = 30     # RSI floor for LONG entry (not already sold off too far)
RSI_LONG_MAX      = 62     # RSI ceiling for LONG entry (not overbought on pullback)
RSI_SHORT_MIN     = 38     # RSI floor for SHORT entry (not oversold on bounce)
RSI_SHORT_MAX     = 70     # RSI ceiling for SHORT entry
MIN_ATR_USDT      = 1.50   # skip entries if 1H ATR < $1.50 (dead/ultra-quiet market)

# ── Trailing Stop Parameters ──────────────────────────────────────────────────
TRAIL_ACTIVATE_ATR = 0.75  # trail activates when profit >= this × ATR
TRAIL_DISTANCE_ATR = 1.40  # fixed trail distance (used as fallback)
HARD_SL_ATR        = 1.50  # hard stop loss = this × ATR from entry (before trail activates)
FEE_RATE           = 0.00075  # 0.075% per side — BNB discount applied

# ── Runtime Globals ───────────────────────────────────────────────────────────
run_count        = 0
current_position = None
last_hold_alert  = 0.0

runtime_settings = {
    'trade_amount_usdt': DEFAULT_TRADE_AMOUNT_USDT,
    'leverage': DEFAULT_LEVERAGE,
    'source': 'env-defaults',
}

state = {
    'prev_position': None,
    'active_trade_amount_usdt': None,
    'active_trade_leverage': None,
    'active_qty': None,
    'trail_entry_price': None,
    'trail_best_price': None,
    'trail_atr': None,
    'trade_opened_at': None,
    'entry_fee_usdt': 0.0,
    'closed_trades_log': [],
    'last_bot_closed_side': None,
    'last_bot_closed_ts': 0,
    'force_trail_processed': False,
}


# ── Utilities ─────────────────────────────────────────────────────────────────
def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def safe_float(v, default=0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default

def safe_int(v, default=0) -> int:
    try:
        return int(v)
    except Exception:
        return default

def read_json(path: str, default):
    try:
        if not os.path.exists(path):
            return default
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f'read_json {path}: {e}')
        return default

def write_json(path: str, data) -> None:
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)

def load_state() -> None:
    global state
    loaded = read_json(BOT_STATE_FILE, None)
    if isinstance(loaded, dict):
        state.update(loaded)

def save_state() -> None:
    write_json(BOT_STATE_FILE, state)

def mask(v: str, s: int = 6, e: int = 4) -> str:
    if not v:
        return 'MISSING'
    if len(v) <= s + e:
        return '*' * len(v)
    return f'{v[:s]}...{v[-e:]}'


# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(msg: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage',
            json={'chat_id': TELEGRAM_CHAT_ID, 'text': msg, 'parse_mode': 'HTML'},
            timeout=10,
        )
    except Exception as e:
        logger.warning(f'Telegram failed: {e}')

def alert_error(err: str) -> None:
    send_telegram(
        f'❌ <b>APEX BOT ERROR</b>\n\n{err}\n\n'
        f'⏰ {datetime.now(MST).strftime("%b %d, %I:%M %p MST")}'
    )


# ── Binance API ───────────────────────────────────────────────────────────────
def binance_signature(params: dict) -> str:
    return hmac.new(
        BINANCE_API_SECRET.encode('utf-8'),
        urlencode(params).encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()

def binance_public(endpoint: str, params: dict = None) -> dict:
    r = requests.get(f'{BINANCE_BASE_URL}{endpoint}', params=params or {}, timeout=10)
    r.raise_for_status()
    return r.json()

def binance_private(method: str, endpoint: str, params: dict = None) -> dict:
    params = params or {}
    params['timestamp'] = int(time.time() * 1000)
    params['signature'] = binance_signature(params)
    headers = {'X-MBX-APIKEY': BINANCE_API_KEY}
    url = f'{BINANCE_BASE_URL}{endpoint}'
    if method.upper() == 'GET':
        r = requests.get(url, params=params, headers=headers, timeout=15)
    else:
        r = requests.post(url, params=params, headers=headers, timeout=15)
    if not r.ok:
        logger.error(f'BINANCE {method} {endpoint} | {r.status_code} | {r.text}')
    r.raise_for_status()
    return r.json()

def get_margin_balance(asset: str) -> dict:
    account = binance_private('GET', '/sapi/v1/margin/account')
    for a in account.get('userAssets', []):
        if a['asset'] == asset:
            return {
                'free': float(a['free']),
                'borrowed': float(a['borrowed']),
                'interest': float(a['interest']),
                'net': float(a['netAsset']),
                'locked': float(a['locked']),
            }
    return {'free': 0.0, 'borrowed': 0.0, 'interest': 0.0, 'net': 0.0, 'locked': 0.0}

def get_margin_level() -> float:
    try:
        return float(binance_private('GET', '/sapi/v1/margin/account').get('marginLevel', 999))
    except Exception:
        return 999.0

def borrow_margin(asset: str, amount: float) -> bool:
    try:
        binance_private('POST', '/sapi/v1/margin/loan', {
            'asset': asset, 'amount': str(round(amount, 8)),
        })
        logger.info(f'🏦 Borrowed {amount:.6f} {asset}')
        return True
    except requests.HTTPError as e:
        txt = getattr(e.response, 'text', '') or ''
        if '"code":-3045' in txt or '"code":-3035' in txt:
            logger.warning(f'Borrow pool unavailable for {asset}')
            return False
        logger.error(f'Borrow {asset} failed: {e}')
        return False
    except Exception as e:
        logger.error(f'Borrow {asset} failed: {e}')
        return False

def repay_margin(asset: str, amount: float) -> bool:
    try:
        binance_private('POST', '/sapi/v1/margin/repay', {
            'asset': asset, 'amount': str(round(amount, 8)),
        })
        logger.info(f'💸 Repaid {amount:.6f} {asset}')
        return True
    except Exception as e:
        logger.error(f'Repay {asset} failed: {e}')
        return False

def margin_order(side: str, quantity: float, side_effect: str = 'NO_SIDE_EFFECT') -> dict:
    return binance_private('POST', '/sapi/v1/margin/order', {
        'symbol': SYMBOL,
        'side': side,
        'type': 'MARKET',
        'quantity': str(quantity),
        'sideEffectType': side_effect,
    })

def get_step_size() -> float:
    info = binance_public('/api/v3/exchangeInfo', {'symbol': SYMBOL})
    for s in info.get('symbols', []):
        if s['symbol'] == SYMBOL:
            for f in s.get('filters', []):
                if f['filterType'] == 'LOT_SIZE':
                    return float(f['stepSize'])
    return 0.01

def round_step(qty: float, step: float) -> float:
    precision = len(str(step).rstrip('0').split('.')[-1])
    return round(qty - (qty % step), precision)

def extract_fill_data(resp: dict) -> dict:
    """Read actual avg fill price and total fee (USDT) from Binance order response."""
    fills = resp.get('fills') or []
    total_qty, total_cost, total_fee = 0.0, 0.0, 0.0
    for f in fills:
        qty   = float(f.get('qty', 0))
        price = float(f.get('price', 0))
        fee   = float(f.get('commission', 0))
        asset = f.get('commissionAsset', '')
        total_qty  += qty
        total_cost += qty * price
        if asset == 'USDT':
            total_fee += fee
        elif asset == 'BNB':
            try:
                bnb_price = float(binance_public('/api/v3/ticker/price', {'symbol': 'BNBUSDT'})['price'])
                total_fee += fee * bnb_price
            except Exception:
                total_fee += qty * price * FEE_RATE   # fallback estimate
        else:
            total_fee += fee * price   # fee in base asset → convert via fill price
    avg_price = total_cost / total_qty if total_qty > 0 else 0.0
    return {'avg_price': avg_price, 'qty': total_qty, 'fee_usdt': total_fee}

def detect_position() -> Optional[str]:
    asset = get_margin_balance(BASE_ASSET)
    step  = get_step_size()
    if asset['net'] >= step and asset['net'] > 0.05:
        return 'LONG'
    if (asset['borrowed'] + asset['interest']) > 0.05:
        return 'SHORT'
    return None

def cleanup_orphan_borrows() -> None:
    """Repay any dangling borrows that are not part of an open trade."""
    try:
        sol = get_margin_balance(BASE_ASSET)
        borrowed = sol['borrowed'] + sol['interest']
        if borrowed > 0.001 and sol['free'] > 0.001:
            repay_margin(BASE_ASSET, min(sol['free'], borrowed))
    except Exception as e:
        logger.warning(f'cleanup SOL borrow: {e}')
    try:
        usdt = get_margin_balance('USDT')
        sol_check = get_margin_balance(BASE_ASSET)
        if sol_check['net'] <= 0.05 and (usdt['borrowed'] + usdt['interest']) > 0.01 and usdt['free'] > 0.01:
            repay_margin('USDT', min(usdt['free'], usdt['borrowed'] + usdt['interest']))
    except Exception as e:
        logger.warning(f'cleanup USDT borrow: {e}')


# ── Market Data & Strategy ────────────────────────────────────────────────────
def get_market_data() -> pd.DataFrame:
    """Fetch 100 × 1H candles with ADX, EMA21, EMA50, RSI(14), ATR(14), volume MA."""
    klines = binance_public('/api/v3/klines', {'symbol': SYMBOL, 'interval': '1h', 'limit': 100})
    df = pd.DataFrame(klines, columns=[
        'time', 'open', 'high', 'low', 'close', 'volume',
        'close_time', 'quote_volume', 'trades', 'taker_base', 'taker_quote', 'ignore',
    ])
    for col in ('open', 'high', 'low', 'close', 'volume'):
        df[col] = df[col].astype(float)

    adx_ind       = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14)
    df['adx']     = adx_ind.adx()
    df['adx_pos'] = adx_ind.adx_pos()   # +DI — bullish directional pressure
    df['adx_neg'] = adx_ind.adx_neg()   # -DI — bearish directional pressure

    df['ema21'] = ta.trend.EMAIndicator(df['close'], window=21).ema_indicator()
    df['ema50'] = ta.trend.EMAIndicator(df['close'], window=50).ema_indicator()
    df['rsi']   = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
    df['atr']   = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range()
    df['vol_ma'] = df['volume'].rolling(20).mean()

    return df


def _snapshot(row) -> dict:
    """Pull indicator values from a DataFrame row for dashboard display."""
    return {
        'adx':     round(float(row['adx']),     2),
        'adx_pos': round(float(row['adx_pos']), 2),
        'adx_neg': round(float(row['adx_neg']), 2),
        'ema21':   round(float(row['ema21']),   4),
        'ema50':   round(float(row['ema50']),   4),
        'rsi':     round(float(row['rsi']),     2),
        'atr':     round(float(row['atr']),     4),
    }


def get_decision(df: pd.DataFrame) -> dict:
    """
    ADX Regime + EMA21 Pullback strategy.

    Returns dict with keys:
      action ('LONG' | 'SHORT' | 'HOLD')
      confidence (0-90 int)
      regime ('TRENDING' | 'CHOPPY')
      trend_direction ('BULLISH' | 'BEARISH' | 'NEUTRAL' | 'MIXED')
      reason (string)
      indicators (dict)
    """
    c = df.iloc[-1]   # most recent closed candle
    p = df.iloc[-2]   # previous candle

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
    snap    = _snapshot(c)

    # ── 1. Regime filter ──────────────────────────────────────────────────────
    if adx < ADX_MIN:
        return {
            'action': 'HOLD', 'confidence': 0,
            'regime': 'CHOPPY', 'trend_direction': 'NEUTRAL',
            'reason': f'ADX {adx:.1f} below {ADX_MIN} — market choppy, standing aside',
            'indicators': snap,
        }

    regime = 'TRENDING'

    # ── 2. Trend direction: +DI/-DI must agree with EMA structure ─────────────
    di_bullish = adx_pos > adx_neg
    ema_bullish = ema21 > ema50

    if di_bullish and ema_bullish:
        trend = 'BULLISH'
    elif not di_bullish and not ema_bullish:
        trend = 'BEARISH'
    else:
        return {
            'action': 'HOLD', 'confidence': 0,
            'regime': regime, 'trend_direction': 'MIXED',
            'reason': (
                f'ADX {adx:.1f} trending but +DI/{adx_pos:.1f} vs -DI/{adx_neg:.1f} '
                f'conflicts with EMA21({ema21:.2f})/EMA50({ema50:.2f}) — waiting for alignment'
            ),
            'indicators': snap,
        }

    # ── 3. ATR filter — skip dead/ultra-quiet markets ────────────────────────
    if atr < MIN_ATR_USDT:
        return {
            'action': 'HOLD', 'confidence': 0,
            'regime': regime, 'trend_direction': trend,
            'reason': f'ATR ${atr:.2f} too low (min ${MIN_ATR_USDT:.2f}) — not enough movement to cover fees',
            'indicators': snap,
        }

    # ── 4. Pullback entry detection ───────────────────────────────────────────
    if trend == 'BULLISH':
        # Price must be within PULLBACK_ZONE_PCT above EMA21 (pulled back to it)
        dist_pct      = (price - ema21) / ema21          # positive = above EMA21
        in_zone       = 0 <= dist_pct <= PULLBACK_ZONE_PCT
        candle_dipped = lo <= ema21 * 1.008              # low touched EMA21 area this hour
        rsi_ok        = RSI_LONG_MIN <= rsi <= RSI_LONG_MAX

        if in_zone and candle_dipped and rsi_ok:
            conf = 70
            if adx > ADX_STRONG:        conf += 7
            if vol > vol_ma * 0.8:      conf += 5   # reasonable volume — not dead
            if 40 <= rsi <= 55:         conf += 5   # ideal RSI zone for pullback entry
            if float(p['close']) < ema21: conf += 5  # previous candle below EMA21 = fresh bounce
            return {
                'action': 'LONG', 'confidence': min(conf, 90),
                'regime': regime, 'trend_direction': trend,
                'reason': (
                    f'Pullback to EMA21 in uptrend | '
                    f'ADX {adx:.1f} | RSI {rsi:.1f} | '
                    f'dist {dist_pct*100:.2f}% above EMA21 | '
                    f'+DI {adx_pos:.1f} > -DI {adx_neg:.1f}'
                ),
                'indicators': snap,
            }
        else:
            parts = []
            if not in_zone:
                if dist_pct < 0:
                    parts.append(f'price ${price:.2f} is below EMA21 — wait for recovery')
                else:
                    parts.append(f'price {dist_pct*100:.2f}% above EMA21 — not pulled back yet')
            if not candle_dipped:
                parts.append(f'candle low ${lo:.2f} has not reached EMA21 ${ema21:.2f}')
            if not rsi_ok:
                parts.append(f'RSI {rsi:.1f} outside [{RSI_LONG_MIN}–{RSI_LONG_MAX}]')
            return {
                'action': 'HOLD', 'confidence': 0,
                'regime': regime, 'trend_direction': trend,
                'reason': f'BULLISH — waiting for EMA21 pullback: {", ".join(parts)}',
                'indicators': snap,
            }

    if trend == 'BEARISH':
        # Price must be within PULLBACK_ZONE_PCT below EMA21 (bounced up to it)
        dist_pct       = (ema21 - price) / ema21         # positive = below EMA21
        in_zone        = 0 <= dist_pct <= PULLBACK_ZONE_PCT
        candle_tapped  = hi >= ema21 * 0.992             # high touched EMA21 area this hour
        rsi_ok         = RSI_SHORT_MIN <= rsi <= RSI_SHORT_MAX

        if in_zone and candle_tapped and rsi_ok:
            conf = 70
            if adx > ADX_STRONG:        conf += 7
            if vol > vol_ma * 0.8:      conf += 5
            if 45 <= rsi <= 60:         conf += 5
            if float(p['close']) > ema21: conf += 5  # previous close above EMA21 = fresh rejection
            return {
                'action': 'SHORT', 'confidence': min(conf, 90),
                'regime': regime, 'trend_direction': trend,
                'reason': (
                    f'Bounce to EMA21 in downtrend | '
                    f'ADX {adx:.1f} | RSI {rsi:.1f} | '
                    f'dist {dist_pct*100:.2f}% below EMA21 | '
                    f'-DI {adx_neg:.1f} > +DI {adx_pos:.1f}'
                ),
                'indicators': snap,
            }
        else:
            parts = []
            if not in_zone:
                if dist_pct < 0:
                    parts.append(f'price ${price:.2f} is above EMA21 — wait for pullback')
                else:
                    parts.append(f'price {dist_pct*100:.2f}% below EMA21 — not bounced to it yet')
            if not candle_tapped:
                parts.append(f'candle high ${hi:.2f} has not reached EMA21 ${ema21:.2f}')
            if not rsi_ok:
                parts.append(f'RSI {rsi:.1f} outside [{RSI_SHORT_MIN}–{RSI_SHORT_MAX}]')
            return {
                'action': 'HOLD', 'confidence': 0,
                'regime': regime, 'trend_direction': trend,
                'reason': f'BEARISH — waiting for EMA21 bounce: {", ".join(parts)}',
                'indicators': snap,
            }

    return {
        'action': 'HOLD', 'confidence': 0,
        'regime': regime, 'trend_direction': 'NEUTRAL',
        'reason': 'No actionable setup this cycle',
        'indicators': snap,
    }


def get_current_price() -> float:
    return float(binance_public('/api/v3/ticker/price', {'symbol': SYMBOL})['price'])

def get_atr_1h(period: int = 14) -> float:
    try:
        klines = binance_public('/api/v3/klines', {'symbol': SYMBOL, 'interval': '1h', 'limit': period + 1})
        ranges = [float(k[2]) - float(k[3]) for k in klines]
        return sum(ranges[-period:]) / period
    except Exception as e:
        logger.warning(f'get_atr_1h failed: {e}')
        return 0.0


# ── Trade Recording ───────────────────────────────────────────────────────────
def record_closed_trade(
    side: str, entry_price: float, exit_price: float,
    qty: float, reason: str, actual_fee: float = None,
) -> None:
    """Append an accurate closed trade record to state (authoritative source for dashboard)."""
    fee = actual_fee if actual_fee is not None else (entry_price + exit_price) * qty * FEE_RATE
    if side == 'LONG':
        pnl = round((exit_price - entry_price) * qty - fee, 4)
    else:
        pnl = round((entry_price - exit_price) * qty - fee, 4)
    record = {
        'source': 'bot',
        'side': side,
        'entry_price': round(entry_price, 4),
        'exit_price':  round(exit_price, 4),
        'qty':         round(qty, 4),
        'fee':         round(fee, 4),
        'pnl':         pnl,
        'win':         pnl > 0,
        'opened_at':   state.get('trade_opened_at'),
        'closed_at':   now_utc_iso(),
        'note':        reason[:120],
    }
    log = state.get('closed_trades_log') or []
    log.append(record)
    state['closed_trades_log'] = log[-200:]
    save_state()
    logger.info(f'📋 Trade recorded | {side} | pnl={pnl:+.4f} USDT | win={pnl > 0}')

def summarize_performance(trades: list) -> dict:
    total = len(trades)
    wins  = sum(1 for t in trades if t.get('win'))
    net   = round(sum(float(t.get('pnl', 0)) for t in trades), 4)
    pnls  = [float(t.get('pnl', 0)) for t in trades]
    return {
        'closed_trades': total,
        'wins':          wins,
        'losses':        total - wins,
        'win_rate':      round(wins / total * 100, 1) if total else 0.0,
        'net_profit':    net,
        'avg_pnl':       round(net / total, 4) if total else 0.0,
        'best_trade':    round(max(pnls, default=0.0), 4),
        'worst_trade':   round(min(pnls, default=0.0), 4),
    }


# ── Trailing Stop ─────────────────────────────────────────────────────────────
def init_trail(entry_price: float) -> None:
    atr = get_atr_1h()
    state['trail_entry_price'] = entry_price
    state['trail_best_price']  = entry_price
    state['trail_atr']         = atr
    save_state()
    hard_sl_long  = entry_price - atr * HARD_SL_ATR
    hard_sl_short = entry_price + atr * HARD_SL_ATR
    logger.info(
        f'📐 Trail init | entry={entry_price:.4f} ATR={atr:.4f} '
        f'hardSL={hard_sl_long:.4f}(L) / {hard_sl_short:.4f}(S) '
        f'activates at {TRAIL_ACTIVATE_ATR}×ATR profit'
    )

def clear_trail() -> None:
    state['trail_entry_price'] = None
    state['trail_best_price']  = None
    state['trail_atr']         = None
    save_state()

def check_sl_trail(position: str, price: float) -> Tuple[bool, str]:
    """
    Returns (should_close, reason_string).
    Hard SL:  1.5×ATR from entry, active only before trail activates.
    Trail:    activates after profit ≥ 0.75×ATR, then locks in at least 65% of gains.
    """
    entry = safe_float(state.get('trail_entry_price'), None)
    best  = safe_float(state.get('trail_best_price'),  None)
    atr   = safe_float(state.get('trail_atr'),         None)

    if entry is None or atr is None or atr <= 0:
        return False, ''

    is_long       = position == 'LONG'
    hard_sl_dist  = atr * HARD_SL_ATR
    activate_dist = atr * TRAIL_ACTIVATE_ATR

    profit_so_far   = (best - entry) if is_long else (entry - best)
    trail_activated = profit_so_far >= activate_dist

    # Hard stop — only enforced before trail activates
    if not trail_activated:
        sl = entry - hard_sl_dist if is_long else entry + hard_sl_dist
        if (is_long and price <= sl) or (not is_long and price >= sl):
            return True, f'🛑 Hard SL hit | entry={entry:.4f} sl={sl:.4f} price={price:.4f} ATR={atr:.4f}'

    # Update best price seen
    if is_long and price > best:
        state['trail_best_price'] = price
        best = price
        save_state()
    elif not is_long and price < best:
        state['trail_best_price'] = price
        best = price
        save_state()

    # Trailing stop — only active after activation threshold
    profit_dist = (best - entry) if is_long else (entry - best)
    if profit_dist < activate_dist:
        logger.info(
            f'📐 Trail not yet active | '
            f'profit={profit_dist:.4f} < activate={activate_dist:.4f} | '
            f'best={best:.4f}'
        )
        return False, ''

    # Dynamic trail distance: locks ~65% of gains, minimum 0.5×ATR breathing room
    dynamic_dist = max(atr * 0.5, profit_dist * 0.35)
    trail_stop   = best - dynamic_dist if is_long else best + dynamic_dist
    locked_pct   = round((profit_dist - dynamic_dist) / profit_dist * 100, 0) if profit_dist > 0 else 0

    if (is_long and price <= trail_stop) or (not is_long and price >= trail_stop):
        return True, (
            f'📐 Trail stop hit | best={best:.4f} stop={trail_stop:.4f} '
            f'price={price:.4f} | locked {locked_pct:.0f}% of gains'
        )

    logger.info(
        f'📐 Trail active | best={best:.4f} stop={trail_stop:.4f} '
        f'price={price:.4f} | locked={locked_pct:.0f}%'
    )
    return False, ''


# ── Trade Execution ───────────────────────────────────────────────────────────
def mark_bot_close(side: str) -> None:
    state['last_bot_closed_side'] = side
    state['last_bot_closed_ts']   = int(time.time())

def open_long(price: float, confidence: int, reason: str) -> bool:
    try:
        step       = get_step_size()
        usdt       = get_margin_balance('USDT')
        collateral = float(runtime_settings['trade_amount_usdt'])
        leverage   = float(runtime_settings['leverage'])
        gross_usdt = collateral * leverage
        borrow_amt = round(gross_usdt - collateral, 2)

        if usdt['free'] < collateral:
            logger.warning(f'Insufficient USDT: need ${collateral:.2f} have ${usdt["free"]:.2f}')
            return False
        if borrow_amt > 0 and not borrow_margin('USDT', borrow_amt):
            return False

        time.sleep(1)
        quantity = round_step((gross_usdt * 0.995) / price, step)
        if quantity < step:
            logger.warning(f'Quantity {quantity} below step size {step}')
            return False

        resp         = margin_order('BUY', quantity, 'NO_SIDE_EFFECT')
        fill         = extract_fill_data(resp)
        actual_price = fill['avg_price'] or price

        state['trade_opened_at']         = now_utc_iso()
        state['entry_fee_usdt']          = fill['fee_usdt']
        state['active_trade_amount_usdt'] = collateral
        state['active_trade_leverage']    = leverage
        state['active_qty']               = fill['qty'] or quantity
        init_trail(actual_price)

        atr = safe_float(state.get('trail_atr'), 0)
        logger.info(f'✅ LONG OPEN {quantity:.4f} SOL @ ${actual_price:.4f} | fee=${fill["fee_usdt"]:.4f}')
        send_telegram(
            f'🟢 <b>APEX v2 — LONG OPENED ({leverage:.0f}x)</b>\n\n'
            f'📌 SOL/USDT\n'
            f'💰 Entry: ${actual_price:,.4f}\n'
            f'💵 Collateral: ${collateral:.2f} | Effective: ${gross_usdt:.2f}\n'
            f'🪙 Qty: {quantity:.4f} SOL\n'
            f'🛑 Hard SL: ${actual_price - atr*HARD_SL_ATR:,.4f}\n'
            f'🎯 Confidence: {confidence}%\n'
            f'📊 {reason}'
        )
        return True
    except Exception as e:
        logger.error(f'open_long failed: {e}')
        alert_error(f'open_long: {e}')
        return False

def close_long(price: float, confidence: int, reason: str) -> bool:
    try:
        step  = get_step_size()
        asset = get_margin_balance(BASE_ASSET)
        sellable = asset['free'] if asset['free'] >= step else asset['locked']
        quantity = round_step(sellable, step)

        if quantity < step:
            logger.warning(f'No SOL to sell (free={asset["free"]:.6f} locked={asset["locked"]:.6f})')
            return False

        entry_price = safe_float(state.get('trail_entry_price'), price)
        entry_fee   = safe_float(state.get('entry_fee_usdt'),    0.0)

        resp         = margin_order('SELL', quantity, 'AUTO_REPAY')
        fill         = extract_fill_data(resp)
        actual_close = fill['avg_price'] or price
        total_fee    = round(entry_fee + fill['fee_usdt'], 4)

        mark_bot_close('LONG')
        record_closed_trade('LONG', entry_price, actual_close, quantity, reason, total_fee)

        gross = (actual_close - entry_price) * quantity
        net   = gross - total_fee
        logger.info(f'✅ LONG CLOSE {quantity:.4f} SOL @ ${actual_close:.4f} | net={net:+.4f} USDT')
        send_telegram(
            f'🔴 <b>APEX v2 — LONG CLOSED</b>\n\n'
            f'💰 Exit: ${actual_close:,.4f} | Entry was: ${entry_price:,.4f}\n'
            f'🪙 Qty: {quantity:.4f} SOL\n'
            f'✅ Gross: {gross:+.4f} USDT\n'
            f'💸 Fees: -{total_fee:.4f} USDT\n'
            f'🏦 Net P&L: {net:+.4f} USDT\n'
            f'📉 {reason}'
        )
        return True
    except Exception as e:
        logger.error(f'close_long failed: {e}')
        alert_error(f'close_long: {e}')
        return False

def open_short(price: float, confidence: int, reason: str) -> bool:
    try:
        step        = get_step_size()
        usdt        = get_margin_balance('USDT')
        collateral  = float(runtime_settings['trade_amount_usdt'])
        leverage    = float(runtime_settings['leverage'])
        gross_usdt  = collateral * leverage
        borrow_base = round_step((gross_usdt * 0.995) / price, step)

        if usdt['free'] < collateral:
            logger.warning(f'Insufficient USDT: need ${collateral:.2f} have ${usdt["free"]:.2f}')
            return False
        if borrow_base < step:
            return False
        if not borrow_margin(BASE_ASSET, borrow_base):
            return False

        time.sleep(1)
        resp         = margin_order('SELL', borrow_base, 'NO_SIDE_EFFECT')
        fill         = extract_fill_data(resp)
        actual_price = fill['avg_price'] or price

        state['trade_opened_at']          = now_utc_iso()
        state['entry_fee_usdt']           = fill['fee_usdt']
        state['active_trade_amount_usdt'] = collateral
        state['active_trade_leverage']    = leverage
        state['active_qty']               = fill['qty'] or borrow_base
        init_trail(actual_price)

        atr = safe_float(state.get('trail_atr'), 0)
        logger.info(f'✅ SHORT OPEN {borrow_base:.4f} SOL @ ${actual_price:.4f} | fee=${fill["fee_usdt"]:.4f}')
        send_telegram(
            f'🔴 <b>APEX v2 — SHORT OPENED ({leverage:.0f}x)</b>\n\n'
            f'📌 SOL/USDT\n'
            f'💰 Entry: ${actual_price:,.4f}\n'
            f'💵 Collateral: ${collateral:.2f} | Effective: ${gross_usdt:.2f}\n'
            f'🪙 Qty: {borrow_base:.4f} SOL\n'
            f'🛑 Hard SL: ${actual_price + atr*HARD_SL_ATR:,.4f}\n'
            f'🎯 Confidence: {confidence}%\n'
            f'📊 {reason}'
        )
        return True
    except Exception as e:
        logger.error(f'open_short failed: {e}')
        alert_error(f'open_short: {e}')
        return False

def close_short(price: float, confidence: int, reason: str) -> bool:
    try:
        step           = get_step_size()
        asset          = get_margin_balance(BASE_ASSET)
        borrowed_total = asset['borrowed'] + asset['interest']

        if borrowed_total < 0.0001:
            logger.info('No borrowed SOL — short already closed')
            return False

        entry_price = safe_float(state.get('trail_entry_price'), price)
        entry_fee   = safe_float(state.get('entry_fee_usdt'),    0.0)

        # Buy slightly more than borrowed to guarantee full repayment
        quantity = round_step(borrowed_total * 1.005, step)
        if quantity < borrowed_total:
            quantity = round_step(borrowed_total + step, step)

        resp         = margin_order('BUY', quantity, 'AUTO_REPAY')
        fill         = extract_fill_data(resp)
        actual_close = fill['avg_price'] or price
        total_fee    = round(entry_fee + fill['fee_usdt'], 4)

        mark_bot_close('SHORT')
        record_closed_trade('SHORT', entry_price, actual_close, quantity, reason, total_fee)

        time.sleep(2)
        cleanup_orphan_borrows()

        # Sell any leftover SOL from the overbuy buffer
        after = get_margin_balance(BASE_ASSET)
        if after['free'] >= step:
            try:
                margin_order('SELL', round_step(after['free'], step), 'AUTO_REPAY')
            except Exception as ex:
                logger.warning(f'Excess SOL sell failed: {ex}')

        gross = (entry_price - actual_close) * quantity
        net   = gross - total_fee
        logger.info(f'✅ SHORT CLOSE {quantity:.4f} SOL @ ${actual_close:.4f} | net={net:+.4f} USDT')
        send_telegram(
            f'🟢 <b>APEX v2 — SHORT CLOSED</b>\n\n'
            f'💰 Exit: ${actual_close:,.4f} | Entry was: ${entry_price:,.4f}\n'
            f'🪙 Qty: {quantity:.4f} SOL\n'
            f'✅ Gross: {gross:+.4f} USDT\n'
            f'💸 Fees: -{total_fee:.4f} USDT\n'
            f'🏦 Net P&L: {net:+.4f} USDT\n'
            f'📈 {reason}'
        )

        after_check = get_margin_balance(BASE_ASSET)
        return (after_check['borrowed'] + after_check['interest']) < 0.01
    except Exception as e:
        logger.error(f'close_short failed: {e}')
        alert_error(f'close_short: {e}')
        return False


# ── Dashboard ─────────────────────────────────────────────────────────────────
def push_dashboard_data(data: dict) -> None:
    if not GH_TOKEN or not DASHBOARD_REPO:
        logger.warning('Dashboard push skipped: GH_TOKEN or DASHBOARD_REPO missing')
        return
    try:
        url     = f'https://api.github.com/repos/{DASHBOARD_REPO}/contents/{DASHBOARD_FILE}'
        headers = {'Authorization': f'token {GH_TOKEN}', 'Accept': 'application/vnd.github+json'}
        content = base64.b64encode(json.dumps(data, indent=2).encode()).decode()
        r       = requests.get(url, headers=headers, timeout=10)
        sha     = r.json().get('sha') if r.status_code == 200 else None
        payload = {
            'message': f'bot: SOL {datetime.now(MST).strftime("%H:%M MST")}',
            'content': content,
        }
        if sha:
            payload['sha'] = sha
        r = requests.put(url, headers=headers, json=payload, timeout=15)
        if r.status_code in (200, 201):
            logger.info('✅ Dashboard updated')
        else:
            logger.warning(f'Dashboard push failed: {r.status_code}')
    except Exception as e:
        logger.warning(f'Dashboard push error: {e}')

def fetch_dashboard_config() -> dict:
    default = {
        'trade_amount_usdt': DEFAULT_TRADE_AMOUNT_USDT,
        'leverage': DEFAULT_LEVERAGE,
        'close_requested': False,
        'close_requested_at': None,
        'bot_paused': False,
        'force_trail': False,
        'force_trail_at': None,
        'updated_at': None,
    }
    if not GH_TOKEN or not DASHBOARD_REPO:
        return default
    try:
        url     = f'https://api.github.com/repos/{DASHBOARD_REPO}/contents/{BOT_CONFIG_FILE}'
        headers = {'Authorization': f'token {GH_TOKEN}', 'Accept': 'application/vnd.github+json'}
        r       = requests.get(url, headers=headers, timeout=10)
        if not r.ok:
            return default
        cfg = json.loads(base64.b64decode(r.json()['content'].replace('\n', '')).decode())
        amt = safe_float(cfg.get('trade_amount_usdt') or cfg.get('trade_amount'), DEFAULT_TRADE_AMOUNT_USDT)
        lev = safe_float(cfg.get('leverage'), DEFAULT_LEVERAGE)
        if lev not in ALLOWED_LEVERAGES:
            lev = DEFAULT_LEVERAGE
        if amt <= 0:
            amt = DEFAULT_TRADE_AMOUNT_USDT
        return {
            'trade_amount_usdt':  amt,
            'leverage':           lev,
            'close_requested':    bool(cfg.get('close_requested', False)),
            'close_requested_at': cfg.get('close_requested_at'),
            'bot_paused':         bool(cfg.get('bot_paused', False)),
            'force_trail':        bool(cfg.get('force_trail', False)),
            'force_trail_at':     cfg.get('force_trail_at'),
            'updated_at':         cfg.get('updated_at'),
        }
    except Exception as e:
        logger.warning(f'Dashboard config read failed: {e}')
        return default

def apply_runtime_settings(position: Optional[str]) -> dict:
    cfg = fetch_dashboard_config()
    if position:
        # Lock settings for the duration of the open trade
        if state.get('active_trade_amount_usdt') is None:
            state['active_trade_amount_usdt'] = cfg['trade_amount_usdt']
        if state.get('active_trade_leverage') is None:
            state['active_trade_leverage'] = cfg['leverage']
        amt = safe_float(state['active_trade_amount_usdt'], cfg['trade_amount_usdt'])
        lev = safe_float(state['active_trade_leverage'],    cfg['leverage'])
        src = 'locked-open-trade'
    else:
        state['active_trade_amount_usdt'] = None
        state['active_trade_leverage']    = None
        amt = cfg['trade_amount_usdt']
        lev = cfg['leverage']
        src = 'dashboard-config'
    runtime_settings.update({'trade_amount_usdt': amt, 'leverage': lev, 'source': src})
    logger.info(f'⚙️ Settings | ${amt:.2f} @ {lev:.0f}x | src={src}')
    return cfg

def clear_flag(flag_name: str) -> None:
    """Set a boolean flag to False in bot_config.json on GitHub."""
    if not GH_TOKEN or not DASHBOARD_REPO:
        return
    try:
        url     = f'https://api.github.com/repos/{DASHBOARD_REPO}/contents/{BOT_CONFIG_FILE}'
        headers = {'Authorization': f'token {GH_TOKEN}', 'Accept': 'application/vnd.github+json'}
        r       = requests.get(url, headers=headers, timeout=10)
        if not r.ok:
            return
        j   = r.json()
        cfg = json.loads(base64.b64decode(j['content'].replace('\n', '')).decode())
        cfg[flag_name] = False
        if flag_name == 'close_requested':
            cfg.pop('close_requested_at', None)
        content = base64.b64encode(json.dumps(cfg, indent=2).encode()).decode()
        requests.put(
            url, headers=headers,
            json={'message': f'bot: clear {flag_name}', 'content': content, 'sha': j.get('sha')},
            timeout=15,
        )
        logger.info(f'✅ Cleared {flag_name}')
    except Exception as e:
        logger.warning(f'clear_flag({flag_name}) failed: {e}')


# ── Main Loop ─────────────────────────────────────────────────────────────────
def build_trail_info(latest_position: Optional[str]) -> dict:
    ep  = state.get('trail_entry_price')
    bp  = state.get('trail_best_price')
    atr = state.get('trail_atr')
    info = {
        'entry_price': ep,
        'best_price':  bp,
        'atr':         atr,
        'sl':          None,
        'trail_stop':  None,
        'active':      False,
    }
    if ep and atr:
        if latest_position == 'LONG':
            info['sl'] = round(ep - atr * HARD_SL_ATR, 4)
        elif latest_position == 'SHORT':
            info['sl'] = round(ep + atr * HARD_SL_ATR, 4)
    if ep and bp and atr:
        profit = (bp - ep) if latest_position == 'LONG' else (ep - bp)
        activate = atr * TRAIL_ACTIVATE_ATR
        if profit >= activate:
            dyn_dist = max(atr * 0.5, profit * 0.35)
            if latest_position == 'LONG':
                info['trail_stop'] = round(bp - dyn_dist, 4)
            elif latest_position == 'SHORT':
                info['trail_stop'] = round(bp + dyn_dist, 4)
            info['active'] = True
    return info


def run_once():
    global run_count, current_position, last_hold_alert
    run_count += 1
    logger.info('=' * 60)
    logger.info(f'  APEX v2 — Run #{run_count}')
    logger.info('=' * 60)

    try:
        # ── Position & runtime settings ───────────────────────────────────────
        current_position = detect_position()
        cfg = apply_runtime_settings(current_position)
        cleanup_orphan_borrows()

        # ── Market data & signal ──────────────────────────────────────────────
        df         = get_market_data()
        price      = get_current_price()
        decision   = get_decision(df)
        action     = decision['action']
        confidence = decision['confidence']
        regime     = decision['regime']
        trend      = decision['trend_direction']
        reason     = decision['reason']
        indicators = decision['indicators']

        logger.info(
            f'💰 SOL ${price:.4f} | signal={action}({confidence}%) | '
            f'pos={current_position} | regime={regime} | trend={trend}'
        )
        logger.info(
            f'   ADX={indicators["adx"]:.1f} +DI={indicators["adx_pos"]:.1f} '
            f'-DI={indicators["adx_neg"]:.1f} | EMA21={indicators["ema21"]:.4f} '
            f'EMA50={indicators["ema50"]:.4f} | RSI={indicators["rsi"]:.1f} | ATR={indicators["atr"]:.4f}'
        )

        # ── Dashboard close request ───────────────────────────────────────────
        dashboard_close_executed = False
        if cfg.get('close_requested') and current_position:
            req_at = cfg.get('close_requested_at')
            try:
                age = (
                    datetime.now(timezone.utc)
                    - datetime.fromisoformat(req_at.replace('Z', '+00:00'))
                ).total_seconds() if req_at else 999
            except Exception:
                age = 999
            if age < 300:
                logger.info(f'📱 Dashboard close request ({age:.0f}s old) — closing {current_position}')
                send_telegram(f'📱 <b>Dashboard Close</b>\nClosing {current_position} @ ${price:,.4f}')
                if current_position == 'LONG':
                    closed = close_long(price, 0, 'Dashboard close request')
                else:
                    closed = close_short(price, 0, 'Dashboard close request')
                if closed:
                    dashboard_close_executed = True
                    clear_trail()
                    current_position = detect_position()
                    clear_flag('close_requested')
                else:
                    alert_error(f'Dashboard close FAILED for {current_position} — retry next cycle')
            else:
                logger.info(f'⚠️ Dashboard close request stale ({age:.0f}s) — ignoring')
                clear_flag('close_requested')

        # ── Force trail (dashboard button) ────────────────────────────────────
        if not cfg.get('force_trail'):
            state['force_trail_processed'] = False
        if cfg.get('force_trail') and current_position and not state.get('force_trail_processed'):
            if not state.get('trail_entry_price'):
                init_trail(price)
            ep_  = safe_float(state.get('trail_entry_price'), price)
            atr_ = safe_float(state.get('trail_atr'), get_atr_1h())
            profit_  = abs(price - ep_)
            approx_  = max(atr_ * 0.5, profit_ * 0.35)
            stop_    = price - approx_ if current_position == 'LONG' else price + approx_
            state['trail_best_price']   = price
            state['force_trail_processed'] = True
            save_state()
            send_telegram(
                f'🔒 <b>Force Trail Activated</b>\n'
                f'Price: ${price:,.4f}\nApprox stop: ${stop_:,.4f}'
            )
            logger.info(f'🔒 Force trail activated | price={price:.4f} approx_stop={stop_:.4f}')
        if cfg.get('force_trail'):
            clear_flag('force_trail')

        # ── Trail state sync ──────────────────────────────────────────────────
        if current_position and state.get('trail_entry_price') is None:
            init_trail(price)
        if not current_position and state.get('trail_entry_price') is not None:
            clear_trail()

        # ── Check SL / trailing stop ──────────────────────────────────────────
        sl_trail_close = False
        status = ''
        if current_position and not dashboard_close_executed:
            sl_hit, sl_reason = check_sl_trail(current_position, price)
            if sl_hit:
                logger.info(sl_reason)
                send_telegram(
                    f'🛑 <b>APEX — SL/Trail Stop</b>\n{sl_reason}\n'
                    f'Closing {current_position} @ ${price:,.4f}'
                )
                if current_position == 'LONG':
                    close_long(price, confidence, sl_reason)
                else:
                    close_short(price, confidence, sl_reason)
                sl_trail_close = True
                clear_trail()
                current_position = detect_position()

        # ── Position management & entry ───────────────────────────────────────
        bot_paused = cfg.get('bot_paused', False)

        if dashboard_close_executed or sl_trail_close:
            action = 'HOLD'
            status = 'CLOSED ✅'
        elif not bot_paused:
            if current_position is None and action in ('LONG', 'SHORT'):
                margin_level = get_margin_level()
                if margin_level < 1.5 and margin_level != 999:
                    status = f'BLOCKED — margin level {margin_level:.2f} too low for new entry'
                    logger.warning(status)
                elif action == 'LONG':
                    ok = open_long(price, confidence, reason)
                    status = 'LONG OPENED ✅' if ok else 'LONG FAILED ❌'
                    if ok:
                        current_position = 'LONG'
                elif action == 'SHORT':
                    ok = open_short(price, confidence, reason)
                    status = 'SHORT OPENED ✅' if ok else 'SHORT FAILED ❌'
                    if ok:
                        current_position = 'SHORT'

            elif current_position == 'LONG':
                if action == 'SHORT':
                    # Signal flipped — close the long, don't immediately re-enter opposite
                    closed = close_long(price, confidence, 'Signal reversed to SHORT — closing LONG')
                    if closed:
                        clear_trail()
                        current_position = None
                        status = 'LONG CLOSED — signal reversed ✅'
                    else:
                        status = 'LONG CLOSE FAILED ❌'
                else:
                    status = f'HOLDING LONG | {reason}'
                    if action == 'HOLD':
                        now_ts = time.time()
                        if now_ts - last_hold_alert > 1800:
                            send_telegram(
                                f'⚠️ <b>Signal Fading (LONG open)</b>\n'
                                f'SOL ${price:.4f}\n{reason}'
                            )
                            last_hold_alert = now_ts

            elif current_position == 'SHORT':
                if action == 'LONG':
                    closed = close_short(price, confidence, 'Signal reversed to LONG — closing SHORT')
                    if closed:
                        clear_trail()
                        current_position = None
                        status = 'SHORT CLOSED — signal reversed ✅'
                    else:
                        status = 'SHORT CLOSE FAILED ❌'
                else:
                    status = f'HOLDING SHORT | {reason}'
                    if action == 'HOLD':
                        now_ts = time.time()
                        if now_ts - last_hold_alert > 1800:
                            send_telegram(
                                f'⚠️ <b>Signal Fading (SHORT open)</b>\n'
                                f'SOL ${price:.4f}\n{reason}'
                            )
                            last_hold_alert = now_ts

        if bot_paused and not status:
            status = '⏸️ BOT PAUSED — no new entries'

        status = status or f'HOLD — {reason}'

        # ── Balances & performance ────────────────────────────────────────────
        usdt_balance = safe_float(get_margin_balance('USDT').get('net'), 0)
        sol_balance  = safe_float(get_margin_balance(BASE_ASSET).get('net'), 0)
        try:
            margin_level = get_margin_level()
        except Exception:
            margin_level = 999.0

        latest_position = detect_position()
        state['prev_position'] = latest_position
        save_state()

        closed_trades = state.get('closed_trades_log') or []
        performance   = summarize_performance(closed_trades)
        trail_info    = build_trail_info(latest_position)

        # ── Push to dashboard ─────────────────────────────────────────────────
        push_dashboard_data({
            'generated_at':   now_utc_iso(),
            'symbol':         SYMBOL,
            'exchange':       'Binance Margin',
            'price':          price,
            'usdt_balance':   usdt_balance,
            'sol_balance':    sol_balance,
            'margin_level':   margin_level,
            'leverage':       runtime_settings['leverage'],
            'trade_amount':   runtime_settings['trade_amount_usdt'],
            'position':       latest_position,
            'active_qty':     state.get('active_qty'),
            'action':         action,
            'confidence':     confidence,
            'status':         status,
            'bot_paused':     bot_paused,
            'regime':         regime,
            'trend_direction': trend,
            'reason':         reason,
            'indicators':     indicators,
            'closed_trades':  closed_trades,
            'performance':    performance,
            'trail':          trail_info,
            'run_count':      run_count,
        })

        logger.info(
            f'✅ Run #{run_count} complete | '
            f'pos={latest_position} | margin={margin_level:.2f} | '
            f'net_pnl={performance["net_profit"]:.4f} USDT'
        )

    except Exception as e:
        logger.error(f'❌ run_once error: {e}', exc_info=True)
        alert_error(str(e))


def main():
    load_state()
    state['force_trail_processed'] = False
    save_state()

    logger.info('🚀 APEX v2 — Binance Cross-Margin SOL/USDT')
    logger.info('   Strategy: ADX Regime Filter + EMA21 Pullback (1H)')
    logger.info(f'  Amount: ${DEFAULT_TRADE_AMOUNT_USDT:.2f} @ {DEFAULT_LEVERAGE:.0f}x')
    logger.info(f'  API Key: {mask(BINANCE_API_KEY)}')
    logger.info(f'  GH Token: {mask(GH_TOKEN)}')
    logger.info(f'  Dashboard: {DASHBOARD_REPO}')

    send_telegram(
        f'🚀 <b>APEX v2 Started</b>\n\n'
        f'📌 SOL/USDT Cross-Margin\n'
        f'🎯 Strategy: ADX Regime + EMA21 Pullback (1H)\n'
        f'📊 No trade when ADX < {ADX_MIN} (choppy filter)\n'
        f'💰 Amount: ${DEFAULT_TRADE_AMOUNT_USDT:.2f} @ {DEFAULT_LEVERAGE:.0f}x\n'
        f'⏱ Cycle: every {CHECK_INTERVAL}s'
    )

    while True:
        try:
            run_once()
        except Exception as e:
            logger.error(f'Main loop error: {e}', exc_info=True)
            alert_error(f'Main loop: {e}')

        logger.info(f'⏳ Sleeping {CHECK_INTERVAL}s (mid-sleep SL checks every 5s)...')
        for _ in range(CHECK_INTERVAL // 5):
            time.sleep(5)
            try:
                quick_cfg = fetch_dashboard_config()
                if quick_cfg.get('close_requested'):
                    logger.info('⚡ Close request detected mid-sleep — waking up')
                    break
                if quick_cfg.get('force_trail'):
                    logger.info('⚡ Force trail detected mid-sleep — waking up')
                    break
                # Check trail/SL every 5s while sleeping
                pos = current_position
                if pos and state.get('trail_entry_price') and state.get('trail_atr'):
                    _price = get_current_price()
                    _hit, _reason = check_sl_trail(pos, _price)
                    if _hit:
                        logger.info(f'⚡ Trail/SL hit mid-sleep ({_reason}) — waking up')
                        break
            except Exception:
                pass


if __name__ == '__main__':
    main()
