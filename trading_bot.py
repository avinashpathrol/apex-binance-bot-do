#!/usr/bin/env python3
"""
APEX Binance Margin SOL Bot — Long & Short Trading

Updates in this version
- Same-side cooldown after a MANUAL close (default 10 min)
- Dashboard-controlled trade size and leverage
- Settings are pulled from bot_config.json in the dashboard repo
- Trade settings are locked for the current open trade and apply new values only to the next trade
- Low-margin Telegram alerts are throttled
- Short-close repayment is more robust and attempts orphaned SOL cleanup
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
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)
log_file = os.path.join(LOG_DIR, "apex_bot.log")

logger = logging.getLogger("apex")
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')

file_handler = logging.FileHandler(log_file, encoding='utf-8')
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(formatter)
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(formatter)
if not logger.handlers:
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

MST = timezone(timedelta(hours=-7))

BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY', '').strip()
BINANCE_API_SECRET = os.environ.get('BINANCE_API_SECRET', '').strip()
SYMBOL = os.environ.get('TRADING_SYMBOL', 'SOLUSDT')  # active symbol, updated each cycle
BASE_ASSET = 'SOL'   # active base asset, updated each cycle
QUOTE_ASSET = 'USDT'

# ── Multi-symbol config ──
SYMBOLS_CONFIG = {
    'SOLUSDT':  {'base': 'SOL',  'dashboard_file': 'data_binance_sol.json',  'min_atr': 0.22},
    'DOGEUSDT': {'base': 'DOGE', 'dashboard_file': 'data_binance_doge.json', 'min_atr': 0.00025},
}
TRADING_SYMBOLS = list(SYMBOLS_CONFIG.keys())
DEFAULT_TRADE_AMOUNT_USDT = float(os.environ.get('TRADE_AMOUNT_USDT', '80'))
DEFAULT_LEVERAGE = float(os.environ.get('LEVERAGE', '4'))
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
GH_TOKEN = os.environ.get('GH_TOKEN', '').strip()
DASHBOARD_REPO = os.environ.get('DASHBOARD_REPO', 'avinashpathrol/apex-dashboard').strip()
MIN_SIGNALS = int(os.environ.get('MIN_SIGNALS', '3'))
CHECK_INTERVAL = int(os.environ.get('CHECK_INTERVAL', '60'))
BINANCE_BASE_URL = 'https://api.binance.com'
GROQ_API_KEY = os.environ.get('GROK', '').strip()
MANUAL_TRADES_FILE = os.environ.get('MANUAL_TRADES_FILE', 'manual_closed_trades.json').strip()
BOT_CONFIG_FILE = os.environ.get('BOT_CONFIG_FILE', 'bot_config.json').strip()
BOT_STATE_FILE = os.environ.get('BOT_STATE_FILE', 'bot_state.json').strip()

LOW_MARGIN_THRESHOLD = float(os.environ.get('LOW_MARGIN_THRESHOLD', '1.50'))
LOW_MARGIN_ALERT_COOLDOWN = int(os.environ.get('LOW_MARGIN_ALERT_COOLDOWN', '1800'))
LOW_MARGIN_ALERT_DELTA = float(os.environ.get('LOW_MARGIN_ALERT_DELTA', '0.05'))
SHORT_CLOSE_BUFFER = float(os.environ.get('SHORT_CLOSE_BUFFER', '1.005'))
MANUAL_REENTRY_COOLDOWN_MIN = int(os.environ.get('MANUAL_REENTRY_COOLDOWN_MIN', '10'))
ALLOWED_LEVERAGES = {3.0, 4.0, 5.0}

# ── Trailing stop tuning ──
TRAIL_ACTIVATE_ATR  = 0.75  # price must move this many × ATR in profit before trail activates
TRAIL_DISTANCE_ATR  = 1.4   # trail follows best price, staying this many × ATR behind it
HARD_SL_ATR         = 1.0   # hard stop loss distance from entry (before trail activates)

run_count = 0
last_hold_alert = 0
last_force_trail_ts = 0
current_position = None
last_ai_analysis = {}
last_margin_alert_ts = 0.0
last_margin_alert_level = None

runtime_settings = {
    'trade_amount_usdt': DEFAULT_TRADE_AMOUNT_USDT,
    'leverage': DEFAULT_LEVERAGE,
    'source': 'env-defaults',
}

state = {
    'prev_position': None,
    'active_trade_amount_usdt': None,
    'active_trade_leverage': None,
    'cooldown_side': None,
    'cooldown_until': 0,
    'cooldown_minutes': MANUAL_REENTRY_COOLDOWN_MIN,
    'last_bot_closed_side': None,
    'last_bot_closed_ts': 0,
    'last_manual_close_ts': 0,
    'trail_entry_price': None,
    'trail_best_price': None,
    'trail_atr': None,
    'flip_pending': None,   # 'LONG' or 'SHORT' — open this side next run after a flip close
    'close_log': [],        # list of {ts_iso, reason} for every bot-initiated close
    'force_trail_processed': False,  # True while force_trail flag is active, prevents duplicate fires
    'active_symbol': None,           # which symbol is currently being traded
    'consecutive_losses': {},         # per-symbol count of losses in a row e.g. {'SOLUSDT': 2}
    'loss_cooldown_until': {},        # per-symbol epoch time e.g. {'SOLUSDT': 1234567890}
}


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def mask_secret(value: str, start: int = 6, end: int = 4) -> str:
    if not value:
        return 'MISSING'
    if len(value) <= start + end:
        return '*' * len(value)
    return f'{value[:start]}...{value[-end:]}'


def safe_float(value, default):
    try:
        return float(value)
    except Exception:
        return default


def safe_int(value, default):
    try:
        return int(value)
    except Exception:
        return default


def read_json_file(path: str, default):
    try:
        if not os.path.exists(path):
            return default
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f'Failed to read {path}: {e}')
        return default


def write_json_file(path: str, data) -> None:
    tmp = f'{path}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load_state() -> None:
    global state
    loaded = read_json_file(BOT_STATE_FILE, None)
    if isinstance(loaded, dict):
        state.update(loaded)


def save_state() -> None:
    write_json_file(BOT_STATE_FILE, state)


def get_public_ip() -> str:
    for service in ['https://api.ipify.org', 'https://ifconfig.me/ip', 'https://icanhazip.com']:
        try:
            resp = requests.get(service, timeout=5)
            if resp.ok and resp.text.strip():
                return resp.text.strip()
        except Exception:
            pass
    return 'UNKNOWN'


def send_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
        requests.post(url, json={'chat_id': TELEGRAM_CHAT_ID, 'text': message, 'parse_mode': 'HTML'}, timeout=10)
        logger.info('📱 Telegram alert sent')
    except Exception as e:
        logger.warning(f'Telegram failed: {e}')


def alert_error(error: str) -> None:
    send_telegram(f'❌ <b>APEX MARGIN BOT ERROR</b>\n\n{error}\n\n⏰ {datetime.now(MST).strftime("%b %d, %I:%M %p MST")}')


def binance_signature(params: dict) -> str:
    query = urlencode(params)
    return hmac.new(BINANCE_API_SECRET.encode('utf-8'), query.encode('utf-8'), hashlib.sha256).hexdigest()


def binance_public(endpoint: str, params: dict = None) -> dict:
    resp = requests.get(f'{BINANCE_BASE_URL}{endpoint}', params=params or {}, timeout=10)
    if not resp.ok:
        logger.error(f'BINANCE PUBLIC FAILED | endpoint={endpoint} | status={resp.status_code} | body={resp.text}')
    resp.raise_for_status()
    return resp.json()


def binance_private(method: str, endpoint: str, params: dict = None) -> dict:
    params = params or {}
    params['timestamp'] = int(time.time() * 1000)
    params['signature'] = binance_signature(params)
    headers = {'X-MBX-APIKEY': BINANCE_API_KEY}
    url = f'{BINANCE_BASE_URL}{endpoint}'
    if method.upper() == 'GET':
        resp = requests.get(url, params=params, headers=headers, timeout=15)
    elif method.upper() == 'POST':
        resp = requests.post(url, params=params, headers=headers, timeout=15)
    else:
        raise ValueError(f'Unknown method: {method}')
    if not resp.ok:
        masked_params = {k: ('***' if k == 'signature' else v) for k, v in params.items()}
        logger.error(f'BINANCE PRIVATE FAILED | {method.upper()} {endpoint} | status={resp.status_code} | params={masked_params} | body={resp.text}')
    resp.raise_for_status()
    return resp.json()


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
    return float(binance_private('GET', '/sapi/v1/margin/account').get('marginLevel', 999))


def borrow_margin(asset: str, amount: float) -> bool:
    try:
        binance_private('POST', '/sapi/v1/margin/loan', {'asset': asset, 'amount': str(round(amount, 8))})
        logger.info(f'🏦 Borrowed {amount:.8f} {asset}')
        return True
    except requests.HTTPError as e:
        txt = getattr(e.response, 'text', '') or ''
        if '"code":-3045' in txt or '"code":-3035' in txt:
            logger.warning(f'⚠️ Borrow unavailable for {asset}: insufficient lending pool')
            return False
        logger.error(f'Borrow {asset} failed: {e}')
        return False
    except Exception as e:
        logger.error(f'Borrow {asset} failed: {e}')
        return False


def repay_margin(asset: str, amount: float) -> bool:
    try:
        binance_private('POST', '/sapi/v1/margin/repay', {'asset': asset, 'amount': str(round(amount, 8))})
        logger.info(f'💸 Repaid {amount:.8f} {asset}')
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


def round_step(quantity: float, step: float) -> float:
    precision = len(str(step).rstrip('0').split('.')[-1])
    return round(quantity - (quantity % step), precision)


def detect_position(base_asset: str = None) -> Optional[str]:
    b = base_asset or BASE_ASSET
    asset = get_margin_balance(b)
    usdt = get_margin_balance('USDT')
    logger.info(f'📊 {b} net={asset["net"]:.4f} borrowed={asset["borrowed"]:.4f} interest={asset["interest"]:.8f}')
    logger.info(f'📊 USDT net={usdt["net"]:.2f} borrowed={usdt["borrowed"]:.2f}')
    step = get_step_size()
    # Only count as LONG if we have at least 1 full tradeable unit (avoids dust confusion)
    if asset['net'] >= step and asset['net'] > 0.05:
        return 'LONG'
    if (asset['borrowed'] + asset['interest']) > 0.05:
        return 'SHORT'
    return None


def detect_active_symbol() -> Optional[str]:
    """Check all symbols for an open position. Returns the symbol that has one, or None."""
    for sym, cfg in SYMBOLS_CONFIG.items():
        asset = get_margin_balance(cfg['base'])
        if asset['net'] > 0.05:
            return sym
        if (asset['borrowed'] + asset['interest']) > 0.05:
            return sym
    return None


def get_last_trade_price(side: str) -> Optional[float]:
    try:
        trades = binance_private('GET', '/sapi/v1/margin/myTrades', {'symbol': SYMBOL, 'limit': 10})
        for t in reversed(trades):
            is_buy = t.get('isBuyer', False)
            if (side == 'BUY' and is_buy) or (side == 'SELL' and not is_buy):
                return float(t['price'])
    except Exception as e:
        logger.warning(f'get_last_trade_price failed: {e}')
    return None


def merge_close_reasons(trades: list) -> list:
    """Match close_log entries to SELL trades by nearest timestamp (within 30s)."""
    log = state.get('close_log') or []
    if not log:
        return trades
    from datetime import datetime
    def parse_ts(s):
        try:
            return datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()
        except Exception:
            return 0
    log_entries = [(parse_ts(e['ts']), e['reason']) for e in log]
    result = []
    for t in trades:
        if t.get('action') == 'SELL':
            t_ts = parse_ts(t.get('time', ''))
            best = min(log_entries, key=lambda e: abs(e[0] - t_ts), default=None)
            if best and abs(best[0] - t_ts) <= 30:
                t = {**t, 'reason': best[1]}
        result.append(t)
    return result


def get_trade_history(symbol: str = None) -> list:
    """Fetch trade history for a specific symbol, or active symbol if not specified."""
    sym = symbol or SYMBOL
    try:
        trades = binance_private('GET', '/sapi/v1/margin/myTrades', {'symbol': sym, 'limit': 1000})
        result = []
        for t in trades:
            result.append({
                'time': datetime.fromtimestamp(t['time']/1000, tz=MST).isoformat(),
                'action': 'BUY' if t['isBuyer'] else 'SELL',
                'price': float(t['price']),
                'qty': float(t['qty']),
                'cost': float(t['quoteQty']),
                'fee': float(t['commission']),
                'symbol': sym,
            })
        return sorted(result, key=lambda x: x['time'])
    except Exception as e:
        logger.warning(f'Trade history failed for {sym}: {e}')
        return []


def cleanup_orphan_short_borrow() -> bool:
    try:
        asset = get_margin_balance(BASE_ASSET)
        total_borrowed = asset['borrowed'] + asset['interest']
        free_asset = asset['free']
        if total_borrowed < 0.0001:
            return False
        repayable = min(free_asset, total_borrowed)
        if repayable < 0.0001:
            logger.info(f'🧹 Orphan short borrow detected but no free {BASE_ASSET} available to repay yet')
            return False
        ok = repay_margin(BASE_ASSET, repayable)
        if ok:
            time.sleep(1)
            after = get_margin_balance(BASE_ASSET)
            logger.info(f'🧹 Orphan {BASE_ASSET} repay cleanup done | remaining borrowed={after["borrowed"] + after["interest"]:.8f}')
        return ok
    except Exception as e:
        logger.warning(f'cleanup_orphan_short_borrow failed: {e}')
        return False


def cleanup_orphan_long_borrow() -> bool:
    try:
        sol = get_margin_balance(BASE_ASSET)
        if sol['net'] > 0.05:
            return False  # LONG position is open, USDT borrow is intentional
        usdt = get_margin_balance('USDT')
        total_borrowed = usdt['borrowed'] + usdt['interest']
        free_usdt = usdt['free']
        if total_borrowed < 0.01:
            return False
        repayable = min(free_usdt, total_borrowed)
        if repayable < 0.01:
            logger.info('🧹 Orphan USDT borrow detected but no free USDT available to repay yet')
            return False
        ok = repay_margin('USDT', repayable)
        if ok:
            time.sleep(1)
            after = get_margin_balance('USDT')
            logger.info(f'🧹 Orphan USDT repay cleanup done | remaining borrowed={after["borrowed"] + after["interest"]:.2f}')
        return ok
    except Exception as e:
        logger.warning(f'cleanup_orphan_long_borrow failed: {e}')
        return False


def load_manual_closed_trades() -> list:
    try:
        if not os.path.exists(MANUAL_TRADES_FILE):
            return []
        data = read_json_file(MANUAL_TRADES_FILE, [])
        if not isinstance(data, list):
            return []
        cleaned = []
        for t in data:
            side = str(t.get('side', '')).upper().strip()
            entry_price = safe_float(t.get('entry_price', 0), 0)
            exit_price = safe_float(t.get('exit_price', 0), 0)
            qty = safe_float(t.get('qty', 0), 0)
            fee = safe_float(t.get('fee', 0), 0)
            if side not in ('LONG', 'SHORT') or entry_price <= 0 or exit_price <= 0 or qty <= 0:
                continue
            lev = runtime_settings['leverage']
            pnl = (exit_price - entry_price) * qty - fee if side == 'LONG' else (entry_price - exit_price) * qty - fee
            pnl_pct = (((exit_price - entry_price) / entry_price) * 100 * lev) if side == 'LONG' else (((entry_price - exit_price) / entry_price) * 100 * lev)
            cleaned.append({
                'source': 'manual', 'side': side, 'entry_price': round(entry_price, 8), 'exit_price': round(exit_price, 8),
                'qty': round(qty, 8), 'fee': round(fee, 8), 'pnl': round(pnl, 4), 'pnl_pct': round(pnl_pct, 2),
                'win': pnl > 0, 'opened_at': t.get('opened_at'), 'closed_at': t.get('closed_at'), 'note': str(t.get('note', 'Manual trade')),
            })
        return cleaned
    except Exception as e:
        logger.warning(f'Failed loading manual closed trades: {e}')
        return []


def build_closed_trades_from_history(trades: list) -> list:
    closed = []
    current_trade = None
    lev = runtime_settings['leverage']
    for t in trades:
        action = t.get('action')
        price = float(t.get('price', 0))
        qty = float(t.get('qty', 0))
        fee = float(t.get('fee', 0))
        ts = t.get('time')
        if action == 'BUY':
            if current_trade is None:
                current_trade = {'side': 'LONG', 'entry_price': price, 'qty': qty, 'entry_fee_usdt': price * qty * 0.001, 'opened_at': ts}
            elif current_trade['side'] == 'SHORT':
                exit_qty = min(current_trade['qty'], qty)
                total_fee = current_trade.get('entry_fee_usdt', 0.0) + price * qty * 0.001
                pnl = (current_trade['entry_price'] - price) * exit_qty - total_fee
                closed.append({'source': 'binance', 'side': 'SHORT', 'entry_price': round(current_trade['entry_price'], 8), 'exit_price': round(price, 8), 'qty': round(exit_qty, 8), 'fee': round(total_fee, 8), 'pnl': round(pnl, 4), 'pnl_pct': round(((current_trade['entry_price'] - price) / current_trade['entry_price']) * 100 * lev, 2), 'win': pnl > 0, 'opened_at': current_trade['opened_at'], 'closed_at': ts, 'note': 'Derived from margin trade history'})
                current_trade = None
        elif action == 'SELL':
            if current_trade is None:
                current_trade = {'side': 'SHORT', 'entry_price': price, 'qty': qty, 'entry_fee_usdt': price * qty * 0.001, 'opened_at': ts}
            elif current_trade['side'] == 'LONG':
                exit_qty = min(current_trade['qty'], qty)
                total_fee = current_trade.get('entry_fee_usdt', 0.0) + price * qty * 0.001
                pnl = (price - current_trade['entry_price']) * exit_qty - total_fee
                closed.append({'source': 'binance', 'side': 'LONG', 'entry_price': round(current_trade['entry_price'], 8), 'exit_price': round(price, 8), 'qty': round(exit_qty, 8), 'fee': round(total_fee, 8), 'pnl': round(pnl, 4), 'pnl_pct': round(((price - current_trade['entry_price']) / current_trade['entry_price']) * 100 * lev, 2), 'win': pnl > 0, 'opened_at': current_trade['opened_at'], 'closed_at': ts, 'note': 'Derived from margin trade history'})
                current_trade = None
    return closed


def summarize_performance(closed_trades: list) -> dict:
    total = len(closed_trades)
    wins = sum(1 for t in closed_trades if t.get('win'))
    net_profit = round(sum(float(t.get('pnl', 0)) for t in closed_trades), 4)
    return {
        'closed_trades': total,
        'wins': wins,
        'losses': total - wins,
        'win_rate': round((wins / total) * 100, 2) if total else 0.0,
        'net_profit': net_profit,
        'avg_profit_per_trade': round(net_profit / total, 4) if total else 0.0,
        'long_trades': sum(1 for t in closed_trades if t.get('side') == 'LONG'),
        'short_trades': sum(1 for t in closed_trades if t.get('side') == 'SHORT'),
    }


def get_market_data(symbol: str, interval: str = '15m', limit: int = 100) -> pd.DataFrame:
    klines = binance_public('/api/v3/klines', {'symbol': symbol, 'interval': interval, 'limit': limit})
    df = pd.DataFrame(klines, columns=['time','open','high','low','close','volume','close_time','quote_volume','trades','taker_base','taker_quote','ignore'])
    df = df.astype({'open': float, 'high': float, 'low': float, 'close': float, 'volume': float})
    df['rsi'] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
    macd = ta.trend.MACD(df['close'])
    df['macd'] = macd.macd(); df['macd_sig'] = macd.macd_signal(); df['macd_hist'] = macd.macd_diff()
    df['ema_20'] = ta.trend.EMAIndicator(df['close'], window=20).ema_indicator()
    df['ema_50'] = ta.trend.EMAIndicator(df['close'], window=50).ema_indicator()
    bb = ta.volatility.BollingerBands(df['close'], window=20, window_dev=2)
    df['bb_upper'] = bb.bollinger_hband(); df['bb_lower'] = bb.bollinger_lband()
    return df


def get_trend_filter(symbol: str) -> str:
    try:
        klines = binance_public('/api/v3/klines', {'symbol': symbol, 'interval': '1h', 'limit': 60})
        closes = [float(k[4]) for k in klines]
        df = pd.DataFrame({'close': closes})
        df['ema_15'] = ta.trend.EMAIndicator(df['close'], window=15).ema_indicator()
        df['ema_40'] = ta.trend.EMAIndicator(df['close'], window=40).ema_indicator()
        price = df['close'].iloc[-1]; e15 = df['ema_15'].iloc[-1]; e40 = df['ema_40'].iloc[-1]
        if price > e15 > e40:
            return 'BULLISH'
        if price < e15 < e40:
            return 'BEARISH'
        return 'SIDEWAYS'
    except Exception as e:
        logger.warning(f'Trend filter failed: {e}')
        return 'NEUTRAL'


def get_current_price(symbol: str) -> float:
    return float(binance_public('/api/v3/ticker/price', {'symbol': symbol})['price'])


def get_decision(df: pd.DataFrame, price: float) -> dict:
    curr = df.iloc[-1]
    prev = df.iloc[-2]
    bullish_signals = []
    bearish_signals = []
    if curr['rsi'] < 35:
        bullish_signals.append(f"RSI oversold ({curr['rsi']:.1f})")
    elif curr['rsi'] < 50 and prev['rsi'] < curr['rsi']:
        bullish_signals.append(f"RSI rising from low ({curr['rsi']:.1f})")
    if curr['rsi'] > 65:
        bearish_signals.append(f"RSI overbought ({curr['rsi']:.1f})")
    elif curr['rsi'] > 50 and prev['rsi'] > curr['rsi']:
        bearish_signals.append(f"RSI falling from high ({curr['rsi']:.1f})")
    if prev['macd'] < prev['macd_sig'] and curr['macd'] > curr['macd_sig']:
        bullish_signals.append('MACD bullish crossover')
    elif curr['macd'] > curr['macd_sig'] and curr['macd_hist'] > 0:
        bullish_signals.append('MACD above signal (bullish)')
    if prev['macd'] > prev['macd_sig'] and curr['macd'] < curr['macd_sig']:
        bearish_signals.append('MACD bearish crossover')
    elif curr['macd'] < curr['macd_sig'] and curr['macd_hist'] < 0:
        bearish_signals.append('MACD below signal (bearish)')
    if curr['ema_20'] > curr['ema_50']:
        bullish_signals.append('EMA-20 above EMA-50 (uptrend)')
    else:
        bearish_signals.append('EMA-20 below EMA-50 (downtrend)')
    if price > curr['ema_20']:
        bullish_signals.append('Price above EMA-20')
    else:
        bearish_signals.append('Price below EMA-20')
    bb_range = curr['bb_upper'] - curr['bb_lower']
    if bb_range > 0:
        bb_pct = (price - curr['bb_lower']) / bb_range
        if bb_pct < 0.20:
            bullish_signals.append(f"Price near BB lower band ({bb_pct*100:.0f}%)")
        elif bb_pct > 0.80:
            bearish_signals.append(f"Price near BB upper band ({bb_pct*100:.0f}%)")
    avg_vol = df['volume'].tail(10).mean()
    if curr['volume'] > avg_vol * 1.3:
        if len(bullish_signals) > len(bearish_signals):
            bullish_signals.append('High volume confirms move')
        else:
            bearish_signals.append('High volume confirms move')
    bull_count = len(bullish_signals)
    bear_count = len(bearish_signals)
    total = bull_count + bear_count or 1
    trend_up = curr['ema_20'] > curr['ema_50']
    trend_down = curr['ema_20'] < curr['ema_50']
    if bull_count >= MIN_SIGNALS and bull_count > bear_count and trend_up:
        return {'action': 'LONG', 'confidence': int(min(95, 55 + (bull_count / total) * 45)), 'risk_level': 'LOW' if bull_count >= 4 else 'MEDIUM', 'reason': ' | '.join(bullish_signals), 'bullish_signals': bullish_signals, 'bearish_signals': bearish_signals}
    if bear_count >= MIN_SIGNALS and bear_count > bull_count and trend_down:
        return {'action': 'SHORT', 'confidence': int(min(95, 55 + (bear_count / total) * 45)), 'risk_level': 'LOW' if bear_count >= 4 else 'MEDIUM', 'reason': ' | '.join(bearish_signals), 'bullish_signals': bullish_signals, 'bearish_signals': bearish_signals}
    reason = f'Mixed signals — {bull_count} bullish, {bear_count} bearish'
    if bull_count >= MIN_SIGNALS and bull_count > bear_count and not trend_up:
        reason = f'Blocked LONG — trend filter active (EMA-20 <= EMA-50) | {bull_count} bullish, {bear_count} bearish'
    elif bear_count >= MIN_SIGNALS and bear_count > bull_count and not trend_down:
        reason = f'Blocked SHORT — trend filter active (EMA-20 >= EMA-50) | {bull_count} bullish, {bear_count} bearish'
    return {'action': 'HOLD', 'confidence': 0, 'risk_level': 'MEDIUM', 'reason': reason, 'bullish_signals': bullish_signals, 'bearish_signals': bearish_signals}


def fetch_dashboard_config() -> dict:
    default = {
        'trade_amount_usdt': DEFAULT_TRADE_AMOUNT_USDT,
        'leverage': DEFAULT_LEVERAGE,
        'manual_reentry_cooldown_minutes': MANUAL_REENTRY_COOLDOWN_MIN,
        'updated_at': None,
    }
    if not DASHBOARD_REPO:
        return default
    try:
        url = f'https://api.github.com/repos/{DASHBOARD_REPO}/contents/{BOT_CONFIG_FILE}'
        headers = {'Authorization': f'token {GH_TOKEN}', 'Accept': 'application/vnd.github+json'}
        resp = requests.get(url, headers=headers, timeout=10)
        if not resp.ok:
            return default
        import base64 as _b64
        cfg = json.loads(_b64.b64decode(resp.json()['content'].replace('\n', '')).decode())
        if not isinstance(cfg, dict):
            return default
        amt = safe_float(cfg.get('trade_amount_usdt') or cfg.get('trade_amount'), default['trade_amount_usdt'])
        lev = safe_float(cfg.get('leverage'), default['leverage'])
        cool = safe_int(cfg.get('manual_reentry_cooldown_minutes'), default['manual_reentry_cooldown_minutes'])
        if lev not in ALLOWED_LEVERAGES:
            lev = default['leverage']
        if amt <= 0:
            amt = default['trade_amount_usdt']
        if cool <= 0:
            cool = default['manual_reentry_cooldown_minutes']
        return {
            'trade_amount_usdt': amt, 'leverage': lev, 'manual_reentry_cooldown_minutes': cool,
            'updated_at': cfg.get('updated_at'),
            'close_requested': bool(cfg.get('close_requested', False)),
            'close_requested_at': cfg.get('close_requested_at'),
            'bot_paused': bool(cfg.get('bot_paused', False)),
            'force_trail': bool(cfg.get('force_trail', False)),
            'force_trail_at': cfg.get('force_trail_at'),
        }
    except Exception as e:
        logger.warning(f'Failed reading dashboard config: {e}')
        return default


def apply_runtime_settings(position: Optional[str]) -> dict:
    cfg = fetch_dashboard_config()
    state['cooldown_minutes'] = cfg['manual_reentry_cooldown_minutes']
    if position:
        if state.get('active_trade_amount_usdt') is None:
            state['active_trade_amount_usdt'] = cfg['trade_amount_usdt']
        if state.get('active_trade_leverage') is None:
            state['active_trade_leverage'] = cfg['leverage']
        source = 'locked-open-trade'
        trade_amount = safe_float(state['active_trade_amount_usdt'], cfg['trade_amount_usdt'])
        leverage = safe_float(state['active_trade_leverage'], cfg['leverage'])
    else:
        state['active_trade_amount_usdt'] = None
        state['active_trade_leverage'] = None
        source = 'dashboard-config'
        trade_amount = cfg['trade_amount_usdt']
        leverage = cfg['leverage']
    runtime_settings.update({'trade_amount_usdt': trade_amount, 'leverage': leverage, 'source': source})
    save_state()
    logger.info(f'⚙️ Runtime settings | amount=${trade_amount:.2f} | lev={leverage}x | cooldown={state["cooldown_minutes"]}m | source={source}')
    return cfg


def format_runtime_summary() -> str:
    return f"${runtime_settings['trade_amount_usdt']:.2f} @ {runtime_settings['leverage']}x"


def mark_bot_close(side: str) -> None:
    state['last_bot_closed_side'] = side
    state['last_bot_closed_ts'] = int(time.time())
    save_state()


def clear_close_request() -> None:
    if not GH_TOKEN or not DASHBOARD_REPO:
        return
    try:
        url = f'https://api.github.com/repos/{DASHBOARD_REPO}/contents/{BOT_CONFIG_FILE}'
        headers = {'Authorization': f'token {GH_TOKEN}', 'Accept': 'application/vnd.github+json'}
        r = requests.get(url, headers=headers, timeout=10)
        if not r.ok:
            logger.warning(f'clear_close_request: GET failed {r.status_code}')
            return
        j = r.json()
        current_cfg = json.loads(base64.b64decode(j['content'].replace('\n', '')).decode())
        current_cfg['close_requested'] = False
        current_cfg.pop('close_requested_at', None)
        content = base64.b64encode(json.dumps(current_cfg, indent=2).encode()).decode()
        payload = {'message': 'bot: clear close request', 'content': content, 'sha': j.get('sha')}
        requests.put(url, headers=headers, json=payload, timeout=15)
        logger.info('✅ Dashboard close request cleared from bot_config.json')
    except Exception as e:
        logger.warning(f'clear_close_request failed: {e}')


def clear_force_trail() -> None:
    try:
        url = f'https://api.github.com/repos/{DASHBOARD_REPO}/contents/{BOT_CONFIG_FILE}'
        headers = {'Authorization': f'token {GH_TOKEN}', 'Accept': 'application/vnd.github+json'}
        r = requests.get(url, headers=headers, timeout=10)
        if not r.ok:
            return
        j = r.json()
        current_cfg = json.loads(base64.b64decode(j['content'].replace('\n', '')).decode())
        current_cfg['force_trail'] = False
        content = base64.b64encode(json.dumps(current_cfg, indent=2).encode()).decode()
        payload = {'message': 'bot: clear force trail flag', 'content': content, 'sha': j.get('sha')}
        requests.put(url, headers=headers, json=payload, timeout=15)
        logger.info('✅ force_trail flag cleared from bot_config.json')
    except Exception as e:
        logger.warning(f'clear_force_trail failed: {e}')


def process_manual_close_detection(current_position: Optional[str]) -> None:
    prev = state.get('prev_position')
    now_ts = int(time.time())
    if prev in ('LONG', 'SHORT') and current_position is None:
        recently_bot_closed = (state.get('last_bot_closed_side') == prev) and ((now_ts - safe_int(state.get('last_bot_closed_ts'), 0)) <= 120)
        if recently_bot_closed:
            logger.info(f'ℹ️ Position transition {prev} -> None came from bot close; no manual cooldown applied')
        else:
            cooldown_minutes = safe_int(state.get('cooldown_minutes'), MANUAL_REENTRY_COOLDOWN_MIN)
            state['cooldown_side'] = prev
            state['cooldown_until'] = now_ts + (cooldown_minutes * 60)
            state['last_manual_close_ts'] = now_ts
            logger.info(f'🕒 Manual close detected on {prev}; blocking same-side re-entry for {cooldown_minutes} min')
            send_telegram(f'🕒 <b>APEX Cooldown Active</b>\n\nManual {prev} close detected.\nSame-side re-entry blocked for {cooldown_minutes} minutes.\nOpposite-side trade is still allowed.')
            save_state()


def get_same_side_cooldown(action: str) -> int:
    if action not in ('LONG', 'SHORT'):
        return 0
    side = state.get('cooldown_side')
    until = safe_int(state.get('cooldown_until'), 0)
    now_ts = int(time.time())
    if side == action and until > now_ts:
        return until - now_ts
    if until <= now_ts and state.get('cooldown_side'):
        state['cooldown_side'] = None
        state['cooldown_until'] = 0
        save_state()
    return 0


def check_margin_safety(current_position: Optional[str]) -> tuple[bool, float]:
    global last_margin_alert_ts, last_margin_alert_level
    try:
        level = get_margin_level()
        logger.info(f'🛡️ Margin level: {level:.2f}')
        if level == 999:
            return True, level
        if level < LOW_MARGIN_THRESHOLD:
            now_ts = time.time()
            should_alert = False
            if (now_ts - last_margin_alert_ts) >= LOW_MARGIN_ALERT_COOLDOWN:
                should_alert = True
            elif last_margin_alert_level is None:
                should_alert = True
            elif level <= (last_margin_alert_level - LOW_MARGIN_ALERT_DELTA):
                should_alert = True
            if should_alert:
                msg = f'⚠️ Margin level low: {level:.2f} — existing {current_position} position managed, no new entries.' if current_position else f'⚠️ Margin level low: {level:.2f} — pausing new entries.'
                logger.warning(msg)
                last_margin_alert_ts = now_ts
                last_margin_alert_level = level
            else:
                logger.warning(f'⚠️ Margin still low at {level:.2f}, alert suppressed')
            return False, level
        if last_margin_alert_level is not None and level >= LOW_MARGIN_THRESHOLD:
            last_margin_alert_level = None
        return True, level
    except Exception as e:
        logger.warning(f'Safety check failed: {e}')
        return True, 999.0


def open_long(price: float, confidence: int, reason: str) -> bool:
    try:
        step = get_step_size()
        usdt = get_margin_balance('USDT')
        available = usdt['free']
        collateral_usdt = float(runtime_settings['trade_amount_usdt'])
        leverage = float(runtime_settings['leverage'])
        gross_usdt = collateral_usdt * leverage
        borrow_amt = round(gross_usdt - collateral_usdt, 2)
        if available < collateral_usdt:
            logger.warning(f'Insufficient USDT: need ${collateral_usdt:.2f}, have ${available:.2f}')
            return False
        if borrow_amt > 0 and not borrow_margin('USDT', borrow_amt):
            return False
        time.sleep(1)
        quantity = round_step((gross_usdt * 0.995) / price, step)
        if quantity < step:
            return False
        resp = margin_order('BUY', quantity, 'NO_SIDE_EFFECT')
        init_trail(price)
        logger.info(f'✅ LONG OPEN: {quantity:.4f} {BASE_ASSET} @ ${price:.4f} | settings={format_runtime_summary()} | id={resp.get("orderId")}')
        atr = safe_float(state.get('trail_atr'), 0)
        sl = price - atr * HARD_SL_ATR
        trail_activates = price + atr
        send_telegram(f'🟢 <b>APEX — LONG OPENED ({leverage}x)</b>\n\n📌 Asset: {SYMBOL[:3]}/USDT\n💰 Price: ${price:,.4f}\n💵 Collateral: ${collateral_usdt:.2f}\n🔥 Effective: ${gross_usdt:.2f}\n🪙 Quantity: {quantity:.4f} {BASE_ASSET}\n🏦 Borrowed: ${borrow_amt:.2f} USDT\n🎯 Confidence: {confidence}%\n🛑 Hard SL: ${sl:.4f}\n📐 Trail activates at: ${trail_activates:.4f}\n📈 Signals: {reason}')
        return True
    except Exception as e:
        logger.error(f'open_long failed: {e}')
        alert_error(f'open_long: {e}')
        return False


def log_close_reason(reason: str) -> None:
    """Append a close reason with current timestamp to state close_log (keep last 50)."""
    entry = {'ts': now_utc_iso(), 'reason': reason}
    log = state.get('close_log') or []
    log.append(entry)
    state['close_log'] = log[-50:]


def close_long(price: float, confidence: int, reason: str) -> bool:
    try:
        step = get_step_size()
        asset = get_margin_balance(BASE_ASSET)
        logger.info(f'close_long | {BASE_ASSET} free={asset["free"]:.6f} locked={asset["locked"]:.6f} net={asset["net"]:.6f} borrowed={asset["borrowed"]:.6f} | step={step}')
        sellable = asset['free']
        if sellable < step and asset['locked'] > step:
            logger.warning(f'close_long | {BASE_ASSET} free=0 but locked={asset["locked"]:.6f} — trying locked amount')
            sellable = asset['locked']
        quantity = round_step(sellable, step)
        if quantity < step:
            logger.warning(f'close_long | No {BASE_ASSET} to sell (free={asset["free"]:.6f} locked={asset["locked"]:.6f}) — long already closed or balance unavailable')
            return False
        buy_price = get_last_trade_price('BUY')
        log_close_reason(reason)
        resp = margin_order('SELL', quantity, 'AUTO_REPAY')
        mark_bot_close('LONG')
        logger.info(f'✅ LONG CLOSE: sold {quantity:.4f} {BASE_ASSET} @ ${price:.4f} | id={resp.get("orderId")}')
        pnl_line = ''
        if buy_price:
            pnl = (price - buy_price) * quantity
            fee = (buy_price + price) * quantity * 0.001  # 0.1% each side
            net = pnl - fee
            pnl_line = f'\n✅ Gross P&amp;L: {pnl:+.4f} USDT\n💸 Fees: -{fee:.4f} USDT\n🏦 Net P&amp;L: {net:+.4f} USDT'
        send_telegram(f'🔴 <b>APEX — LONG CLOSED</b>\n\n💰 Price: ${price:,.4f}\n🪙 Quantity: {quantity:.4f} {BASE_ASSET}\n🎯 Confidence: {confidence}%\n📉 Signals: {reason}{pnl_line}')
        return True
    except Exception as e:
        logger.error(f'close_long failed: {e}')
        alert_error(f'close_long: {e}')
        return False


def open_short(price: float, confidence: int, reason: str) -> bool:
    try:
        step = get_step_size()
        usdt = get_margin_balance('USDT')
        available = usdt['free']
        collateral_usdt = float(runtime_settings['trade_amount_usdt'])
        leverage = float(runtime_settings['leverage'])
        gross_usdt = collateral_usdt * leverage
        if available < collateral_usdt:
            logger.warning(f'Insufficient USDT collateral: need ${collateral_usdt:.2f}, have ${available:.2f}')
            return False
        borrow_base = round_step((gross_usdt * 0.995) / price, step)
        if borrow_base < step:
            return False
        if not borrow_margin(BASE_ASSET, borrow_base):
            return False
        time.sleep(1)
        resp = margin_order('SELL', borrow_base, 'NO_SIDE_EFFECT')
        init_trail(price)
        logger.info(f'✅ SHORT OPEN: sold {borrow_base:.4f} {BASE_ASSET} @ ${price:.4f} | settings={format_runtime_summary()} | id={resp.get("orderId")}')
        atr = safe_float(state.get('trail_atr'), 0)
        sl = price + atr * HARD_SL_ATR
        trail_activates = price - atr
        send_telegram(f'🔴 <b>APEX — SHORT OPENED ({leverage}x)</b>\n\n📌 Asset: {SYMBOL[:3]}/USDT\n💰 Price: ${price:,.4f}\n💵 Collateral: ${collateral_usdt:.2f}\n🔥 Effective: ${gross_usdt:.2f}\n🪙 Quantity: {borrow_base:.4f} {BASE_ASSET}\n🏦 Borrowed: {borrow_base:.4f} {BASE_ASSET}\n🎯 Confidence: {confidence}%\n🛑 Hard SL: ${sl:.4f}\n📐 Trail activates at: ${trail_activates:.4f}\n📉 Signals: {reason}')
        return True
    except Exception as e:
        logger.error(f'open_short failed: {e}')
        alert_error(f'open_short: {e}')
        return False


def close_short(price: float, confidence: int, reason: str) -> bool:
    try:
        step = get_step_size()
        asset = get_margin_balance(BASE_ASSET)
        borrowed_total = asset['borrowed'] + asset['interest']
        if borrowed_total < 0.0001:
            logger.info(f'No borrowed {BASE_ASSET} to repay — short already closed')
            cleanup_orphan_short_borrow()
            return False
        sell_price = get_last_trade_price('SELL')
        log_close_reason(reason)
        quantity = round_step(borrowed_total * SHORT_CLOSE_BUFFER, step)
        if quantity < borrowed_total:
            quantity = round_step(borrowed_total + step, step)
        resp = margin_order('BUY', quantity, 'AUTO_REPAY')
        mark_bot_close('SHORT')
        logger.info(f'✅ SHORT CLOSE: bought {quantity:.4f} {BASE_ASSET} @ ${price:.4f} | borrowed_total={borrowed_total:.8f} | id={resp.get("orderId")}')
        time.sleep(2)
        cleanup_orphan_short_borrow()
        after = get_margin_balance(BASE_ASSET)
        remaining = after['borrowed'] + after['interest']
        # Sell any excess base asset left over from SHORT_CLOSE_BUFFER overbuy
        if after['free'] >= step:
            logger.info(f'🧹 Selling excess {BASE_ASSET} from short close | free={after["free"]:.4f}')
            try:
                margin_order('SELL', round_step(after['free'], step), 'AUTO_REPAY')
            except Exception as ex:
                logger.warning(f'Excess {BASE_ASSET} sell failed: {ex}')
        pnl_line = ''
        if sell_price:
            pnl = (sell_price - price) * quantity
            fee = (sell_price + price) * quantity * 0.001  # 0.1% each side
            net = pnl - fee
            pnl_line = f'\n✅ Gross P&amp;L: {pnl:+.4f} USDT\n💸 Fees: -{fee:.4f} USDT\n🏦 Net P&amp;L: {net:+.4f} USDT'
        send_telegram(f'🟢 <b>APEX — SHORT CLOSED</b>\n\n💰 Close: ${price:,.4f}\n🪙 Quantity: {quantity:.4f} {BASE_ASSET}\n🎯 Confidence: {confidence}%\n📈 Signals: {reason}{pnl_line}')
        return remaining < 0.01
    except Exception as e:
        logger.error(f'close_short failed: {e}')
        alert_error(f'close_short: {e}')
        return False


def push_dashboard_data(data: dict, dashboard_file: str = 'data_binance_sol.json') -> None:
    if not GH_TOKEN or not DASHBOARD_REPO:
        logger.warning('Dashboard push skipped: GH_TOKEN or DASHBOARD_REPO missing')
        return
    try:
        sym_label = SYMBOLS_CONFIG.get(SYMBOL, {}).get('base', 'SOL')
        url = f'https://api.github.com/repos/{DASHBOARD_REPO}/contents/{dashboard_file}'
        headers = {'Authorization': f'token {GH_TOKEN}', 'Accept': 'application/vnd.github+json'}
        content = base64.b64encode(json.dumps(data, indent=2).encode()).decode()
        r = requests.get(url, headers=headers, timeout=10)
        sha = r.json().get('sha') if r.status_code == 200 else None
        payload = {'message': f'bot: {sym_label} {datetime.now(MST).strftime("%H:%M MST")}', 'content': content}
        if sha:
            payload['sha'] = sha
        r = requests.put(url, headers=headers, json=payload, timeout=15)
        if r.status_code in (200, 201):
            logger.info(f'✅ Dashboard updated ({dashboard_file})')
        else:
            logger.warning(f'Dashboard push failed: {r.status_code} | body={r.text}')
    except Exception as e:
        logger.warning(f'Dashboard error: {e}')


AI_ANALYSIS_INTERVAL = 1  # run AI analysis every cycle


def fetch_ai_analysis(price: float, position: Optional[str], decision: dict) -> dict:
    if not GROQ_API_KEY:
        return {}
    try:
        from groq import Groq
        client = Groq(api_key=GROQ_API_KEY)

        timeframes = {'5m': 60, '15m': 60, '30m': 48, '1h': 48}
        summaries = {}
        for interval, limit in timeframes.items():
            klines = binance_public('/api/v3/klines', {'symbol': SYMBOL, 'interval': interval, 'limit': limit})
            closes = [float(k[4]) for k in klines]
            highs  = [float(k[2]) for k in klines]
            lows   = [float(k[3]) for k in klines]
            vols   = [float(k[5]) for k in klines]
            atr    = sum(h - l for h, l in zip(highs[-14:], lows[-14:])) / 14
            chg    = ((closes[-1] - closes[0]) / closes[0]) * 100
            high24 = max(highs)
            low24  = min(lows)
            summaries[interval] = {
                'close': round(closes[-1], 4),
                'open':  round(closes[0], 4),
                'high':  round(high24, 4),
                'low':   round(low24, 4),
                'change_pct': round(chg, 2),
                'atr':   round(atr, 4),
                'avg_vol': round(sum(vols) / len(vols), 2),
                'last_vol': round(vols[-1], 2),
            }

        prompt = f"""You are a professional crypto trading analyst. Analyze SOL/USDT cross-margin trading data and give a concise, actionable market analysis.

Current price: ${price:.4f}
Current bot position: {position or 'FLAT'}
Bot signal: {decision.get('action','HOLD')} (confidence {decision.get('confidence',0)}%)
Bullish signals: {', '.join(decision.get('bullish_signals', [])) or 'none'}
Bearish signals: {', '.join(decision.get('bearish_signals', [])) or 'none'}

Multi-timeframe OHLC summary:
5m:  open={summaries['5m']['open']} close={summaries['5m']['close']} high={summaries['5m']['high']} low={summaries['5m']['low']} chg={summaries['5m']['change_pct']}% ATR={summaries['5m']['atr']}
15m: open={summaries['15m']['open']} close={summaries['15m']['close']} high={summaries['15m']['high']} low={summaries['15m']['low']} chg={summaries['15m']['change_pct']}% ATR={summaries['15m']['atr']}
30m: open={summaries['30m']['open']} close={summaries['30m']['close']} high={summaries['30m']['high']} low={summaries['30m']['low']} chg={summaries['30m']['change_pct']}% ATR={summaries['30m']['atr']}
1h:  open={summaries['1h']['open']} close={summaries['1h']['close']} high={summaries['1h']['high']} low={summaries['1h']['low']} chg={summaries['1h']['change_pct']}% ATR={summaries['1h']['atr']}

Respond in this exact JSON format, no extra text:
{{
  "verdict": "BULLISH" | "BEARISH" | "NEUTRAL" | "CHOPPY",
  "strength": 1-10,
  "summary": "2-3 sentence plain English summary of what the market is doing across timeframes",
  "5m": "one sentence analysis",
  "15m": "one sentence analysis",
  "30m": "one sentence analysis",
  "1h": "one sentence analysis",
  "key_level_support": price or null,
  "key_level_resistance": price or null,
  "recommendation": "one sentence — what a trader should watch for",
  "risk_warning": "one sentence or null"
}}"""

        resp = client.chat.completions.create(
            model='llama-3.3-70b-versatile',
            messages=[{'role': 'user', 'content': prompt}],
            temperature=0.3,
            max_tokens=600,
        )
        raw = resp.choices[0].message.content.strip()
        # Strip markdown code fences if present
        if raw.startswith('```'):
            raw = raw.split('```')[1]
            if raw.startswith('json'):
                raw = raw[4:]
        analysis = json.loads(raw.strip())
        analysis['generated_at'] = now_utc_iso()
        analysis['timeframes'] = summaries
        logger.info(f'🤖 AI analysis: {analysis.get("verdict")} strength={analysis.get("strength")}')
        return analysis
    except Exception as e:
        logger.warning(f'AI analysis failed: {e}')
        return {}


def get_atr(period: int = 14) -> float:
    try:
        klines = binance_public('/api/v3/klines', {'symbol': SYMBOL, 'interval': '15m', 'limit': period + 1})
        ranges = [float(k[2]) - float(k[3]) for k in klines]
        return sum(ranges[-period:]) / period
    except Exception as e:
        logger.warning(f'get_atr failed: {e}')
        return 0.0


def init_trail(entry_price: float) -> None:
    atr = get_atr()
    state['trail_entry_price'] = entry_price
    state['trail_best_price'] = entry_price
    state['trail_atr'] = atr
    save_state()
    logger.info(f'📐 Trail initialised | entry={entry_price:.4f} | ATR={atr:.4f} | SL={entry_price - atr*HARD_SL_ATR:.4f} (LONG) or {entry_price + atr*HARD_SL_ATR:.4f} (SHORT) | activates at {TRAIL_ACTIVATE_ATR}×ATR | trails {TRAIL_DISTANCE_ATR}×ATR')


def clear_trail() -> None:
    state['trail_entry_price'] = None
    state['trail_best_price'] = None
    state['trail_atr'] = None
    save_state()


def check_sl_trail(position: str, price: float) -> tuple[bool, str]:
    """
    Returns (should_close, reason).
    Hard SL at 1.5x ATR from entry.
    Trailing stop activates after 1x ATR profit, trails best price by 1.5x ATR.
    """
    entry = safe_float(state.get('trail_entry_price'), None)
    best  = safe_float(state.get('trail_best_price'), None)
    atr   = safe_float(state.get('trail_atr'), None)

    if entry is None or atr is None or atr <= 0:
        return False, ''

    is_long = position == 'LONG'
    hard_sl_dist  = atr * HARD_SL_ATR
    trail_dist    = atr * TRAIL_DISTANCE_ATR
    activate_dist = atr * TRAIL_ACTIVATE_ATR

    # ── Hard stop loss — once trail activates, floor rises to entry + fee buffer ──
    # Fee buffer: 0.1% per side × 2 = 0.2% round trip, so close is profitable after fees
    FEE_BUFFER = 0.005  # 0.5% of entry price — guarantees net profit after round-trip fees
    profit_dist_now = (best - entry) if is_long else (entry - best)
    trail_activated = profit_dist_now >= activate_dist
    if trail_activated:
        # Trail active — floor guarantees profit after fees
        sl = entry * (1 + FEE_BUFFER) if is_long else entry * (1 - FEE_BUFFER)
    else:
        sl = entry - hard_sl_dist if is_long else entry + hard_sl_dist
    if is_long and price <= sl:
        label = '✅ Fee-covered SL hit' if trail_activated else '🛑 Hard SL hit'
        return True, f'{label} | entry={entry:.4f} sl={sl:.4f} price={price:.4f} | ATR={atr:.4f}'
    if not is_long and price >= sl:
        label = '✅ Fee-covered SL hit' if trail_activated else '🛑 Hard SL hit'
        return True, f'{label} | entry={entry:.4f} sl={sl:.4f} price={price:.4f} | ATR={atr:.4f}'

    # ── Update best price ──
    if is_long:
        if price > best:
            state['trail_best_price'] = price
            best = price
            save_state()
    else:
        if price < best:
            state['trail_best_price'] = price
            best = price
            save_state()

    # ── Trailing stop — activates after TRAIL_ACTIVATE_ATR profit ──
    profit_dist = (best - entry) if is_long else (entry - best)
    if profit_dist < activate_dist:
        logger.info(f'📐 Trail not yet active | profit_dist={profit_dist:.4f} < activate={activate_dist:.4f} | best={best:.4f}')
        return False, ''

    # Dynamic trail: locks in 65% of gains, with 0.5×ATR minimum for breathing room
    dynamic_trail_dist = max(atr * 0.5, profit_dist * 0.35)
    trail_stop = best - dynamic_trail_dist if is_long else best + dynamic_trail_dist
    locked_pct = ((profit_dist - dynamic_trail_dist) / profit_dist * 100) if profit_dist > 0 else 0
    if is_long and price <= trail_stop:
        return True, f'📐 Trailing stop hit | best={best:.4f} trail_stop={trail_stop:.4f} price={price:.4f}'
    if not is_long and price >= trail_stop:
        return True, f'📐 Trailing stop hit | best={best:.4f} trail_stop={trail_stop:.4f} price={price:.4f}'

    logger.info(f'📐 Trail active | best={best:.4f} trail_stop={trail_stop:.4f} price={price:.4f} | locked={locked_pct:.0f}% of gains | dist={dynamic_trail_dist:.4f}')
    return False, ''


def run_once():
    global run_count, last_hold_alert, current_position, last_ai_analysis, SYMBOL, BASE_ASSET
    run_count += 1
    logger.info('=' * 60)
    logger.info(f'  APEX BINANCE MARGIN BOT — Run #{run_count}')
    logger.info('=' * 60)
    try:
        # ── Multi-symbol: determine which symbol to trade this cycle ──
        open_sym = detect_active_symbol()
        if open_sym:
            # Position already open — stay with that symbol
            SYMBOL = open_sym
            BASE_ASSET = SYMBOLS_CONFIG[open_sym]['base']
            state['active_symbol'] = open_sym
        elif state.get('active_symbol') and state.get('trail_entry_price'):
            # Trail state exists but no position detected — keep symbol until trail clears
            SYMBOL = state['active_symbol']
            BASE_ASSET = SYMBOLS_CONFIG.get(SYMBOL, {}).get('base', 'SOL')
        else:
            # No open position — evaluate all symbols, pick highest confidence
            best_sym, best_conf = None, 0
            for sym in TRADING_SYMBOLS:
                try:
                    _df = get_market_data(sym)
                    _price = get_current_price(sym)
                    _dec = get_decision(_df, _price)
                    _action = _dec['action']
                    _conf = _dec['confidence'] if _action in ('LONG', 'SHORT') else 0
                    # Apply 15m trend filter
                    if _action == 'LONG' and not (_df.iloc[-1]['ema_20'] > _df.iloc[-1]['ema_50']):
                        _conf = 0
                    if _action == 'SHORT' and not (_df.iloc[-1]['ema_20'] < _df.iloc[-1]['ema_50']):
                        _conf = 0
                    # Apply 1h trend filter at selection time
                    _1h_trend = get_trend_filter(sym)
                    if _action == 'LONG' and _1h_trend == 'BEARISH':
                        _conf = 0
                    if _action == 'SHORT' and _1h_trend == 'BULLISH':
                        _conf = 0
                    # DOGE: block LONG in SIDEWAYS, block all SHORTs
                    if sym == 'DOGEUSDT' and _action == 'LONG' and _1h_trend == 'SIDEWAYS':
                        _conf = 0
                    if sym == 'DOGEUSDT' and _action == 'SHORT':
                        _conf = 0
                    # Respect paused symbols
                    _paused = cfg.get('paused_symbols', []) if 'cfg' in dir() else []
                    if SYMBOLS_CONFIG[sym]['base'] in _paused:
                        _conf = 0
                    logger.info(f'📊 {sym}: price={_price:.4f} signal={_action} 1h={_1h_trend} conf={_conf}%')
                    if _conf > best_conf:
                        best_conf, best_sym = _conf, sym
                except Exception as e:
                    logger.warning(f'Symbol eval failed for {sym}: {e}')
            # Default to DOGE if nothing beats threshold (will be blocked by filters if no signal)
            SYMBOL = best_sym or 'DOGEUSDT'
            BASE_ASSET = SYMBOLS_CONFIG[SYMBOL]['base']
            state['active_symbol'] = SYMBOL
            logger.info(f'🎯 Selected symbol: {SYMBOL} (conf={best_conf}%)')

        current_position = detect_position()
        cfg = apply_runtime_settings(current_position)
        process_manual_close_detection(current_position)
        can_open_new_positions, margin_level = check_margin_safety(current_position)
        cleanup_orphan_short_borrow()
        cleanup_orphan_long_borrow()
        df = get_market_data(SYMBOL)
        price = get_current_price(SYMBOL)

        # ── Dashboard close request ──
        dashboard_close_executed = False
        if cfg.get('close_requested'):
            req_at = cfg.get('close_requested_at')
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(req_at.replace('Z', '+00:00'))).total_seconds() if req_at else 999
            except Exception:
                age = 999
            if age < 300 and current_position:
                logger.info(f'📱 Dashboard close request ({age:.0f}s old) — closing {current_position}')
                send_telegram(f'📱 <b>APEX — Dashboard Close</b>\n\nClosing {current_position} @ ${price:,.4f}\nRequested {age:.0f}s ago via dashboard.')
                if current_position == 'LONG':
                    closed = close_long(price, 0, 'Dashboard close request')
                else:
                    closed = close_short(price, 0, 'Dashboard close request')
                if closed:
                    dashboard_close_executed = True
                    current_position = detect_position()
                    clear_close_request()
                else:
                    logger.error(f'❌ Dashboard close FAILED for {current_position} — will retry next cycle')
                    alert_error(f'Dashboard close failed for {current_position} — check server logs')
            else:
                logger.info(f'⚠️ Dashboard close request ignored: age={age:.0f}s, position={current_position}')
                clear_close_request()

        # ── Force trail activation — process once per flag raise, reset when flag cleared ──
        global last_force_trail_ts
        if not cfg.get('force_trail'):
            state['force_trail_processed'] = False  # flag cleared — ready for next click
        if cfg.get('force_trail') and current_position and not state.get('force_trail_processed'):
            # If trail state missing (e.g. bot restarted mid-trade), initialise it now
            if not state.get('trail_entry_price') or not state.get('trail_atr'):
                logger.info('🔒 Force trail: trail state missing — initialising from current price')
                init_trail(price)
            entry_p = safe_float(state['trail_entry_price'], price)
            atr_p   = safe_float(state['trail_atr'], get_atr())
            # Set trail best = current price so trail stop is naturally below it.
            # Previous logic set new_best = price + 0.70×ATR (above current price)
            # which caused an immediate trigger on the next cycle.
            bounce_buffer = atr_p * 0.70
            # Approximate stop shown in message: price - max(0.5×ATR, 0.35×profit_dist)
            profit_dist = abs(price - entry_p)
            approx_trail_dist = max(atr_p * 0.5, profit_dist * 0.35)
            approx_stop = price - approx_trail_dist if current_position == 'LONG' else price + approx_trail_dist
            # new_best = current price so trail stop is computed below it, never above
            state['trail_best_price'] = price
            last_force_trail_ts = time.time()
            state['force_trail_processed'] = True
            save_state()
            logger.info(f'🔒 Force trail activated via dashboard | position={current_position} | click_price={price:.4f} | approx_stop={approx_stop:.4f} | trail_dist={approx_trail_dist:.4f}')
            send_telegram(f'🔒 <b>APEX — Force Trail Activated</b>\n\nClick price: ${price:,.4f}\nApprox stop: ${approx_stop:,.4f}\nTrail follows price up from here.')
        if cfg.get('force_trail'):
            clear_force_trail()

        decision = get_decision(df, price)
        action = decision['action']
        confidence = decision['confidence']
        risk = decision['risk_level']
        reason = decision['reason']
        trend = get_trend_filter(SYMBOL)
        logger.info(f'💰 {SYMBOL}: ${price:.4f} | signal={action} | position={current_position} | settings={format_runtime_summary()}')

        # ── Init trail state when a new position is detected ──
        if current_position and state.get('trail_entry_price') is None:
            entry_trade = get_last_trade_price('BUY' if current_position == 'LONG' else 'SELL')
            init_trail(entry_trade or price)

        # ── Clear trail when flat ──
        if not current_position and state.get('trail_entry_price') is not None:
            clear_trail()

        # ── Check SL / trailing stop BEFORE signal logic ──
        sl_trail_close = False
        if current_position and not dashboard_close_executed:
            sl_hit, sl_reason = check_sl_trail(current_position, price)
            if sl_hit:
                logger.info(sl_reason)
                send_telegram(f'🛑 <b>APEX — SL/Trail Stop</b>\n\n{sl_reason}\n\nClosing {current_position} @ ${price:,.4f}')
                if current_position == 'LONG':
                    close_long(price, confidence, sl_reason)
                else:
                    close_short(price, confidence, sl_reason)
                sl_trail_close = True
                clear_trail()
                current_position = detect_position()

        status = ''
        if dashboard_close_executed or sl_trail_close:
            action = 'HOLD'
            status = 'CLOSED BY DASHBOARD ✅' if dashboard_close_executed else 'SL/TRAIL STOP HIT ✅'
            reason = 'Dashboard close request executed' if dashboard_close_executed else sl_reason
            confidence = 0
            # ── Check last 3 closed trades from history for loss streak cooldown ──
            try:
                cooldown_map = state.get('loss_cooldown_until') or {}
                if not isinstance(cooldown_map, dict): cooldown_map = {}
                recent = get_trade_history(SYMBOL)
                min_trade_qty = max(get_step_size() * 10, 0.1)  # ignore dust sells < 10 units
                # Pair into closed trades to get last 3 results
                _opens, _closed = [], []
                for t in sorted(recent, key=lambda x: x['time']):
                    if t['qty'] < min_trade_qty:
                        continue  # skip dust fills (leftover from SHORT_CLOSE_BUFFER excess)
                    if t['action'] == 'BUY':
                        if _opens and _opens[-1]['action'] == 'BUY':
                            o = _opens[-1]
                            tq = o['qty'] + t['qty']
                            o['price'] = (o['price']*o['qty'] + t['price']*t['qty'])/tq
                            o['qty'] = tq
                        else:
                            _opens.append({'action':'BUY','price':t['price'],'qty':t['qty'],'time':t['time']})
                    elif t['action'] == 'SELL' and _opens:
                        o = _opens.pop()
                        gross = (t['price'] - o['price']) * min(o['qty'], t['qty'])
                        fee = (o['price'] + t['price']) * min(o['qty'], t['qty']) * 0.001
                        _closed.append({'net': gross - fee, 'time': t['time']})
                last3 = _closed[-3:] if len(_closed) >= 3 else []
                streak = sum(1 for t in last3 if t['net'] <= 0)
                logger.info(f'📉 {SYMBOL} last 3 trades: {["loss" if t["net"]<=0 else "win" for t in last3]} streak={streak}')
                if streak >= 3 and time.time() > cooldown_map.get(SYMBOL, 0):
                    cooldown_mins = 30
                    cooldown_map[SYMBOL] = time.time() + cooldown_mins * 60
                    state['loss_cooldown_until'] = cooldown_map
                    save_state()
                    logger.info(f'⏸️ Last 3 {SYMBOL} trades all losses — pausing for {cooldown_mins} min')
                    send_telegram(f'⚠️ <b>APEX — Loss Streak Cooldown</b>\n\nLast 3 trades on {SYMBOL} all losses\nPausing new entries for {cooldown_mins} minutes.\nSL/trail still active.')
            except Exception as _e:
                logger.warning(f'Loss streak check failed: {_e}')
        cooldown_remaining = get_same_side_cooldown(action)
        if cooldown_remaining > 0 and current_position is None:
            mins = cooldown_remaining // 60
            secs = cooldown_remaining % 60
            logger.info(f'🕒 Same-side cooldown blocking {action} for {mins}m {secs}s after manual close')
            action = 'HOLD'
            status = f'COOLDOWN BLOCKED — manual {state.get("cooldown_side")} close; same-side re-entry blocked for {mins}m {secs}s'
            reason = status + f' | original signal: {decision["action"]}'
            confidence = 0

        if action in ('LONG', 'SHORT') and confidence < 85:
            action = 'HOLD'
            status = f'SKIPPED — confidence {confidence}% below minimum 85%'

        # ATR filter — skip entries when market is too tight to cover fees
        if current_position is None and action in ('LONG', 'SHORT'):
            _atr = state.get('trail_atr') or get_atr()
            _min_atr = SYMBOLS_CONFIG.get(SYMBOL, {}).get('min_atr', 0.22)
            if _atr and _atr < _min_atr:
                action = 'HOLD'
                status = f'SKIPPED — ATR {_atr:.4f} too small (min {_min_atr}), market too choppy to cover fees'
                logger.info(status)

        if trend == 'SIDEWAYS' and action in ('LONG', 'SHORT') and confidence < 85:
            action = 'HOLD'
            status = f'SKIPPED — sideways + confidence {confidence}% < 85%'

        # ── Consecutive loss cooldown (per symbol, based on trade history) ──
        cooldown_map = state.get('loss_cooldown_until') or {}
        if not isinstance(cooldown_map, dict): cooldown_map = {}
        loss_cooldown_until = cooldown_map.get(SYMBOL, 0)
        if current_position is None and action in ('LONG', 'SHORT') and time.time() < loss_cooldown_until:
            remaining = int((loss_cooldown_until - time.time()) / 60)
            action = 'HOLD'
            status = f'⏸️ LOSS STREAK COOLDOWN — {remaining}m remaining on {SYMBOL}'
            logger.info(status)

        # ── 1h trend alignment — block entries that fight the hourly trend ──
        if current_position is None and action == 'LONG' and trend == 'BEARISH':
            action = 'HOLD'
            status = f'SKIPPED — LONG blocked, 1h trend is BEARISH (price < EMA-15 < EMA-40)'
            logger.info(status)
        if current_position is None and action == 'SHORT' and trend == 'BULLISH':
            action = 'HOLD'
            status = f'SKIPPED — SHORT blocked, 1h trend is BULLISH (price > EMA-15 > EMA-40)'
            logger.info(status)
        # DOGE long-only: also block LONG when trend is SIDEWAYS (only trade confirmed uptrends)
        if current_position is None and action == 'LONG' and trend == 'SIDEWAYS' and SYMBOL == 'DOGEUSDT':
            action = 'HOLD'
            status = 'SKIPPED — LONG blocked for DOGE, 1h trend is SIDEWAYS (needs confirmed uptrend)'
            logger.info(status)
        # DOGE is long-only — shorts perform poorly on this asset
        if current_position is None and action == 'SHORT' and SYMBOL == 'DOGEUSDT':
            action = 'HOLD'
            status = 'SKIPPED — SHORT blocked for DOGE (long-only mode)'
            logger.info(status)

        paused_symbols = cfg.get('paused_symbols', [])
        sym_label = SYMBOLS_CONFIG.get(SYMBOL, {}).get('base', 'SOL')
        bot_paused = cfg.get('bot_paused', False) or sym_label in paused_symbols
        if bot_paused:
            logger.info(f'⏸️ {SYMBOL} entries paused — SL/trail still active')
            if current_position is None:
                status = status or f'⏸️ PAUSED — {SYMBOL} entries disabled via dashboard'

        if current_position is None and not bot_paused:
            flip = state.get('flip_pending')
            if flip:
                state['flip_pending'] = None
                save_state()
                if action != flip:
                    logger.info(f'🔄 Flip cooldown: signal changed to {action} (wanted {flip}) — skipping flip entry, good call')
                    send_telegram(f'🔄 <b>APEX — Flip Skipped</b>\n\nWanted to open {flip} after cooldown but signal changed to {action}.\nAvoided a bad entry.')
                    status = f'FLIP SKIPPED — signal changed {flip}→{action} during cooldown'
                elif not can_open_new_positions:
                    status = f'FLIP BLOCKED — low margin {margin_level:.2f}'
                elif risk != 'HIGH':
                    if flip == 'LONG':
                        status = 'FLIPPED →LONG ✅' if open_long(price, confidence, reason) else 'FLIP LONG FAILED ❌'
                    else:
                        status = 'FLIPPED →SHORT ✅' if open_short(price, confidence, reason) else 'FLIP SHORT FAILED ❌'
                else:
                    status = f'FLIP BLOCKED — risk={risk}'
            elif action in ('LONG', 'SHORT') and not can_open_new_positions:
                status = f'BLOCKED — low margin level {margin_level:.2f}, no new entries'
            elif action == 'LONG' and risk != 'HIGH':
                status = 'LONG OPENED ✅' if open_long(price, confidence, reason) else 'LONG FAILED ❌'
            elif action == 'SHORT' and risk != 'HIGH':
                status = 'SHORT OPENED ✅' if open_short(price, confidence, reason) else 'SHORT FAILED ❌'
            else:
                status = status or f'HOLD — {reason}'
        elif current_position == 'LONG':
            if action == 'SHORT':
                closed = close_long(price, confidence, 'Signal flipped to SHORT — waiting one cycle before re-entry')
                if closed:
                    state['flip_pending'] = 'SHORT'
                    save_state()
                    status = 'LONG CLOSED — SHORT queued next run ⏳'
                    logger.info('🔄 Flip cooldown: LONG closed, SHORT will open next run if signal holds')
                else:
                    status = 'LONG CLOSE FAILED ❌'
            else:
                status = f'HOLDING LONG — {reason}'
                # Alert only when signal flips to HOLD while in a LONG
                if action == 'HOLD':
                    now_ts = time.time()
                    if now_ts - last_hold_alert > 1800:
                        send_telegram(f'⚠️ <b>APEX — Signal Fading (LONG open)</b>\n💰 {SYMBOL}: ${price:.4f}\n🟢 {len(decision["bullish_signals"])} bullish | 🔴 {len(decision["bearish_signals"])} bearish\n💬 {reason}')
                        last_hold_alert = now_ts
        elif current_position == 'SHORT':
            if action == 'LONG':
                closed = close_short(price, confidence, 'Signal flipped to LONG — waiting one cycle before re-entry')
                if closed:
                    state['flip_pending'] = 'LONG'
                    save_state()
                    status = 'SHORT CLOSED — LONG queued next run ⏳'
                    logger.info('🔄 Flip cooldown: SHORT closed, LONG will open next run if signal holds')
                else:
                    status = 'SHORT CLOSE FAILED ❌'
            else:
                status = f'HOLDING SHORT — {reason}'
                # Alert only when signal flips to HOLD while in a SHORT
                if action == 'HOLD':
                    now_ts = time.time()
                    if now_ts - last_hold_alert > 1800:
                        send_telegram(f'⚠️ <b>APEX — Signal Fading (SHORT open)</b>\n💰 {SYMBOL}: ${price:.4f}\n🟢 {len(decision["bullish_signals"])} bullish | 🔴 {len(decision["bearish_signals"])} bearish\n💬 {reason}')
                        last_hold_alert = now_ts

        usdt_balance = 0.0
        sol_balance = 0.0
        try:
            usdt_b = get_margin_balance('USDT')
            sol_b = get_margin_balance(BASE_ASSET)
            usdt_balance = usdt_b['net']
            sol_balance = sol_b['net']
            margin_level = get_margin_level()
        except Exception as e:
            logger.warning(f'Balance fetch failed: {e}')

        # ── AI analysis every run ──
        if GROQ_API_KEY and run_count % AI_ANALYSIS_INTERVAL == 0:
            last_ai_analysis = {}  # AI analysis disabled — using trend filter instead

        # Always fetch trade history for the active symbol specifically
        trade_history = merge_close_reasons(get_trade_history(SYMBOL))
        closed_trades = sorted(build_closed_trades_from_history(trade_history) + load_manual_closed_trades(), key=lambda x: x.get('closed_at') or '')
        performance = summarize_performance(closed_trades)

        latest_position = detect_position()
        state['prev_position'] = latest_position
        save_state()
        cfg = fetch_dashboard_config()
        queued_settings = None
        if latest_position:
            queued_settings = {'trade_amount_usdt': cfg['trade_amount_usdt'], 'leverage': cfg['leverage'], 'applies_when_flat': True}

        push_dashboard_data({
            'generated_at': now_utc_iso(),
            'symbol': SYMBOL,
            'exchange': 'Binance Margin',
            'leverage': runtime_settings['leverage'],
            'price': price,
            'usdt_balance': usdt_balance,
            'sol_balance': sol_balance,
            'cad_balance': usdt_balance,
            'margin_level': margin_level,
            'position': latest_position,
            'action': action,
            'confidence': confidence,
            'risk': risk,
            'status': status,
            'bot_paused': bot_paused,
            'paused_symbols': cfg.get('paused_symbols', []),
            'flip_pending': state.get('flip_pending'),
            'reason': reason,
            'trend': trend,
            'bullish': decision['bullish_signals'],
            'bearish': decision['bearish_signals'],
            'trades': trade_history,
            'closed_trades': closed_trades,
            'performance': performance,
            'run_count': run_count,
            'trade_amount': runtime_settings['trade_amount_usdt'],
            'min_signals': MIN_SIGNALS,
            'bot_config': {
                'current': {'trade_amount_usdt': runtime_settings['trade_amount_usdt'], 'leverage': runtime_settings['leverage'], 'source': runtime_settings['source']},
                'dashboard_requested': cfg,
                'queued': queued_settings,
            },
            'cooldown': {
                'side': state.get('cooldown_side'),
                'until': state.get('cooldown_until'),
                'remaining_seconds': get_same_side_cooldown(state.get('cooldown_side') or ''),
                'minutes': state.get('cooldown_minutes', MANUAL_REENTRY_COOLDOWN_MIN),
            },
            'ai_analysis': last_ai_analysis if last_ai_analysis.get('verdict') else None,
            'trail': {
                'entry_price': state.get('trail_entry_price'),
                'best_price': state.get('trail_best_price'),
                'atr': state.get('trail_atr'),
                'sl': (state['trail_entry_price'] - state['trail_atr'] * 1.5) if latest_position == 'LONG' and state.get('trail_entry_price') and state.get('trail_atr') else
                      (state['trail_entry_price'] + state['trail_atr'] * 1.5) if latest_position == 'SHORT' and state.get('trail_entry_price') and state.get('trail_atr') else None,
                'trail_stop': (state['trail_best_price'] - state['trail_atr'] * 1.5) if latest_position == 'LONG' and state.get('trail_best_price') and state.get('trail_atr') else
                              (state['trail_best_price'] + state['trail_atr'] * 1.5) if latest_position == 'SHORT' and state.get('trail_best_price') and state.get('trail_atr') else None,
                'active': state.get('trail_best_price') is not None and state.get('trail_atr') is not None and state.get('trail_entry_price') is not None and
                          abs((state.get('trail_best_price', 0) - state.get('trail_entry_price', 0))) >= state.get('trail_atr', 999),
            },
        }, dashboard_file=SYMBOLS_CONFIG.get(SYMBOL, {}).get('dashboard_file', 'data_binance_sol.json'))

        # Push idle data for inactive symbols so their dashboard tabs stay fresh
        for idle_sym, idle_cfg in SYMBOLS_CONFIG.items():
            if idle_sym == SYMBOL:
                continue
            try:
                idle_history = merge_close_reasons(get_trade_history(idle_sym))
                idle_closed = sorted(build_closed_trades_from_history(idle_history), key=lambda x: x.get('closed_at') or '')
                idle_perf = summarize_performance(idle_closed)
                idle_price = get_current_price(idle_sym)
                idle_df = get_market_data(idle_sym)
                idle_dec = get_decision(idle_df, idle_price)
                push_dashboard_data({
                    'generated_at': now_utc_iso(),
                    'symbol': idle_sym,
                    'exchange': 'Binance Margin',
                    'leverage': runtime_settings['leverage'],
                    'price': idle_price,
                    'position': None,
                    'action': idle_dec['action'],
                    'confidence': idle_dec['confidence'],
                    'risk': idle_dec['risk_level'],
                    'status': f'Idle — {SYMBOL} active',
                    'bullish': idle_dec['bullish_signals'],
                    'bearish': idle_dec['bearish_signals'],
                    'trades': idle_history,
                    'closed_trades': idle_closed,
                    'performance': idle_perf,
                    'run_count': run_count,
                    'trade_amount': runtime_settings['trade_amount_usdt'],
                    'trail': {'entry_price': None, 'best_price': None, 'atr': None, 'sl': None, 'trail_stop': None, 'active': False},
                }, dashboard_file=idle_cfg['dashboard_file'])
            except Exception as _e:
                logger.warning(f'Idle dashboard push failed for {idle_sym}: {_e}')

        logger.info(f'✅ Run #{run_count} complete | pos={latest_position} | margin={margin_level:.2f} | pnl={performance["net_profit"]:.4f}')
    except Exception as e:
        logger.error(f'❌ Bot error: {e}', exc_info=True)
        alert_error(str(e))


def run_startup_binance_tests() -> None:
    logger.info('Running Binance startup self-tests...')
    try:
        spot_test = binance_private('GET', '/api/v3/account')
        logger.info(f"SPOT TEST OK | accountType={spot_test.get('accountType')} | canTrade={spot_test.get('canTrade')}")
    except Exception as e:
        logger.error(f'SPOT TEST FAILED: {e}', exc_info=True)
    try:
        margin_test = binance_private('GET', '/sapi/v1/margin/account')
        logger.info(f"MARGIN TEST OK | marginLevel={margin_test.get('marginLevel')} | userAssets={len(margin_test.get('userAssets', []))}")
    except Exception as e:
        logger.error(f'MARGIN TEST FAILED: {e}', exc_info=True)


def main():
    load_state()
    state['force_trail_processed'] = False  # always reset on startup so button works after restart
    save_state()
    logger.info('🚀 APEX Binance Margin Bot starting (multi-symbol)...')
    logger.info(f'   Symbols: {", ".join(TRADING_SYMBOLS)}')
    logger.info(f'   Default amount: ${DEFAULT_TRADE_AMOUNT_USDT:.2f}')
    logger.info(f'   Default leverage: {DEFAULT_LEVERAGE}x')
    logger.info(f'   API Key: {mask_secret(BINANCE_API_KEY)}')
    logger.info(f'   GH Token: {mask_secret(GH_TOKEN)}')
    logger.info(f'   Dashboard: {DASHBOARD_REPO or "MISSING"}')
    logger.info(f'   Public IP: {get_public_ip()}')
    send_telegram(
        f'🚀 <b>APEX Binance Margin Bot Started</b>\n\n📌 Symbols: {", ".join(TRADING_SYMBOLS)}\n🛡 Low-margin alerts throttled\n🕒 Same-side manual-close cooldown enabled\n⚙️ Dashboard config: {BOT_CONFIG_FILE}\n⏱ Every {CHECK_INTERVAL} seconds'
    )
    run_startup_binance_tests()
    while True:
        try:
            run_once()
        except Exception as e:
            logger.error(f'Main loop error: {e}', exc_info=True)
            alert_error(f'Main loop crashed: {e}')
        logger.info(f'⏳ Sleeping {CHECK_INTERVAL}s (polling every 5s for close/trail/price)...')
        for _ in range(CHECK_INTERVAL // 5):
            time.sleep(5)
            try:
                quick_cfg = fetch_dashboard_config()
                if quick_cfg.get('close_requested'):
                    logger.info('⚡ Close request detected mid-sleep — waking up immediately')
                    break
                if quick_cfg.get('force_trail'):
                    logger.info('⚡ Force trail request detected mid-sleep — waking up immediately')
                    break
                # ── Check trail stop mid-sleep so close delay is max 5s not 60s ──
                _pos = current_position
                if _pos and state.get('trail_entry_price') and state.get('trail_atr'):
                    _price = get_current_price(SYMBOL)
                    _sl_hit, _sl_reason = check_sl_trail(_pos, _price)
                    if _sl_hit:
                        logger.info(f'⚡ Trail/SL hit mid-sleep ({_sl_reason}) — waking up immediately')
                        break
            except Exception:
                pass


if __name__ == '__main__':
    main()
