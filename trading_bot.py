"""
APEX Binance Margin SOL Bot — Long & Short Trading
Exchange: Binance Cross Margin (SOLUSDT)
Strategy: RSI + MACD + EMA + Bollinger Bands + Volume
Leverage: 4x Cross Margin
Long:     Buy SOL when bullish signals fire
Short:    Sell SOL when bearish signals fire
Exit:     Signal-based only (no trailing stop)
Alerts:   Telegram
Data:     Pushes data_binance_sol.json to apex-dashboard
Runner:   Continuous loop

UPDATED:
- Includes manual closed trades from manual_closed_trades.json
- Builds closed trades from Binance margin trade history
- Calculates wins, losses, win rate, and net profit
- Pushes closed_trades + performance to dashboard
- Throttles low-margin Telegram alerts so you are not spammed every 60s
- Does NOT stop managing an existing live position just because margin is low
- Blocks opening NEW trades when margin is unsafe
- Makes short-close repayment more robust with explicit repay cleanup
- Adds orphaned SOL borrow cleanup helper for manual-close edge cases
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

# ─────────────────────────────────────────────
# Logging (File + Console) — single setup
# ─────────────────────────────────────────────
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

# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────
BINANCE_API_KEY    = os.environ.get('BINANCE_API_KEY', '').strip()
BINANCE_API_SECRET = os.environ.get('BINANCE_API_SECRET', '').strip()
SYMBOL             = os.environ.get('TRADING_SYMBOL', 'SOLUSDT')
BASE_ASSET         = 'SOL'
QUOTE_ASSET        = 'USDT'
TRADE_AMOUNT_USDT  = float(os.environ.get('TRADE_AMOUNT_USDT', '80'))
LEVERAGE           = float(os.environ.get('LEVERAGE', '4'))
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
TELEGRAM_CHAT_ID   = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
GH_TOKEN           = os.environ.get('GH_TOKEN', '').strip()
DASHBOARD_REPO     = os.environ.get('DASHBOARD_REPO', 'avinashpathrol/apex-dashboard').strip()
MIN_SIGNALS        = int(os.environ.get('MIN_SIGNALS', '3'))
CHECK_INTERVAL     = int(os.environ.get('CHECK_INTERVAL', '60'))
BINANCE_BASE_URL   = 'https://api.binance.com'

# Optional manual trades file for older/missed trades
MANUAL_TRADES_FILE = os.environ.get('MANUAL_TRADES_FILE', 'manual_closed_trades.json').strip()

# Safety / alert tuning
LOW_MARGIN_THRESHOLD = float(os.environ.get('LOW_MARGIN_THRESHOLD', '1.50'))
LOW_MARGIN_ALERT_COOLDOWN = int(os.environ.get('LOW_MARGIN_ALERT_COOLDOWN', '1800'))  # 30 min
LOW_MARGIN_ALERT_DELTA = float(os.environ.get('LOW_MARGIN_ALERT_DELTA', '0.05'))      # re-alert if level worsens by this much
SHORT_CLOSE_BUFFER = float(os.environ.get('SHORT_CLOSE_BUFFER', '1.005'))              # 0.5% buy buffer to cover interest/slippage

# ─────────────────────────────────────────────
# State tracking
# ─────────────────────────────────────────────
run_count = 0
last_hold_alert = 0
current_position = None  # 'LONG', 'SHORT', or None

last_margin_alert_ts = 0.0
last_margin_alert_level = None

# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
def mask_secret(value: str, start: int = 6, end: int = 4) -> str:
    if not value:
        return 'MISSING'
    if len(value) <= start + end:
        return '*' * len(value)
    return f'{value[:start]}...{value[-end:]}'


def get_public_ip() -> str:
    services = [
        'https://api.ipify.org',
        'https://ifconfig.me/ip',
        'https://icanhazip.com',
    ]
    for service in services:
        try:
            resp = requests.get(service, timeout=5)
            if resp.ok and resp.text.strip():
                return resp.text.strip()
        except Exception:
            pass
    return 'UNKNOWN'


# ─────────────────────────────────────────────
# 1. TELEGRAM ALERTS
# ─────────────────────────────────────────────
def send_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
        requests.post(url, json={
            'chat_id':    TELEGRAM_CHAT_ID,
            'text':       message,
            'parse_mode': 'HTML',
        }, timeout=10)
        logger.info('📱 Telegram alert sent')
    except Exception as e:
        logger.warning(f'Telegram failed: {e}')


def alert_long_open(price: float, quantity: float, confidence: int, reason: str, borrowed: float) -> None:
    now = datetime.now(MST).strftime('%b %d, %I:%M %p MST')
    msg = (
        f'🟢 <b>APEX — LONG OPENED ({LEVERAGE}x)</b>\n\n'
        f'📌 Asset:      SOL/USDT\n'
        f'💰 Price:      ${price:,.4f} USDT\n'
        f'💵 Collateral: ${TRADE_AMOUNT_USDT:.2f} USDT\n'
        f'🔥 Effective:  ${TRADE_AMOUNT_USDT * LEVERAGE:.2f} USDT\n'
        f'🪙 Quantity:   {quantity:.4f} SOL\n'
        f'🏦 Borrowed:   ${borrowed:.2f} USDT\n'
        f'🎯 Confidence: {confidence}%\n'
        f'📈 Signals:    {reason}\n\n'
        f'⏰ {now}'
    )
    send_telegram(msg)


def alert_long_close(price: float, quantity: float, confidence: int, reason: str, buy_price: Optional[float] = None) -> None:
    now = datetime.now(MST).strftime('%b %d, %I:%M %p MST')
    pnl_line = ''
    if buy_price and buy_price > 0:
        pnl = (price - buy_price) * quantity
        pct = ((price - buy_price) / buy_price) * 100
        levered_pct = pct * LEVERAGE
        emoji = '✅' if pnl >= 0 else '❌'
        pnl_line = f'\n{emoji} Est P&amp;L: {"+" if pnl >= 0 else ""}${pnl:.4f} ({levered_pct:+.2f}% levered)'
    msg = (
        f'🔴 <b>APEX — LONG CLOSED ({LEVERAGE}x)</b>\n\n'
        f'📌 Asset:      SOL/USDT\n'
        f'💰 Price:      ${price:,.4f} USDT\n'
        f'🪙 Quantity:   {quantity:.4f} SOL\n'
        f'🎯 Confidence: {confidence}%\n'
        f'📉 Signals:    {reason}'
        f'{pnl_line}\n\n'
        f'⏰ {now}'
    )
    send_telegram(msg)


def alert_short_open(price: float, quantity: float, confidence: int, reason: str, borrowed: float) -> None:
    now = datetime.now(MST).strftime('%b %d, %I:%M %p MST')
    msg = (
        f'🔴 <b>APEX — SHORT OPENED ({LEVERAGE}x)</b>\n\n'
        f'📌 Asset:      SOL/USDT\n'
        f'💰 Price:      ${price:,.4f} USDT\n'
        f'💵 Collateral: ${TRADE_AMOUNT_USDT:.2f} USDT\n'
        f'🔥 Effective:  ${TRADE_AMOUNT_USDT * LEVERAGE:.2f} USDT\n'
        f'🪙 Quantity:   {quantity:.4f} SOL\n'
        f'🏦 Borrowed:   {borrowed:.4f} SOL\n'
        f'🎯 Confidence: {confidence}%\n'
        f'📉 Signals:    {reason}\n\n'
        f'⏰ {now}'
    )
    send_telegram(msg)


def alert_short_close(price: float, quantity: float, confidence: int, reason: str, sell_price: Optional[float] = None) -> None:
    now = datetime.now(MST).strftime('%b %d, %I:%M %p MST')
    pnl_line = ''
    if sell_price and sell_price > 0:
        pnl = (sell_price - price) * quantity
        pct = ((sell_price - price) / sell_price) * 100
        levered_pct = pct * LEVERAGE
        emoji = '✅' if pnl >= 0 else '❌'
        pnl_line = f'\n{emoji} Est P&amp;L: {"+" if pnl >= 0 else ""}${pnl:.4f} ({levered_pct:+.2f}% levered)'
    msg = (
        f'🟢 <b>APEX — SHORT CLOSED ({LEVERAGE}x)</b>\n\n'
        f'📌 Asset:      SOL/USDT\n'
        f'💰 Close:      ${price:,.4f} USDT\n'
        f'🪙 Quantity:   {quantity:.4f} SOL\n'
        f'🎯 Confidence: {confidence}%\n'
        f'📈 Signals:    {reason}'
        f'{pnl_line}\n\n'
        f'⏰ {now}'
    )
    send_telegram(msg)


def alert_error(error: str) -> None:
    msg = f'❌ <b>APEX MARGIN BOT ERROR</b>\n\n{error}\n\n⏰ {datetime.now(MST).strftime("%b %d, %I:%M %p MST")}'
    send_telegram(msg)


# ─────────────────────────────────────────────
# 2. BINANCE API HELPERS
# ─────────────────────────────────────────────
def binance_signature(params: dict) -> str:
    query = urlencode(params)
    return hmac.new(
        BINANCE_API_SECRET.encode('utf-8'),
        query.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()


def binance_public(endpoint: str, params: dict = None) -> dict:
    url = f'{BINANCE_BASE_URL}{endpoint}'
    resp = requests.get(url, params=params or {}, timeout=10)
    logger.debug(f'BINANCE PUBLIC | endpoint={endpoint} | status={resp.status_code}')
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
        logger.error(
            f'BINANCE PRIVATE FAILED | method={method.upper()} | endpoint={endpoint} | '
            f'status={resp.status_code} | params={masked_params} | body={resp.text}'
        )
    else:
        logger.debug(f'BINANCE PRIVATE OK | method={method.upper()} | endpoint={endpoint} | status={resp.status_code}')

    resp.raise_for_status()
    return resp.json()


def run_startup_binance_tests() -> None:
    logger.info('Running Binance startup self-tests...')
    try:
        spot_test = binance_private('GET', '/api/v3/account')
        logger.info(
            f"SPOT TEST OK | accountType={spot_test.get('accountType')} | "
            f"canTrade={spot_test.get('canTrade')}"
        )
    except Exception as e:
        logger.error(f'SPOT TEST FAILED: {e}', exc_info=True)

    try:
        margin_test = binance_private('GET', '/sapi/v1/margin/account')
        logger.info(
            f"MARGIN TEST OK | marginLevel={margin_test.get('marginLevel')} | "
            f"userAssets={len(margin_test.get('userAssets', []))}"
        )
    except Exception as e:
        logger.error(f'MARGIN TEST FAILED: {e}', exc_info=True)


# ─────────────────────────────────────────────
# 3. MARGIN HELPERS
# ─────────────────────────────────────────────
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
    account = binance_private('GET', '/sapi/v1/margin/account')
    return float(account.get('marginLevel', 999))


def borrow_margin(asset: str, amount: float) -> bool:
    try:
        binance_private('POST', '/sapi/v1/margin/loan', {
            'asset':  asset,
            'amount': str(round(amount, 8)),
        })
        logger.info(f'🏦 Borrowed {amount:.4f} {asset}')
        return True
    except requests.HTTPError as e:
        response_text = ''
        try:
            response_text = e.response.text
        except Exception:
            pass
        if '"code":-3045' in response_text or '"code":-3035' in response_text:
            logger.warning(f'⚠️ Borrow unavailable for {asset}: insufficient lending pool')
            return False
        logger.error(f'Borrow {asset} failed: {e}')
        return False
    except Exception as e:
        logger.error(f'Borrow {asset} failed: {e}')
        return False


def repay_margin(asset: str, amount: float) -> bool:
    try:
        binance_private('POST', '/sapi/v1/margin/repay', {
            'asset':  asset,
            'amount': str(round(amount, 8)),
        })
        logger.info(f'💸 Repaid {amount:.8f} {asset}')
        return True
    except Exception as e:
        logger.error(f'Repay {asset} failed: {e}')
        return False


def margin_order(side: str, quantity: float, side_effect: str = 'NO_SIDE_EFFECT') -> dict:
    return binance_private('POST', '/sapi/v1/margin/order', {
        'symbol':         SYMBOL,
        'side':           side,
        'type':           'MARKET',
        'quantity':       str(quantity),
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


def detect_position() -> Optional[str]:
    sol  = get_margin_balance('SOL')
    usdt = get_margin_balance('USDT')
    logger.info(f'📊 SOL net={sol["net"]:.4f} borrowed={sol["borrowed"]:.4f} interest={sol["interest"]:.6f}')
    logger.info(f'📊 USDT net={usdt["net"]:.2f} borrowed={usdt["borrowed"]:.2f}')
    if sol['net'] > 0.05:
        logger.info('📍 Position: LONG')
        return 'LONG'
    elif (sol['borrowed'] + sol['interest']) > 0.05:
        logger.info('📍 Position: SHORT')
        return 'SHORT'
    logger.info('📍 Position: None')
    return None


def get_last_trade_price(side: str) -> Optional[float]:
    try:
        trades = binance_private('GET', '/sapi/v1/margin/myTrades', {
            'symbol': SYMBOL,
            'limit':  10,
        })
        for t in reversed(trades):
            is_buy = t.get('isBuyer', False)
            if (side == 'BUY' and is_buy) or (side == 'SELL' and not is_buy):
                return float(t['price'])
    except Exception as e:
        logger.warning(f'get_last_trade_price failed: {e}')
    return None


def get_trade_history() -> list:
    try:
        trades = binance_private('GET', '/sapi/v1/margin/myTrades', {
            'symbol': SYMBOL,
            'limit':  100,
        })
        result = []
        for t in trades:
            result.append({
                'time':   datetime.fromtimestamp(t['time']/1000, tz=MST).isoformat(),
                'action': 'BUY' if t['isBuyer'] else 'SELL',
                'price':  float(t['price']),
                'qty':    float(t['qty']),
                'cost':   float(t['quoteQty']),
                'fee':    float(t['commission']),
            })
        return sorted(result, key=lambda x: x['time'])
    except Exception as e:
        logger.warning(f'Trade history failed: {e}')
        return []


def cleanup_orphan_short_borrow() -> bool:
    """
    Manual-close edge case:
    Sometimes a short may be bought back manually, but borrowed SOL / interest is still left behind.
    If there is enough free SOL available, explicitly repay it.
    """
    try:
        sol = get_margin_balance('SOL')
        total_borrowed = sol['borrowed'] + sol['interest']
        free_sol = sol['free']

        if total_borrowed < 0.0001:
            return False

        repayable = min(free_sol, total_borrowed)
        if repayable < 0.0001:
            logger.info('🧹 Orphan short borrow detected but no free SOL available to repay yet')
            return False

        ok = repay_margin('SOL', repayable)
        if ok:
            time.sleep(1)
            sol_after = get_margin_balance('SOL')
            remaining = sol_after['borrowed'] + sol_after['interest']
            logger.info(f'🧹 Orphan SOL repay cleanup done | remaining borrowed={remaining:.8f}')
        return ok
    except Exception as e:
        logger.warning(f'cleanup_orphan_short_borrow failed: {e}')
        return False


# ─────────────────────────────────────────────
# 3B. CLOSED TRADE / PERFORMANCE HELPERS
# ─────────────────────────────────────────────
def load_manual_closed_trades() -> list:
    """
    Optional file: manual_closed_trades.json
    Example:
    [
      {
        "side": "LONG",
        "entry_price": 84.20,
        "exit_price": 81.90,
        "qty": 3.80,
        "fee": 0.50,
        "opened_at": "2026-03-28T21:10:00-07:00",
        "closed_at": "2026-03-28T22:35:00-07:00",
        "note": "Manual lost trade added for dashboard accuracy"
      }
    ]
    """
    try:
        if not os.path.exists(MANUAL_TRADES_FILE):
            return []

        with open(MANUAL_TRADES_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)

        if not isinstance(data, list):
            logger.warning(f'{MANUAL_TRADES_FILE} must contain a list')
            return []

        cleaned = []
        for t in data:
            if not isinstance(t, dict):
                continue

            side = str(t.get('side', '')).upper().strip()
            entry_price = float(t.get('entry_price', 0))
            exit_price = float(t.get('exit_price', 0))
            qty = float(t.get('qty', 0))
            fee = float(t.get('fee', 0))
            opened_at = t.get('opened_at')
            closed_at = t.get('closed_at')
            note = str(t.get('note', 'Manual trade'))

            if side not in ('LONG', 'SHORT'):
                logger.warning(f'Skipping manual trade with invalid side: {t}')
                continue
            if entry_price <= 0 or exit_price <= 0 or qty <= 0:
                logger.warning(f'Skipping manual trade with invalid values: {t}')
                continue

            if side == 'LONG':
                pnl = (exit_price - entry_price) * qty - fee
                pnl_pct = ((exit_price - entry_price) / entry_price) * 100 * LEVERAGE
            else:
                pnl = (entry_price - exit_price) * qty - fee
                pnl_pct = ((entry_price - exit_price) / entry_price) * 100 * LEVERAGE

            cleaned.append({
                'source': 'manual',
                'side': side,
                'entry_price': round(entry_price, 8),
                'exit_price': round(exit_price, 8),
                'qty': round(qty, 8),
                'fee': round(fee, 8),
                'pnl': round(pnl, 4),
                'pnl_pct': round(pnl_pct, 2),
                'win': pnl > 0,
                'opened_at': opened_at,
                'closed_at': closed_at,
                'note': note,
            })

        return cleaned

    except Exception as e:
        logger.warning(f'Failed loading manual closed trades: {e}')
        return []


def build_closed_trades_from_history(trades: list) -> list:
    """
    Converts raw Binance BUY/SELL history into logical closed trades.
    Assumes your bot generally runs full LONG / full SHORT and closes/flips fully.
    """
    closed = []
    current_trade = None

    for t in trades:
        action = t.get('action')
        price = float(t.get('price', 0))
        qty = float(t.get('qty', 0))
        fee = float(t.get('fee', 0))
        ts = t.get('time')

        if action == 'BUY':
            if current_trade is None:
                current_trade = {
                    'side': 'LONG',
                    'entry_price': price,
                    'qty': qty,
                    'entry_fee': fee,
                    'opened_at': ts,
                }
            elif current_trade['side'] == 'SHORT':
                exit_qty = min(current_trade['qty'], qty)
                total_fee = current_trade.get('entry_fee', 0.0) + fee
                pnl = (current_trade['entry_price'] - price) * exit_qty - total_fee

                closed.append({
                    'source': 'binance',
                    'side': 'SHORT',
                    'entry_price': round(current_trade['entry_price'], 8),
                    'exit_price': round(price, 8),
                    'qty': round(exit_qty, 8),
                    'fee': round(total_fee, 8),
                    'pnl': round(pnl, 4),
                    'pnl_pct': round(((current_trade['entry_price'] - price) / current_trade['entry_price']) * 100 * LEVERAGE, 2),
                    'win': pnl > 0,
                    'opened_at': current_trade['opened_at'],
                    'closed_at': ts,
                    'note': 'Derived from margin trade history',
                })
                current_trade = None

        elif action == 'SELL':
            if current_trade is None:
                current_trade = {
                    'side': 'SHORT',
                    'entry_price': price,
                    'qty': qty,
                    'entry_fee': fee,
                    'opened_at': ts,
                }
            elif current_trade['side'] == 'LONG':
                exit_qty = min(current_trade['qty'], qty)
                total_fee = current_trade.get('entry_fee', 0.0) + fee
                pnl = (price - current_trade['entry_price']) * exit_qty - total_fee

                closed.append({
                    'source': 'binance',
                    'side': 'LONG',
                    'entry_price': round(current_trade['entry_price'], 8),
                    'exit_price': round(price, 8),
                    'qty': round(exit_qty, 8),
                    'fee': round(total_fee, 8),
                    'pnl': round(pnl, 4),
                    'pnl_pct': round(((price - current_trade['entry_price']) / current_trade['entry_price']) * 100 * LEVERAGE, 2),
                    'win': pnl > 0,
                    'opened_at': current_trade['opened_at'],
                    'closed_at': ts,
                    'note': 'Derived from margin trade history',
                })
                current_trade = None

    return closed


def summarize_performance(closed_trades: list) -> dict:
    total = len(closed_trades)
    wins = sum(1 for t in closed_trades if t.get('win'))
    losses = total - wins
    net_profit = round(sum(float(t.get('pnl', 0)) for t in closed_trades), 4)
    avg_profit = round(net_profit / total, 4) if total else 0.0
    win_rate = round((wins / total) * 100, 2) if total else 0.0

    long_trades = [t for t in closed_trades if t.get('side') == 'LONG']
    short_trades = [t for t in closed_trades if t.get('side') == 'SHORT']

    return {
        'closed_trades': total,
        'wins': wins,
        'losses': losses,
        'win_rate': win_rate,
        'net_profit': net_profit,
        'avg_profit_per_trade': avg_profit,
        'long_trades': len(long_trades),
        'short_trades': len(short_trades),
    }


# ─────────────────────────────────────────────
# 4. MARKET DATA + INDICATORS
# ─────────────────────────────────────────────
def get_market_data(symbol: str, interval: str = '15m', limit: int = 100) -> pd.DataFrame:
    klines = binance_public('/api/v3/klines', {
        'symbol':   symbol,
        'interval': interval,
        'limit':    limit,
    })
    df = pd.DataFrame(klines, columns=[
        'time', 'open', 'high', 'low', 'close', 'volume',
        'close_time', 'quote_volume', 'trades',
        'taker_base', 'taker_quote', 'ignore'
    ])
    df = df.astype({
        'open': float, 'high': float,
        'low':  float, 'close': float, 'volume': float
    })
    df['rsi']       = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
    macd_obj        = ta.trend.MACD(df['close'])
    df['macd']      = macd_obj.macd()
    df['macd_sig']  = macd_obj.macd_signal()
    df['macd_hist'] = macd_obj.macd_diff()
    df['ema_20']    = ta.trend.EMAIndicator(df['close'], window=20).ema_indicator()
    df['ema_50']    = ta.trend.EMAIndicator(df['close'], window=50).ema_indicator()
    bb              = ta.volatility.BollingerBands(df['close'], window=20, window_dev=2)
    df['bb_upper']  = bb.bollinger_hband()
    df['bb_lower']  = bb.bollinger_lband()
    return df


def get_trend_filter(symbol: str) -> str:
    try:
        klines = binance_public('/api/v3/klines', {
            'symbol':   symbol,
            'interval': '1h',
            'limit':    60,
        })
        closes = [float(k[4]) for k in klines]
        df = pd.DataFrame({'close': closes})
        df['ema_15'] = ta.trend.EMAIndicator(df['close'], window=15).ema_indicator()
        df['ema_40'] = ta.trend.EMAIndicator(df['close'], window=40).ema_indicator()
        price = df['close'].iloc[-1]
        e15   = df['ema_15'].iloc[-1]
        e40   = df['ema_40'].iloc[-1]
        if price > e15 > e40:
            logger.info(f'📈 1H: BULLISH (${price:.2f} > EMA15 ${e15:.2f} > EMA40 ${e40:.2f})')
            return 'BULLISH'
        elif price < e15 < e40:
            logger.info(f'📉 1H: BEARISH (${price:.2f} < EMA15 ${e15:.2f} < EMA40 ${e40:.2f})')
            return 'BEARISH'
        else:
            logger.info('↔️  1H: SIDEWAYS')
            return 'SIDEWAYS'
    except Exception as e:
        logger.warning(f'Trend filter failed: {e}')
        return 'NEUTRAL'


def get_current_price(symbol: str) -> float:
    result = binance_public('/api/v3/ticker/price', {'symbol': symbol})
    return float(result['price'])


# ─────────────────────────────────────────────
# 5. DECISION ENGINE
# ─────────────────────────────────────────────
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
        bullish_signals.append("MACD bullish crossover")
    elif curr['macd'] > curr['macd_sig'] and curr['macd_hist'] > 0:
        bullish_signals.append("MACD above signal (bullish)")

    if prev['macd'] > prev['macd_sig'] and curr['macd'] < curr['macd_sig']:
        bearish_signals.append("MACD bearish crossover")
    elif curr['macd'] < curr['macd_sig'] and curr['macd_hist'] < 0:
        bearish_signals.append("MACD below signal (bearish)")

    if curr['ema_20'] > curr['ema_50']:
        bullish_signals.append("EMA-20 above EMA-50 (uptrend)")
    else:
        bearish_signals.append("EMA-20 below EMA-50 (downtrend)")

    if price > curr['ema_20']:
        bullish_signals.append("Price above EMA-20")
    else:
        bearish_signals.append("Price below EMA-20")

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
            bullish_signals.append("High volume confirms move")
        else:
            bearish_signals.append("High volume confirms move")

    bull_count = len(bullish_signals)
    bear_count = len(bearish_signals)
    total = bull_count + bear_count or 1

    trend_up = curr['ema_20'] > curr['ema_50']
    trend_down = curr['ema_20'] < curr['ema_50']

    if bull_count >= MIN_SIGNALS and bull_count > bear_count and trend_up:
        action = 'LONG'
        confidence = int(min(95, 55 + (bull_count / total) * 45))
        risk = 'LOW' if bull_count >= 4 else 'MEDIUM'
        reason = ' | '.join(bullish_signals)

    elif bear_count >= MIN_SIGNALS and bear_count > bull_count and trend_down:
        action = 'SHORT'
        confidence = int(min(95, 55 + (bear_count / total) * 45))
        risk = 'LOW' if bear_count >= 4 else 'MEDIUM'
        reason = ' | '.join(bearish_signals)

    else:
        action = 'HOLD'
        confidence = 0
        risk = 'MEDIUM'

        if bull_count >= MIN_SIGNALS and bull_count > bear_count and not trend_up:
            reason = f"Blocked LONG — trend filter active (EMA-20 <= EMA-50) | {bull_count} bullish, {bear_count} bearish"
        elif bear_count >= MIN_SIGNALS and bear_count > bull_count and not trend_down:
            reason = f"Blocked SHORT — trend filter active (EMA-20 >= EMA-50) | {bull_count} bullish, {bear_count} bearish"
        else:
            reason = f"Mixed signals — {bull_count} bullish, {bear_count} bearish"

    return {
        'action': action,
        'confidence': confidence,
        'risk_level': risk,
        'reason': reason,
        'bullish_signals': bullish_signals,
        'bearish_signals': bearish_signals,
    }


# ─────────────────────────────────────────────
# 6. TRADE EXECUTORS
# ─────────────────────────────────────────────
def open_long(price: float, confidence: int, reason: str) -> bool:
    try:
        step = get_step_size()
        usdt = get_margin_balance('USDT')
        available = usdt['free']

        collateral_usdt = float(TRADE_AMOUNT_USDT)
        gross_usdt      = collateral_usdt * LEVERAGE
        borrow_amt      = round(gross_usdt - collateral_usdt, 2)

        if available < collateral_usdt:
            logger.warning(f'Insufficient USDT: need ${collateral_usdt:.2f}, have ${available:.2f}')
            return False

        if borrow_amt > 0:
            borrowed = borrow_margin('USDT', borrow_amt)
            if not borrowed:
                return False
            time.sleep(1)

        quantity = round_step((gross_usdt * 0.995) / price, step)

        if quantity < 0.01:
            logger.warning(f'Quantity too small: {quantity}')
            return False

        resp = margin_order('BUY', quantity, 'NO_SIDE_EFFECT')
        logger.info(
            f'✅ LONG OPEN: {quantity:.4f} SOL @ ${price:.4f} | '
            f'collateral=${collateral_usdt:.2f} | borrowed=${borrow_amt:.2f} | '
            f'id: {resp.get("orderId")}'
        )
        alert_long_open(price, quantity, confidence, reason, borrow_amt)
        return True

    except Exception as e:
        logger.error(f'open_long failed: {e}')
        alert_error(f'open_long: {e}')
        return False


def close_long(price: float, confidence: int, reason: str) -> bool:
    try:
        step     = get_step_size()
        sol      = get_margin_balance('SOL')
        quantity = round_step(sol['free'], step)

        if quantity < 0.01:
            logger.info('No SOL to sell — long already closed')
            return False

        buy_price = get_last_trade_price('BUY')
        resp = margin_order('SELL', quantity, 'AUTO_REPAY')
        logger.info(f'✅ LONG CLOSE: sold {quantity:.4f} SOL @ ${price:.4f} | id: {resp.get("orderId")}')
        alert_long_close(price, quantity, confidence, reason, buy_price)
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

        collateral_usdt = float(TRADE_AMOUNT_USDT)
        gross_usdt      = collateral_usdt * LEVERAGE

        if available < collateral_usdt:
            logger.warning(f'Insufficient USDT collateral: need ${collateral_usdt:.2f}, have ${available:.2f}')
            return False

        borrow_sol = round_step((gross_usdt * 0.995) / price, step)

        if borrow_sol < 0.01:
            logger.warning(f'Borrow quantity too small: {borrow_sol}')
            return False

        borrowed = borrow_margin('SOL', borrow_sol)
        if not borrowed:
            return False

        time.sleep(1)

        resp = margin_order('SELL', borrow_sol, 'NO_SIDE_EFFECT')
        logger.info(
            f'✅ SHORT OPEN: sold {borrow_sol:.4f} SOL @ ${price:.4f} | '
            f'collateral=${collateral_usdt:.2f} | effective=${gross_usdt:.2f} | '
            f'id: {resp.get("orderId")}'
        )
        alert_short_open(price, borrow_sol, confidence, reason, borrow_sol)
        return True

    except Exception as e:
        logger.error(f'open_short failed: {e}')
        alert_error(f'open_short: {e}')
        return False


def close_short(price: float, confidence: int, reason: str) -> bool:
    try:
        step = get_step_size()
        sol = get_margin_balance('SOL')
        borrowed_total = sol['borrowed'] + sol['interest']

        if borrowed_total < 0.0001:
            logger.info('No borrowed SOL to repay — short already closed')
            cleanup_orphan_short_borrow()
            return False

        sell_price = get_last_trade_price('SELL')

        quantity = round_step(borrowed_total * SHORT_CLOSE_BUFFER, step)
        if quantity < borrowed_total:
            quantity = round_step(borrowed_total + step, step)

        resp = margin_order('BUY', quantity, 'AUTO_REPAY')
        logger.info(
            f'✅ SHORT CLOSE: bought {quantity:.4f} SOL @ ${price:.4f} | '
            f'borrowed_total={borrowed_total:.8f} | id: {resp.get("orderId")}'
        )

        time.sleep(2)
        cleanup_orphan_short_borrow()

        sol_after = get_margin_balance('SOL')
        remaining_borrow = sol_after['borrowed'] + sol_after['interest']
        logger.info(f'🔎 Remaining SOL borrowed after close attempt: {remaining_borrow:.8f}')

        alert_short_close(price, quantity, confidence, reason, sell_price)
        return remaining_borrow < 0.01

    except Exception as e:
        logger.error(f'close_short failed: {e}')
        alert_error(f'close_short: {e}')
        return False


# ─────────────────────────────────────────────
# 7. SAFETY CHECK
# ─────────────────────────────────────────────
def check_margin_safety(current_position: Optional[str]) -> tuple[bool, float]:
    """
    Returns:
        can_open_new_positions: bool
        level: float

    Behavior:
    - If margin is low and there is NO active position -> block new entries.
    - If margin is low and there IS an active position -> continue managing that position,
      but do not spam Telegram repeatedly.
    """
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
                if current_position:
                    msg = (
                        f'⚠️ MARGIN LEVEL LOW: {level:.2f} — existing {current_position} position still being managed, '
                        f'but no new trades will be opened until margin improves.'
                    )
                else:
                    msg = f'⚠️ MARGIN LEVEL LOW: {level:.2f} — pausing new entries to avoid liquidation!'
                logger.warning(msg)
                send_telegram(msg)
                last_margin_alert_ts = now_ts
                last_margin_alert_level = level
            else:
                logger.warning(
                    f'⚠️ Margin still low at {level:.2f}, alert suppressed '
                    f'(cooldown {LOW_MARGIN_ALERT_COOLDOWN}s)'
                )

            return False, level

        if last_margin_alert_level is not None and level >= LOW_MARGIN_THRESHOLD:
            logger.info(f'✅ Margin recovered above threshold: {level:.2f}')
            last_margin_alert_level = None

        return True, level

    except Exception as e:
        logger.warning(f'Safety check failed: {e}')
        return True, 999.0


# ─────────────────────────────────────────────
# 8. DASHBOARD
# ─────────────────────────────────────────────
def push_dashboard_data(data: dict) -> None:
    if not GH_TOKEN or not DASHBOARD_REPO:
        logger.warning('Dashboard push skipped: GH_TOKEN or DASHBOARD_REPO missing')
        return
    try:
        url = f'https://api.github.com/repos/{DASHBOARD_REPO}/contents/data_binance_sol.json'
        headers = {
            'Authorization': f'token {GH_TOKEN}',
            'Accept':        'application/vnd.github+json',
        }
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
            logger.warning(f'Dashboard push failed: {r.status_code} | body={r.text}')
    except Exception as e:
        logger.warning(f'Dashboard error: {e}')


# ─────────────────────────────────────────────
# 9. MAIN LOOP
# ─────────────────────────────────────────────
def run_once():
    global run_count, last_hold_alert, current_position
    run_count += 1

    logger.info('=' * 60)
    logger.info(f'  APEX BINANCE MARGIN SOL — Run #{run_count}')
    logger.info(f'  {SYMBOL} | {LEVERAGE}x | ${TRADE_AMOUNT_USDT} USDT collateral')
    logger.info('=' * 60)

    try:
        current_position = detect_position()
        can_open_new_positions, margin_level = check_margin_safety(current_position)

        cleanup_orphan_short_borrow()

        df    = get_market_data(SYMBOL)
        price = get_current_price(SYMBOL)
        logger.info(f'💰 {SYMBOL}: ${price:.4f}')

        decision   = get_decision(df, price)
        action     = decision['action']
        confidence = decision['confidence']
        risk       = decision['risk_level']
        reason     = decision['reason']

        logger.info(f'📈 Signal: {action} | {confidence}% | {risk}')
        logger.info(f'🟢 Bullish: {decision["bullish_signals"]}')
        logger.info(f'🔴 Bearish: {decision["bearish_signals"]}')
        logger.info(f'📍 Position: {current_position}')

        trend  = get_trend_filter(SYMBOL)
        status = ''

        if trend == 'SIDEWAYS' and action in ('LONG', 'SHORT') and confidence < 80:
            action = 'HOLD'
            status = f'SKIPPED — sideways + confidence {confidence}% < 80%'
            logger.info(f'⛔ {status}')

        if current_position is None:
            if not can_open_new_positions and action in ('LONG', 'SHORT'):
                status = f'BLOCKED — low margin level {margin_level:.2f}, no new entries'
                logger.info(f'⛔ {status}')

            elif action == 'LONG' and risk != 'HIGH':
                logger.info('🟢 Opening LONG...')
                ok     = open_long(price, confidence, reason)
                status = 'LONG OPENED ✅' if ok else 'LONG FAILED ❌'

            elif action == 'SHORT' and risk != 'HIGH':
                logger.info('🔴 Opening SHORT...')
                ok     = open_short(price, confidence, reason)
                status = 'SHORT OPENED ✅' if ok else 'SHORT FAILED ❌'

            else:
                status = f'HOLD — {reason}'
                logger.info(f'🔒 {status}')
                now_ts = time.time()
                if now_ts - last_hold_alert > 1800:
                    send_telegram(
                        f'🔒 <b>APEX — HOLD</b>\n'
                        f'💰 SOL: ${price:.4f}\n'
                        f'🟢 {len(decision["bullish_signals"])} bullish | '
                        f'🔴 {len(decision["bearish_signals"])} bearish\n'
                        f'💬 {reason}'
                    )
                    last_hold_alert = now_ts

        elif current_position == 'LONG':
            if action == 'SHORT':
                logger.info('🔄 Flipping LONG → SHORT...')
                closed = close_long(price, confidence, 'Flipping to SHORT — ' + reason)
                if closed:
                    time.sleep(3)
                    if can_open_new_positions:
                        ok     = open_short(price, confidence, reason)
                        status = 'FLIPPED LONG→SHORT ✅' if ok else 'CLOSED LONG, SHORT FAILED'
                    else:
                        status = f'CLOSED LONG ONLY — low margin {margin_level:.2f} blocked new SHORT'
                        logger.info(f'⛔ {status}')
                else:
                    status = 'LONG CLOSE FAILED ❌'
            else:
                status = f'HOLDING LONG — {reason}'
                logger.info(f'📍 {status}')

        elif current_position == 'SHORT':
            if action == 'LONG':
                logger.info('🔄 Flipping SHORT → LONG...')
                closed = close_short(price, confidence, 'Flipping to LONG — ' + reason)
                if closed:
                    time.sleep(3)
                    if can_open_new_positions:
                        ok     = open_long(price, confidence, reason)
                        status = 'FLIPPED SHORT→LONG ✅' if ok else 'CLOSED SHORT, LONG FAILED'
                    else:
                        status = f'CLOSED SHORT ONLY — low margin {margin_level:.2f} blocked new LONG'
                        logger.info(f'⛔ {status}')
                else:
                    status = 'SHORT CLOSE FAILED ❌'
            else:
                status = f'HOLDING SHORT — {reason}'
                logger.info(f'📍 {status}')

        usdt_balance = 0.0
        sol_balance  = 0.0
        try:
            usdt_b       = get_margin_balance('USDT')
            sol_b        = get_margin_balance(BASE_ASSET)
            usdt_balance = usdt_b['net']
            sol_balance  = sol_b['net']
            margin_level = get_margin_level()
        except Exception as e:
            logger.warning(f'Balance fetch failed: {e}')

        trade_history = []
        closed_trades = []
        performance = {
            'closed_trades': 0,
            'wins': 0,
            'losses': 0,
            'win_rate': 0.0,
            'net_profit': 0.0,
            'avg_profit_per_trade': 0.0,
            'long_trades': 0,
            'short_trades': 0,
        }

        try:
            trade_history = get_trade_history()
            derived_closed_trades = build_closed_trades_from_history(trade_history)
            manual_closed_trades = load_manual_closed_trades()

            closed_trades = sorted(
                derived_closed_trades + manual_closed_trades,
                key=lambda x: x.get('closed_at') or ''
            )
            performance = summarize_performance(closed_trades)
        except Exception as e:
            logger.warning(f'Trade history/performance failed: {e}')

        latest_position = detect_position()

        push_dashboard_data({
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'symbol':       SYMBOL,
            'exchange':     'Binance Margin',
            'leverage':     LEVERAGE,
            'price':        price,
            'usdt_balance': usdt_balance,
            'sol_balance':  sol_balance,
            'btc_balance':  sol_balance,
            'cad_balance':  usdt_balance,
            'margin_level': margin_level,
            'position':     latest_position,
            'action':       action,
            'confidence':   confidence,
            'risk':         risk,
            'status':       status,
            'reason':       reason,
            'trend':        trend,
            'bullish':      decision['bullish_signals'],
            'bearish':      decision['bearish_signals'],
            'trades':       trade_history,
            'closed_trades': closed_trades,
            'performance':  performance,
            'run_count':    run_count,
            'trade_amount': TRADE_AMOUNT_USDT,
            'min_signals':  MIN_SIGNALS,
        })

        logger.info(
            f'✅ Run #{run_count} — {action} | ${price:.4f} | '
            f'pos: {latest_position} | margin: {margin_level:.2f} | '
            f'win_rate: {performance["win_rate"]:.2f}% | net: {performance["net_profit"]:.4f}'
        )

    except Exception as e:
        logger.error(f'❌ Bot error: {e}', exc_info=True)
        alert_error(str(e))


def main():
    logger.info('🚀 APEX Binance Margin SOL Bot starting...')
    logger.info(f'   Symbol:      {SYMBOL}')
    logger.info(f'   Leverage:    {LEVERAGE}x Cross Margin')
    logger.info(f'   Collateral:  ${TRADE_AMOUNT_USDT} USDT')
    logger.info(f'   Effective:   ${TRADE_AMOUNT_USDT * LEVERAGE:.0f} USDT per trade')
    logger.info(f'   Interval:    every {CHECK_INTERVAL}s')
    logger.info(f'   Longs ✅  Shorts ✅  Trailing stop ❌')
    logger.info(f'   Manual file: {MANUAL_TRADES_FILE}')
    logger.info(f'   Low margin threshold: {LOW_MARGIN_THRESHOLD:.2f}')
    logger.info(f'   Margin alert cooldown: {LOW_MARGIN_ALERT_COOLDOWN}s')
    logger.info(f'   API Key:     {mask_secret(BINANCE_API_KEY)}')
    logger.info(f'   API Secret:  {mask_secret(BINANCE_API_SECRET)}')
    logger.info(f'   GH Token:    {mask_secret(GH_TOKEN)}')
    logger.info(f'   Dashboard:   {DASHBOARD_REPO or "MISSING"}')
    logger.info(f'   Public IP:   {get_public_ip()}')
    logger.info('=' * 60)

    send_telegram(
        f'🚀 <b>APEX Binance Margin Bot Started</b>\n\n'
        f'📌 Symbol:     {SYMBOL}\n'
        f'🔥 Leverage:   {LEVERAGE}x Cross Margin\n'
        f'💵 Collateral: ${TRADE_AMOUNT_USDT} USDT\n'
        f'💪 Effective:  ${TRADE_AMOUNT_USDT * LEVERAGE:.0f} USDT per trade\n'
        f'🟢 Longs + 🔴 Shorts enabled\n'
        f'🚫 No trailing stop — signals only\n'
        f'🛡 Low-margin alerts throttled ({LOW_MARGIN_ALERT_COOLDOWN}s cooldown)\n'
        f'⏱ Every {CHECK_INTERVAL} seconds\n'
        f'⏰ {datetime.now(MST).strftime("%b %d, %I:%M %p MST")}'
    )

    run_startup_binance_tests()

    while True:
        try:
            run_once()
        except Exception as e:
            logger.error(f'Main loop error: {e}', exc_info=True)
            alert_error(f'Main loop crashed: {e}')
        logger.info(f'⏳ Sleeping {CHECK_INTERVAL}s...')
        time.sleep(CHECK_INTERVAL)


if __name__ == '__main__':
    main()
