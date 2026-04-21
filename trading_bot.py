#!/usr/bin/env python3
"""
APEX v2 — Binance Cross-Margin Bot
Symbol: BTC/USDT
Strategy: ADX Regime Filter + EMA21 Pullback (1H timeframe)
Both LONG and SHORT enabled.

Entry logic:
  LONG:  ADX >= 25, +DI > -DI, EMA21 > EMA50,
         price pulls back within 1.8% above EMA21, candle low touched EMA21, RSI 30-62
  SHORT: ADX >= 25, -DI > +DI, EMA21 < EMA50,
         price bounces within 1.8% below EMA21, candle high touched EMA21, RSI 38-70
  HOLD:  ADX < 25 (choppy), mixed DI/EMA, price not at EMA21 yet
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

# ── Environment ───────────────────────────────────────────────────────────────
MST                = timezone(timedelta(hours=-7))
BINANCE_BASE_URL   = 'https://api.binance.com'
BINANCE_API_KEY    = os.environ.get('BINANCE_API_KEY', '').strip()
BINANCE_API_SECRET = os.environ.get('BINANCE_API_SECRET', '').strip()
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
TELEGRAM_CHAT_ID   = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
GH_TOKEN            = os.environ.get('GH_TOKEN', '').strip()
LUNARCRUSH_API_KEY  = os.environ.get('LUNARCRUSH_API_KEY', '').strip()
DASHBOARD_REPO      = os.environ.get('DASHBOARD_REPO', 'avinashpathrol/apex-dashboard').strip()
BOT_CONFIG_FILE    = os.environ.get('BOT_CONFIG_FILE', 'bot_config.json').strip()
BOT_STATE_FILE     = os.environ.get('BOT_STATE_FILE', 'bot_state.json').strip()

DEFAULT_TRADE_AMOUNT_USDT = float(os.environ.get('TRADE_AMOUNT_USDT', '40'))
DEFAULT_LEVERAGE          = float(os.environ.get('LEVERAGE', '5'))
CHECK_INTERVAL            = int(os.environ.get('CHECK_INTERVAL', '60'))
ALLOWED_LEVERAGES         = {3.0, 4.0, 5.0}

# ── Symbol Config ─────────────────────────────────────────────────────────────
SYMBOLS_CONFIG = {
    'BTCUSDT': {'base': 'BTC', 'dashboard_file': 'data_binance_btc.json', 'min_atr': 200.0},
}
TRADING_SYMBOLS = list(SYMBOLS_CONFIG.keys())

# ── Strategy Parameters ───────────────────────────────────────────────────────
ADX_MIN           = 25.0
ADX_STRONG        = 30.0
PULLBACK_ZONE_PCT = 0.018   # within 1.8% of EMA21
RSI_LONG_MIN      = 30
RSI_LONG_MAX      = 62
RSI_SHORT_MIN     = 38
RSI_SHORT_MAX     = 70

# ── Trail Parameters ──────────────────────────────────────────────────────────
TRAIL_ACTIVATE_ATR = 0.75
HARD_SL_ATR        = 1.50
FEE_RATE           = 0.00075   # 0.075% BNB discount

# ── Per-symbol state (keyed by symbol string) ─────────────────────────────────
# Each entry holds everything needed for one independent symbol position.
def _empty_sym_state() -> dict:
    return {
        'position':               None,   # 'LONG' | 'SHORT' | None
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
        'closed_trades_log':      [],     # authoritative trade log for this symbol
    }

# Top-level state file structure
state = {
    'symbols': {
        'BTCUSDT': _empty_sym_state(),
    },
    'runtime': {
        'trade_amount_usdt': DEFAULT_TRADE_AMOUNT_USDT,
        'leverage':          DEFAULT_LEVERAGE,
        'source':            'env-defaults',
    },
}

run_count       = 0
last_hold_alert: dict = {}   # per-symbol last hold alert ts


# ── Utilities ─────────────────────────────────────────────────────────────────
def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def safe_float(v, default=0.0) -> float:
    try: return float(v)
    except Exception: return default

def safe_int(v, default=0) -> int:
    try: return int(v)
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
    if not isinstance(loaded, dict):
        return
    # Support both old flat format and new nested format
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
    r = (requests.get if method.upper() == 'GET' else requests.post)(
        url, params=params, headers=headers, timeout=15
    )
    if not r.ok:
        logger.error(f'BINANCE {method} {endpoint} | {r.status_code} | {r.text}')
    r.raise_for_status()
    return r.json()

def get_margin_balance(asset: str) -> dict:
    account = binance_private('GET', '/sapi/v1/margin/account')
    for a in account.get('userAssets', []):
        if a['asset'] == asset:
            return {
                'free':     float(a['free']),
                'borrowed': float(a['borrowed']),
                'interest': float(a['interest']),
                'net':      float(a['netAsset']),
                'locked':   float(a['locked']),
            }
    return {'free': 0.0, 'borrowed': 0.0, 'interest': 0.0, 'net': 0.0, 'locked': 0.0}

def get_margin_level() -> float:
    try: return float(binance_private('GET', '/sapi/v1/margin/account').get('marginLevel', 999))
    except Exception: return 999.0

def borrow_margin(asset: str, amount: float) -> bool:
    try:
        binance_private('POST', '/sapi/v1/margin/loan', {'asset': asset, 'amount': str(round(amount, 8))})
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
        binance_private('POST', '/sapi/v1/margin/repay', {'asset': asset, 'amount': str(round(amount, 8))})
        logger.info(f'💸 Repaid {amount:.6f} {asset}')
        return True
    except Exception as e:
        logger.error(f'Repay {asset} failed: {e}')
        return False

def margin_order(symbol: str, side: str, quantity: float, side_effect: str = 'NO_SIDE_EFFECT') -> dict:
    return binance_private('POST', '/sapi/v1/margin/order', {
        'symbol': symbol, 'side': side, 'type': 'MARKET',
        'quantity': str(quantity), 'sideEffectType': side_effect,
    })

def get_step_size(symbol: str) -> float:
    info = binance_public('/api/v3/exchangeInfo', {'symbol': symbol})
    for s in info.get('symbols', []):
        if s['symbol'] == symbol:
            for f in s.get('filters', []):
                if f['filterType'] == 'LOT_SIZE':
                    return float(f['stepSize'])
    return 0.01

def round_step(qty: float, step: float) -> float:
    precision = len(str(step).rstrip('0').split('.')[-1])
    return round(qty - (qty % step), precision)

def extract_fill_data(resp: dict) -> dict:
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
                total_fee += qty * price * FEE_RATE
        else:
            total_fee += fee * price
    avg_price = total_cost / total_qty if total_qty > 0 else 0.0
    return {'avg_price': avg_price, 'qty': total_qty, 'fee_usdt': total_fee}

def detect_position(symbol: str) -> Optional[str]:
    base = SYMBOLS_CONFIG[symbol]['base']
    asset = get_margin_balance(base)
    step  = get_step_size(symbol)
    min_qty = max(step * 2, step)
    if asset['net'] > min_qty:
        return 'LONG'
    if (asset['borrowed'] + asset['interest']) > min_qty:
        return 'SHORT'
    return None

def get_current_price(symbol: str) -> float:
    return float(binance_public('/api/v3/ticker/price', {'symbol': symbol})['price'])

def cleanup_orphan_borrows(symbol: str) -> None:
    base = SYMBOLS_CONFIG[symbol]['base']
    try:
        step  = get_step_size(symbol)
        asset = get_margin_balance(base)
        borrowed = asset['borrowed'] + asset['interest']
        if borrowed > step and asset['free'] > step:
            repay_margin(base, min(asset['free'], borrowed))
    except Exception as e:
        logger.warning(f'cleanup {base} borrow: {e}')
    try:
        pos = detect_position(symbol)
        if pos != 'LONG':
            usdt = get_margin_balance('USDT')
            if (usdt['borrowed'] + usdt['interest']) > 0.01 and usdt['free'] > 0.01:
                repay_margin('USDT', min(usdt['free'], usdt['borrowed'] + usdt['interest']))
    except Exception as e:
        logger.warning(f'cleanup USDT borrow ({symbol}): {e}')


# ── LunarCrush Sentiment ─────────────────────────────────────────────────────
_sentiment_cache: dict = {'value': None, 'ts': 0}
SENTIMENT_CACHE_TTL = 600  # 10 minutes

def get_btc_sentiment() -> Optional[float]:
    """Fetch BTC social sentiment from LunarCrush (0-100). Returns None on failure or no key."""
    if not LUNARCRUSH_API_KEY:
        return None
    now = time.time()
    if _sentiment_cache['value'] is not None and (now - _sentiment_cache['ts']) < SENTIMENT_CACHE_TTL:
        return _sentiment_cache['value']
    try:
        r = requests.get(
            'https://lunarcrush.com/api4/public/topic/bitcoin/v1',
            headers={'Authorization': f'Bearer {LUNARCRUSH_API_KEY}'},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        sentiment = float((data.get('data') or {}).get('sentiment') or 0)
        _sentiment_cache['value'] = sentiment
        _sentiment_cache['ts'] = now
        logger.info(f'🌙 LunarCrush BTC sentiment: {sentiment:.0f}%')
        return sentiment
    except Exception as e:
        logger.warning(f'LunarCrush sentiment fetch failed: {e}')
        return None


# ── Market Data & Signal ──────────────────────────────────────────────────────
def get_market_data(symbol: str) -> pd.DataFrame:
    klines = binance_public('/api/v3/klines', {'symbol': symbol, 'interval': '1h', 'limit': 100})
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
        'ema21':   round(ema21, 6),
        'ema50':   round(ema50, 6),
        'rsi':     round(rsi, 2),
        'atr':     round(atr, 6),
    }

    def hold(reason, trend='NEUTRAL'):
        return {'action': 'HOLD', 'confidence': 0, 'regime': 'CHOPPY' if adx < ADX_MIN else 'TRENDING',
                'trend_direction': trend, 'reason': reason, 'indicators': snap}

    if adx < ADX_MIN:
        return hold(f'ADX {adx:.1f} < {ADX_MIN} — market choppy, no entry')

    ss = sym_state(symbol)
    last_closed_ts = ss.get('last_bot_closed_ts', 0)
    if last_closed_ts and (time.time() - last_closed_ts) < 3600:
        mins_left = int((3600 - (time.time() - last_closed_ts)) / 60) + 1
        return hold(f'Post-trade cooldown — {mins_left}m remaining before next entry')

    if atr < min_atr:
        return hold(f'ATR {atr:.6f} too low (min {min_atr}) — not enough movement to cover fees')

    di_bullish  = adx_pos > adx_neg
    ema_bullish = ema21 > ema50

    if di_bullish and ema_bullish:
        trend = 'BULLISH'
    elif not di_bullish and not ema_bullish:
        trend = 'BEARISH'
    else:
        return hold(
            f'ADX {adx:.1f} trending but +DI/{adx_pos:.1f} vs -DI/{adx_neg:.1f} '
            f'conflicts with EMA21/EMA50 — waiting for alignment', 'MIXED'
        )

    if trend == 'BULLISH':
        dist_pct      = (price - ema21) / ema21
        in_zone       = 0 <= dist_pct <= PULLBACK_ZONE_PCT
        candle_dipped = lo <= ema21 * 1.008
        rsi_ok        = RSI_LONG_MIN <= rsi <= RSI_LONG_MAX
        if in_zone and candle_dipped and rsi_ok:
            sentiment = get_btc_sentiment()
            if sentiment is not None and sentiment < 50:
                return hold(f'BULLISH setup blocked — social sentiment {sentiment:.0f}% bearish', trend)
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
                           f'dist {dist_pct*100:.2f}% above EMA21 | +DI {adx_pos:.1f} > -DI {adx_neg:.1f}'),
                'indicators': snap,
            }
        parts = []
        if not in_zone:
            parts.append(f'price {dist_pct*100:.2f}% from EMA21 (need 0–1.8% above)')
        if not candle_dipped:
            parts.append(f'candle low ${lo:.5f} not near EMA21 ${ema21:.5f}')
        if not rsi_ok:
            parts.append(f'RSI {rsi:.1f} outside [{RSI_LONG_MIN}–{RSI_LONG_MAX}]')
        return hold(f'BULLISH — waiting for EMA21 pullback: {", ".join(parts)}', trend)

    if trend == 'BEARISH':
        dist_pct      = (ema21 - price) / ema21
        in_zone       = 0 <= dist_pct <= PULLBACK_ZONE_PCT
        candle_tapped = hi >= ema21 * 0.992
        rsi_ok        = RSI_SHORT_MIN <= rsi <= RSI_SHORT_MAX
        if in_zone and candle_tapped and rsi_ok:
            sentiment = get_btc_sentiment()
            if sentiment is not None and sentiment > 50:
                return hold(f'BEARISH setup blocked — social sentiment {sentiment:.0f}% bullish', trend)
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
                           f'dist {dist_pct*100:.2f}% below EMA21 | -DI {adx_neg:.1f} > +DI {adx_pos:.1f}'),
                'indicators': snap,
            }
        parts = []
        if not in_zone:
            parts.append(f'price {dist_pct*100:.2f}% from EMA21 (need 0–1.8% below)')
        if not candle_tapped:
            parts.append(f'candle high ${hi:.5f} not near EMA21 ${ema21:.5f}')
        if not rsi_ok:
            parts.append(f'RSI {rsi:.1f} outside [{RSI_SHORT_MIN}–{RSI_SHORT_MAX}]')
        return hold(f'BEARISH — waiting for EMA21 bounce: {", ".join(parts)}', trend)

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
        'entry_price': round(entry_price, 8),
        'exit_price':  round(exit_price, 8),
        'qty':         round(qty, 8),
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
    save_state()
    logger.info(f'📋 [{symbol}] Trade recorded | {side} | pnl={pnl:+.4f} USDT | win={pnl > 0}')

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
        klines = binance_public('/api/v3/klines', {'symbol': symbol, 'interval': '1h', 'limit': period + 1})
        ranges = [float(k[2]) - float(k[3]) for k in klines]
        return sum(ranges[-period:]) / period
    except Exception as e:
        logger.warning(f'get_atr_1h {symbol}: {e}')
        return 0.0

def init_trail(symbol: str, entry_price: float) -> None:
    ss  = sym_state(symbol)
    atr = get_atr_1h(symbol)
    ss['trail_entry_price'] = entry_price
    ss['trail_best_price']  = entry_price
    ss['trail_atr']         = atr
    save_state()
    logger.info(f'📐 [{symbol}] Trail init | entry={entry_price:.6f} ATR={atr:.6f} '
                f'hardSL±{atr*HARD_SL_ATR:.6f} | activates at {TRAIL_ACTIVATE_ATR}×ATR')

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
            return True, f'🛑 Hard SL hit | entry={entry:.6f} sl={sl:.6f} price={price:.6f}'

    if is_long and price > best:
        ss['trail_best_price'] = price; best = price; save_state()
    elif not is_long and price < best:
        ss['trail_best_price'] = price; best = price; save_state()

    profit_dist = (best - entry) if is_long else (entry - best)
    if profit_dist < activate_dist:
        logger.info(f'📐 [{symbol}] Trail not active | profit={profit_dist:.6f} < {activate_dist:.6f}')
        return False, ''

    dyn_dist   = max(atr * 0.5, profit_dist * 0.35)
    trail_stop = best - dyn_dist if is_long else best + dyn_dist
    locked_pct = round((profit_dist - dyn_dist) / profit_dist * 100) if profit_dist > 0 else 0
    if (is_long and price <= trail_stop) or (not is_long and price >= trail_stop):
        return True, (f'📐 Trail stop hit | best={best:.6f} stop={trail_stop:.6f} '
                      f'price={price:.6f} | locked {locked_pct:.0f}%')
    logger.info(f'📐 [{symbol}] Trail active | best={best:.6f} stop={trail_stop:.6f} locked={locked_pct:.0f}%')
    return False, ''

def build_trail_info(symbol: str, position: Optional[str]) -> dict:
    ss  = sym_state(symbol)
    ep  = ss.get('trail_entry_price')
    bp  = ss.get('trail_best_price')
    atr = ss.get('trail_atr')
    info = {'entry_price': ep, 'best_price': bp, 'atr': atr,
            'sl': None, 'trail_stop': None, 'active': False}
    if ep and atr:
        info['sl'] = round(ep - atr * HARD_SL_ATR, 8) if position == 'LONG' \
                else round(ep + atr * HARD_SL_ATR, 8)
    if ep and bp and atr:
        profit = (bp - ep) if position == 'LONG' else (ep - bp)
        if profit >= atr * TRAIL_ACTIVATE_ATR:
            dyn = max(atr * 0.5, profit * 0.35)
            info['trail_stop'] = round(bp - dyn, 8) if position == 'LONG' else round(bp + dyn, 8)
            info['active'] = True
    return info


# ── Trade Execution ───────────────────────────────────────────────────────────
def open_long(symbol: str, price: float, confidence: int, reason: str) -> bool:
    ss   = sym_state(symbol)
    base = SYMBOLS_CONFIG[symbol]['base']
    try:
        step       = get_step_size(symbol)
        usdt       = get_margin_balance('USDT')
        collateral = float(state['runtime']['trade_amount_usdt'])
        leverage   = float(state['runtime']['leverage'])
        gross_usdt = collateral * leverage
        borrow_amt = round(gross_usdt - collateral, 2)
        if usdt['free'] < collateral:
            logger.warning(f'[{symbol}] Insufficient USDT: need ${collateral:.2f} have ${usdt["free"]:.2f}')
            return False
        if borrow_amt > 0 and not borrow_margin('USDT', borrow_amt):
            return False
        time.sleep(1)
        quantity = round_step((gross_usdt * 0.995) / price, step)
        if quantity < step:
            return False
        resp         = margin_order(symbol, 'BUY', quantity, 'NO_SIDE_EFFECT')
        fill         = extract_fill_data(resp)
        actual_price = fill['avg_price'] or price
        ss['trade_opened_at']      = now_utc_iso()
        ss['entry_fee_usdt']       = fill['fee_usdt']
        ss['active_trade_amount']  = collateral
        ss['active_leverage']      = leverage
        ss['active_qty']           = fill['qty'] or quantity
        init_trail(symbol, actual_price)
        atr = safe_float(ss.get('trail_atr'), 0)
        logger.info(f'✅ [{symbol}] LONG OPEN {quantity:.4f} {base} @ ${actual_price:.6f} fee=${fill["fee_usdt"]:.4f}')
        send_telegram(
            f'🟢 <b>APEX — LONG {base}/USDT ({leverage:.0f}x)</b>\n\n'
            f'💰 Entry: ${actual_price:,.6f}\n'
            f'💵 Collateral: ${collateral:.2f} | Effective: ${gross_usdt:.2f}\n'
            f'🪙 Qty: {quantity:.4f} {base}\n'
            f'🛑 Hard SL: ${actual_price - atr*HARD_SL_ATR:,.6f}\n'
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
        step  = get_step_size(symbol)
        asset = get_margin_balance(base)
        sellable = asset['free'] if asset['free'] >= step else asset['locked']
        quantity = round_step(sellable, step)
        if quantity < step:
            logger.warning(f'[{symbol}] No {base} to sell')
            return False
        entry_price = safe_float(ss.get('trail_entry_price'), price)
        entry_fee   = safe_float(ss.get('entry_fee_usdt'), 0.0)
        resp         = margin_order(symbol, 'SELL', quantity, 'AUTO_REPAY')
        fill         = extract_fill_data(resp)
        actual_close = fill['avg_price'] or price
        total_fee    = round(entry_fee + fill['fee_usdt'], 6)
        ss['last_bot_closed_side'] = 'LONG'
        ss['last_bot_closed_ts']   = int(time.time())
        record_closed_trade(symbol, 'LONG', entry_price, actual_close, quantity, reason, total_fee)
        gross = (actual_close - entry_price) * quantity
        net   = gross - total_fee
        logger.info(f'✅ [{symbol}] LONG CLOSE {quantity:.4f} {base} @ ${actual_close:.6f} net={net:+.4f}')
        send_telegram(
            f'🔴 <b>APEX — LONG {base}/USDT CLOSED</b>\n\n'
            f'💰 Exit: ${actual_close:,.6f} | Entry: ${entry_price:,.6f}\n'
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
        step        = get_step_size(symbol)
        usdt        = get_margin_balance('USDT')
        collateral  = float(state['runtime']['trade_amount_usdt'])
        leverage    = float(state['runtime']['leverage'])
        gross_usdt  = collateral * leverage
        borrow_base = round_step((gross_usdt * 0.995) / price, step)
        if usdt['free'] < collateral:
            logger.warning(f'[{symbol}] Insufficient USDT: need ${collateral:.2f} have ${usdt["free"]:.2f}')
            return False
        if borrow_base < step or not borrow_margin(base, borrow_base):
            return False
        time.sleep(1)
        resp         = margin_order(symbol, 'SELL', borrow_base, 'NO_SIDE_EFFECT')
        fill         = extract_fill_data(resp)
        actual_price = fill['avg_price'] or price
        ss['trade_opened_at']     = now_utc_iso()
        ss['entry_fee_usdt']      = fill['fee_usdt']
        ss['active_trade_amount'] = collateral
        ss['active_leverage']     = leverage
        ss['active_qty']          = fill['qty'] or borrow_base
        init_trail(symbol, actual_price)
        atr = safe_float(ss.get('trail_atr'), 0)
        logger.info(f'✅ [{symbol}] SHORT OPEN {borrow_base:.4f} {base} @ ${actual_price:.6f} fee=${fill["fee_usdt"]:.4f}')
        send_telegram(
            f'🔴 <b>APEX — SHORT {base}/USDT ({leverage:.0f}x)</b>\n\n'
            f'💰 Entry: ${actual_price:,.6f}\n'
            f'💵 Collateral: ${collateral:.2f} | Effective: ${gross_usdt:.2f}\n'
            f'🪙 Qty: {borrow_base:.4f} {base}\n'
            f'🛑 Hard SL: ${actual_price + atr*HARD_SL_ATR:,.6f}\n'
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
        step           = get_step_size(symbol)
        asset          = get_margin_balance(base)
        borrowed_total = asset['borrowed'] + asset['interest']
        if borrowed_total < 0.0001:
            logger.info(f'[{symbol}] No borrowed {base} — short already closed')
            return False
        entry_price = safe_float(ss.get('trail_entry_price'), price)
        entry_fee   = safe_float(ss.get('entry_fee_usdt'), 0.0)
        quantity    = round_step(borrowed_total * 1.005, step)
        if quantity < borrowed_total:
            quantity = round_step(borrowed_total + step, step)
        resp         = margin_order(symbol, 'BUY', quantity, 'AUTO_REPAY')
        fill         = extract_fill_data(resp)
        actual_close = fill['avg_price'] or price
        total_fee    = round(entry_fee + fill['fee_usdt'], 6)
        ss['last_bot_closed_side'] = 'SHORT'
        ss['last_bot_closed_ts']   = int(time.time())
        record_closed_trade(symbol, 'SHORT', entry_price, actual_close, quantity, reason, total_fee)
        time.sleep(2)
        cleanup_orphan_borrows(symbol)
        after = get_margin_balance(base)
        if after['free'] >= step:
            try: margin_order(symbol, 'SELL', round_step(after['free'], step), 'AUTO_REPAY')
            except Exception as ex: logger.warning(f'Excess {base} sell failed: {ex}')
        gross = (entry_price - actual_close) * quantity
        net   = gross - total_fee
        logger.info(f'✅ [{symbol}] SHORT CLOSE {quantity:.4f} {base} @ ${actual_close:.6f} net={net:+.4f}')
        send_telegram(
            f'🟢 <b>APEX — SHORT {base}/USDT CLOSED</b>\n\n'
            f'💰 Exit: ${actual_close:,.6f} | Entry: ${entry_price:,.6f}\n'
            f'🪙 Qty: {quantity:.4f} {base}\n'
            f'✅ Gross: {gross:+.4f} USDT\n💸 Fees: -{total_fee:.4f} USDT\n'
            f'🏦 Net P&L: {net:+.4f} USDT\n📈 {reason}'
        )
        after_check = get_margin_balance(base)
        return (after_check['borrowed'] + after_check['interest']) < 0.01
    except Exception as e:
        logger.error(f'close_short [{symbol}] failed: {e}')
        alert_error(f'close_short {symbol}: {e}')
        return False


# ── Dashboard ─────────────────────────────────────────────────────────────────
def push_dashboard_data(data: dict, dashboard_file: str) -> None:
    if not GH_TOKEN or not DASHBOARD_REPO: return
    try:
        url     = f'https://api.github.com/repos/{DASHBOARD_REPO}/contents/{dashboard_file}'
        headers = {'Authorization': f'token {GH_TOKEN}', 'Accept': 'application/vnd.github+json'}
        content = base64.b64encode(json.dumps(data, indent=2).encode()).decode()
        r       = requests.get(url, headers=headers, timeout=10)
        sha     = r.json().get('sha') if r.status_code == 200 else None
        base_lbl = data.get('symbol', 'BOT')[:4]
        payload = {'message': f'bot: {base_lbl} {datetime.now(MST).strftime("%H:%M MST")}', 'content': content}
        if sha: payload['sha'] = sha
        r = requests.put(url, headers=headers, json=payload, timeout=15)
        if r.status_code in (200, 201):
            logger.info(f'✅ Dashboard updated ({dashboard_file})')
        else:
            logger.warning(f'Dashboard push failed: {r.status_code}')
    except Exception as e:
        logger.warning(f'Dashboard push error ({dashboard_file}): {e}')

def fetch_dashboard_config() -> dict:
    default = {
        'trade_amount_usdt': DEFAULT_TRADE_AMOUNT_USDT, 'leverage': DEFAULT_LEVERAGE,
        'close_requested': False, 'close_requested_at': None,
        'bot_paused': False, 'force_trail': False, 'force_trail_at': None,
        'close_symbol': None, 'updated_at': None,
    }
    if not GH_TOKEN or not DASHBOARD_REPO: return default
    try:
        url     = f'https://api.github.com/repos/{DASHBOARD_REPO}/contents/{BOT_CONFIG_FILE}'
        headers = {'Authorization': f'token {GH_TOKEN}', 'Accept': 'application/vnd.github+json'}
        r       = requests.get(url, headers=headers, timeout=10)
        if not r.ok: return default
        cfg = json.loads(base64.b64decode(r.json()['content'].replace('\n', '')).decode())
        amt = safe_float(cfg.get('trade_amount_usdt') or cfg.get('trade_amount'), DEFAULT_TRADE_AMOUNT_USDT)
        lev = safe_float(cfg.get('leverage'), DEFAULT_LEVERAGE)
        if lev not in ALLOWED_LEVERAGES: lev = DEFAULT_LEVERAGE
        if amt <= 0: amt = DEFAULT_TRADE_AMOUNT_USDT
        return {
            'trade_amount_usdt':  amt, 'leverage': lev,
            'close_requested':    bool(cfg.get('close_requested', False)),
            'close_requested_at': cfg.get('close_requested_at'),
            'close_symbol':       cfg.get('close_symbol'),   # which symbol to close
            'bot_paused':         bool(cfg.get('bot_paused', False)),
            'force_trail':        bool(cfg.get('force_trail', False)),
            'force_trail_at':     cfg.get('force_trail_at'),
            'force_trail_symbol': cfg.get('force_trail_symbol'),
            'updated_at':         cfg.get('updated_at'),
        }
    except Exception as e:
        logger.warning(f'Dashboard config read failed: {e}')
        return default

def apply_runtime_settings(cfg: dict) -> None:
    amt = cfg['trade_amount_usdt']
    lev = cfg['leverage']
    state['runtime'].update({'trade_amount_usdt': amt, 'leverage': lev, 'source': 'dashboard-config'})
    logger.info(f'⚙️ Runtime | ${amt:.2f} @ {lev:.0f}x')

def clear_flag(flag_name: str) -> None:
    if not GH_TOKEN or not DASHBOARD_REPO: return
    try:
        url     = f'https://api.github.com/repos/{DASHBOARD_REPO}/contents/{BOT_CONFIG_FILE}'
        headers = {'Authorization': f'token {GH_TOKEN}', 'Accept': 'application/vnd.github+json'}
        r       = requests.get(url, headers=headers, timeout=10)
        if not r.ok: return
        j   = r.json()
        cfg = json.loads(base64.b64decode(j['content'].replace('\n', '')).decode())
        cfg[flag_name] = False
        if flag_name == 'close_requested':
            cfg.pop('close_requested_at', None)
            cfg.pop('close_symbol', None)
        content = base64.b64encode(json.dumps(cfg, indent=2).encode()).decode()
        requests.put(url, headers=headers,
                     json={'message': f'bot: clear {flag_name}', 'content': content, 'sha': j.get('sha')},
                     timeout=15)
        logger.info(f'✅ Cleared flag: {flag_name}')
    except Exception as e:
        logger.warning(f'clear_flag({flag_name}) failed: {e}')


# ── Per-symbol cycle ──────────────────────────────────────────────────────────
def run_symbol(symbol: str, cfg: dict) -> dict:
    """Run one full cycle for a single symbol. Returns dashboard payload dict."""
    global last_hold_alert
    ss   = sym_state(symbol)
    base = SYMBOLS_CONFIG[symbol]['base']
    dash_file = SYMBOLS_CONFIG[symbol]['dashboard_file']

    try:
        position = detect_position(symbol)
        ss['position'] = position
        cleanup_orphan_borrows(symbol)

        df    = get_market_data(symbol)
        price = get_current_price(symbol)
        dec   = get_decision(symbol, df)
        action     = dec['action']
        confidence = dec['confidence']
        regime     = dec['regime']
        trend      = dec['trend_direction']
        reason     = dec['reason']
        indicators = dec['indicators']

        logger.info(f'💰 [{symbol}] ${price:.6f} | signal={action}({confidence}%) | pos={position} | {regime}/{trend}')
        logger.info(f'   ADX={indicators["adx"]:.1f} +DI={indicators["adx_pos"]:.1f} -DI={indicators["adx_neg"]:.1f} | '
                    f'EMA21={indicators["ema21"]:.5f} EMA50={indicators["ema50"]:.5f} | '
                    f'RSI={indicators["rsi"]:.1f} ATR={indicators["atr"]:.6f}')

        # ── Dashboard close request (per-symbol) ──────────────────────────────
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
                send_telegram(f'📱 <b>Dashboard Close</b>\nClosing {position} {base}/USDT @ ${price:,.6f}')
                closed = close_long(symbol, price, 'Dashboard close') if position == 'LONG' \
                    else close_short(symbol, price, 'Dashboard close')
                if closed:
                    dashboard_close_executed = True
                    clear_trail(symbol)
                    position = None
                    clear_flag('close_requested')

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
            send_telegram(f'🔒 <b>Force Trail {base}/USDT</b>\nPrice: ${price:,.6f}\nApprox stop: ${stop:,.6f}')

        # ── Trail sync ────────────────────────────────────────────────────────
        if position and ss.get('trail_entry_price') is None:
            init_trail(symbol, price)
        if not position and ss.get('trail_entry_price') is not None:
            clear_trail(symbol)

        # ── SL / Trail check ──────────────────────────────────────────────────
        sl_close = False
        status   = ''
        if position and not dashboard_close_executed:
            sl_hit, sl_reason = check_sl_trail(symbol, position, price)
            if sl_hit:
                send_telegram(f'🛑 <b>SL/Trail {base}/USDT</b>\n{sl_reason}\nClosing @ ${price:,.6f}')
                if position == 'LONG': close_long(symbol, price, sl_reason)
                else:                  close_short(symbol, price, sl_reason)
                sl_close = True
                clear_trail(symbol)
                position = detect_position(symbol)

        # ── Entry / management ────────────────────────────────────────────────
        bot_paused = cfg.get('bot_paused', False)
        if dashboard_close_executed or sl_close:
            action = 'HOLD'
            status = 'CLOSED ✅'
        elif not bot_paused:
            if position is None and action in ('LONG', 'SHORT'):
                ml = get_margin_level()
                if ml < 1.5 and ml != 999:
                    status = f'BLOCKED — margin {ml:.2f} too low'
                elif action == 'LONG':
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
                            send_telegram(f'⚠️ <b>Signal Fading — LONG {base}/USDT</b>\n${price:.6f}\n{reason}')
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
                            send_telegram(f'⚠️ <b>Signal Fading — SHORT {base}/USDT</b>\n${price:.6f}\n{reason}')
                            last_hold_alert[symbol] = now_ts

        if bot_paused and not status:
            status = '⏸️ BOT PAUSED'
        status = status or f'HOLD — {reason}'

        # ── Final state ───────────────────────────────────────────────────────
        latest_pos = detect_position(symbol)
        ss['position'] = latest_pos
        save_state()

        closed_trades = ss.get('closed_trades_log') or []
        performance   = summarize_performance(closed_trades)
        trail_info    = build_trail_info(symbol, latest_pos)

        usdt_balance = safe_float(get_margin_balance('USDT').get('net'), 0)

        payload = {
            'generated_at':    now_utc_iso(),
            'symbol':          symbol,
            'exchange':        'Binance Margin',
            'price':           price,
            'usdt_balance':    usdt_balance,
            'leverage':        state['runtime']['leverage'],
            'trade_amount':    state['runtime']['trade_amount_usdt'],
            'position':        latest_pos,
            'active_qty':      ss.get('active_qty'),
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
    logger.info(f'  APEX v2 — Run #{run_count}')
    logger.info('=' * 60)
    try:
        cfg = fetch_dashboard_config()
        apply_runtime_settings(cfg)
        for symbol in TRADING_SYMBOLS:
            run_symbol(symbol, cfg)
        # Clear force_trail flag after all symbols processed
        if cfg.get('force_trail'):
            clear_flag('force_trail')
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

    logger.info('🚀 APEX v2 — BTC/USDT')
    logger.info('   Strategy: ADX Regime + EMA21 Pullback (1H) | Long + Short')
    logger.info(f'  ${amt:.2f} @ {lev:.0f}x | API Key: {mask(BINANCE_API_KEY)} | GH: {mask(GH_TOKEN)}')

    send_telegram(
        f'🚀 <b>APEX v2 Started — BTC/USDT</b>\n\n'
        f'📌 BTC/USDT Cross-Margin\n'
        f'🎯 ADX Regime + EMA21 Pullback (1H)\n'
        f'↕️ Long + Short enabled\n'
        f'💰 ${amt:.2f} @ {lev:.0f}x per trade\n'
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
                # Mid-sleep SL check for both symbols
                for sym in TRADING_SYMBOLS:
                    ss  = sym_state(sym)
                    pos = ss.get('position')
                    if pos and ss.get('trail_entry_price') and ss.get('trail_atr'):
                        _price = get_current_price(sym)
                        _hit, _reason = check_sl_trail(sym, pos, _price)
                        if _hit:
                            logger.info(f'⚡ [{sym}] Trail/SL hit mid-sleep — waking up')
                            break
            except Exception:
                pass


if __name__ == '__main__':
    main()
