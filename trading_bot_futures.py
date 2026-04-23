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
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
TELEGRAM_CHAT_ID   = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
LUNARCRUSH_API_KEY = os.environ.get('LUNARCRUSH_API_KEY', '').strip()
BOT_CONFIG_FILE    = os.environ.get('BOT_CONFIG_FILE', 'bot_config.json').strip()
BOT_STATE_FILE     = os.environ.get('BOT_STATE_FILE_FUTURES', 'bot_state_futures.json').strip()
TRADES_LOG_FILE    = os.environ.get('FUTURES_TRADES_LOG', 'trades_log_futures.json').strip()

DEFAULT_TRADE_AMOUNT_USDT = float(os.environ.get('FUTURES_TRADE_AMOUNT_USDT', '20'))
DEFAULT_LEVERAGE          = float(os.environ.get('FUTURES_LEVERAGE', '10'))
CHECK_INTERVAL            = int(os.environ.get('CHECK_INTERVAL', '60'))
ALLOWED_LEVERAGES         = {3.0, 4.0, 5.0, 10.0}

# ── Symbol Config ─────────────────────────────────────────────────────────────
SYMBOLS_CONFIG = {
    'ETHUSDT': {
        'base': 'ETH',
        'dashboard_file': 'data_futures_eth.json',
        'min_atr': 8.0,    # ETH 1H ATR typically $15-50
        'trade_amount': 20.0,
    },
    'SOLUSDT': {
        'base': 'SOL',
        'dashboard_file': 'data_futures_sol.json',
        'min_atr': 1.5,    # SOL 1H ATR typically $2-8
        'trade_amount': 20.0,
    },
    'AAVEUSDT': {
        'base': 'AAVE',
        'dashboard_file': 'data_futures_aave.json',
        'min_atr': 0.8,    # AAVE 1H ATR typically $1-3
        'trade_amount': 20.0,
    },
}
TRADING_SYMBOLS = list(SYMBOLS_CONFIG.keys())

# ── Strategy Parameters ───────────────────────────────────────────────────────
ADX_MIN           = 25.0
ADX_STRONG        = 30.0
PULLBACK_ZONE_PCT = 0.018
RSI_LONG_MIN      = 30
RSI_LONG_MAX      = 62
RSI_SHORT_MIN     = 38
RSI_SHORT_MAX     = 70

# ── Trail Parameters ──────────────────────────────────────────────────────────
TRAIL_ACTIVATE_ATR = 0.75
HARD_SL_ATR        = 1.50
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
        'closed_trades_log':      [],
    }

state = {
    'symbols': {sym: _empty_sym_state() for sym in ['ETHUSDT', 'SOLUSDT', 'AAVEUSDT']},
    'runtime': {
        'trade_amount_usdt': DEFAULT_TRADE_AMOUNT_USDT,
        'leverage':          DEFAULT_LEVERAGE,
        'source':            'env-defaults',
    },
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
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f: json.dump(data, f, indent=2)
    os.replace(tmp, path)

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

def save_state() -> None:
    write_json(BOT_STATE_FILE, state)

def sym_state(symbol: str) -> dict:
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

def mask(v: str, s: int = 6, e: int = 4) -> str:
    if not v: return 'MISSING'
    if len(v) <= s + e: return '*' * len(v)
    return f'{v[:s]}...{v[-e:]}'


# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(msg: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
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
                result['side']           = side
                result['qty']            = abs(amt)
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
                         position_side: str = 'LONG') -> dict:
    # Always send positionSide to support both Hedge Mode and One-way Mode
    return binance_futures_private('POST', '/fapi/v1/order', {
        'symbol':       symbol,
        'side':         side,
        'type':         'MARKET',
        'quantity':     str(quantity),
        'positionSide': position_side,
    })

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


# ── LunarCrush ETH Sentiment ──────────────────────────────────────────────────
_sentiment_cache: dict = {'value': None, 'ts': 0}
SENTIMENT_CACHE_TTL = 600

def get_eth_sentiment() -> Optional[float]:
    if not LUNARCRUSH_API_KEY: return None
    now = time.time()
    if _sentiment_cache['value'] is not None and (now - _sentiment_cache['ts']) < SENTIMENT_CACHE_TTL:
        return _sentiment_cache['value']
    try:
        r = requests.get(
            'https://lunarcrush.com/api4/public/topic/ethereum/v1',
            headers={'Authorization': f'Bearer {LUNARCRUSH_API_KEY}'},
            timeout=10,
        )
        r.raise_for_status()
        sentiment = float((r.json().get('data') or {}).get('sentiment') or 0)
        _sentiment_cache['value'] = sentiment
        _sentiment_cache['ts'] = now
        logger.info(f'🌙 LunarCrush ETH sentiment: {sentiment:.0f}%')
        return sentiment
    except Exception as e:
        logger.warning(f'LunarCrush ETH sentiment fetch failed: {e}')
        return None


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
    return df

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
    min_atr = SYMBOLS_CONFIG[symbol]['min_atr']

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

    if trend == 'BULLISH':
        dist_pct      = (price - ema21) / ema21
        in_zone       = 0 <= dist_pct <= PULLBACK_ZONE_PCT
        candle_dipped = lo <= ema21 * 1.008
        rsi_ok        = RSI_LONG_MIN <= rsi <= RSI_LONG_MAX
        if in_zone and candle_dipped and rsi_ok:
            sentiment = get_eth_sentiment()
            if sentiment is not None and sentiment < 50:
                return hold(f'BULLISH blocked — ETH sentiment {sentiment:.0f}% bearish', trend)
            conf = 70
            if adx > ADX_STRONG:             conf += 7
            if vol > vol_ma * 0.8:           conf += 5
            if 40 <= rsi <= 55:              conf += 5
            if float(p['close']) < ema21:    conf += 5
            if sentiment is not None and sentiment >= 70: conf += 5
            return {
                'action': 'LONG', 'confidence': min(conf, 90),
                'regime': 'TRENDING', 'trend_direction': trend,
                'reason': (f'Pullback to EMA21 in uptrend | ADX {adx:.1f} | RSI {rsi:.1f} | '
                           f'dist {dist_pct*100:.2f}% above EMA21'),
                'indicators': snap,
            }
        parts = []
        if not in_zone:       parts.append(f'price {dist_pct*100:.2f}% from EMA21 (need 0–1.8%)')
        if not candle_dipped: parts.append('candle low not near EMA21')
        if not rsi_ok:        parts.append(f'RSI {rsi:.1f} outside [{RSI_LONG_MIN}–{RSI_LONG_MAX}]')
        return hold(f'BULLISH — waiting: {", ".join(parts)}', trend)

    if trend == 'BEARISH':
        dist_pct      = (ema21 - price) / ema21
        in_zone       = 0 <= dist_pct <= PULLBACK_ZONE_PCT
        candle_tapped = hi >= ema21 * 0.992
        rsi_ok        = RSI_SHORT_MIN <= rsi <= RSI_SHORT_MAX
        if in_zone and candle_tapped and rsi_ok:
            sentiment = get_eth_sentiment()
            if sentiment is not None and sentiment > 50:
                return hold(f'BEARISH blocked — ETH sentiment {sentiment:.0f}% bullish', trend)
            conf = 70
            if adx > ADX_STRONG:             conf += 7
            if vol > vol_ma * 0.8:           conf += 5
            if 45 <= rsi <= 60:              conf += 5
            if float(p['close']) > ema21:    conf += 5
            if sentiment is not None and sentiment <= 30: conf += 5
            return {
                'action': 'SHORT', 'confidence': min(conf, 90),
                'regime': 'TRENDING', 'trend_direction': trend,
                'reason': (f'Bounce to EMA21 in downtrend | ADX {adx:.1f} | RSI {rsi:.1f} | '
                           f'dist {dist_pct*100:.2f}% below EMA21'),
                'indicators': snap,
            }
        parts = []
        if not in_zone:       parts.append(f'price {dist_pct*100:.2f}% from EMA21 (need 0–1.8%)')
        if not candle_tapped: parts.append('candle high not near EMA21')
        if not rsi_ok:        parts.append(f'RSI {rsi:.1f} outside [{RSI_SHORT_MIN}–{RSI_SHORT_MAX}]')
        return hold(f'BEARISH — waiting: {", ".join(parts)}', trend)

    return hold('No actionable setup')


# ── Trade Recording ───────────────────────────────────────────────────────────
def record_closed_trade(symbol: str, side: str, entry_price: float, exit_price: float,
                        qty: float, reason: str, actual_fee: float = None) -> None:
    ss  = sym_state(symbol)
    fee = actual_fee if actual_fee is not None else (entry_price + exit_price) * qty * FEE_RATE
    pnl = round((exit_price - entry_price) * qty - fee, 6) if side == 'LONG' \
        else round((entry_price - exit_price) * qty - fee, 6)
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
        'opened_at':   ss.get('trade_opened_at'),
        'closed_at':   now_utc_iso(),
        'note':        reason[:120],
    }
    log = ss.get('closed_trades_log') or []
    log.append(record)
    ss['closed_trades_log'] = log[-200:]
    append_trade_log(record)   # persist to append-only file (survives restarts)
    save_state()
    logger.info(f'📋 [{symbol}] Trade | {side} | pnl={pnl:+.4f} USDT | win={pnl > 0}')

def summarize_performance(trades: list) -> dict:
    total = len(trades)
    wins  = sum(1 for t in trades if t.get('win'))
    net   = round(sum(float(t.get('pnl', 0)) for t in trades), 4)
    pnls  = [float(t.get('pnl', 0)) for t in trades]
    return {
        'closed_trades': total, 'wins': wins, 'losses': total - wins,
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
    ss['trail_entry_price'] = None
    ss['trail_best_price']  = None
    ss['trail_atr']         = None
    save_state()

def check_sl_trail(symbol: str, position: str, price: float) -> Tuple[bool, str]:
    ss    = sym_state(symbol)
    entry = safe_float(ss.get('trail_entry_price'), None)
    best  = safe_float(ss.get('trail_best_price'),  None)
    atr   = safe_float(ss.get('trail_atr'),         None)
    if entry is None or atr is None or atr <= 0:
        return False, ''
    is_long       = position == 'LONG'
    hard_sl_dist  = atr * HARD_SL_ATR
    activate_dist = atr * TRAIL_ACTIVATE_ATR
    profit_so_far = (best - entry) if is_long else (entry - best)
    trail_active  = profit_so_far >= activate_dist

    if not trail_active:
        sl = entry - hard_sl_dist if is_long else entry + hard_sl_dist
        if (is_long and price <= sl) or (not is_long and price >= sl):
            return True, f'🛑 Hard SL hit | entry={entry:.4f} sl={sl:.4f} price={price:.4f}'

    if is_long and price > best:
        ss['trail_best_price'] = price; best = price; save_state()
    elif not is_long and price < best:
        ss['trail_best_price'] = price; best = price; save_state()

    profit_dist = (best - entry) if is_long else (entry - best)
    if profit_dist < activate_dist:
        logger.info(f'📐 [{symbol}] Trail not active | profit={profit_dist:.4f} < {activate_dist:.4f}')
        return False, ''

    dyn_dist   = max(atr * 0.4, profit_dist * 0.25)
    trail_stop = best - dyn_dist if is_long else best + dyn_dist
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
        info['sl'] = round(ep - atr * HARD_SL_ATR, 4) if position == 'LONG' \
                else round(ep + atr * HARD_SL_ATR, 4)
    if ep and bp and atr:
        profit = (bp - ep) if position == 'LONG' else (ep - bp)
        if profit >= atr * TRAIL_ACTIVATE_ATR:
            dyn = max(atr * 0.4, profit * 0.25)
            info['trail_stop'] = round(bp - dyn, 4) if position == 'LONG' else round(bp + dyn, 4)
            info['active'] = True
    return info


# ── Trade Execution ───────────────────────────────────────────────────────────
def open_long(symbol: str, price: float, confidence: int, reason: str) -> bool:
    ss   = sym_state(symbol)
    base = SYMBOLS_CONFIG[symbol]['base']
    try:
        collateral = float(SYMBOLS_CONFIG[symbol].get('trade_amount') or state['runtime']['trade_amount_usdt'])
        leverage   = int(state['runtime']['leverage'])
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
        qty_filled   = float(resp.get('executedQty') or quantity)
        fee_usdt     = qty_filled * actual_price * FEE_RATE

        ss['trade_opened_at']     = now_utc_iso()
        ss['entry_fee_usdt']      = fee_usdt
        ss['active_trade_amount'] = collateral
        ss['active_leverage']     = leverage
        ss['active_qty']          = qty_filled
        init_trail(symbol, actual_price)
        atr = safe_float(ss.get('trail_atr'), 0)

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
        step      = get_futures_step_size(symbol)
        positions = binance_futures_private('GET', '/fapi/v2/positionRisk', {'symbol': symbol})
        qty_held  = 0.0
        for p in positions:
            if p['symbol'] == symbol and p.get('positionSide', 'BOTH') in ('LONG', 'BOTH'):
                qty_held = abs(float(p['positionAmt']))
                if qty_held > 1e-8: break
        quantity = round_step(qty_held, step)
        if quantity < step:
            logger.warning(f'[{symbol}] No {base} long position to close')
            return False

        entry_price = safe_float(ss.get('trail_entry_price'), price)
        entry_fee   = safe_float(ss.get('entry_fee_usdt'), 0.0)

        resp         = futures_market_order(symbol, 'SELL', quantity, position_side='LONG')
        actual_close = get_fill_price(resp, price)
        exit_fee     = quantity * actual_close * FEE_RATE
        total_fee    = round(entry_fee + exit_fee, 6)

        ss['last_bot_closed_side'] = 'LONG'
        ss['last_bot_closed_ts']   = int(time.time())
        record_closed_trade(symbol, 'LONG', entry_price, actual_close, quantity, reason, total_fee)
        gross = (actual_close - entry_price) * quantity
        net   = gross - total_fee
        logger.info(f'✅ [{symbol}] LONG CLOSE {quantity:.4f} {base} @ ${actual_close:.4f} net={net:+.4f}')
        send_telegram(
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

def open_short(symbol: str, price: float, confidence: int, reason: str) -> bool:
    ss   = sym_state(symbol)
    base = SYMBOLS_CONFIG[symbol]['base']
    try:
        collateral = float(SYMBOLS_CONFIG[symbol].get('trade_amount') or state['runtime']['trade_amount_usdt'])
        leverage   = int(state['runtime']['leverage'])
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
        qty_filled   = float(resp.get('executedQty') or quantity)
        fee_usdt     = qty_filled * actual_price * FEE_RATE

        ss['trade_opened_at']     = now_utc_iso()
        ss['entry_fee_usdt']      = fee_usdt
        ss['active_trade_amount'] = collateral
        ss['active_leverage']     = leverage
        ss['active_qty']          = qty_filled
        init_trail(symbol, actual_price)
        atr = safe_float(ss.get('trail_atr'), 0)

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
        step      = get_futures_step_size(symbol)
        positions = binance_futures_private('GET', '/fapi/v2/positionRisk', {'symbol': symbol})
        qty_held  = 0.0
        for p in positions:
            if p['symbol'] == symbol and p.get('positionSide', 'BOTH') in ('SHORT', 'BOTH'):
                qty_held = abs(float(p['positionAmt']))
                if qty_held > 1e-8: break
        quantity = round_step(qty_held, step)
        if quantity < step:
            logger.warning(f'[{symbol}] No {base} short position to close')
            return False

        entry_price = safe_float(ss.get('trail_entry_price'), price)
        entry_fee   = safe_float(ss.get('entry_fee_usdt'), 0.0)

        resp         = futures_market_order(symbol, 'BUY', quantity, position_side='SHORT')
        actual_close = get_fill_price(resp, price)
        exit_fee     = quantity * actual_close * FEE_RATE
        total_fee    = round(entry_fee + exit_fee, 6)

        ss['last_bot_closed_side'] = 'SHORT'
        ss['last_bot_closed_ts']   = int(time.time())
        record_closed_trade(symbol, 'SHORT', entry_price, actual_close, quantity, reason, total_fee)
        gross = (entry_price - actual_close) * quantity
        net   = gross - total_fee
        logger.info(f'✅ [{symbol}] SHORT CLOSE {quantity:.4f} {base} @ ${actual_close:.4f} net={net:+.4f}')
        send_telegram(
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
            ep_  = safe_float(ss.get('trail_entry_price'), price)
            atr_ = safe_float(ss.get('trail_atr'), get_atr_1h(symbol))
            prof = abs(price - ep_)
            stop = price - max(atr_*0.5, prof*0.35) if position == 'LONG' \
                else price + max(atr_*0.5, prof*0.35)
            ss['trail_best_price']      = price
            ss['force_trail_processed'] = True
            save_state()
            send_telegram(f'🔒 <b>Force Trail {base}/USDT Futures</b>\nPrice: ${price:,.4f}\nApprox stop: ${stop:,.4f}')

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
                if position == 'LONG': close_long(symbol, price, sl_reason)
                else:                  close_short(symbol, price, sl_reason)
                sl_close = True
                clear_trail(symbol)
                position = detect_futures_position(symbol)

        # ── Entry / management ────────────────────────────────────────────────
        bot_paused = cfg.get('bot_paused', False)
        if dashboard_close_executed or sl_close:
            action = 'HOLD'
            status = 'CLOSED ✅'
        elif not bot_paused:
            if position is None and action in ('LONG', 'SHORT') and not allow_new_entry:
                status = f'HOLD — other symbol has better setup right now'
            elif position is None and action in ('LONG', 'SHORT'):
                if action == 'LONG':
                    ok = open_long(symbol, price, confidence, reason)
                    status = 'LONG OPENED ✅' if ok else 'LONG FAILED ❌'
                    if ok: position = 'LONG'
                else:
                    ok = open_short(symbol, price, confidence, reason)
                    status = 'SHORT OPENED ✅' if ok else 'SHORT FAILED ❌'
                    if ok: position = 'SHORT'
            elif position == 'LONG':
                if action == 'SHORT':
                    if close_long(symbol, price, 'Signal reversed to SHORT'):
                        clear_trail(symbol); position = None
                        status = 'LONG CLOSED — signal reversed ✅'
                    else:
                        status = 'LONG CLOSE FAILED ❌'
                else:
                    status = f'HOLDING LONG | {reason}'
                    if action == 'HOLD':
                        now_ts = time.time()
                        if now_ts - last_hold_alert.get(symbol, 0) > 1800:
                            send_telegram(f'⚠️ <b>Signal Fading — LONG {base}/USDT Futures</b>\n${price:.4f}\n{reason}')
                            last_hold_alert[symbol] = now_ts
            elif position == 'SHORT':
                if action == 'LONG':
                    if close_short(symbol, price, 'Signal reversed to LONG'):
                        clear_trail(symbol); position = None
                        status = 'SHORT CLOSED — signal reversed ✅'
                    else:
                        status = 'SHORT CLOSE FAILED ❌'
                else:
                    status = f'HOLDING SHORT | {reason}'
                    if action == 'HOLD':
                        now_ts = time.time()
                        if now_ts - last_hold_alert.get(symbol, 0) > 1800:
                            send_telegram(f'⚠️ <b>Signal Fading — SHORT {base}/USDT Futures</b>\n${price:.4f}\n{reason}')
                            last_hold_alert[symbol] = now_ts

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
            'leverage':        state['runtime']['leverage'],
            'trade_amount':    state['runtime']['trade_amount_usdt'],
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
            'sentiment':       _sentiment_cache.get('value'),
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

        # ── Find which symbols already have open positions ────────────────────
        open_syms = set()
        for sym in TRADING_SYMBOLS:
            if detect_futures_position(sym) is not None:
                open_syms.add(sym)

        # ── If no position open, pick highest-confidence signal ───────────────
        best_entry_sym = None
        if not open_syms:
            best_conf = -1
            for sym in TRADING_SYMBOLS:
                try:
                    df  = get_market_data(sym)
                    dec = get_decision(sym, df)
                    if dec['action'] in ('LONG', 'SHORT') and dec['confidence'] > best_conf:
                        best_conf      = dec['confidence']
                        best_entry_sym = sym
                except Exception as e:
                    logger.warning(f'Pre-scan [{sym}]: {e}')
            if best_entry_sym:
                logger.info(f'🏆 Best entry selected: {best_entry_sym} (conf={best_conf}%)')
            else:
                logger.info('⏳ No actionable setup on either symbol — holding')

        # ── Run each symbol; only winner gets to open a new trade ─────────────
        for symbol in TRADING_SYMBOLS:
            allow_entry = (symbol in open_syms) or (symbol == best_entry_sym)
            run_symbol(symbol, cfg, allow_new_entry=allow_entry)

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

    amt = state['runtime']['trade_amount_usdt']
    lev = state['runtime']['leverage']

    logger.info('🚀 APEX Futures v1 — ETH + SOL + AAVE Perpetuals')
    logger.info(f'   Trade: ${amt:.2f} @ {lev:.0f}x ISOLATED | API: {mask(BINANCE_API_KEY)}')
    logger.info(f'   Mode: best-signal selector (1 trade at a time)')

    send_telegram(
        f'🚀 <b>APEX Futures Started — ETH + SOL + AAVE Perps</b>\n\n'
        f'📌 ETH/USDT + SOL/USDT + AAVE/USDT: ${amt:.2f} @ {lev:.0f}x (isolated)\n'
        f'🏆 Best-signal selector — trades highest confidence setup\n'
        f'🎯 ADX Regime + EMA21 Pullback (1H)\n'
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
        for _ in range(CHECK_INTERVAL // 5):
            time.sleep(5)
            try:
                quick_cfg = fetch_dashboard_config()
                if quick_cfg.get('close_requested') or quick_cfg.get('force_trail'):
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
                    # Mid-sleep SL check
                    if pos and ss.get('trail_entry_price') and ss.get('trail_atr'):
                        _hit, _reason = check_sl_trail(sym, pos, _price)
                        if _hit:
                            logger.info(f'⚡ [{sym}] Trail/SL hit mid-sleep — waking up')
                            wake = True
                if wake:
                    break
            except Exception:
                pass


if __name__ == '__main__':
    main()
