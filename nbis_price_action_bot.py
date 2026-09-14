#!/usr/bin/env python3
"""
Sentinel — multi-ticker price-action bot, standalone (does not import trading_bot_futures.py).

Each ticker runs its own independently-validated strategy on the same shared
process, Telegram/Discord channel, and dashboard:

  NBISUSDT — 1H-aggregated S/R + engulfing entry + 100-MA trend filter
    Backtest (93 days, 15-min bars, after fees): 20/20 param combos positive,
    positive every calendar month tested, out-of-sample holdout +5.93%.

  RKLBUSDT — 30-min-aggregated S/R + plain engulfing entry, NO trend filter
    Backtest (full available history since 2026-05-18, after fees): 24/25
    param combos positive, positive on both a 65%-train and 35%-test split
    evaluated independently, net +$53.26 on $20@20x sizing over ~4 months.

Runs on the SAME Binance account as the main Apex futures bot. Every symbol
here checks the REAL LIVE position on Binance (not just its own internal
state) before opening anything — if Apex or another process already has a
position open on that symbol, this bot skips its turn for that symbol. No
shared lock file needed — Binance's own account state is the single source
of truth.
"""

import os, sys, time, json, hmac, hashlib, logging, requests
from datetime import datetime, timezone
from typing import Optional

# ── Environment ───────────────────────────────────────────────────────────────
FUTURES_BASE_URL    = 'https://fapi.binance.com'
BINANCE_API_KEY     = os.environ.get('BINANCE_API_KEY', '').strip()
BINANCE_API_SECRET  = os.environ.get('BINANCE_API_SECRET', '').strip()
DISCORD_WEBHOOK_URL = os.environ.get('DISCORD_WEBHOOK_URL', '').strip()
TELEGRAM_BOT_TOKEN  = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
TELEGRAM_CHAT_ID    = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
WEB_ROOT             = os.environ.get('WEB_ROOT', '/var/www/apex').strip()
STATE_FILE           = os.environ.get('NBIS_PA_STATE_FILE', 'nbis_pa_state.json').strip()
DASHBOARD_FILE       = os.path.join(WEB_ROOT, 'data_nbis_pa.json')
JOURNAL_FILE         = os.environ.get('SENTINEL_JOURNAL_FILE', 'sentinel_trade_journal.jsonl').strip()

# ── Per-symbol strategy config (each validated independently by backtest) ─────
SYMBOLS_CONFIG = {
    'NBISUSDT': {
        'base': 'NBIS', 'interval': '15m', 'agg': 4, 'ma_period': 100, 'swing_lookback': 3,
        'proximity_pct': 0.20, 'rr_target': 1.8, 'max_hold_bars': 16,
        'trade_amount': 20.0, 'leverage': 20, 'use_trend_filter': True,
        'strategy_label': '1H S/R + engulfing + trend filter (MA100)',
    },
    'RKLBUSDT': {
        'base': 'RKLB', 'interval': '15m', 'agg': 2, 'ma_period': 100, 'swing_lookback': 3,
        'proximity_pct': 0.20, 'rr_target': 1.8, 'max_hold_bars': 16,
        'trade_amount': 20.0, 'leverage': 20, 'use_trend_filter': False,
        'strategy_label': '30min S/R + plain engulfing (no trend filter)',
    },
}

FEE_RATE = 0.0005  # 0.05% taker fee, matches Apex
DUST_QTY_THRESHOLD = 0.1  # matches Apex's own dust convention — a leftover
                          # sub-0.1 remainder from a prior close shouldn't
                          # permanently block either bot from trading again

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('sentinel')


# ── State persistence ──────────────────────────────────────────────────────────
def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
    except Exception:
        data = {}

    # Migrate legacy single-symbol (NBIS-only) state format to the multi-symbol one.
    if 'position' in data or 'positions' not in data:
        old_pos = data.get('position')
        old_trades = data.get('trades', [])
        data = {
            'positions': {'NBISUSDT': old_pos},
            'trades': [{**t, 'symbol': t.get('symbol', 'NBISUSDT')} for t in old_trades],
            'run_count': data.get('run_count', 0),
        }

    data.setdefault('positions', {})
    data.setdefault('trades', [])
    data.setdefault('run_count', 0)
    for sym in SYMBOLS_CONFIG:
        data['positions'].setdefault(sym, None)
    return data

def save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logger.warning(f'save_state failed: {e}')

state = load_state()
LATEST_PRICE: dict = {}


# ── Trade journal — append-only, one JSON line per closed trade, never trimmed ─
# This is separate from state['trades'] (which the dashboard uses and keeps
# only the last 30 for payload size). The journal keeps everything forever
# with full entry context, so losing patterns can be analyzed later even
# after months of trading.
def log_trade_to_journal(record: dict) -> None:
    try:
        with open(JOURNAL_FILE, 'a') as f:
            f.write(json.dumps(record) + '\n')
    except Exception as e:
        logger.warning(f'log_trade_to_journal failed: {e}')

def load_journal() -> list:
    records = []
    try:
        with open(JOURNAL_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except Exception:
                    continue
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning(f'load_journal failed: {e}')
    return records


# ── Notifications — same Discord + Telegram channels as the rest of the fleet ──
def notify(msg: str) -> None:
    if DISCORD_WEBHOOK_URL:
        try:
            requests.post(DISCORD_WEBHOOK_URL, json={'content': '───────────────────\n' + msg}, timeout=10)
        except Exception as e:
            logger.warning(f'discord send failed: {e}')
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            text = msg.replace('**', '*')
            r = requests.post(
                f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage',
                json={'chat_id': TELEGRAM_CHAT_ID, 'text': text, 'parse_mode': 'Markdown'},
                timeout=10,
            )
            if not r.json().get('ok'):
                requests.post(
                    f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage',
                    json={'chat_id': TELEGRAM_CHAT_ID, 'text': text},
                    timeout=10,
                )
        except Exception as e:
            logger.warning(f'telegram send failed: {e}')

def alert_error(base: str, err: str) -> None:
    notify(f'❌ **Sentinel [{base}] ERROR**\n\n{err}')


# ── Binance API helpers ─────────────────────────────────────────────────────────
def binance_futures_public(endpoint: str, params: dict = None) -> dict:
    r = requests.get(FUTURES_BASE_URL + endpoint, params=params or {}, timeout=15)
    r.raise_for_status()
    return r.json()

def binance_futures_private(method: str, endpoint: str, params: dict = None) -> dict:
    params = dict(params or {})
    params['timestamp'] = int(time.time() * 1000)
    query = '&'.join(f'{k}={v}' for k, v in params.items())
    sig = hmac.new(BINANCE_API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f'{FUTURES_BASE_URL}{endpoint}?{query}&signature={sig}'
    headers = {'X-MBX-APIKEY': BINANCE_API_KEY}
    r = requests.request(method, url, headers=headers, timeout=15)
    if not r.ok:
        raise RuntimeError(f'{method} {endpoint} -> {r.status_code}: {r.text}')
    return r.json()

def get_futures_balance(asset: str = 'USDT') -> dict:
    try:
        balances = binance_futures_private('GET', '/fapi/v2/balance')
        for b in balances:
            if b['asset'] == asset:
                return {'free': float(b['availableBalance']), 'total': float(b['balance'])}
    except Exception as e:
        logger.warning(f'get_futures_balance: {e}')
    return {'free': 0.0, 'total': 0.0}

def get_live_position(symbol: str) -> dict:
    """Real, current Binance account position for `symbol` — the single source
    of truth both this bot and Apex check before opening anything new."""
    try:
        positions = binance_futures_private('GET', '/fapi/v2/positionRisk', {'symbol': symbol})
        for p in positions:
            if p['symbol'] != symbol:
                continue
            amt = float(p['positionAmt'])
            if abs(amt) < DUST_QTY_THRESHOLD:
                return {'side': None, 'qty': 0.0, 'entry_price': None, 'unrealized_pnl': 0.0}
            return {
                'side': 'LONG' if amt > 0 else 'SHORT',
                'qty': abs(amt),
                'entry_price': float(p['entryPrice']),
                'unrealized_pnl': float(p['unRealizedProfit']),
            }
    except Exception as e:
        logger.warning(f'get_live_position({symbol}): {e}')
    return {'side': None, 'qty': 0.0, 'entry_price': None, 'unrealized_pnl': 0.0}

def set_leverage(symbol: str, leverage: int) -> None:
    try:
        binance_futures_private('POST', '/fapi/v1/leverage', {'symbol': symbol, 'leverage': leverage})
    except Exception as e:
        logger.warning(f'set_leverage({symbol}): {e}')

def get_step_size(symbol: str) -> float:
    info = binance_futures_public('/fapi/v1/exchangeInfo')
    for s in info['symbols']:
        if s['symbol'] == symbol:
            for f in s['filters']:
                if f['filterType'] == 'LOT_SIZE':
                    return float(f['stepSize'])
    return 0.001

def round_step(qty: float, step: float) -> float:
    precision = max(0, len(str(step).split('.')[-1].rstrip('0'))) if '.' in str(step) else 0
    return round(qty - (qty % step), precision) if step else qty

def market_order(symbol: str, side: str, quantity: float) -> dict:
    return binance_futures_private('POST', '/fapi/v1/order', {
        'symbol': symbol, 'side': side, 'type': 'MARKET', 'quantity': quantity,
    })

def get_fill_price(resp: dict, fallback: float) -> float:
    try:
        return float(resp.get('avgPrice') or fallback) or fallback
    except Exception:
        return fallback

def get_current_price(symbol: str) -> float:
    d = binance_futures_public('/fapi/v1/ticker/price', {'symbol': symbol})
    return float(d['price'])


# ── Market data + indicators ────────────────────────────────────────────────────
def get_candles(symbol: str, interval: str, limit: int = 500) -> list:
    raw = binance_futures_public('/fapi/v1/klines', {'symbol': symbol, 'interval': interval, 'limit': limit})
    return [{'time': k[0], 'open': float(k[1]), 'high': float(k[2]), 'low': float(k[3]),
             'close': float(k[4]), 'volume': float(k[5])} for k in raw]

def find_swing_points(bars: list, lookback: int) -> tuple:
    n = len(bars)
    highs, lows = [], []
    for i in range(lookback, n - lookback):
        wh = [bars[j]['high'] for j in range(i - lookback, i + lookback + 1)]
        wl = [bars[j]['low']  for j in range(i - lookback, i + lookback + 1)]
        if bars[i]['high'] == max(wh): highs.append((i, bars[i]['high']))
        if bars[i]['low']  == min(wl): lows.append((i, bars[i]['low']))
    return highs, lows

def body(c): return abs(c['close'] - c['open'])
def bull(c): return c['close'] > c['open']
def bear(c): return c['close'] < c['open']

def is_bullish_engulfing(prev, cur):
    return (bear(prev) and bull(cur) and cur['close'] >= prev['open']
            and cur['open'] <= prev['close'] and body(cur) > body(prev))

def is_bearish_engulfing(prev, cur):
    return (bull(prev) and bear(cur) and cur['open'] >= prev['close']
            and cur['close'] <= prev['open'] and body(cur) > body(prev))

def sma(candles, i, period):
    if i < period: return None
    return sum(c['close'] for c in candles[i - period:i]) / period

def build_htf_levels(candles: list, agg: int, swing_lookback: int) -> tuple:
    """Aggregate 15-min bars into `agg`-bar blocks and find swing highs/lows
    on those, mapped back to the original candle index."""
    htf = []
    for i in range(0, len(candles) - agg, agg):
        chunk = candles[i:i + agg]
        htf.append({'high': max(c['high'] for c in chunk), 'low': min(c['low'] for c in chunk),
                    'orig_idx_end': i + agg - 1})
    highs, lows = find_swing_points([{'high': h['high'], 'low': h['low']} for h in htf], swing_lookback)
    high_levels = [(htf[idx]['orig_idx_end'], price) for idx, price in highs]
    low_levels  = [(htf[idx]['orig_idx_end'], price) for idx, price in lows]
    return high_levels, low_levels


def get_decision(candles: list, cfg: dict) -> Optional[dict]:
    """Evaluate `cfg`'s strategy on the LATEST closed candle. Returns a signal
    dict if a valid entry condition is met right now, else None."""
    i = len(candles) - 1
    min_bars = max(200, cfg['ma_period'] + 5) if cfg['use_trend_filter'] else 60
    if i < min_bars:
        return None
    cur, prev = candles[i], candles[i - 1]

    if cfg['use_trend_filter']:
        ma = sma(candles, i, cfg['ma_period'])
        if ma is None:
            return None
        trend_up, trend_down = cur['close'] > ma, cur['close'] < ma
    else:
        trend_up = trend_down = True

    high_levels, low_levels = build_htf_levels(candles, cfg['agg'], cfg['swing_lookback'])
    recent_lows  = [p for (idx, p) in low_levels  if idx < i and idx > i - 200]
    recent_highs = [p for (idx, p) in high_levels if idx < i and idx > i - 200]
    if not recent_lows or not recent_highs:
        return None
    nearest_support    = max([p for p in recent_lows  if p <= cur['close'] * 1.03], default=None)
    nearest_resistance = min([p for p in recent_highs if p >= cur['close'] * 0.97], default=None)

    bull_sig = is_bullish_engulfing(prev, cur)
    bear_sig = is_bearish_engulfing(prev, cur)
    prox, rr = cfg['proximity_pct'], cfg['rr_target']
    trend_note = f', trend up (MA{cfg["ma_period"]})' if cfg['use_trend_filter'] else ''
    trend_note_down = f', trend down (MA{cfg["ma_period"]})' if cfg['use_trend_filter'] else ''

    if trend_up and nearest_support and bull_sig:
        dist_pct = abs(cur['low'] - nearest_support) / nearest_support * 100
        if dist_pct <= prox:
            stop = min(cur['low'], prev['low']) * 0.999
            risk = cur['close'] - stop
            if risk > 0:
                return {'side': 'LONG', 'entry': cur['close'], 'stop': stop,
                        'target': cur['close'] + risk * rr,
                        'reason': f'Bullish engulfing at S/R ${nearest_support:.2f}{trend_note}',
                        'pattern': 'engulfing', 'sr_level': nearest_support, 'sr_distance_pct': round(dist_pct, 4),
                        'trend_aligned': True if cfg['use_trend_filter'] else None,
                        'entry_hour_utc': datetime.fromtimestamp(cur['time'] / 1000, tz=timezone.utc).hour,
                        'entry_weekday': datetime.fromtimestamp(cur['time'] / 1000, tz=timezone.utc).strftime('%A')}

    if trend_down and nearest_resistance and bear_sig:
        dist_pct = abs(cur['high'] - nearest_resistance) / nearest_resistance * 100
        if dist_pct <= prox:
            stop = max(cur['high'], prev['high']) * 1.001
            risk = stop - cur['close']
            if risk > 0:
                return {'side': 'SHORT', 'entry': cur['close'], 'stop': stop,
                        'target': cur['close'] - risk * rr,
                        'reason': f'Bearish engulfing at S/R ${nearest_resistance:.2f}{trend_note_down}',
                        'pattern': 'engulfing', 'sr_level': nearest_resistance, 'sr_distance_pct': round(dist_pct, 4),
                        'trend_aligned': True if cfg['use_trend_filter'] else None,
                        'entry_hour_utc': datetime.fromtimestamp(cur['time'] / 1000, tz=timezone.utc).hour,
                        'entry_weekday': datetime.fromtimestamp(cur['time'] / 1000, tz=timezone.utc).strftime('%A')}
    return None


# ── Trading actions ──────────────────────────────────────────────────────────
def open_position(symbol: str, cfg: dict, signal: dict) -> bool:
    base = cfg['base']
    live = get_live_position(symbol)
    if live['side'] is not None:
        logger.info(f'[Sentinel:{base}] Skipping entry — live position already open ({live["side"]}), '
                    f'likely opened by Apex. Coordination check working as intended.')
        return False

    try:
        leverage, trade_amount = cfg['leverage'], cfg['trade_amount']
        set_leverage(symbol, leverage)
        step = get_step_size(symbol)
        price = get_current_price(symbol)
        quantity = round_step((trade_amount * leverage * 0.995) / price, step)
        if quantity < step:
            logger.warning(f'[Sentinel:{base}] Quantity too small, skipping')
            return False

        balance = get_futures_balance('USDT')
        if balance['free'] < trade_amount:
            logger.warning(f'[Sentinel:{base}] Insufficient balance: need ${trade_amount}, have ${balance["free"]:.2f}')
            return False

        side = 'BUY' if signal['side'] == 'LONG' else 'SELL'
        resp = market_order(symbol, side, quantity)
        actual_price = get_fill_price(resp, price)
        qty_filled = float(resp.get('executedQty') or 0) or quantity

        state['positions'][symbol] = {
            'side': signal['side'], 'entry_price': actual_price, 'qty': qty_filled,
            'stop': signal['stop'], 'target': signal['target'],
            'opened_at': datetime.now(timezone.utc).isoformat(),
            'entry_fee': qty_filled * actual_price * FEE_RATE,
            # entry context, carried through to the journal record on close —
            # this is what lets losing trades be analyzed for a pattern later
            'pattern': signal.get('pattern'),
            'reason': signal.get('reason'),
            'sr_level': signal.get('sr_level'),
            'sr_distance_pct': signal.get('sr_distance_pct'),
            'trend_aligned': signal.get('trend_aligned'),
            'entry_hour_utc': signal.get('entry_hour_utc'),
            'entry_weekday': signal.get('entry_weekday'),
            'rr_target': cfg['rr_target'],
        }
        save_state(state)
        logger.info(f'[Sentinel:{base}] {signal["side"]} opened @ {actual_price:.4f} | stop={signal["stop"]:.4f} target={signal["target"]:.4f}')
        notify(
            f'🎯 **Sentinel [{base}] — {signal["side"]} OPENED**\n\n'
            f'💰 Entry: ${actual_price:,.4f}\n'
            f'🛑 Stop: ${signal["stop"]:,.4f}\n'
            f'🎯 Target: ${signal["target"]:,.4f}\n'
            f'📊 {signal["reason"]}\n'
            f'💵 Size: ${trade_amount} @ {leverage}x'
        )
        return True
    except Exception as e:
        logger.error(f'[Sentinel:{base}] open_position failed: {e}')
        alert_error(base, f'Open position failed: {e}')
        return False

def close_position(symbol: str, cfg: dict, reason: str) -> bool:
    base = cfg['base']
    pos = state['positions'].get(symbol)
    if not pos:
        return False
    try:
        live = get_live_position(symbol)
        if live['side'] is None:
            logger.warning(f'[Sentinel:{base}] No live position found to close — clearing local state')
            state['positions'][symbol] = None
            save_state(state)
            return False

        close_side = 'SELL' if pos['side'] == 'LONG' else 'BUY'
        resp = market_order(symbol, close_side, live['qty'])
        price = get_current_price(symbol)
        actual_close = get_fill_price(resp, price)

        entry = pos['entry_price']
        qty = pos['qty']
        exit_fee = qty * actual_close * FEE_RATE
        total_fee = pos.get('entry_fee', 0.0) + exit_fee
        gross = (actual_close - entry) * qty if pos['side'] == 'LONG' else (entry - actual_close) * qty
        net = gross - total_fee

        closed_at_dt = datetime.now(timezone.utc)
        today = closed_at_dt.strftime('%Y-%m-%d')
        opened_at_dt = datetime.fromisoformat(pos['opened_at'])
        hold_minutes = round((closed_at_dt - opened_at_dt).total_seconds() / 60, 1)
        initial_risk = abs(entry - pos['stop']) * qty
        r_multiple = round(net / initial_risk, 3) if initial_risk > 0 else None

        trade_record = {
            'symbol': symbol, 'base': base, 'side': pos['side'],
            'entry_price': entry, 'exit_price': actual_close, 'qty': qty,
            'stop': pos.get('stop'), 'target': pos.get('target'), 'rr_target': pos.get('rr_target'),
            'pnl': round(net, 4), 'win': net > 0, 'r_multiple': r_multiple,
            'exit_reason': reason,
            'pattern': pos.get('pattern'), 'entry_reason': pos.get('reason'),
            'sr_level': pos.get('sr_level'), 'sr_distance_pct': pos.get('sr_distance_pct'),
            'trend_aligned': pos.get('trend_aligned'),
            'entry_hour_utc': pos.get('entry_hour_utc'), 'entry_weekday': pos.get('entry_weekday'),
            'hold_minutes': hold_minutes,
            'opened_at': pos['opened_at'], 'closed_at': closed_at_dt.isoformat(),
            'date': today,
        }
        state.setdefault('trades', []).append(trade_record)
        state['positions'][symbol] = None
        save_state(state)
        log_trade_to_journal(trade_record)

        emoji = '🟢' if net >= 0 else '🔴'
        notify(
            f'{emoji} **Sentinel [{base}] — CLOSED ({reason})**\n\n'
            f'{pos["side"]} ${entry:,.4f} → ${actual_close:,.4f}\n'
            f'Net P&L: **${net:+.2f}**'
        )
        logger.info(f'[Sentinel:{base}] Closed {pos["side"]} @ {actual_close:.4f} net={net:+.2f} reason={reason}')
        return True
    except Exception as e:
        logger.error(f'[Sentinel:{base}] close_position failed: {e}')
        alert_error(base, f'Close position failed: {e}')
        return False


# ── Dashboard output ────────────────────────────────────────────────────────────
def write_dashboard() -> None:
    symbols_payload = {}
    for symbol, cfg in SYMBOLS_CONFIG.items():
        base = cfg['base']
        pos = state['positions'].get(symbol)
        trades = [t for t in state.get('trades', []) if t.get('symbol') == symbol]
        wins = [t for t in trades if t['win']]
        losses = [t for t in trades if not t['win']]
        total = len(trades)

        current_price = LATEST_PRICE.get(symbol)
        unrealized = None
        if pos and current_price:
            unrealized = round((current_price - pos['entry_price']) * pos['qty'], 2) if pos['side'] == 'LONG' \
                else round((pos['entry_price'] - current_price) * pos['qty'], 2)

        daily_pnl = {}
        for t in trades:
            d = t.get('date') or t['closed_at'][:10]
            daily_pnl[d] = round(daily_pnl.get(d, 0) + t['pnl'], 2)

        symbols_payload[base] = {
            'symbol': symbol,
            'strategy_label': cfg['strategy_label'],
            'price': current_price,
            'position': pos['side'] if pos else None,
            'entry_price': pos['entry_price'] if pos else None,
            'stop': pos['stop'] if pos else None,
            'target': pos['target'] if pos else None,
            'unrealized_pnl': unrealized,
            'trade_amount': cfg['trade_amount'],
            'leverage': cfg['leverage'],
            'performance': {
                'total': total, 'wins': len(wins), 'losses': len(losses),
                'win_rate': round(len(wins) / total * 100, 1) if total else 0,
                'net_pnl': round(sum(t['pnl'] for t in trades), 2),
            },
            'daily_pnl': daily_pnl,
            'trades': list(reversed(trades[-30:])),
        }

    payload = {'generated_at': datetime.now(timezone.utc).isoformat(), 'symbols': symbols_payload}
    try:
        with open(DASHBOARD_FILE, 'w') as f:
            json.dump(payload, f, indent=2)
    except Exception as e:
        logger.warning(f'write_dashboard failed: {e}')


# ── Weekly trade review — read-only self-analysis, no auto-tuning ──────────
# Mirrors Apex's weekly ATR health check: gated to run once every N days,
# safe to call every slow-loop cycle, never touches trading logic. Buckets
# every journaled trade by hour/weekday/side/exit-reason/hold-time/S-R
# proximity and flags whichever conditions are dragging down that ticker's
# win rate or net P&L — purely informational, a human decides what (if
# anything) to change in SYMBOLS_CONFIG.
TRADE_REVIEW_INTERVAL_DAYS = 7
TRADE_REVIEW_MIN_TRADES    = 10   # per ticker, before bothering to bucket anything
TRADE_REVIEW_MIN_BUCKET_N  = 5    # ignore buckets too small to mean anything

def _bucket_stats(records: list, key_fn, min_n: int = TRADE_REVIEW_MIN_BUCKET_N) -> dict:
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
        wins = len([r for r in rs if r['win']])
        net = sum(r['pnl'] for r in rs)
        r_mults = [r['r_multiple'] for r in rs if r.get('r_multiple') is not None]
        avg_r = sum(r_mults) / len(r_mults) if r_mults else None
        stats[k] = {'n': n, 'win_rate': round(wins / n * 100, 1), 'net_pnl': round(net, 2),
                    'avg_r': round(avg_r, 2) if avg_r is not None else None}
    return stats

def _hour_bucket(r):
    h = r.get('entry_hour_utc')
    if h is None: return None
    if 0 <= h < 6:   return 'night (00-06 UTC)'
    if 6 <= h < 12:  return 'morning (06-12 UTC)'
    if 12 <= h < 18: return 'afternoon (12-18 UTC)'
    return 'evening (18-24 UTC)'

def run_weekly_trade_review() -> None:
    last = state.get('last_trade_review')
    if last:
        try:
            age_days = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).days
        except Exception:
            age_days = TRADE_REVIEW_INTERVAL_DAYS
        if age_days < TRADE_REVIEW_INTERVAL_DAYS:
            return

    records = load_journal()
    state['last_trade_review'] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    if not records:
        logger.info('[Sentinel] Weekly trade review: no journaled trades yet')
        return

    for base in sorted({r['base'] for r in records}):
        base_records = [r for r in records if r['base'] == base]
        n = len(base_records)
        if n < TRADE_REVIEW_MIN_TRADES:
            logger.info(f'[Sentinel:{base}] Weekly trade review skipped — only {n} trades logged so far')
            continue

        overall_wr  = len([r for r in base_records if r['win']]) / n * 100
        overall_net = sum(r['pnl'] for r in base_records)

        cfg = next((c for c in SYMBOLS_CONFIG.values() if c['base'] == base), None)
        max_hold_minutes = (cfg['max_hold_bars'] * 15) if cfg else None

        def hold_bucket(r):
            hm = r.get('hold_minutes')
            if hm is None or not max_hold_minutes:
                return None
            frac = hm / max_hold_minutes
            if frac < 0.33: return 'quick exit (<1/3 max hold)'
            if frac < 0.75: return 'medium hold'
            return 'long hold (near/at time-exit)'

        def prox_bucket(r):
            d = r.get('sr_distance_pct')
            if d is None: return None
            return 'tight (<0.10%)' if d < 0.10 else 'loose (>=0.10%)'

        bucket_groups = {
            'Hour of day':    _bucket_stats(base_records, _hour_bucket),
            'Day of week':    _bucket_stats(base_records, lambda r: r.get('entry_weekday')),
            'Side':           _bucket_stats(base_records, lambda r: r.get('side')),
            'Exit reason':    _bucket_stats(base_records, lambda r: r.get('exit_reason')),
            'Hold time':      _bucket_stats(base_records, hold_bucket),
            'S/R proximity':  _bucket_stats(base_records, prox_bucket),
        }

        flags = []
        for group_name, stats in bucket_groups.items():
            for bucket_name, s in stats.items():
                if s['win_rate'] < overall_wr - 15 or s['net_pnl'] < 0:
                    r_note = f", avgR={s['avg_r']:+.2f}" if s['avg_r'] is not None else ''
                    flags.append(f"  • {group_name} = {bucket_name}: {s['n']} trades, "
                                 f"{s['win_rate']}%W, net ${s['net_pnl']:+.2f}{r_note}")

        if flags:
            msg = (f'📊 **Sentinel [{base}] Weekly Trade Review**\n\n'
                   f'Overall: {n} trades, {overall_wr:.1f}% win rate, net ${overall_net:+.2f}\n\n'
                   f'Underperforming conditions worth a manual look:\n' + '\n'.join(flags))
        else:
            msg = (f'📊 **Sentinel [{base}] Weekly Trade Review**\n\n'
                   f'Overall: {n} trades, {overall_wr:.1f}% win rate, net ${overall_net:+.2f}\n'
                   f'No condition stands out as a consistent drag — looks healthy.')
        notify(msg)
        logger.info(f'[Sentinel:{base}] Weekly trade review sent ({len(flags)} flags)')


# ── Main loop — two speeds, same pattern as Apex ────────────────────────────
# Apex runs a full strategy cycle every 30s (new candles, new-entry decisions)
# but nests a 2-second loop inside that purely to react fast if an already-open
# position's stop/target gets hit. Mirroring that here: FAST_CHECK reacts to
# price on an open position, SLOW_CHECK does the heavier candle-fetch +
# new-signal evaluation, which only matters once per 15-min candle close anyway.
# Both loops iterate over every configured symbol.
FAST_CHECK_SECONDS = 2
SLOW_CHECK_SECONDS = 5 * 60

def check_position_fast(symbol: str, cfg: dict):
    """Lightweight — one ticker-price call, no candle fetch. Only acts if a
    position is open. Matches Apex's 2-second stop/target reaction time."""
    base = cfg['base']
    pos = state['positions'].get(symbol)
    if not pos:
        return
    try:
        live = get_live_position(symbol)
        if live['side'] is None:
            logger.info(f'[Sentinel:{base}] Local state showed open position but live account is flat — reconciling')
            state['positions'][symbol] = None
            save_state(state)
            return

        price = get_current_price(symbol)
        LATEST_PRICE[symbol] = price
        hit_reason = None
        if pos['side'] == 'LONG':
            if price <= pos['stop']: hit_reason = 'Stop Loss'
            elif price >= pos['target']: hit_reason = 'Target Hit'
        else:
            if price >= pos['stop']: hit_reason = 'Stop Loss'
            elif price <= pos['target']: hit_reason = 'Target Hit'

        if hit_reason:
            close_position(symbol, cfg, hit_reason)
            return

        opened_at = datetime.fromisoformat(pos['opened_at'])
        held_hours = (datetime.now(timezone.utc) - opened_at).total_seconds() / 3600
        if held_hours >= (cfg['max_hold_bars'] * 15 / 60):
            close_position(symbol, cfg, 'Time Exit')
    except Exception as e:
        logger.warning(f'[Sentinel:{base}] check_position_fast error: {e}')

def check_for_new_entry(symbol: str, cfg: dict):
    """Heavier — fetches fresh candles, evaluates the strategy. Meaningful
    once per 15-min candle close; checked every 5 min for a safety margin
    against timing drift."""
    base = cfg['base']
    try:
        candles = get_candles(symbol, cfg['interval'], limit=500)
        if candles:
            LATEST_PRICE[symbol] = candles[-1]['close']
        pos = state['positions'].get(symbol)
        if not pos:
            live = get_live_position(symbol)
            if live['side'] is not None:
                logger.info(f'[Sentinel:{base}] {base} already in a live {live["side"]} position (likely Apex) — sitting out this cycle')
            else:
                signal = get_decision(candles, cfg)
                if signal:
                    open_position(symbol, cfg, signal)
                else:
                    logger.info(f'[Sentinel:{base}] No signal this cycle')
    except Exception as e:
        logger.error(f'[Sentinel:{base}] check_for_new_entry error: {e}', exc_info=True)
        alert_error(base, str(e))


def main():
    tickers_line = ', '.join(f'{cfg["base"]} (${cfg["trade_amount"]}@{cfg["leverage"]}x)' for cfg in SYMBOLS_CONFIG.values())
    logger.info(f'🚀 Sentinel starting — {tickers_line}')
    strategy_lines = '\n'.join(f'  • {cfg["base"]}: {cfg["strategy_label"]}' for cfg in SYMBOLS_CONFIG.values())
    notify(
        f'🚀 **Sentinel Started**\n\n'
        f'{strategy_lines}\n'
        f'💵 {tickers_line}\n'
        f'⏱ New-signal scan every 5 min | Stop/target reaction every 2s | Coordinated with Apex'
    )
    state['run_count'] = state.get('run_count', 0)
    last_slow_check = 0.0
    while True:
        for symbol, cfg in SYMBOLS_CONFIG.items():
            check_position_fast(symbol, cfg)
        now = time.time()
        if now - last_slow_check >= SLOW_CHECK_SECONDS:
            state['run_count'] += 1
            for symbol, cfg in SYMBOLS_CONFIG.items():
                check_for_new_entry(symbol, cfg)
            write_dashboard()
            run_weekly_trade_review()
            last_slow_check = now
        time.sleep(FAST_CHECK_SECONDS)


if __name__ == '__main__':
    main()
