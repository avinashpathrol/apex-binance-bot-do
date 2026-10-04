#!/usr/bin/env python3
"""
Sentinel -- overnight-hold trader. TWO separate engines in this one process:

1. REAL money: MU and MRVL (class RealOvernightEngine, added 2026-10-03).
   Places actual Binance orders -- migrated from trading_bot_futures.py for MU
   ("separation of concerns, one bot one strategy"); MRVL starts here directly.
   Own state file (sentinel_real_state.json), own signed-POST allow-list
   (BinanceFeed.SIGNED_POST_PATHS: /fapi/v1/leverage, /fapi/v1/order). MU's
   trade history was carried forward from Apex on migration so its win-rate/
   P&L track record reads as one continuous series, not a restart.

2. PAPER only: SNDK / WDC / LITE / CRDO (class Sentinel, the original engine).
   Fills are simulated by walking the live order book -- no real orders, ever,
   for these. The only signed calls this half makes are GETs
   (BinanceFeed.SIGNED_PATHS: leverageBracket / positionRisk / order-status).

Both share the same rules as the live MU overnight strategy always has:
  * enter LONG at market 15:55-16:05 ET on US trading days (never on holidays;
    early-close days use close-5min instead of 15:55)
  * hard stop 3.5% below the fill, checked in software every cycle (~30s) --
    no exchange-side stop order
  * exit at market 9:28-9:40 ET on the next trading day (holds through weekends
    and holidays); no trailing stop
  * fees 0.05% taker per side; real funding paid/received while the position is open

Sized at $100 margin x the MAXIMUM leverage Binance allows for that symbol at that
size (read from /fapi/v1/leverageBracket, re-checked daily) -- both real and paper.

Liquidation is NOT modeled in either engine: like MU's live cross-margin account,
P&L is simply notional x return (a stop-out can lose more than the $100 stake).

Standalone: does not import trading_bot_futures.py. The paper half replaces the
old NBIS/RKLB price-action "Sentinel" (retired 2026-09-20).
"""

import json
import logging
import math
import os
import sys
import time
import hmac
import hashlib
import urllib.parse
from datetime import date, datetime, timedelta, timezone

import requests

# ── Config ────────────────────────────────────────────────────────────────────
FUTURES_BASE_URL   = 'https://fapi.binance.com'
BINANCE_API_KEY    = os.environ.get('BINANCE_API_KEY', '').strip()
BINANCE_API_SECRET = os.environ.get('BINANCE_API_SECRET', '').strip()
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
TELEGRAM_CHAT_ID   = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
WEB_ROOT           = os.environ.get('WEB_ROOT', '/var/www/apex').strip()
STATE_FILE         = os.environ.get('SENTINEL_STATE_FILE', 'sentinel_state.json').strip()
JOURNAL_FILE       = os.environ.get('SENTINEL_JOURNAL_FILE', 'sentinel_paper_trades.jsonl').strip()
DASHBOARD_FILE     = os.path.join(WEB_ROOT, 'data_sentinel.json')

# name -> why it is here (closest-to-MU ranking from the 2026-09-20 peer screen)
SYMBOLS = {
    'SNDKUSDT': {'base': 'SNDK', 'name': 'SanDisk',           'note': 'closest MU peer (NAND memory), overnight corr 0.91'},
    'WDCUSDT':  {'base': 'WDC',  'name': 'Western Digital',   'note': 'storage/memory peer, corr 0.77, thin perp'},
    'LITEUSDT': {'base': 'LITE', 'name': 'Lumentum',          'note': 'AI optics, less correlated (0.60), earnings-sensitive'},
    'MRVLUSDT': {'base': 'MRVL', 'name': 'Marvell',            'note': 'semis, edge not earnings-dependent, added 2026-09-29 as an alternative to SNDK'},
    'CRDOUSDT': {'base': 'CRDO', 'name': 'Credo Technology',   'note': 'best backtest edge (t=2.1) but thinnest real perp (~$5.5M/day) of the group -- added 2026-09-29 specifically to see how a thin book behaves; watch avg entry/exit slippage below before ever considering this one live'},
}

STAKE_USDT       = 100.0     # margin per trade (user request 2026-09-20)
SL_PCT           = 0.035     # same hard stop as MU
FEE_RATE         = 0.0005    # 0.05% taker per side, same as Apex/MU accounting
CYCLE_SECONDS    = 30
ENTRY_LEAD_MIN   = 5         # window = close-5min .. close+5min (MU: 15:55-16:05)
ENTRY_LAG_MIN    = 5
EXIT_START_MIN   = 9 * 60 + 28    # 9:28 ET
EXIT_ONTIME_MIN  = 9 * 60 + 40    # after 9:40 ET a still-open position is a "Late Exit"
LEVERAGE_TTL_S   = 24 * 3600
FILTERS_TTL_S    = 24 * 3600
MAX_TRADES_KEPT  = 3000
BAD_TICK_FRACTION = 0.30     # ignore a single quote >30% away from the last one (needs 2 in a row)
DASHBOARD_IDLE_S = 300

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('sentinel')


# ══ US/Eastern time + NYSE calendar (pure python, no tz database needed) ══════
def _nth_weekday(year, month, weekday, n):
    """n-th (1-based) `weekday` of month; n=-1 -> last."""
    if n > 0:
        d = date(year, month, 1)
        d += timedelta(days=(weekday - d.weekday()) % 7)
        return d + timedelta(days=7 * (n - 1))
    nxt = date(year + (month == 12), (month % 12) + 1, 1)
    d = nxt - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year):
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _observed(d):
    """NYSE observance for a fixed-date holiday: Sat -> Fri, Sun -> Mon."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


_HOLIDAY_CACHE = {}


def nyse_holidays(year):
    if year in _HOLIDAY_CACHE:
        return _HOLIDAY_CACHE[year]
    h = set()
    ny = date(year, 1, 1)
    if ny.weekday() != 5:                       # Sat New Year's is NOT observed on Friday
        h.add(_observed(ny))
    h.add(_nth_weekday(year, 1, 0, 3))           # MLK
    h.add(_nth_weekday(year, 2, 0, 3))           # Presidents' Day
    h.add(_easter(year) - timedelta(days=2))     # Good Friday
    h.add(_nth_weekday(year, 5, 0, -1))          # Memorial Day
    if year >= 2022:
        h.add(_observed(date(year, 6, 19)))      # Juneteenth
    h.add(_observed(date(year, 7, 4)))           # Independence Day
    h.add(_nth_weekday(year, 9, 0, 1))           # Labor Day
    h.add(_nth_weekday(year, 11, 3, 4))          # Thanksgiving
    h.add(_observed(date(year, 12, 25)))         # Christmas
    _HOLIDAY_CACHE[year] = h
    return h


def nyse_early_closes(year):
    """1:00pm ET closes: day after Thanksgiving, Dec 24 and Jul 3 when they are ordinary weekdays."""
    out = {_nth_weekday(year, 11, 3, 4) + timedelta(days=1)}
    hol = nyse_holidays(year)
    for d in (date(year, 12, 24), date(year, 7, 3)):
        if d.weekday() < 5 and d not in hol and (d.month != 7 or d.weekday() < 4):
            out.add(d)
    return out


def is_trading_day(d):
    return d.weekday() < 5 and d not in nyse_holidays(d.year)


def close_minute(d):
    """Minute-of-day (ET) of the official close."""
    return 13 * 60 if d in nyse_early_closes(d.year) else 16 * 60


def _dst_bounds_utc(year):
    """(start, end) UTC instants of US daylight time."""
    start = datetime(year, 3, 1, 7, 0) + timedelta(days=(_nth_weekday(year, 3, 6, 2) - date(year, 3, 1)).days)
    end = datetime(year, 11, 1, 6, 0) + timedelta(days=(_nth_weekday(year, 11, 6, 1) - date(year, 11, 1)).days)
    return start, end


def et_offset_hours(dt_utc_naive):
    s, e = _dst_bounds_utc(dt_utc_naive.year)
    return -4 if s <= dt_utc_naive < e else -5


def et_from_utc(dt_utc):
    """aware/naive UTC datetime -> naive US/Eastern datetime."""
    n = dt_utc.replace(tzinfo=None) if dt_utc.tzinfo is None else dt_utc.astimezone(timezone.utc).replace(tzinfo=None)
    return n + timedelta(hours=et_offset_hours(n))


def utc_from_et(dt_et_naive):
    guess = dt_et_naive + timedelta(hours=4)
    if et_offset_hours(guess) == -4:
        return guess.replace(tzinfo=timezone.utc)
    return (dt_et_naive + timedelta(hours=5)).replace(tzinfo=timezone.utc)


def minute_of_day(et):
    return et.hour * 60 + et.minute


def in_entry_window(et):
    d = et.date()
    if not is_trading_day(d):
        return False
    m, c = minute_of_day(et), close_minute(d)
    return c - ENTRY_LEAD_MIN <= m <= c + ENTRY_LAG_MIN


def next_entry_utc(now_utc):
    """UTC instant the next entry window opens (for the dashboard countdown)."""
    et = et_from_utc(now_utc)
    d = et.date()
    for _ in range(12):
        if is_trading_day(d):
            start = datetime(d.year, d.month, d.day) + timedelta(minutes=close_minute(d) - ENTRY_LEAD_MIN)
            if start + timedelta(minutes=ENTRY_LEAD_MIN + ENTRY_LAG_MIN + 1) > et:
                return utc_from_et(start)
        d += timedelta(days=1)
    return None


# ══ small helpers ═════════════════════════════════════════════════════════════
def now_iso(ts=None):
    return datetime.fromtimestamp(ts if ts is not None else time.time(), timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def floor_step(x, step):
    if step <= 0:
        return x
    n = math.floor(x / step + 1e-9)
    return round(n * step, 12)


def round_tick(x, tick):
    if tick <= 0:
        return x
    return round(round(x / tick) * tick, 10)


def atomic_write_json(path, obj):
    tmp = '%s.%d.tmp' % (path, os.getpid())
    with open(tmp, 'w') as f:
        json.dump(obj, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def pick_max_leverage(brackets, stake):
    """Highest leverage L such that the bracket containing notional stake*L allows >= L."""
    top = int(max(b['initialLeverage'] for b in brackets))
    for L in range(top, 0, -1):
        n = stake * L
        for b in brackets:
            if float(b['notionalFloor']) <= n < float(b['notionalCap']):
                if int(b['initialLeverage']) >= L:
                    return L
                break
    return 1


def walk_book(levels, qty):
    """VWAP of taking `qty` from [(price, size), ...]. Returns (vwap, filled_qty)."""
    remaining, cost = qty, 0.0
    for p, q in levels:
        take = min(remaining, q)
        cost += take * p
        remaining -= take
        if remaining <= 1e-12:
            break
    filled = qty - max(remaining, 0.0)
    return (cost / filled if filled > 0 else None), filled


# ══ Binance access (public + ONE allow-listed signed read) ════════════════════
class RateLimited(Exception):
    pass


class BinanceFeed:
    PUBLIC_PATHS = {'/fapi/v1/ticker/bookTicker', '/fapi/v1/depth', '/fapi/v1/exchangeInfo', '/fapi/v1/fundingRate',
                    '/fapi/v1/premiumIndex'}
    # Real trading added 2026-10-03 (user request -- MU + MRVL migrating from Apex's
    # trading_bot_futures.py, "separation of concerns, one bot one strategy"). Explicit
    # allow-lists stay the governing safety fence, same pattern as before, just widened to
    # what the real engine actually needs -- never an open-ended "any signed call allowed."
    SIGNED_PATHS      = {'/fapi/v1/leverageBracket', '/fapi/v2/positionRisk', '/fapi/v1/order'}   # signed GETs
    SIGNED_POST_PATHS = {'/fapi/v1/leverage', '/fapi/v1/order'}                                    # signed POSTs

    def __init__(self):
        self.s = requests.Session()
        self._lev = {}        # sym -> (lev, ts)
        self._filters = {}    # sym -> (filters, ts)

    def _get(self, url, params, headers=None):
        last = None
        for attempt in range(3):
            try:
                r = self.s.get(url, params=params, headers=headers or {}, timeout=10)
                if r.status_code in (418, 429):
                    raise RateLimited('HTTP %d from Binance (Retry-After %s)' % (r.status_code, r.headers.get('Retry-After')))
                if r.status_code >= 500:
                    last = RuntimeError('HTTP %d' % r.status_code)
                    time.sleep(0.5 * (attempt + 1))
                    continue
                if r.status_code != 200:
                    raise RuntimeError('HTTP %d: %s' % (r.status_code, r.text[:200]))
                return r.json()
            except (requests.ConnectionError, requests.Timeout) as e:
                last = e
                time.sleep(0.5 * (attempt + 1))
        raise RuntimeError('request failed: %s' % (last,))

    def public_get(self, path, params=None):
        if path not in self.PUBLIC_PATHS:
            raise PermissionError('path not allow-listed: %s' % path)
        return self._get(FUTURES_BASE_URL + path, params or {})

    def signed_get(self, path, params=None):
        if path not in self.SIGNED_PATHS:
            raise PermissionError('signed path not allow-listed: %s' % path)
        if not (BINANCE_API_KEY and BINANCE_API_SECRET):
            raise RuntimeError('no Binance API key configured for the leverage lookup')
        p = dict(params or {})
        p['timestamp'] = int(time.time() * 1000)
        p['recvWindow'] = 10000
        q = urllib.parse.urlencode(p)
        sig = hmac.new(BINANCE_API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
        return self._get('%s%s?%s&signature=%s' % (FUTURES_BASE_URL, path, q, sig), None, {'X-MBX-APIKEY': BINANCE_API_KEY})

    def _post(self, url, params, headers):
        last = None
        for attempt in range(3):
            try:
                r = self.s.post(url, params=params, headers=headers, timeout=15)
                if r.status_code in (418, 429):
                    raise RateLimited('HTTP %d from Binance (Retry-After %s)' % (r.status_code, r.headers.get('Retry-After')))
                if r.status_code >= 500:
                    last = RuntimeError('HTTP %d' % r.status_code)
                    time.sleep(0.5 * (attempt + 1))
                    continue
                if not r.ok:
                    logger.error('SIGNED POST %s | %s | %s', url, r.status_code, r.text[:300])
                    raise RuntimeError('HTTP %d: %s' % (r.status_code, r.text[:300]))
                return r.json()
            except (requests.ConnectionError, requests.Timeout) as e:
                last = e
                time.sleep(0.5 * (attempt + 1))
        raise RuntimeError('request failed: %s' % (last,))

    def signed_post(self, path, params=None):
        """Real order-placement / leverage-set calls -- allow-listed, same pattern as
        signed_get(). Added 2026-10-03 for MU/MRVL real trading (see module docstring)."""
        if path not in self.SIGNED_POST_PATHS:
            raise PermissionError('signed POST path not allow-listed: %s' % path)
        if not (BINANCE_API_KEY and BINANCE_API_SECRET):
            raise RuntimeError('no Binance API key configured')
        p = dict(params or {})
        p['timestamp'] = int(time.time() * 1000)
        p['recvWindow'] = 10000
        q = urllib.parse.urlencode(p)
        sig = hmac.new(BINANCE_API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
        return self._post('%s%s?%s&signature=%s' % (FUTURES_BASE_URL, path, q, sig), None, {'X-MBX-APIKEY': BINANCE_API_KEY})

    # ---- real trading (MU/MRVL) -- places actual Binance orders ----
    def set_leverage(self, sym, lev):
        self.signed_post('/fapi/v1/leverage', {'symbol': sym, 'leverage': int(lev)})

    def position_risk(self, sym):
        rows = self.signed_get('/fapi/v2/positionRisk', {'symbol': sym})
        for p in rows:
            if p['symbol'] == sym:
                amt = float(p['positionAmt'])
                return {'qty': abs(amt), 'side': 'LONG' if amt > 1e-8 else None,
                        'entry_price': float(p.get('entryPrice') or 0), 'unrealized_pnl': float(p.get('unRealizedProfit') or 0)}
        return {'qty': 0.0, 'side': None, 'entry_price': 0.0, 'unrealized_pnl': 0.0}

    def market_order(self, sym, side, qty, reduce_only=False):
        """One-way position mode (account-wide setting, same as Apex's MUUSDT/MRVL config)
        -- no positionSide param, reduceOnly on closes only. Mirrors
        trading_bot_futures.py's futures_market_order() exactly."""
        params = {'symbol': sym, 'side': side, 'type': 'MARKET', 'quantity': str(qty)}
        if reduce_only:
            params['reduceOnly'] = 'true'
        resp = self.signed_post('/fapi/v1/order', params)
        return self._with_actual_fill(sym, resp)

    def _with_actual_fill(self, sym, resp):
        """Market-order responses often carry avgPrice=0 -- look the order back up to get
        the real fill. Mirrors trading_bot_futures.py's _with_actual_fill() exactly (found
        there 2026-09-19: recorded price vs real fill differed by ~$5 on MU once).
        Fail-safe: the order is already placed by the time this runs, so any problem here
        just returns the original response unchanged, never raises."""
        try:
            if float(resp.get('avgPrice') or 0) > 0 or float(resp.get('cumQuote') or 0) > 0:
                return resp
            order_id = resp.get('orderId')
            if not order_id:
                return resp
            for attempt in range(4):
                if attempt:
                    time.sleep(0.25)
                o = self.signed_get('/fapi/v1/order', {'symbol': sym, 'orderId': order_id})
                if o.get('status') == 'FILLED' and float(o.get('avgPrice') or 0) > 0:
                    merged = dict(resp)
                    for k in ('avgPrice', 'executedQty', 'cumQuote', 'status'):
                        if k in o:
                            merged[k] = o[k]
                    return merged
            return resp
        except Exception as e:
            logger.warning('[%s] actual-fill lookup failed (using order response as-is): %s', sym, e)
            return resp

    @staticmethod
    def fill_price(resp, fallback):
        try:
            avg = float(resp.get('avgPrice', 0))
            if avg > 0:
                return avg
            cq, eq = float(resp.get('cumQuote', 0)), float(resp.get('executedQty', 0))
            if cq > 0 and eq > 0:
                return cq / eq
        except Exception:
            pass
        return fallback

    def step_size(self, sym):
        filt = self.filters(sym)
        return filt.get('step', 0.001)

    @staticmethod
    def round_step(qty, step):
        precision = len(str(step).rstrip('0').split('.')[-1]) if '.' in str(step) else 0
        return round(qty - (qty % step), precision)

    # ---- feed interface used by the engine ----
    def quote(self, sym):
        d = self.public_get('/fapi/v1/ticker/bookTicker', {'symbol': sym})
        bid, ask = float(d['bidPrice']), float(d['askPrice'])
        if bid <= 0 or ask <= 0 or ask < bid:
            raise RuntimeError('bad quote for %s: bid=%s ask=%s' % (sym, bid, ask))
        return bid, ask

    def fill(self, sym, side, qty):
        """Simulated market-order fill: walk the live book. -> dict(price, source, bid, ask, filled)"""
        d = self.public_get('/fapi/v1/depth', {'symbol': sym, 'limit': 100})
        bids = [(float(p), float(q)) for p, q in d['bids']]
        asks = [(float(p), float(q)) for p, q in d['asks']]
        if not bids or not asks:
            raise RuntimeError('empty book for %s' % sym)
        bid, ask = bids[0][0], asks[0][0]
        vwap, filled = walk_book(asks if side == 'BUY' else bids, qty)
        if vwap is None:
            price, source = (ask if side == 'BUY' else bid), 'top_of_book'
        elif filled < qty - 1e-9:
            worst = (asks if side == 'BUY' else bids)[-1][0]
            rem = qty - filled
            price, source = (vwap * filled + worst * rem) / qty, 'depth_partial'
        else:
            price, source = vwap, 'depth'
        return {'price': price, 'source': source, 'bid': bid, 'ask': ask}

    def leverage(self, sym, stake):
        c = self._lev.get(sym)
        if c and time.time() - c[1] < LEVERAGE_TTL_S:
            return c[0]
        try:
            br = self.signed_get('/fapi/v1/leverageBracket', {'symbol': sym})
            br = (br[0] if isinstance(br, list) else br)['brackets']
            lev = pick_max_leverage(br, stake)
            self._lev[sym] = (lev, time.time())
            return lev
        except Exception:
            if c:                                   # stale value beats none; log and keep going
                logger.warning('[%s] leverage refresh failed -- using last known %sx', sym, c[0])
                return c[0]
            raise

    def filters(self, sym):
        c = self._filters.get(sym)
        if c and time.time() - c[1] < FILTERS_TTL_S:
            return c[0]
        info = self.public_get('/fapi/v1/exchangeInfo')
        for s in info['symbols']:
            f = {x['filterType']: x for x in s['filters']}
            self._filters[s['symbol']] = ({
                'step': float(f['MARKET_LOT_SIZE']['stepSize']), 'min_qty': float(f['MARKET_LOT_SIZE']['minQty']),
                'max_qty': float(f['MARKET_LOT_SIZE']['maxQty']), 'tick': float(f['PRICE_FILTER']['tickSize']),
                'min_notional': float(f.get('MIN_NOTIONAL', {}).get('notional', 5)),
                'status': s.get('status'),
            }, time.time())
        return self._filters[sym][0]

    def funding(self, sym, start_ms, end_ms):
        """[(fundingTime_ms, rate, markPrice_or_None)] with start < t <= end. Raises on failure."""
        rows = self.public_get('/fapi/v1/fundingRate', {'symbol': sym, 'startTime': int(start_ms), 'endTime': int(end_ms), 'limit': 100})
        out = []
        for r in rows:
            t = int(r['fundingTime'])
            if start_ms < t <= end_ms:
                mp = float(r['markPrice']) if r.get('markPrice') not in (None, '') else None
                out.append((t, float(r['fundingRate']), mp))
        return out

    def last_funding_rate(self, sym):
        return float(self.public_get('/fapi/v1/premiumIndex', {'symbol': sym}).get('lastFundingRate') or 0.0)


# ══ notifications ═════════════════════════════════════════════════════════════
def send_telegram(text):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return False
    try:
        r = requests.post('https://api.telegram.org/bot%s/sendMessage' % TELEGRAM_BOT_TOKEN,
                          json={'chat_id': TELEGRAM_CHAT_ID, 'text': text, 'parse_mode': 'HTML', 'disable_web_page_preview': True},
                          timeout=10)
        return r.status_code == 200
    except Exception as e:
        logger.warning('telegram failed: %s', e)
        return False


# ══ the engine ════════════════════════════════════════════════════════════════
def new_state():
    return {'version': 1, 'created_at': now_iso(), 'positions': {s: None for s in SYMBOLS}, 'trades': [],
            'last_entry_day': {}, 'leverage': {}, 'skips': []}


def load_state(path=None):
    path = path or STATE_FILE
    try:
        with open(path) as f:
            st = json.load(f)
    except FileNotFoundError:
        return new_state()
    except Exception as e:                       # corrupt file: keep it for inspection, start clean
        bad = '%s.corrupt.%d' % (path, int(time.time()))
        try:
            os.replace(path, bad)
        except Exception:
            pass
        logger.error('state file unreadable (%s) -- moved to %s and starting fresh', e, bad)
        return new_state()
    base = new_state()
    for k, v in base.items():
        st.setdefault(k, v)
    for s in SYMBOLS:
        st['positions'].setdefault(s, None)
    return st


class Sentinel:
    def __init__(self, feed, state, notify=send_telegram, state_path=None, journal_path=None, dashboard_path=None, write_files=True):
        self.feed, self.st, self.notify = feed, state, notify
        self.state_path = state_path or STATE_FILE
        self.journal_path = journal_path or JOURNAL_FILE
        self.dashboard_path = dashboard_path or DASHBOARD_FILE
        self.write_files = write_files
        self.errors = {}                 # sym -> last error string (shown on the dashboard)
        self._alerted = {}               # key -> ts of last Telegram alert (rate limit)
        self._last_dash = 0.0
        self._last_save = time.time()
        self._last_lev_check = 0.0
        self.backoff_s = 0
        self._logged_skip_day = None

    # ---- persistence ----
    def save(self):
        self._last_save = time.time()
        if self.write_files:
            atomic_write_json(self.state_path, self.st)

    def _refresh_leverage(self, now_utc):
        """Hourly: keep the dashboard's leverage/notional current (the feed itself only re-reads Binance daily)."""
        if time.time() - self._last_lev_check < 3600:
            return
        self._last_lev_check = time.time()
        for sym in SYMBOLS:
            try:
                lev = self.feed.leverage(sym, STAKE_USDT)
                if (self.st['leverage'].get(sym) or {}).get('lev') != lev:
                    if self.st['leverage'].get(sym):
                        logger.info('[%s] max leverage changed %sx -> %sx', sym, self.st['leverage'][sym]['lev'], lev)
                    self.st['leverage'][sym] = {'lev': lev, 'at': now_iso()}
                    self.save()
            except Exception as e:
                logger.warning('[%s] leverage refresh failed: %s', sym, e)

    def _journal(self, rec):
        if self.write_files:
            with open(self.journal_path, 'a') as f:
                f.write(json.dumps(rec) + '\n')

    def _alert(self, key, text, every=1800):
        if time.time() - self._alerted.get(key, 0) < every:
            return
        self._alerted[key] = time.time()
        self.notify('⚠️ <b>Sentinel</b> %s' % text)

    def _skip(self, day, sym, why):
        skips = self.st.setdefault('skips', [])
        if not any(s['day'] == day and s['symbol'] == sym and s['why'] == why for s in skips[-20:]):
            skips.append({'day': day, 'symbol': sym, 'why': why})
            del skips[:-60]
            self.save()

    # ---- one pass ----
    def cycle(self, now_utc):
        et = et_from_utc(now_utc)
        events = []
        for sym in SYMBOLS:
            try:
                self._step(sym, now_utc, et, events)
                self.errors.pop(sym, None)
            except RateLimited as e:
                self.errors[sym] = str(e)
                self.backoff_s = 60
                logger.warning('[%s] %s -- backing off', sym, e)
                break
            except Exception as e:
                self.errors[sym] = '%s: %s' % (type(e).__name__, e)
                logger.error('[%s] cycle error: %s', sym, e)
                if self.st['positions'].get(sym):
                    self._alert('err-' + sym, '%s: %s (open paper position is NOT being monitored this cycle)' % (SYMBOLS[sym]['base'], e))
        if events:
            self._notify_events(events)
        holding = any(self.st['positions'].values())
        if holding and time.time() - self._last_save >= 300:        # persist mark-to-market extremes (MAE/MFE) too
            self.save()
        self._refresh_leverage(now_utc)
        if events or holding or time.time() - self._last_dash >= DASHBOARD_IDLE_S:
            self.write_dashboard(now_utc)
        return events

    def _step(self, sym, now_utc, et, events):
        pos = self.st['positions'].get(sym)
        if pos:
            bid, ask = self.feed.quote(sym)
            last = pos.get('last_bid') or pos['entry_price']
            if abs(bid / last - 1) > BAD_TICK_FRACTION and not pos.get('suspect_tick'):
                pos['suspect_tick'] = True              # one wild quote alone never triggers a close
                logger.warning('[%s] ignoring outlier quote bid=%s (last %s) once', sym, bid, last)
                return
            pos['suspect_tick'] = False
            pos['last_bid'], pos['last_ask'], pos['last_quote_ts'] = bid, ask, time.time()
            pos['low_bid'] = min(pos.get('low_bid', bid), bid)
            pos['high_bid'] = max(pos.get('high_bid', bid), bid)
            if bid <= pos['sl_price']:
                events.append(self._close(sym, 'Stop Loss', now_utc, et))
                return
            reason = self._exit_reason(pos, et)
            if reason:
                events.append(self._close(sym, reason, now_utc, et))
            return

        today = et.date().isoformat()
        if in_entry_window(et):
            if self.st['last_entry_day'].get(sym) == today:
                return
            ev = self._open(sym, now_utc, et)
            if ev:
                events.append(ev)
        elif et.date().weekday() < 5 and not is_trading_day(et.date()) and minute_of_day(et) == 16 * 60 - ENTRY_LEAD_MIN:
            if self._logged_skip_day != today:
                self._logged_skip_day = today
                logger.info('US market holiday -- no entries today')
            self._skip(today, sym, 'US market holiday')

    @staticmethod
    def _exit_reason(pos, et):
        d = et.date()
        entry_day = date(*[int(x) for x in pos['entry_day'].split('-')])
        if not is_trading_day(d) or d <= entry_day or minute_of_day(et) < EXIT_START_MIN:
            return None
        return 'Market Open' if minute_of_day(et) <= EXIT_ONTIME_MIN else 'Late Exit'

    # ---- entry ----
    def _open(self, sym, now_utc, et):
        day = et.date().isoformat()
        try:
            lev = self.feed.leverage(sym, STAKE_USDT)
        except Exception as e:                      # transient: retried every cycle while the window is open
            self._alert('lev-' + sym, '%s: cannot determine max leverage (%s) -- no paper entry until it works' % (SYMBOLS[sym]['base'], e))
            raise
        f = self.feed.filters(sym)
        if f.get('status') not in (None, 'TRADING'):
            self._skip(day, sym, 'symbol status %s' % f.get('status'))
            self.st['last_entry_day'][sym] = day
            self.save()
            return None
        bid, ask = self.feed.quote(sym)
        qty = floor_step(STAKE_USDT * lev / ask, f['step'])
        qty = min(qty, f['max_qty'])
        if qty < f['min_qty'] or qty * ask < f['min_notional']:
            self._skip(day, sym, 'size below exchange minimum')
            self.st['last_entry_day'][sym] = day
            self.save()
            return None
        fl = self.feed.fill(sym, 'BUY', qty)
        price, mid = fl['price'], (fl['bid'] + fl['ask']) / 2
        pos = {
            'position': 'LONG', 'symbol': sym, 'base': SYMBOLS[sym]['base'],
            'entry_price': price, 'qty': qty, 'notional': round(qty * price, 4), 'stake': STAKE_USDT, 'leverage': lev,
            'entry_fee': qty * price * FEE_RATE, 'sl_price': round_tick(price * (1 - SL_PCT), f['tick']),
            'opened_at': now_iso(now_utc.timestamp()), 'opened_ts': now_utc.timestamp(), 'entry_day': day,
            'entry_mid': mid, 'entry_slip_bps': round((price / mid - 1) * 1e4, 3), 'entry_fill_source': fl['source'],
            'last_bid': fl['bid'], 'last_ask': fl['ask'], 'low_bid': fl['bid'], 'high_bid': fl['bid'],
        }
        self.st['positions'][sym] = pos
        self.st['last_entry_day'][sym] = day
        self.st['leverage'][sym] = {'lev': lev, 'at': now_iso()}
        self.save()
        logger.info('📝 [%s] PAPER OPEN %s @ %.4f (%dx, notional $%.2f, SL %.4f, slip %+.2fbp, %s)',
                    sym, qty, price, lev, pos['notional'], pos['sl_price'], pos['entry_slip_bps'], fl['source'])
        return {'type': 'open', 'pos': pos}

    # ---- exit ----
    def _funding(self, sym, pos, end_ts):
        """Funding paid by the long while the position was open (positive = cost). -> (cost, estimated?)"""
        start_ms, end_ms = int(pos['opened_ts'] * 1000), int(end_ts * 1000)
        try:
            rows = self.feed.funding(sym, start_ms, end_ms)
            cost = sum(rate * pos['qty'] * (mp or pos['entry_price']) for (_, rate, mp) in rows)
            return cost, False
        except Exception as e:
            logger.warning('[%s] funding history unavailable (%s) -- estimating', sym, e)
        try:
            rate = self.feed.last_funding_rate(sym)
            n = 0
            t = (int(pos['opened_ts'] // 28800) + 1) * 28800          # 8h settlement grid (00/08/16 UTC)
            while t <= end_ts:
                n += 1
                t += 28800
            return rate * n * pos['qty'] * pos['entry_price'], True
        except Exception:
            return 0.0, True

    def _close(self, sym, reason, now_utc, et):
        pos = self.st['positions'][sym]
        try:
            fl = self.feed.fill(sym, 'SELL', pos['qty'])
        except RateLimited:
            raise
        except Exception as e:
            logger.warning('[%s] depth unavailable at close (%s) -- using the bid', sym, e)
            fl = {'price': pos['last_bid'], 'bid': pos['last_bid'], 'ask': pos['last_ask'], 'source': 'quote_fallback'}
        price, mid = fl['price'], (fl['bid'] + fl['ask']) / 2
        qty = pos['qty']
        exit_fee = qty * price * FEE_RATE
        fee = pos['entry_fee'] + exit_fee
        gross = (price - pos['entry_price']) * qty
        funding, funding_est = self._funding(sym, pos, now_utc.timestamp())
        net = gross - fee - funding
        lo, hi = pos.get('low_bid', pos['entry_price']), pos.get('high_bid', pos['entry_price'])
        rec = {
            'symbol': sym, 'base': pos['base'], 'side': 'LONG',
            'opened_at': pos['opened_at'], 'closed_at': now_iso(now_utc.timestamp()),
            'entry_day': pos['entry_day'], 'exit_day': et.date().isoformat(),
            'entry_price': pos['entry_price'], 'exit_price': price, 'qty': qty,
            'notional': pos['notional'], 'stake': pos['stake'], 'leverage': pos['leverage'],
            'gross': round(gross, 4), 'fee': round(fee, 4), 'funding': round(funding, 4), 'net': round(net, 4),
            'net_pct_notional': round(net / pos['notional'] * 100, 4), 'net_pct_stake': round(net / pos['stake'] * 100, 3),
            'reason': reason, 'stopped': reason == 'Stop Loss', 'late': reason == 'Late Exit',
            'entry_slip_bps': pos['entry_slip_bps'], 'exit_slip_bps': round((price / mid - 1) * 1e4, 3),
            'entry_fill_source': pos['entry_fill_source'], 'exit_fill_source': fl['source'],
            'funding_estimated': funding_est,
            'hold_hours': round((now_utc.timestamp() - pos['opened_ts']) / 3600, 2),
            'mae_pct': round((lo / pos['entry_price'] - 1) * 100, 3), 'mfe_pct': round((hi / pos['entry_price'] - 1) * 100, 3),
        }
        trades = self.st['trades']
        trades.append(rec)
        del trades[:-MAX_TRADES_KEPT]
        self.st['positions'][sym] = None
        self.save()
        self._journal(rec)
        logger.info('📝 [%s] PAPER CLOSE @ %.4f net=%+.2f (%+.2f%% of notional) reason=%s', sym, price, net, rec['net_pct_notional'], reason)
        return {'type': 'close', 'rec': rec}

    # ---- messages ----
    def _notify_events(self, events):
        opens = [e['pos'] for e in events if e['type'] == 'open']
        closes = [e['rec'] for e in events if e['type'] == 'close']
        parts = []
        if opens:
            lines = ['📝 <b>SENTINEL (paper) — LONG opened</b>']
            for p in opens:
                lines.append('• <b>%s</b> %dx · notional $%s · fill $%s · stop $%s · slip %+.1fbp'
                             % (p['base'], p['leverage'], format(p['notional'], ',.0f'), format(p['entry_price'], ',.2f'),
                                format(p['sl_price'], ',.2f'), p['entry_slip_bps']))
            lines.append('⏰ Exit at the next US open (~9:28 AM ET)')
            parts.append('\n'.join(lines))
        if closes:
            tot = sum(r['net'] for r in closes)
            lines = ['%s <b>SENTINEL (paper) — closed</b>' % ('🟢' if tot >= 0 else '🔴')]
            for r in closes:
                lines.append('• <b>%s</b> (%s) net %s%.2f · %+.2f%% of notional · %+.0f%% of $%d stake'
                             % (r['base'], r['reason'], '+' if r['net'] >= 0 else '−', abs(r['net']), r['net_pct_notional'],
                                r['net_pct_stake'], r['stake']))
            if len(closes) > 1:
                lines.append('Total: %s%.2f' % ('+' if tot >= 0 else '−', abs(tot)))
            parts.append('\n'.join(lines))
        for p in parts:
            self.notify(p)

    # ---- dashboard ----
    @staticmethod
    def perf(trades):
        wins = [t for t in trades if t['net'] > 0]
        losses = [t for t in trades if t['net'] <= 0]
        n = len(trades)
        return {
            'total': n, 'wins': len(wins), 'losses': len(losses), 'win_rate': round(len(wins) / n * 100, 1) if n else 0,
            'win_pnl': round(sum(t['net'] for t in wins), 2), 'loss_pnl': round(sum(t['net'] for t in losses), 2),
            'total_fees': round(sum(t['fee'] for t in trades), 2), 'total_funding': round(sum(t['funding'] for t in trades), 2),
            'net_pnl': round(sum(t['net'] for t in trades), 2),
            'avg_net_pct_notional': round(sum(t['net_pct_notional'] for t in trades) / n, 4) if n else 0,
            'stops': sum(1 for t in trades if t['stopped']), 'late_exits': sum(1 for t in trades if t.get('late')),
            # liquidity check (added 2026-09-29 for thin-book candidates like CRDO): how far the simulated fill
            # landed from the mid-price at entry/exit. Consistently large slippage here means the real order book
            # can't actually absorb this stake -- a red flag worth seeing before ever trading a symbol live,
            # separate from whether the strategy itself made money.
            'avg_entry_slip_bps': round(sum(t['entry_slip_bps'] for t in trades) / n, 2) if n else 0,
            'avg_exit_slip_bps': round(sum(t['exit_slip_bps'] for t in trades) / n, 2) if n else 0,
            'max_entry_slip_bps': round(max((t['entry_slip_bps'] for t in trades), default=0), 2),
            'max_exit_slip_bps': round(max((t['exit_slip_bps'] for t in trades), default=0), 2),
            'partial_fills': sum(1 for t in trades if 'partial' in (t.get('entry_fill_source') or '') or 'partial' in (t.get('exit_fill_source') or '')),
        }

    def write_dashboard(self, now_utc):
        self._last_dash = time.time()
        syms = {}
        daily = {}
        for t in self.st['trades']:
            daily[t['exit_day']] = round(daily.get(t['exit_day'], 0.0) + t['net'], 2)
        for sym, meta in SYMBOLS.items():
            pos = self.st['positions'].get(sym)
            trades = [t for t in self.st['trades'] if t['symbol'] == sym]
            view = None
            if pos:
                bid = pos.get('last_bid') or pos['entry_price']
                gross_u = (bid - pos['entry_price']) * pos['qty']
                view = dict(pos)
                view.update({'current_bid': bid, 'unrealized_pnl': round(gross_u, 4),
                             'unrealized_pct_notional': round((bid / pos['entry_price'] - 1) * 100, 3)})
            lev = (self.st.get('leverage', {}).get(sym) or {}).get('lev')
            syms[sym] = {'base': meta['base'], 'name': meta['name'], 'note': meta['note'], 'stake': STAKE_USDT, 'leverage': lev,
                         'notional_target': round(STAKE_USDT * lev, 2) if lev else None, 'position': view,
                         'performance': self.perf(trades), 'trades': list(reversed(trades[-150:])), 'error': self.errors.get(sym)}
        # NOTE: real MU/MRVL data lives in data_sentinel_real.json now (written by
        # RealOvernightEngine.write_dashboard(), see bottom of this file) -- this payload
        # covers only the paper candidates below. The dashboard frontend fetches both files.
        nxt = next_entry_utc(now_utc)
        payload = {
            'generated_at': now_iso(now_utc.timestamp()), 'mode': 'paper', 'live_orders': False,
            'rules': {'stake': STAKE_USDT, 'sl_pct': round(SL_PCT * 100, 4), 'fee_per_side_pct': round(FEE_RATE * 100, 4),
                      'entry': '15:55-16:05 ET (close-5min on early-close days), trading days only',
                      'exit': '9:28-9:40 ET next trading day at market', 'trail': None,
                      'liquidation_modeled': False, 'fills': 'simulated by walking the live order book'},
            'next_entry_at': now_iso(nxt.timestamp()) if nxt else None,
            'symbols': syms, 'totals': {'performance': self.perf(self.st['trades']), 'daily_pnl': daily},
            'skips': self.st.get('skips', [])[-10:],
        }
        if self.write_files:
            try:
                atomic_write_json(self.dashboard_path, payload)
            except Exception as e:
                logger.warning('dashboard write failed (non-critical): %s', e)
        return payload

    def startup(self, now_utc):
        lines = []
        for sym, meta in SYMBOLS.items():
            try:
                lev = self.feed.leverage(sym, STAKE_USDT)
                self.st['leverage'][sym] = {'lev': lev, 'at': now_iso()}
                lines.append('• %s %dx → notional $%s' % (meta['base'], lev, format(STAKE_USDT * lev, ',.0f')))
            except Exception as e:
                lines.append('• %s leverage lookup failed: %s' % (meta['base'], e))
        self.save()
        self.write_dashboard(now_utc)
        held = [s for s, p in self.st['positions'].items() if p]
        if time.time() - self.st.get('last_start_msg_ts', 0) < 600:        # crash-loop guard: at most one start message / 10 min
            logger.info('start message suppressed (sent recently)')
            return
        self.st['last_start_msg_ts'] = time.time()
        self.save()
        self.notify('📝 <b>Sentinel started — paper candidates below (PAPER, no real orders)</b>\nOvernight hold, MU rules, $%d stake at max leverage:\n%s%s'
                    % (STAKE_USDT, '\n'.join(lines), ('\nResuming open paper positions: ' + ', '.join(SYMBOLS[s]['base'] for s in held)) if held else ''))


# ══ REAL trading: MU + MRVL (migrated from Apex 2026-10-03) ═══════════════════
# Separation of concerns, user request: MU's real overnight-hold trading moves
# here from trading_bot_futures.py (which gets a one-time auto-disable the
# instant its current MU position next closes, see close_overnight_mu() there);
# MRVL starts here directly, never lived in Apex. Fully independent of the
# Sentinel/paper engine above: own state file, own cycle, zero shared mutable
# state -- a bug here cannot corrupt the paper simulation and vice versa.
# Leverage is looked up dynamically via feed.leverage() (same mechanism the
# paper engine already uses for every symbol, re-checked daily) rather than
# hardcoded, matching this file's existing "re-checked daily" philosophy.
REAL_OVERNIGHT_CFG = {
    'MU':   {'symbol': 'MUUSDT',   'amount': 100.0, 'sl_pct': 0.035},
    'MRVL': {'symbol': 'MRVLUSDT', 'amount': 100.0, 'sl_pct': 0.035},
}
REAL_STATE_FILE     = os.environ.get('SENTINEL_REAL_STATE_FILE', 'sentinel_real_state.json').strip()
REAL_DASHBOARD_FILE = os.path.join(WEB_ROOT, 'data_sentinel_real.json')

# Dashboard-driven exit controls (stop / take-profit / manual close), ported from Apex's
# apply_mu_levels_request() / fetch_dashboard_config() / clear_flag() -- same mechanism,
# same file (bot_config.json is a generic shared key/value store the frontend PUTs to and
# any backend process reads), new keys namespaced "sentinel_<mu|mrvl>_..." so Apex's own
# "futures_mu_..." keys are untouched. MU_LEVEL_MIN_GAP matches trading_bot_futures.py and
# mu_levels.js exactly -- keep the three in sync if it ever changes.
MU_LEVEL_MIN_GAP = 0.0005
BOT_CONFIG_FILE  = os.path.join(WEB_ROOT, os.environ.get('BOT_CONFIG_FILE', 'bot_config.json').strip())

# Profit-protection trail (user request 2026-10-04, backtested against the real 26-trade MU
# history before building: of 26 overnight holds, 3 peaked above $40 unrealized and gave most
# or all of it back by the fixed morning exit -- worst case Sep 30 peaked +$113 and closed
# -$61. Continuous trail: once peak net first crosses TRAIL_ACTIVATE_NET the stop locks to
# +$40, then keeps following the peak at a fixed TRAIL_GIVEBACK_NET below it, only ever moving
# up (never chases price back down). Same $ thresholds for MU and MRVL since both run the same
# $100-margin-at-max-leverage sizing.
TRAIL_ACTIVATE_NET  = 40.0
TRAIL_GIVEBACK_NET  = 15.0


def fetch_bot_config():
    try:
        with open(BOT_CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def clear_bot_config_flag(flag_name):
    try:
        cfg = fetch_bot_config()
        cfg[flag_name] = False
        tmp = BOT_CONFIG_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, BOT_CONFIG_FILE)
    except Exception as e:
        logger.warning('clear_bot_config_flag(%s) failed: %s', flag_name, e)


def _net_at(pos, px):
    """Net P&L (entry fee already paid + exit fee) if the long were closed at px."""
    qty = pos.get('qty', 0.0)
    return (px - pos['entry_price']) * qty - pos.get('entry_fee', 0.0) - qty * px * FEE_RATE


def _price_for_net(pos, target_net):
    """Inverse of _net_at: the price that nets exactly target_net (exit fee included)."""
    qty = pos.get('qty', 0.0)
    return (target_net + pos['entry_price'] * qty + pos.get('entry_fee', 0.0)) / (qty * (1 - FEE_RATE))


def new_real_state():
    return {'version': 1, 'created_at': now_iso(),
            'positions': {k: None for k in REAL_OVERNIGHT_CFG},
            'trades': {k: [] for k in REAL_OVERNIGHT_CFG},
            'last_entry_day': {}}


def load_real_state(path=None):
    path = path or REAL_STATE_FILE
    try:
        with open(path) as f:
            st = json.load(f)
    except FileNotFoundError:
        return new_real_state()
    except Exception as e:
        bad = '%s.corrupt.%d' % (path, int(time.time()))
        try:
            os.replace(path, bad)
        except Exception:
            pass
        logger.error('real state file unreadable (%s) -- moved to %s and starting fresh', e, bad)
        return new_real_state()
    base = new_real_state()
    for k, v in base.items():
        st.setdefault(k, v)
    for k in REAL_OVERNIGHT_CFG:
        st['positions'].setdefault(k, None)
        st['trades'].setdefault(k, [])
    return st


class RealOvernightEngine:
    """Places actual Binance orders for MU and MRVL. Same rules as Apex's
    trading_bot_futures.py OVERNIGHT_CFG (which this replaces for MU): enter
    15:55-16:05 ET (close-5min window, Friday included for weekend hold,
    never on a US holiday), hard stop 3.5% below fill checked every cycle
    (software stop -- no exchange-side stop order, identical to Apex), exit
    9:28-9:40 ET next trading day, no trailing stop. One-way position mode
    (account-wide setting -- see BinanceFeed.market_order)."""

    def __init__(self, feed, state, notify=send_telegram, state_path=None, write_files=True):
        self.feed, self.st, self.notify = feed, state, notify
        self.state_path = state_path or REAL_STATE_FILE
        self.write_files = write_files
        self.errors = {}
        self._last_save = 0.0

    def save(self):
        if not self.write_files:
            return
        tmp = self.state_path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(self.st, f)
        os.replace(tmp, self.state_path)
        self._last_save = time.time()

    def cycle(self, now_utc):
        et = et_from_utc(now_utc)
        for key, cfg in REAL_OVERNIGHT_CFG.items():
            try:
                self._step(key, cfg, now_utc, et)
                self.errors.pop(key, None)
            except Exception as e:
                self.errors[key] = '%s: %s' % (type(e).__name__, e)
                logger.error('[REAL:%s] cycle error: %s', key, e)
                if self.st['positions'].get(key):
                    self.notify('⚠️ <b>REAL %s monitoring error</b>\n%s\nOpen position is NOT being checked '
                                'this cycle -- investigate now.' % (key, e))
        if time.time() - self._last_save >= 60:
            self.save()

    def _step(self, key, cfg, now_utc, et):
        sym = cfg['symbol']
        pos = self.st['positions'].get(key)
        if pos:
            bid, _ask = self.feed.quote(sym)
            kk = key.lower()
            # Dashboard exit controls (stop/take-profit/close), applied BEFORE the checks
            # below so a change takes effect this very cycle -- mirrors Apex's
            # run_overnight_strategy() ordering exactly.
            bc = {}
            try:
                bc = fetch_bot_config()
            except Exception as e:
                logger.warning('[REAL:%s] dashboard config read failed (ignored): %s', key, e)
            lv_req = bc.get('sentinel_%s_levels_request' % kk)
            if lv_req:
                try:
                    self.apply_levels_request(key, cfg, lv_req, bid)
                except Exception as e:
                    logger.warning('[REAL:%s] exit-levels request failed (ignored): %s', key, e)
                pos = self.st['positions'].get(key)
                if not pos:
                    return
            # Profit-protection trail (revised per user clarification 2026-10-04: continuous,
            # not a one-time ratchet -- "after 40, stop should move with trail"). Tracks the
            # running peak price since entry; once peak unrealized net first crosses the
            # activation threshold, the stop locks to +$40 immediately, then on every later
            # cycle keeps following the peak at a fixed $15 give-back, only ever moving up.
            # Sep 30 reconstruction: peaked +$113 net, actually closed -$61 (fixed exit, no
            # protection) -- this design would have trailed the stop up to roughly +$98 instead.
            pos['high_bid'] = max(pos.get('high_bid', bid), bid)
            peak_net = _net_at(pos, pos['high_bid'])
            if peak_net >= TRAIL_ACTIVATE_NET:
                just_armed = not pos.get('trail_armed')
                pos['trail_armed'] = True
                target_net = max(TRAIL_ACTIVATE_NET, peak_net - TRAIL_GIVEBACK_NET)
                trail_price = round(_price_for_net(pos, target_net), 4)
                if trail_price > pos.get('sl_price', 0):
                    pos['sl_price'] = trail_price
                    self.save()
                    logger.info('[REAL:%s] trail -- stop raised to $%.4f (locks +$%.2f, peak +$%.2f)', key, trail_price, target_net, peak_net)
                    if just_armed:
                        self.notify('🔒 <b>%s trail armed</b>\nPeak hit +$%.0f unrealized -- stop now trails $%.0f below the peak '
                                    '(locked in at least +$%.2f net right now).' % (key, TRAIL_ACTIVATE_NET, TRAIL_GIVEBACK_NET, target_net))
            if pos.get('sl_price') and bid <= pos['sl_price']:
                reason = 'Trail Stop' if pos.get('trail_armed') else ('Custom Stop' if pos.get('sl_custom') else 'Stop Loss')
                self._close(key, cfg, reason, now_utc, et, bid)
                return
            if pos.get('target_price') and bid >= pos['target_price']:
                self._close(key, cfg, 'Target Hit', now_utc, et, bid)
                return
            if bc.get('sentinel_%s_close_requested' % kk):
                req_at = bc.get('sentinel_%s_close_requested_at' % kk)
                age = 999
                try:
                    if req_at:
                        age = (now_utc - datetime.fromisoformat(req_at.replace('Z', '+00:00'))).total_seconds()
                except Exception:
                    pass
                if age < 300:
                    self.notify('📱 <b>Dashboard Close</b>\nClosing %s at market' % key)
                    if self._close(key, cfg, 'Dashboard Close', now_utc, et, bid):
                        clear_bot_config_flag('sentinel_%s_close_requested' % kk)
                    return
                else:
                    clear_bot_config_flag('sentinel_%s_close_requested' % kk)
            reason = self._exit_reason(pos, et)
            if reason:
                self._close(key, cfg, reason, now_utc, et, bid)
            return
        today = et.date().isoformat()
        if in_entry_window(et):
            if self.st['last_entry_day'].get(key) == today:
                return
            self._open(key, cfg, now_utc, et)

    def apply_levels_request(self, key, cfg, req, price):
        """Ported from trading_bot_futures.py's apply_mu_levels_request() -- same validation,
        same two-sided (price or net-$) request shape, same safety rails: a stop can only be
        tightened above the default (never loosened), both stop and target need a minimum gap
        from the current price so they can't fire immediately, and a stale request (for a
        position that already closed) is rejected rather than silently misapplied."""
        pos = self.st['positions'].get(key)
        if not pos:
            clear_bot_config_flag('sentinel_%s_levels_request' % key.lower())
            return
        applied, rejected = [], []
        try:
            if req.get('for_opened_at') != pos.get('opened_at'):
                rejected.append('ignored: the request was for a different position')
            else:
                default_sl = round(pos['entry_price'] * (1 - cfg['sl_pct']), 4)
                if req.get('reset_stop'):
                    pos['sl_price'] = default_sl
                    pos.pop('sl_custom', None)
                    applied.append('stop reset to the default $%.2f' % default_sl)
                if req.get('stop') is not None:
                    stop = float(req.get('stop') or 0)
                    if stop <= default_sl:
                        rejected.append('stop $%.2f is not above the default stop $%.2f' % (stop, default_sl))
                    elif stop >= price * (1 - MU_LEVEL_MIN_GAP):
                        rejected.append('stop $%.2f is at or above the current price $%.2f -- it would close '
                                        'immediately (use Close instead)' % (stop, price))
                    else:
                        pos['sl_price'] = round(stop, 4)
                        pos['sl_custom'] = True
                        applied.append('stop set to $%.2f (about %+.2f USDT net if it fills there)' % (stop, _net_at(pos, stop)))
                if req.get('clear_target'):
                    if pos.pop('target_price', None) is not None:
                        applied.append('take-profit target cleared')
                if req.get('target') is not None:
                    target = float(req.get('target') or 0)
                    if target <= price * (1 + MU_LEVEL_MIN_GAP):
                        rejected.append('target $%.2f is at or below the current price $%.2f -- it would close '
                                        'immediately (use Close instead)' % (target, price))
                    else:
                        pos['target_price'] = round(target, 4)
                        applied.append('take-profit set to $%.2f (about %+.2f USDT net)' % (target, _net_at(pos, target)))
        except Exception as e:
            rejected.append('error while applying: %s' % e)
        msg = '; '.join(applied + rejected) or 'no change'
        pos['levels_status'] = {'at': now_iso(), 'ok': bool(applied) and not rejected, 'msg': msg}
        self.st['positions'][key] = pos
        self.save()
        logger.info('[REAL:%s] exit levels request: %s', key, msg)
        self.notify(('🛡 <b>%s exit levels updated</b>\n' % key if not rejected else '⚠️ <b>%s exit levels request</b>\n' % key)
                    + '\n'.join('• ' + m for m in applied + rejected))
        clear_bot_config_flag('sentinel_%s_levels_request' % key.lower())
        self.write_dashboard(datetime.now(timezone.utc))

    @staticmethod
    def _exit_reason(pos, et):
        d = et.date()
        entry_day = date(*[int(x) for x in pos['entry_day'].split('-')])
        if not is_trading_day(d) or d <= entry_day or minute_of_day(et) < EXIT_START_MIN:
            return None
        return 'Market Open' if minute_of_day(et) <= EXIT_ONTIME_MIN else 'Late Exit'

    def _open(self, key, cfg, now_utc, et):
        sym, amount = cfg['symbol'], cfg['amount']
        day = et.date().isoformat()
        self.st['last_entry_day'][key] = day   # mark attempted regardless of outcome -- once/day, matches Apex
        try:
            lev = self.feed.leverage(sym, amount)
            self.feed.set_leverage(sym, lev)
            step = self.feed.step_size(sym)
            price_now = self.feed.quote(sym)[1]   # ask -- conservative for sizing a BUY
            qty = self.feed.round_step((amount * lev * 0.995) / price_now, step)
            if qty < step:
                logger.warning('[REAL:%s] quantity too small, skipping entry', key)
                return
            resp = self.feed.market_order(sym, 'BUY', qty)
            actual_price = self.feed.fill_price(resp, price_now)
            qty_filled = float(resp.get('executedQty') or 0) or qty
            sl_price = round(actual_price * (1 - cfg['sl_pct']), 4)
            self.st['positions'][key] = {
                'entry_price': actual_price, 'qty': qty_filled,
                'entry_fee': qty_filled * actual_price * FEE_RATE,
                'sl_price': sl_price, 'leverage': lev, 'amount': amount,
                'opened_at': now_iso(now_utc.timestamp()), 'entry_day': day,
            }
            self.save()
            logger.info('🌙 [REAL:%s] OPEN %.4f @ $%.4f SL=$%.4f (%sx)', key, qty_filled, actual_price, sl_price, lev)
            self.notify('🌙 <b>REAL — %s LONG OPEN (%sx)</b>\n\n💰 Entry: $%.4f\n💵 Collateral: $%.2f | Notional: $%.2f\n'
                        '🛑 Stop Loss: $%.4f (3.5%%)\n⏰ Exit at market open ~9:28 AM ET\n\n<i>Executed by Sentinel.</i>'
                        % (key, lev, actual_price, amount, amount * lev, sl_price))
            self.write_dashboard(now_utc)
        except Exception as e:
            logger.error('[REAL:%s] open failed: %s', key, e)
            self.notify('❌ <b>REAL %s open FAILED</b>\n%s' % (key, e))

    def _close(self, key, cfg, reason, now_utc, et, hint_price):
        sym = cfg['symbol']
        pos = self.st['positions'].get(key)
        if not pos:
            return False
        try:
            pr = self.feed.position_risk(sym)
            step = self.feed.step_size(sym)
            qty = self.feed.round_step(pr['qty'], step)
            if qty < step:
                logger.warning('[REAL:%s] no exchange position found at close -- clearing local state', key)
                self.st['positions'][key] = None
                self.save()
                return True
            resp = self.feed.market_order(sym, 'SELL', qty, reduce_only=True)
            actual_close = self.feed.fill_price(resp, hint_price)
            entry_price = pos['entry_price']
            total_fee = pos.get('entry_fee', 0.0) + qty * actual_close * FEE_RATE
            gross = (actual_close - entry_price) * qty
            net = gross - total_fee
            self.st.setdefault('trades', {}).setdefault(key, []).append({
                'opened_at': pos.get('opened_at'), 'closed_at': now_iso(now_utc.timestamp()),
                'entry_price': entry_price, 'exit_price': actual_close, 'qty': qty,
                'gross': round(gross, 4), 'fee': round(total_fee, 4), 'net': round(net, 4), 'reason': reason,
            })
            self.st['positions'][key] = None
            self.save()
            emoji = '🟢' if net >= 0 else '🔴'
            logger.info('🌅 [REAL:%s] CLOSE @ $%.4f net=%+.4f reason=%s', key, actual_close, net, reason)
            self.notify('%s $%+.2f\n🌅 <b>REAL — %s CLOSED (%s)</b>\n\n💰 Exit: $%.4f | Entry: $%.4f\n'
                        '✅ Gross: %+.4f | 💸 Fees: -%.4f\n\n<i>Executed by Sentinel.</i>'
                        % (emoji, net, key, reason, actual_close, entry_price, gross, total_fee))
            self.write_dashboard(now_utc)
            return True
        except Exception as e:
            logger.error('[REAL:%s] close failed: %s', key, e)
            self.notify('❌ <b>REAL %s close FAILED</b>\n%s\n\nPosition may still be open on the '
                        'exchange -- check manually.' % (key, e))
            return False

    def write_dashboard(self, now_utc):
        out = {'generated_at': now_iso(now_utc.timestamp()), 'real_money': True}
        for key, cfg in REAL_OVERNIGHT_CFG.items():
            pos = self.st['positions'].get(key)
            trades = self.st.get('trades', {}).get(key, [])
            current_price = unrealized_pnl = None
            if pos:
                try:
                    pr = self.feed.position_risk(cfg['symbol'])
                    current_price = self.feed.quote(cfg['symbol'])[0]
                    unrealized_pnl = pr.get('unrealized_pnl')
                except Exception:
                    pass
            wins   = [t for t in trades if t.get('net', 0) > 0]
            losses = [t for t in trades if t.get('net', 0) <= 0]
            out[key] = {
                'base': key, 'symbol': cfg['symbol'], 'real_money': True,
                'position': ({'direction': 'LONG', 'entry_price': pos['entry_price'], 'current_price': current_price,
                              'unrealized_pnl': unrealized_pnl, 'sl_price': pos['sl_price'], 'qty': pos['qty'],
                              'leverage': pos.get('leverage'), 'opened_at': pos.get('opened_at'),
                              # exit-controls fields -- consumed by mu_levels.js, same contract as Apex's
                              # data_overnight_mu.json (see trading_bot_futures.py write_overnight_dashboard())
                              'sl_default': round(pos['entry_price'] * (1 - cfg['sl_pct']), 4),
                              'sl_custom': bool(pos.get('sl_custom')), 'target_price': pos.get('target_price'),
                              'entry_fee': pos.get('entry_fee'), 'fee_rate': FEE_RATE,
                              'levels_status': pos.get('levels_status')} if pos else None),
                'performance': {
                    'total': len(trades), 'wins': len(wins), 'losses': len(losses),
                    'win_rate': round(len(wins) / len(trades) * 100, 1) if trades else 0,
                    'win_pnl': round(sum(t['net'] for t in wins), 2), 'loss_pnl': round(sum(t['net'] for t in losses), 2),
                    'total_fees': round(sum(t.get('fee', 0) for t in trades), 2),
                    'net_pnl': round(sum(t.get('net', 0) for t in trades), 2),
                },
                'trades': list(reversed(trades[-150:])),
            }
        if self.write_files:
            tmp = REAL_DASHBOARD_FILE + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(out, f, indent=2)
            os.replace(tmp, REAL_DASHBOARD_FILE)


# ══ entry points ══════════════════════════════════════════════════════════════
def selftest():
    """Read-only live check: prints what an entry would look like right now. Writes nothing, sends nothing."""
    feed = BinanceFeed()
    print('Sentinel self-test (read-only)  now ET: %s  | trading day: %s | in entry window: %s'
          % (et_from_utc(datetime.now(timezone.utc)).strftime('%Y-%m-%d %H:%M'), is_trading_day(et_from_utc(datetime.now(timezone.utc)).date()),
             in_entry_window(et_from_utc(datetime.now(timezone.utc)))))
    nxt = next_entry_utc(datetime.now(timezone.utc))
    print('next entry window opens: %s ET' % et_from_utc(nxt).strftime('%a %Y-%m-%d %H:%M'))
    ok = True
    for sym, meta in SYMBOLS.items():
        try:
            lev = feed.leverage(sym, STAKE_USDT)
            f = feed.filters(sym)
            bid, ask = feed.quote(sym)
            qty = floor_step(STAKE_USDT * lev / ask, f['step'])
            fl = feed.fill(sym, 'BUY', qty)
            sl = round_tick(fl['price'] * (1 - SL_PCT), f['tick'])
            print('%-9s status=%s lev=%dx qty=%s notional=$%s | bid %s ask %s | fill %.4f (%s, slip %+.2fbp vs mid) | stop %.4f'
                  % (sym, f['status'], lev, qty, format(qty * fl['price'], ',.2f'), bid, ask, fl['price'], fl['source'],
                     (fl['price'] / ((fl['bid'] + fl['ask']) / 2) - 1) * 1e4, sl))
        except Exception as e:
            ok = False
            print('%-9s FAILED: %s: %s' % (sym, type(e).__name__, e))
    try:
        feed.signed_get('/fapi/v1/order', {'symbol': 'SNDKUSDT'})
        print('!! guard failure: order endpoint was reachable')
        ok = False
    except PermissionError:
        print('guard OK: non-allow-listed signed endpoints are refused')
    return 0 if ok else 1


def main():
    if '--selftest' in sys.argv:
        sys.exit(selftest())
    st = load_state()
    feed = BinanceFeed()
    bot = Sentinel(feed, st)
    bot.startup(datetime.now(timezone.utc))

    real_st = load_real_state()
    real_engine = RealOvernightEngine(feed, real_st)   # same feed instance -- shares the leverage/filters cache, zero other coupling
    real_engine.write_dashboard(datetime.now(timezone.utc))
    held_real = [k for k, v in real_st['positions'].items() if v]
    logger.info('Sentinel running. PAPER symbols=%s stake=$%s | REAL symbols=%s%s',
                ','.join(SYMBOLS), STAKE_USDT, ','.join(REAL_OVERNIGHT_CFG),
                (' (resuming open: %s)' % ','.join(held_real)) if held_real else '')

    while True:
        t0 = time.time()
        try:
            bot.cycle(datetime.now(timezone.utc))
        except Exception as e:                        # never let the loop die
            logger.error('paper cycle crashed: %s', e, exc_info=True)
        try:
            real_engine.cycle(datetime.now(timezone.utc))
        except Exception as e:                        # never let the loop die -- paper trading must keep running even if this fails
            logger.error('real cycle crashed: %s', e, exc_info=True)
        extra, bot.backoff_s = bot.backoff_s, 0
        time.sleep(max(1.0, CYCLE_SECONDS - (time.time() - t0)) + extra)


if __name__ == '__main__':
    main()
