#!/usr/bin/env python3
"""SPY Options Signal Bot — GEX-based daily spread signals with dashboard API."""

import os, json, math, logging, time, threading
from datetime import datetime, date, timezone, timedelta
from math import log, sqrt, exp, pi, erf
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv('.env')
except Exception:
    pass

import requests

# ── Config ────────────────────────────────────────────────────────────────────
WEB_ROOT     = os.getenv('WEB_ROOT', '/var/www/apex')
SIGNAL_FILE  = os.path.join(WEB_ROOT, 'spy_signal.json')
STATE_FILE   = os.path.join(WEB_ROOT, 'spy_state.json')
TRIGGER_FILE = os.path.join(WEB_ROOT, 'spy_trigger.json')
API_PORT          = 5001
SPREAD_WIDTH      = 2       # SPY spread width ($)
SPX_SPREAD_WIDTH  = 10      # SPX spread width ($) — $10 wide is standard 0DTE
# A flat minimum credit (e.g. $0.05) isn't enough on its own — a $0.14 credit on
# a $2-wide spread still clears that bar but is a genuinely poor ~14:1 risk/reward
# (this happened live: a "last-call" BEAR_CALL 764/766 signal for $0.14 credit —
# 7% of width — got suggested and taken). Require credit to be a real fraction of
# the spread width regardless of the flat-dollar floor.
MIN_CREDIT_WIDTH_RATIO = 0.12   # credit must be >= 12% of spread width
# CBOE comparison uses no API key — public CDN endpoint

# SPY alerts go to their own channel (DISCORD_SPY_WEBHOOK).
# Falls back to the main webhook if not set.
DISCORD_SPY_WEBHOOK  = os.getenv('DISCORD_SPY_WEBHOOK', '')
DISCORD_WEBHOOK      = os.getenv('DISCORD_WEBHOOK_URL', '')
TELEGRAM_BOT_TOKEN   = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID     = os.getenv('TELEGRAM_CHAT_ID', '')

# Signal schedule (ET): (hour, minute, label, mst_display)
# 5 runs from market open → 12:15 PM — last useful 0DTE entry window
SCHEDULES = [
    (9,  35, 'market-open',  '7:35 AM MST'),   # 5 min after open — lets rotation settle
    (10, 15, 'mid-morning',  '8:15 AM MST'),
    (11,  0, 'late-morning', '9:00 AM MST'),
    (11, 45, 'pre-noon',     '9:45 AM MST'),
    (12, 15, 'last-call',    '10:15 AM MST'),
]

# VIX go/no-go thresholds
VIX_MIN = 12   # below this: spreads pay near nothing — skip
VIX_MAX = 30   # above this: 0DTE too volatile — skip (warn and skip)

# Daily loss limit — stop signaling when today's closed P&L hits this
DAILY_LOSS_LIMIT_USD = -110  # ≈ -$150 CAD

# ── Crypto 0DTE paper trading — BTC/ETH daily-expiry options on Binance ────────
# Same idea as the SPY signals above (0DTE credit spreads), different
# instrument (Binance's crypto options, eapi.binance.com) and — unlike SPY,
# which is signal-only because Binance can't execute it — this runs fully
# automatically since it IS on Binance. Paper only for now: every "trade" is
# priced from REAL live bid/ask/IV and tracked to REAL settlement, but no
# funds move. Validated first via backtest (2yr BTC/ETH history, out-of-
# sample checked) before this paper stage; see session notes.
FUTURES_BASE_URL         = 'https://fapi.binance.com'
OPTIONS_BASE_URL          = 'https://eapi.binance.com'
CRYPTO_STATE_FILE         = os.path.join(WEB_ROOT, 'crypto0dte_state.json')
CRYPTO_DASHBOARD_FILE     = os.path.join(WEB_ROOT, 'data_crypto0dte.json')
CRYPTO_SYMBOLS_CONFIG     = {
    'BTCUSDT': {'base': 'BTC', 'opt_prefix': 'BTC'},
    'ETHUSDT': {'base': 'ETH', 'opt_prefix': 'ETH'},
}
CRYPTO_TRADE_RISK_USD     = 100.0   # paper max-loss-per-spread — explicitly a paper test size
CRYPTO_K1                 = 0.7     # short strike distance, in 1-day sigma units (live IV based)
CRYPTO_K2                 = 0.5     # extra width beyond the short strike, in sigma units
CRYPTO_ADX_MIN            = 25.0
CRYPTO_CHECK_INTERVAL_SEC = 300     # 0DTE spreads aren't actively managed — just watched for rollover
CRYPTO_CHAIN_CACHE_TTL    = 3600    # strikes don't change intraday

# ── Economic calendar ─────────────────────────────────────────────────────────
# FOMC decision days, CPI and NFP release dates.
# Update annually:  FOMC → federalreserve.gov  |  CPI/NFP → bls.gov
HIGH_IMPACT_EVENTS: dict = {
    # 2026 — remaining
    '2026-09-04': 'NFP (Jobs Report)',
    '2026-09-10': 'CPI Release',
    '2026-09-17': 'FOMC Decision',
    '2026-10-02': 'NFP (Jobs Report)',
    '2026-10-13': 'CPI Release',
    '2026-10-29': 'FOMC Decision',
    '2026-11-06': 'NFP (Jobs Report)',
    '2026-11-12': 'CPI Release',
    '2026-12-04': 'NFP (Jobs Report)',
    '2026-12-10': 'CPI Release',
    '2026-12-16': 'FOMC Decision',
    # 2027 — update when Fed publishes schedule
}

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)
logger = logging.getLogger('spy_bot')

# ── ET timezone ───────────────────────────────────────────────────────────────
try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo('America/New_York')
    def et_now(): return datetime.now(_ET)
except Exception:
    def et_now():
        utc_m = datetime.now(timezone.utc).month
        return datetime.now(timezone(timedelta(hours=-4 if 3 <= utc_m <= 11 else -5)))

US_HOLIDAYS = {(1,1),(1,19),(2,16),(4,3),(5,25),(7,4),(9,7),(11,26),(12,25)}

def is_market_day() -> bool:
    n = et_now()
    return n.weekday() < 5 and (n.month, n.day) not in US_HOLIDAYS

def is_market_open() -> bool:
    if not is_market_day(): return False
    n = et_now()
    return n.hour > 9 or (n.hour == 9 and n.minute >= 30)

# ── State ─────────────────────────────────────────────────────────────────────
def load_state() -> dict:
    try:
        if os.path.exists(STATE_FILE):
            return json.load(open(STATE_FILE))
    except Exception:
        pass
    return {'trades': [], 'pnl': {}, 'fired': {}}

def save_state(st: dict):
    try:
        json.dump(st, open(STATE_FILE, 'w'), indent=2, default=str)
    except Exception as e:
        logger.warning(f'save_state: {e}')

def load_signal() -> dict:
    try:
        if os.path.exists(SIGNAL_FILE):
            return json.load(open(SIGNAL_FILE))
    except Exception:
        pass
    return {}

def save_signal(sig: dict):
    try:
        json.dump(sig, open(SIGNAL_FILE, 'w'), indent=2, default=str)
    except Exception as e:
        logger.warning(f'save_signal: {e}')

def load_crypto_state() -> dict:
    try:
        if os.path.exists(CRYPTO_STATE_FILE):
            data = json.load(open(CRYPTO_STATE_FILE))
        else:
            data = {}
    except Exception:
        data = {}
    data.setdefault('symbols', {})
    for sym in CRYPTO_SYMBOLS_CONFIG:
        data['symbols'].setdefault(sym, {'current_option_date': None, 'open_positions': [], 'trades': []})
        sym_state = data['symbols'][sym]
        # Migration: positions opened before held_expiry_ms/expiry_ms existed
        # would otherwise look "expired" on the first check and get settled
        # early. Backfill from the date code in the position's own symbol
        # string (format PREFIX-YYMMDD-STRIKE-C/P, expiry always 08:00 UTC).
        if sym_state.get('held_expiry_ms') is None and sym_state.get('open_positions'):
            first = sym_state['open_positions'][0]
            expiry_ms = first.get('expiry_ms')
            if expiry_ms is None:
                try:
                    date_code = first['short_symbol'].split('-')[1]
                    dt = datetime.strptime(date_code, '%y%m%d').replace(hour=8, tzinfo=timezone.utc)
                    expiry_ms = dt.timestamp() * 1000
                except Exception:
                    expiry_ms = None
            if expiry_ms is not None:
                sym_state['held_expiry_ms'] = expiry_ms
                for p in sym_state['open_positions']:
                    p.setdefault('expiry_ms', expiry_ms)
    return data

def save_crypto_state(st: dict):
    try:
        json.dump(st, open(CRYPTO_STATE_FILE, 'w'), indent=2, default=str)
    except Exception as e:
        logger.warning(f'save_crypto_state: {e}')

# ── Helpers ───────────────────────────────────────────────────────────────────
def _safe_float(v, default: float = 0.0) -> float:
    """float() that converts NaN and None to default. NaN is truthy so 'x or 0' doesn't catch it."""
    try:
        f = float(v) if v is not None else default
        return default if f != f else f  # f != f is True only for NaN
    except (TypeError, ValueError):
        return default

# ── VIX ──────────────────────────────────────────────────────────────────────
def fetch_vix() -> Optional[float]:
    """Current VIX level from Yahoo Finance — same yf import already used for SPY."""
    try:
        import yfinance as yf
        fi = yf.Ticker('^VIX').fast_info
        v = fi.get('lastPrice') or fi.get('regularMarketPrice') or fi.get('previousClose')
        return round(float(v), 2) if v else None
    except Exception as e:
        logger.warning(f'fetch_vix: {e}')
        return None

def today_market_event() -> Optional[str]:
    """Returns event label if today is a high-impact economic day, else None."""
    return HIGH_IMPACT_EVENTS.get(et_now().strftime('%Y-%m-%d'))

def fetch_spy_daily_trend() -> Optional[str]:
    """20-day EMA trend from SPY daily closes. Returns 'BULLISH', 'BEARISH', or None."""
    try:
        import yfinance as yf
        hist = yf.Ticker('SPY').history(period='25d', interval='1d')
        if hist.empty or len(hist) < 20:
            return None
        closes = hist['Close'].dropna()
        ema20  = float(closes.ewm(span=20, adjust=False).mean().iloc[-1])
        last   = float(closes.iloc[-1])
        trend  = 'BULLISH' if last > ema20 else 'BEARISH'
        logger.info(f'SPY daily trend: {trend} (close ${last:.2f} vs EMA20 ${ema20:.2f})')
        return trend
    except Exception as e:
        logger.warning(f'fetch_spy_daily_trend: {e}')
        return None

# ── Black-Scholes gamma ───────────────────────────────────────────────────────
def _norm_pdf(x: float) -> float:
    return exp(-0.5 * x * x) / sqrt(2 * pi)

def bs_gamma(S: float, K: float, T: float, iv: float, r: float = 0.05) -> float:
    """Black-Scholes gamma. T in years, iv as decimal (e.g. 0.20)."""
    if T <= 0 or iv <= 0 or S <= 0 or K <= 0:
        return 0.0
    try:
        d1 = (log(S / K) + (r + 0.5 * iv * iv) * T) / (iv * sqrt(T))
        return _norm_pdf(d1) / (S * iv * sqrt(T))
    except Exception:
        return 0.0

def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + erf(x / sqrt(2)))

def bs_delta(S: float, K: float, T: float, iv: float, is_call: bool, r: float = 0.05) -> float:
    """Black-Scholes delta. Also the standard options-trading approximation
    for the option's own risk-neutral probability of finishing ITM at expiry
    (i.e. win probability for a short position = 1 - |delta|)."""
    if T <= 0 or iv <= 0 or S <= 0 or K <= 0:
        return 1.0 if (is_call and S > K) else (-1.0 if (not is_call and S < K) else 0.0)
    try:
        d1 = (log(S / K) + (r + 0.5 * iv * iv) * T) / (iv * sqrt(T))
        return _norm_cdf(d1) if is_call else _norm_cdf(d1) - 1
    except Exception:
        return 0.0

# ── GEX + options chain ───────────────────────────────────────────────────────
def fetch_spy_chain() -> Optional[dict]:
    try:
        import yfinance as yf
        ticker = yf.Ticker('SPY')
        fi   = ticker.fast_info
        spot = (fi.get('lastPrice') or fi.get('regularMarketPrice') or
                fi.get('previousClose') or fi.get('regularMarketPreviousClose'))
        if not spot or spot <= 0:
            return None

        exps = ticker.options
        if not exps:
            return None

        today_str = date.today().strftime('%Y-%m-%d')
        target    = today_str if today_str in exps else exps[0]

        exp_date = datetime.strptime(target, '%Y-%m-%d').date()
        T_days   = max((exp_date - date.today()).days, 0)
        T_years  = max(T_days / 365.0, 1 / 365.0)  # at least 1 day for 0DTE

        chain    = ticker.option_chain(target)
        calls    = chain.calls
        puts     = chain.puts

        gex: dict      = {}
        vol_gex: dict  = {}   # same formula, weighted by VOLUME not OI — genuinely
                              # intraday-live, unlike OI which is frozen until tomorrow
        call_map: dict = {}
        put_map: dict  = {}

        for _, r in calls.iterrows():
            s   = _safe_float(r['strike'])
            iv  = _safe_float(r.get('impliedVolatility'))
            g   = bs_gamma(float(spot), s, T_years, iv)
            oi  = _safe_float(r.get('openInterest'))
            vol = _safe_float(r.get('volume'))
            gex[s]     = gex.get(s, 0) + g * oi * 100 * float(spot)
            vol_gex[s] = vol_gex.get(s, 0) + g * vol * 100 * float(spot)
            call_map[s] = {
                'bid': _safe_float(r.get('bid')),
                'ask': _safe_float(r.get('ask')),
                'oi':  int(oi),
                'iv':  iv,
                'volume': int(vol),
            }

        for _, r in puts.iterrows():
            s   = _safe_float(r['strike'])
            iv  = _safe_float(r.get('impliedVolatility'))
            g   = bs_gamma(float(spot), s, T_years, iv)
            oi  = _safe_float(r.get('openInterest'))
            vol = _safe_float(r.get('volume'))
            gex[s]     = gex.get(s, 0) - g * oi * 100 * float(spot)
            vol_gex[s] = vol_gex.get(s, 0) - g * vol * 100 * float(spot)
            put_map[s] = {
                'bid': _safe_float(r.get('bid')),
                'ask': _safe_float(r.get('ask')),
                'oi':  int(oi),
                'iv':  iv,
                'volume': int(vol),
            }

        total_call_oi = sum(v['oi'] for v in call_map.values())
        total_put_oi  = sum(v['oi'] for v in put_map.values())
        pc_ratio = round(total_put_oi / total_call_oi, 2) if total_call_oi else None

        total_call_vol = sum(v['volume'] for v in call_map.values())
        total_put_vol  = sum(v['volume'] for v in put_map.values())
        pc_ratio_vol = round(total_put_vol / total_call_vol, 2) if total_call_vol else None

        # Max pain — the strike where option WRITERS (net) owe the least if
        # price settled there today. Simple, well-known formula using OI.
        all_strikes = sorted(set(call_map) | set(put_map))
        max_pain = None
        if all_strikes:
            best_strike, best_payout = None, None
            for k in all_strikes:
                payout = 0.0
                for s2, v2 in call_map.items():
                    if k > s2: payout += (k - s2) * v2['oi']
                for s2, v2 in put_map.items():
                    if k < s2: payout += (s2 - k) * v2['oi']
                if best_payout is None or payout < best_payout:
                    best_payout, best_strike = payout, k
            max_pain = best_strike

        return {
            'spot':      round(float(spot), 2),
            'expiry':    target,
            'gex':       gex,
            'vol_gex':   vol_gex,
            'call_map':  call_map,
            'put_map':   put_map,
            'total_gex': sum(gex.values()),
            'total_vol_gex': sum(vol_gex.values()),
            'pc_ratio':  pc_ratio,
            'pc_ratio_vol': pc_ratio_vol,
            'max_pain':  max_pain,
            'T_years':   T_years,
        }
    except Exception as e:
        logger.error(f'fetch_spy_chain: {e}')
        return None

# ── CBOE GEX comparison (no API key needed) ───────────────────────────────────
def fetch_cboe_gex(expiry: str, spot: float) -> Optional[dict]:
    """
    Fetch SPY options from CBOE's public CDN (delayed ~15 min, no auth).
    CBOE provides pre-calculated gamma. Falls back to BS when gamma=0 (deep OTM/ITM).
    Option symbol format: SPY260827C00760000 → YYMMDD + C/P + strike*1000
    """
    try:
        resp = requests.get(
            'https://cdn.cboe.com/api/global/delayed_quotes/options/SPY.json',
            timeout=15,
            headers={'User-Agent': 'Mozilla/5.0'},
        )
        resp.raise_for_status()
        data    = resp.json()
        options = data.get('data', {}).get('options', [])
        if not options:
            logger.warning('CBOE: no options returned')
            return None

        exp_date  = datetime.strptime(expiry, '%Y-%m-%d').date()
        T_days    = max((exp_date - date.today()).days, 0)
        T_years   = max(T_days / 365.0, 1 / 365.0)
        exp_short = exp_date.strftime('%y%m%d')  # YYMMDD to match symbol

        gex: dict = {}
        for opt in options:
            sym = opt.get('option', '')
            # Symbol: SPY{YYMMDD}{C|P}{strike*1000 zero-padded to 8 chars}
            if len(sym) < 15:
                continue
            body = sym[3:]  # strip "SPY"
            if not body[:6] == exp_short:
                continue
            ctype = body[6]  # 'C' or 'P'
            try:
                s   = float(body[7:]) / 1000.0
                oi  = float(opt.get('open_interest', 0) or 0)
                raw_g = float(opt.get('gamma', 0) or 0)
                # CBOE gamma is per-share; use it if non-zero, else compute from IV
                if raw_g != 0.0:
                    g = abs(raw_g)
                else:
                    iv = float(opt.get('iv', 0) or 0)
                    g  = bs_gamma(spot, s, T_years, iv)

                if ctype == 'C':
                    gex[s] = gex.get(s, 0) + g * oi * 100 * spot
                elif ctype == 'P':
                    gex[s] = gex.get(s, 0) - g * oi * 100 * spot
            except Exception:
                continue

        if not gex:
            logger.warning(f'CBOE: no contracts matched expiry {expiry} (exp_short={exp_short})')
            return None

        total       = sum(gex.values())
        total_gex_b = round(total / 1e9, 2)
        nearby      = {s: v for s, v in gex.items() if abs(s - spot) <= 20}
        pin_strike  = max(nearby, key=lambda s: nearby[s]) if nearby else None
        below_pos   = {s: v for s, v in nearby.items() if s < spot and v > 0}
        above_pos   = {s: v for s, v in nearby.items() if s > spot and v > 0}
        lower_wall  = max(below_pos, key=lambda s: below_pos[s]) if below_pos else spot - 6
        upper_wall  = max(above_pos, key=lambda s: above_pos[s]) if above_pos else spot + 6
        direction   = 'BULL_PUT' if (spot - lower_wall) <= (upper_wall - spot) else 'BEAR_CALL'

        logger.info(f'CBOE GEX: ${total_gex_b:.2f}B | pin={pin_strike} | {direction}')
        return {
            'source':      'CBOE',
            'total_gex_b': total_gex_b,
            'gex_regime':  'positive' if total >= 0 else 'negative',
            'pin_strike':  pin_strike,
            'upper_wall':  upper_wall,
            'lower_wall':  lower_wall,
            'direction':   direction,
        }
    except Exception as e:
        logger.warning(f'fetch_cboe_gex: {e}')
        return None

# ── Spread builder ────────────────────────────────────────────────────────────
def make_spread(data: dict, direction: str, short_s: float) -> Optional[dict]:
    pm, cm = data['put_map'], data['call_map']
    w = SPREAD_WIDTH

    if direction == 'BULL_PUT':
        long_s    = short_s - w
        short_leg = pm.get(short_s, {})
        short_bid = short_leg.get('bid', 0)
        long_ask  = pm.get(long_s,  {}).get('ask', 0)
    else:
        long_s    = short_s + w
        short_leg = cm.get(short_s, {})
        short_bid = short_leg.get('bid', 0)
        long_ask  = cm.get(long_s,  {}).get('ask', 0)

    credit = round(float(short_bid) - float(long_ask), 2)
    if credit <= 0.05 or credit < w * MIN_CREDIT_WIDTH_RATIO:
        return None

    be = round(short_s - credit, 2) if direction == 'BULL_PUT' else round(short_s + credit, 2)

    # Win probability: the short leg's own delta approximates its risk-neutral
    # probability of finishing ITM at expiry (standard options heuristic), so
    # 1-|delta| is the probability this spread expires at max profit.
    win_probability = None
    short_iv = short_leg.get('iv')
    if short_iv:
        delta = bs_delta(data['spot'], short_s, data['T_years'], short_iv, is_call=(direction=='BEAR_CALL'))
        win_probability = round((1 - abs(delta)) * 100, 1)

    return {
        'direction':    direction,
        'short_strike': short_s,
        'long_strike':  long_s,
        'short_bid':    round(float(short_bid), 2),
        'long_ask':     round(float(long_ask), 2),
        'net_credit':   credit,
        'max_profit':   round(credit * 100, 2),
        'max_loss':     round((w - credit) * 100, 2),
        'breakeven':    be,
        'width':        w,
        'win_probability': win_probability,
    }

# ── SPX parallel spread ───────────────────────────────────────────────────────
def fetch_spx_spreads(direction: str, spy_spot: float, spy_lower: float, spy_upper: float) -> Optional[dict]:
    """
    Fetch SPX options and calculate equivalent spreads using the same direction as SPY GEX.
    Strike selection scales the SPY wall distance proportionally to SPX price.
    Returns 3 variants (Conservative/Suggested/Aggressive) + instructions.
    """
    try:
        import yfinance as yf
        ticker = yf.Ticker('^SPX')
        fi     = ticker.fast_info
        spot   = float(fi.get('lastPrice') or fi.get('previousClose') or 0)
        if not spot:
            return None

        exps      = ticker.options
        today_str = date.today().strftime('%Y-%m-%d')
        expiry    = today_str if today_str in exps else exps[0]
        chain     = ticker.option_chain(expiry)
        w         = SPX_SPREAD_WIDTH

        # Scale SPY wall distance to SPX price proportionally
        ratio = spot / spy_spot if spy_spot else 10.0
        if direction == 'BULL_PUT':
            wall_pct  = (spy_spot - spy_lower) / spy_spot
            base_s    = round(spot * (1 - wall_pct) / 5) * 5  # round to nearest $5
            variants  = [(+10, 'Conservative'), (0, 'Suggested'), (-10, 'Aggressive')]
            pm        = {float(r['strike']): r for _, r in chain.puts.iterrows()}
        else:
            wall_pct  = (spy_upper - spy_spot) / spy_spot
            base_s    = round(spot * (1 + wall_pct) / 5) * 5
            variants  = [(-10, 'Conservative'), (0, 'Suggested'), (+10, 'Aggressive')]
            cm        = {float(r['strike']): r for _, r in chain.calls.iterrows()}

        def _spx_spread(short_s: float) -> Optional[dict]:
            long_s = float(short_s - w) if direction == 'BULL_PUT' else float(short_s + w)
            row_s  = pm.get(short_s, {}) if direction == 'BULL_PUT' else cm.get(short_s, {})
            row_l  = pm.get(long_s,  {}) if direction == 'BULL_PUT' else cm.get(long_s,  {})
            short_bid = _safe_float(row_s.get('bid'))
            long_ask  = _safe_float(row_l.get('ask'))
            credit    = round(short_bid - long_ask, 2)
            if credit <= 0.10 or credit < w * MIN_CREDIT_WIDTH_RATIO:
                return None
            be = round(short_s - credit, 2) if direction == 'BULL_PUT' else round(short_s + credit, 2)
            return {
                'direction':    direction,
                'short_strike': short_s,
                'long_strike':  long_s,
                'short_bid':    round(short_bid, 2),
                'long_ask':     round(long_ask, 2),
                'net_credit':   credit,
                'max_profit':   round(credit * 100, 2),
                'max_loss':     round((w - credit) * 100, 2),
                'breakeven':    be,
                'width':        w,
            }

        spreads = []
        for (delta, lbl) in variants:
            sp = _spx_spread(float(base_s + delta))
            if sp:
                sp['label'] = lbl
                spreads.append(sp)

        # Fallback: walk from spot toward wall in $5 steps
        if not spreads:
            candidates = []
            step = 5
            if direction == 'BULL_PUT':
                spx_floor = round(spot * (1 - (spy_spot - spy_lower) / spy_spot) / step) * step
                for s in range(int(spot) - step, int(spx_floor) - step, -step):
                    sp = _spx_spread(float(s))
                    if sp:
                        candidates.append(sp)
                        if len(candidates) >= 3:
                            break
                candidates.reverse()
            else:
                spx_ceil = round(spot * (1 + (spy_upper - spy_spot) / spy_spot) / step) * step
                for s in range(int(spot) + step, int(spx_ceil) + step * 2, step):
                    sp = _spx_spread(float(s))
                    if sp:
                        candidates.append(sp)
                        if len(candidates) >= 3:
                            break
                candidates.reverse()
            lbls = ['Conservative', 'Suggested', 'Aggressive']
            for i, sp in enumerate(candidates):
                sp['label'] = lbls[min(i, len(lbls) - 1)]
            spreads = candidates

        if not spreads:
            return None

        suggested = next((s for s in spreads if s['label'] == 'Suggested'), spreads[0])
        s   = suggested
        tab = 'Put' if direction == 'BULL_PUT' else 'Call'
        instructions = [
            f"Open SPX on Wealthsimple → Options → **{tab}** tab",
            f"Expiry **{expiry}** | SPX at **${spot:.0f}**",
            f"Find **${s['short_strike']:.0f}** → tap 🔴 **Bid (Sell)** = ${s['short_bid']:.2f}",
            f"Find **${s['long_strike']:.0f}** → tap 🟢 **Ask (Buy)** = ${s['long_ask']:.2f}",
            f"Net credit: **${s['net_credit']:.2f}/share = ${s['max_profit']:.0f} per contract**",
            f"Max loss: **${s['max_loss']:.0f}/contract** | Breakeven: **${s['breakeven']:.2f}**",
            f"⚠️ Cash settled — no assignment risk",
        ]

        return {
            'spx_price':    round(spot, 2),
            'expiry':       expiry,
            'direction':    direction,
            'suggested':    s,
            'spreads':      spreads,
            'instructions': instructions,
        }
    except Exception as e:
        logger.warning(f'fetch_spx_spreads: {e}')
        return None

CONFIDENCE_MAX = 7

def compute_confidence(direction: str, vix: Optional[float], pc_ratio: Optional[float],
                       gex_compare: Optional[dict], gex_regime: str,
                       volume_gex_direction: Optional[str] = None,
                       pc_ratio_vol: Optional[float] = None,
                       spy_trend: Optional[str] = None) -> int:
    """Score 0–7: original 4 (VIX zone, OI P/C confirms, CBOE agrees, positive
    GEX regime) plus 3 genuinely-live confirmations — volume-weighted GEX
    agreeing with the OI-based direction, live (volume) P/C ratio confirming,
    and SPY's own daily trend not conflicting. More agreement across
    independent, differently-stale signals = a real confluence, not just a
    bigger number for its own sake."""
    score = 0
    if vix is not None and 15 <= vix <= 20:
        score += 1
    if pc_ratio is not None:
        if direction == 'BULL_PUT' and pc_ratio < 0.9:
            score += 1
        elif direction == 'BEAR_CALL' and pc_ratio > 1.0:
            score += 1
    if gex_compare and gex_compare.get('direction_agree'):
        score += 1
    if gex_regime == 'positive':
        score += 1
    # Genuinely-live confirmations (not built on stale OI):
    if volume_gex_direction is not None and volume_gex_direction == direction:
        score += 1
    if pc_ratio_vol is not None:
        if direction == 'BULL_PUT' and pc_ratio_vol < 0.9:
            score += 1
        elif direction == 'BEAR_CALL' and pc_ratio_vol > 1.0:
            score += 1
    if spy_trend is not None:
        trend_agrees = (spy_trend == 'BULLISH' and direction == 'BULL_PUT') or \
                       (spy_trend == 'BEARISH' and direction == 'BEAR_CALL')
        if trend_agrees:
            score += 1
    return score


def build_signal(data: dict, label: str, vix: Optional[float] = None, spy_trend: Optional[str] = None) -> Optional[dict]:
    spot      = data['spot']
    gex       = data['gex']
    total_gex = data['total_gex']

    # Key levels: strikes within ±20 of spot
    nearby = {s: g for s, g in gex.items() if abs(s - spot) <= 20}
    if not nearby:
        return None

    pin_strike  = max(nearby, key=lambda s: nearby[s])
    below_pos   = {s: g for s, g in nearby.items() if s < spot and g > 0}
    above_pos   = {s: g for s, g in nearby.items() if s > spot and g > 0}
    lower_wall  = max(below_pos, key=lambda s: below_pos[s]) if below_pos else spot - 6
    upper_wall  = max(above_pos, key=lambda s: above_pos[s]) if above_pos else spot + 6
    gex_regime  = 'positive' if total_gex > 0 else 'negative'

    # Direction: sell the side with the stronger GEX wall
    lower_dist = spot - lower_wall
    upper_dist = upper_wall - spot
    direction  = 'BULL_PUT' if lower_dist <= upper_dist else 'BEAR_CALL'

    # Volume-weighted GEX — same wall logic, but weighted by today's live
    # trading volume instead of yesterday's frozen open interest. This is
    # the genuinely fresh cross-check: when it disagrees with the OI-based
    # direction above, that's a real signal something has shifted today
    # that the primary (stale) signal can't see.
    vol_gex = data.get('vol_gex', {})
    vg_nearby = {s: g for s, g in vol_gex.items() if abs(s - spot) <= 20}
    vg_below_pos = {s: g for s, g in vg_nearby.items() if s < spot and g > 0}
    vg_above_pos = {s: g for s, g in vg_nearby.items() if s > spot and g > 0}
    vg_lower_wall = max(vg_below_pos, key=lambda s: vg_below_pos[s]) if vg_below_pos else None
    vg_upper_wall = max(vg_above_pos, key=lambda s: vg_above_pos[s]) if vg_above_pos else None
    volume_gex_direction = None
    if vg_lower_wall is not None and vg_upper_wall is not None:
        volume_gex_direction = 'BULL_PUT' if (spot - vg_lower_wall) <= (vg_upper_wall - spot) else 'BEAR_CALL'

    # 3 strike variants — "Suggested" sits at the nearest whole-dollar strike
    # to the wall itself (SPY strikes are $1 apart near the money, no $0.50
    # increments exist, so this is the tightest buffer actually tradeable).
    # Backtested against 3 months of real SPY+VIX data: this placement beat a
    # full $1+ buffer on both win rate (81.3% vs 80.7%) and total P&L (+$962
    # vs +$544 over 75 vs 57 trades) — tighter meant more premium collected
    # per trade without a worse hit rate.
    # floor/ceil (not round) so the strike always lands on the safe side of
    # the wall -- rounding up on a BULL_PUT would put the short strike ABOVE
    # support, meaning it's already breached the moment price touches the wall.
    spreads = []
    if direction == 'BULL_PUT':
        base = math.floor(lower_wall)
        variants = [(+2, 'Conservative'), (0, 'Suggested'), (-2, 'Aggressive')]
    else:
        base = math.ceil(upper_wall)
        variants = [(-2, 'Conservative'), (0, 'Suggested'), (+2, 'Aggressive')]

    for (delta, lbl) in variants:
        short_s = float(base + delta) if direction == 'BEAR_CALL' else float(base - delta)
        sp = make_spread(data, direction, short_s)
        if sp:
            sp['label'] = lbl
            spreads.append(sp)

    # Fallback: GEX wall may be far from spot (low premium there).
    # Scan between wall and spot, starting at least $2 OTM to avoid near-ATM strikes.
    if not spreads:
        candidates = []
        if direction == 'BULL_PUT':
            # Start $2 below spot (floor), walk down to GEX wall — ensures min ~$2 OTM
            for s in range(int(spot) - 2, int(lower_wall) - 1, -1):
                sp = make_spread(data, direction, float(s))
                if sp:
                    candidates.append(sp)
                    if len(candidates) >= 3:
                        break
            candidates.reverse()  # lowest premium (furthest) first → Conservative
        else:
            # Start $2 above spot (ceil), walk up to GEX wall — ensures min ~$2 OTM
            for s in range(int(spot) + 2, int(upper_wall) + 2):
                sp = make_spread(data, direction, float(s))
                if sp:
                    candidates.append(sp)
                    if len(candidates) >= 3:
                        break
            candidates.reverse()  # lowest premium (furthest) first → Conservative
        lbls = ['Conservative', 'Suggested', 'Aggressive']
        for i, sp in enumerate(candidates):
            sp['label'] = lbls[min(i, len(lbls) - 1)]
        spreads = candidates
        if spreads:
            logger.info(f'[{label}] GEX-wall strikes illiquid — using spot-relative fallback strikes')

    if not spreads:
        return None

    suggested = next((s for s in spreads if s['label'] == 'Suggested'), spreads[0])
    s = suggested

    # Step-by-step instructions matching Wealthsimple UI
    tab = 'Put' if direction == 'BULL_PUT' else 'Call'
    expiry_display = data['expiry']
    instructions = [
        f"Open SPY on Wealthsimple → tap Options → switch to **{tab}** tab",
        f"Set expiry to **{expiry_display}**",
        f"Find **${s['short_strike']:.0f}** → tap 🔴 **Bid (Sell)** = ${s['short_bid']:.2f}",
        f"Find **${s['long_strike']:.0f}** → tap 🟢 **Ask (Buy)** = ${s['long_ask']:.2f}",
        f"Net credit: **${s['net_credit']:.2f}/share = ${s['max_profit']:.0f} per contract**",
        f"Max loss: **${s['max_loss']:.0f}/contract** | Breakeven: **${s['breakeven']:.2f}**",
    ]

    gex_b  = total_gex / 1e9

    # ── Alpha Vantage comparison (optional — only runs if key set) ────────────
    # ── SPX parallel spread ───────────────────────────────────────────────────
    spx = fetch_spx_spreads(direction, spot, lower_wall, upper_wall)
    if spx:
        logger.info(f'SPX {direction} ${spx["suggested"]["short_strike"]:.0f}/${spx["suggested"]["long_strike"]:.0f} credit ${spx["suggested"]["net_credit"]:.2f}')

    cboe   = fetch_cboe_gex(expiry_display, spot)
    if cboe:
        gex_diff_b = round(round(gex_b, 2) - cboe['total_gex_b'], 2)
        dir_agree  = cboe['direction'] == direction
        gex_compare = {
            'av_total_gex_b': cboe['total_gex_b'],
            'av_gex_regime':  cboe['gex_regime'],
            'av_pin_strike':  cboe['pin_strike'],
            'av_upper_wall':  cboe['upper_wall'],
            'av_lower_wall':  cboe['lower_wall'],
            'av_direction':   cboe['direction'],
            'diff_b':         gex_diff_b,
            'direction_agree': dir_agree,
            'source':         'CBOE',
        }
        logger.info(f'GEX compare — ours: ${gex_b:.2f}B | CBOE: ${cboe["total_gex_b"]:.2f}B | diff: ${gex_diff_b:.2f}B | dir_agree={dir_agree}')
    else:
        gex_compare = None

    confidence = compute_confidence(direction, vix, data.get('pc_ratio'), gex_compare, gex_regime,
                                     volume_gex_direction=volume_gex_direction,
                                     pc_ratio_vol=data.get('pc_ratio_vol'),
                                     spy_trend=spy_trend)

    # Plain-language checklist — one row per factor, computed once here so
    # the dashboard/Discord never have to re-derive agreement logic themselves.
    pc_ratio = data.get('pc_ratio')
    pc_ratio_vol = data.get('pc_ratio_vol')
    pc_confirms = (direction == 'BULL_PUT' and pc_ratio is not None and pc_ratio < 0.9) or \
                  (direction == 'BEAR_CALL' and pc_ratio is not None and pc_ratio > 1.0)
    pc_vol_confirms = (direction == 'BULL_PUT' and pc_ratio_vol is not None and pc_ratio_vol < 0.9) or \
                      (direction == 'BEAR_CALL' and pc_ratio_vol is not None and pc_ratio_vol > 1.0)
    trend_confirms = spy_trend is not None and (
        (spy_trend == 'BULLISH' and direction == 'BULL_PUT') or
        (spy_trend == 'BEARISH' and direction == 'BEAR_CALL'))
    confidence_checklist = [
        {'label': 'VIX in ideal selling zone (15-20)', 'pass': vix is not None and 15 <= vix <= 20, 'known': vix is not None},
        {'label': 'Open-interest P/C ratio confirms', 'pass': pc_confirms, 'known': pc_ratio is not None},
        {'label': 'CBOE cross-check agrees', 'pass': bool(gex_compare and gex_compare.get('direction_agree')), 'known': gex_compare is not None},
        {'label': 'GEX regime is positive (pinning)', 'pass': gex_regime == 'positive', 'known': True},
        {'label': "Today's live volume agrees (fresh)", 'pass': volume_gex_direction == direction, 'known': volume_gex_direction is not None},
        {'label': 'Live volume P/C ratio confirms', 'pass': pc_vol_confirms, 'known': pc_ratio_vol is not None},
        {'label': "SPY's own daily trend doesn't conflict", 'pass': trend_confirms, 'known': spy_trend is not None},
    ]

    return {
        'label':        label,
        'generated_at': et_now().isoformat(),
        'spy_price':    spot,
        'expiry':       expiry_display,
        'total_gex_b':  round(gex_b, 2),
        'gex_regime':   gex_regime,
        'pin_strike':   pin_strike,
        'upper_wall':   upper_wall,
        'lower_wall':   lower_wall,
        'direction':    direction,
        'tab':          tab,
        'suggested':    s,
        'spreads':      spreads,
        'instructions': instructions,
        'gex_compare':  gex_compare,
        'spx':          spx,
        'vix':          vix,
        'pc_ratio':     data.get('pc_ratio'),
        'pc_ratio_vol': data.get('pc_ratio_vol'),
        'volume_gex_direction': volume_gex_direction,
        'max_pain':     data.get('max_pain'),
        'spy_trend':    spy_trend,
        'confidence':   confidence,
        'confidence_max': CONFIDENCE_MAX,
        'confidence_label': confidence_label(confidence),
        'confidence_checklist': confidence_checklist,
        'open_trade':   None,
    }

def confidence_label(conf: int) -> str:
    """One plain-language word for the confidence score — this is the ONE
    thing to glance at; the checklist is the detail behind it, not the
    headline."""
    frac = conf / CONFIDENCE_MAX
    if frac >= 0.7:  return 'Strong'
    if frac >= 0.45: return 'Moderate'
    if frac > 0:     return 'Weak'
    return 'Skip'

# ── Discord ───────────────────────────────────────────────────────────────────
def _discord_fmt(msg: str) -> str:
    """Discord uses **bold** — prepend a divider so messages don't stack unreadably."""
    return '─────────────────────\n' + msg

def _telegram_fmt(msg: str) -> str:
    """Convert Discord markdown to Telegram MarkdownV2-safe plain bold."""
    import re
    # **text** → *text* (Telegram bold)
    msg = re.sub(r'\*\*(.*?)\*\*', r'*\1*', msg)
    # Escape MarkdownV2 special chars outside of bold markers
    # Use plain Markdown (v1) instead — simpler and sufficient
    return msg

def _send_discord(msg: str):
    target = DISCORD_SPY_WEBHOOK or DISCORD_WEBHOOK
    if not target:
        return
    try:
        requests.post(target, json={'content': _discord_fmt(msg)}, timeout=10)
    except Exception as e:
        logger.warning(f'discord send failed: {e}')

def _send_telegram(msg: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    text = _telegram_fmt(msg)
    try:
        r = requests.post(
            f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage',
            json={'chat_id': TELEGRAM_CHAT_ID, 'text': text, 'parse_mode': 'Markdown'},
            timeout=10,
        )
        if not r.json().get('ok'):
            # Markdown parse error — retry as plain text
            requests.post(
                f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage',
                json={'chat_id': TELEGRAM_CHAT_ID, 'text': text},
                timeout=10,
            )
    except Exception as e:
        logger.warning(f'telegram send failed: {e}')

def notify(msg: str):
    """Fire Discord and Telegram in parallel — failure of one never blocks the other."""
    t1 = threading.Thread(target=_send_discord,  args=(msg,), daemon=True)
    t2 = threading.Thread(target=_send_telegram, args=(msg,), daemon=True)
    t1.start()
    t2.start()

discord = notify

def format_discord(sig: dict) -> str:
    s   = sig['suggested']
    mst = {
        'market-open':  '7:35 AM MST 🔔',
        'mid-morning':  '8:15 AM MST 📊',
        'late-morning': '9:00 AM MST 🕙',
        'pre-noon':     '9:45 AM MST 🕒',
        'last-call':    '10:15 AM MST ⏰',
        'on_demand':    'On Demand 🔄',
        # legacy labels for backward compat
        'premarket':    '6:30 AM MST ☀️',
        'midmorning':   '9:15 AM MST 📊',
        'late':         '9:50 AM MST 🕙',
    }
    regime_txt = '📌 Pinning (range-bound)' if sig['gex_regime'] == 'positive' else '⚡ Trending (volatile)'
    dir_emoji  = '🐂' if sig['direction'] == 'BULL_PUT' else '🐻'
    dir_name   = 'Bull Put Spread' if sig['direction'] == 'BULL_PUT' else 'Bear Call Spread'
    close_at   = round(s['net_credit'] * 0.20, 2)

    open_note = '\n_⏳ Signal at 9:35 — open rotation settles first. Confirm 9:30 candle direction before entering._' \
        if sig['label'] == 'market-open' else ''
    event = sig.get('market_event')
    event_banner = f"\n🚨 **{event} day** — IV elevated, GEX unreliable. 1 contract max or skip." \
        if event else ''
    lines = [
        f"**📈 SPY Options — {mst.get(sig['label'], sig['label'])}**{open_note}{event_banner}",
        f"",
        f"SPY **${sig['spy_price']:.2f}** | Expiry **{sig['expiry']}**",
        f"GEX: {regime_txt} | Net GEX: **${sig['total_gex_b']:.1f}B**",
        f"📌 Pin **${sig['pin_strike']:.0f}** | Support **${sig['lower_wall']:.0f}** | Resist **${sig['upper_wall']:.0f}**",
    ]
    conf = sig.get('confidence', 0)
    conf_label = sig.get('confidence_label', '')
    label_emoji = {'Strong': '🟢', 'Moderate': '🟡', 'Weak': '🟠', 'Skip': '🔴'}.get(conf_label, '')
    stars = '⭐' * conf + '☆' * (CONFIDENCE_MAX - conf)
    lines.append(f"")
    lines.append(f"{label_emoji} **Confidence: {conf_label}** {stars} ({conf}/{CONFIDENCE_MAX})")
    for item in sig.get('confidence_checklist', []):
        if not item['known']:
            continue  # don't show checks we have no data for — nothing to read into
        icon = '✅' if item['pass'] else '❌'
        lines.append(f"  {icon} {item['label']}")

    # Tiers as (min score to reach this tier, label) — ordered low to high.
    SIZE_TIERS = [(0, '🚫 Skip — no factors align'), (1, '⚠️ 1 contract max'),
                  (3, '1 contract'), (5, '1–2 contracts'), (6, '🔥 Full size (2–3 contracts)')]
    sizing = SIZE_TIERS[0][1]
    for min_score, label in SIZE_TIERS:
        if conf >= min_score:
            sizing = label
    next_higher = next((min_score for min_score, _ in SIZE_TIERS if min_score > conf), None)
    size_note = f" _(need {next_higher - conf} more ✅ to size up)_" if next_higher is not None else ''
    # Late signal: cap at 1 contract when confidence is in the lower half — gamma risk spikes near noon
    if sig['label'] == 'last-call' and conf < CONFIDENCE_MAX // 2 + 1:
        sizing = '⏰ Late signal — 1 contract max (gamma risk near noon)'
        size_note = ''
    # High-impact event always caps at 1 contract
    if event:
        sizing = '⚠️ Event day — 1 contract max'
        size_note = ''
    lines.append(f"👉 Size: **{sizing}**{size_note}")
    win_prob = s.get('win_probability')
    if win_prob is not None:
        wp_emoji = '🟢' if win_prob >= 70 else '🟡' if win_prob >= 55 else '🔴'
        lines.append(f"{wp_emoji} **Win probability: {win_prob:.0f}%** (market-implied, via option delta)")

    lines += [
        f"",
        f"{dir_emoji} **{dir_name}** (Suggested)",
        f"",
    ]
    for i, step in enumerate(sig['instructions'], 1):
        lines.append(f"**{i}.** {step}")

    lines += [
        f"",
        f"🎯 Close when debit ≤ **${close_at:.2f}** (80% profit)",
        f"⏰ **Always close by 3:30 PM ET regardless**",
    ]

    # SPX comparison section
    spx = sig.get('spx')
    if spx:
        sx = spx['suggested']
        spx_close = round(sx['net_credit'] * 0.20, 2)
        lines += [
            f"",
            f"─────────────────────────",
            f"**📊 SPX Equivalent** (${spx['spx_price']:.0f}) — Cash settled, no assignment risk",
            f"{dir_emoji} **{dir_name}** ${sx['short_strike']:.0f}/${sx['long_strike']:.0f}",
            f"Credit: **${sx['net_credit']:.2f}** = **${sx['max_profit']:.0f}/contract** | Max loss: ${sx['max_loss']:.0f}",
            f"Margin used: ~${sx['max_loss']:.0f} | Close at: ${spx_close:.2f} debit",
            f"💡 vs SPY: {round(sx['max_profit']/max((sig['suggested']['max_profit'] or 1),1), 1)}x more premium per contract",
        ]

    lines += [
        f"",
        f"👇 Log trade at your SPY dashboard",
    ]
    return '\n'.join(lines)

# ── Trade monitoring ──────────────────────────────────────────────────────────
PROFIT_MILESTONES = [10, 25, 50, 65, 75, 80]   # alert at each of these %
LOSS_WARN_PCT     = -20                 # early heads-up when loss exceeds this
STOP_LOSS_MULT    = 2.5                 # firm stop: cost to close has grown to this many x the credit received
UPDATE_INTERVAL   = 900                 # send regular P&L update every 15 min

def check_trades(state: dict) -> list:
    open_trades = [t for t in state.get('trades', []) if t.get('status') == 'open']
    if not open_trades:
        return []

    alerts = []
    try:
        import yfinance as yf
        now_et = et_now()
        now_ts = time.time()

        # Pre-fetch spot prices for each ticker used across open trades
        _spots: dict = {}
        _yf_tickers: dict = {}
        for t in open_trades:
            tk = t.get('ticker', 'SPY')
            if tk not in _spots:
                yft = yf.Ticker('^SPX' if tk == 'SPX' else 'SPY')
                fi  = yft.fast_info
                sp  = float(fi.get('lastPrice') or fi.get('previousClose') or 0)
                _spots[tk]      = sp
                _yf_tickers[tk] = yft

        for trade in open_trades:
            ticker_name = trade.get('ticker', 'SPY')
            yf_ticker   = _yf_tickers.get(ticker_name)
            spot        = _spots.get(ticker_name, 0)
            if not yf_ticker or not spot:
                continue

            direction  = trade.get('direction')
            short_s    = float(trade.get('short_strike', 0))
            long_s     = float(trade.get('long_strike', 0))
            credit     = float(trade.get('credit', 0))
            contracts  = int(trade.get('contracts', 1))
            expiry     = trade.get('expiry', date.today().strftime('%Y-%m-%d'))
            tid        = trade.get('trade_id', '?')
            isBull     = direction == 'BULL_PUT'
            dir_name   = 'Bull Put' if isBull else 'Bear Call'

            try:
                chain = yf_ticker.option_chain(expiry)
                if isBull:
                    pm        = {float(r['strike']): r for _, r in chain.puts.iterrows()}
                    short_ask = _safe_float(pm.get(short_s, {}).get('ask'))
                    long_bid  = _safe_float(pm.get(long_s,  {}).get('bid'))
                else:
                    cm        = {float(r['strike']): r for _, r in chain.calls.iterrows()}
                    short_ask = _safe_float(cm.get(short_s, {}).get('ask'))
                    long_bid  = _safe_float(cm.get(long_s,  {}).get('bid'))

                cur_debit  = round(short_ask - long_bid, 2)
                profit_pct = round((credit - cur_debit) / credit * 100, 1) if credit else 0
                pnl_now    = round((credit - cur_debit) * 100 * contracts, 2)

                trade['current_debit'] = cur_debit
                trade['profit_pct']    = profit_pct
                trade['pnl_now']       = pnl_now
                trade['spot_now']      = spot

                milestones_hit = trade.setdefault('milestones_hit', [])
                last_update_ts = trade.get('last_update_ts', 0)

                # ── Profit milestones (fire once each) ──────────────────────
                for m in PROFIT_MILESTONES:
                    if profit_pct >= m and m not in milestones_hit:
                        milestones_hit.append(m)
                        emoji  = '🚀' if m >= 75 else '✅' if m >= 50 else '💰' if m >= 25 else '📍'
                        advice = 'Strong close signal — lock in gains!' if m >= 75 else \
                                 'Good time to close — solid profit secured' if m >= 65 else \
                                 'Consider closing half if you want to play it safe' if m >= 25 else \
                                 'Trade moving in your favour — hold or set a mental stop'
                        close_cost = round(cur_debit * 100 * contracts, 2)
                        alerts.append(
                            f"{emoji} **{ticker_name} {tid} — {m}% profit reached!**\n"
                            f"{dir_name} ${short_s:.0f}/${long_s:.0f} | {ticker_name} ${spot:.2f}\n"
                            f"Close now: buy back at **${cur_debit:.2f}** debit\n"
                            f"P&L if closed now: **+${pnl_now:.2f}** (${close_cost:.2f} cost × {contracts} contract{'s' if contracts>1 else ''})\n"
                            f"💡 {advice}"
                        )

                # ── Afternoon rule: suggest close at 30%+ after 2 PM ET ────
                if now_et.hour >= 14 and profit_pct >= 30 and 'afternoon_close' not in milestones_hit:
                    milestones_hit.append('afternoon_close')
                    alerts.append(
                        f"🕑 **{ticker_name} {tid} — Afternoon close suggestion**\n"
                        f"It's after 2 PM ET. Profit at **{profit_pct:.0f}%** (${pnl_now:+.2f}).\n"
                        f"Close debit: **${cur_debit:.2f}** | Time decay risk increasing — consider locking in."
                    )

                # ── Loss warning ────────────────────────────────────────────
                if profit_pct <= LOSS_WARN_PCT and 'loss_warn' not in milestones_hit:
                    milestones_hit.append('loss_warn')
                    danger = isBull and spot <= short_s + 1.0 or not isBull and spot >= short_s - 1.0
                    alerts.append(
                        f"🔴 **{ticker_name} {tid} — Loss warning {profit_pct:.0f}%**\n"
                        f"{ticker_name} ${spot:.2f} | P&L: **${pnl_now:+.2f}**\n"
                        f"{'⚠️ '+ticker_name+' approaching your short strike $'+str(int(short_s))+' — close to limit damage' if danger else 'Consider cutting loss now before it gets worse'}"
                    )

                # ── Stop loss (firm) ─────────────────────────────────────────
                # Backtested against 3 months of real SPY+VIX data: closing at
                # 2.5x the credit received caps the worst-case loss (~$167 ->
                # ~$100-113 per contract) at the cost of ~25-40% less total
                # profit, since some trades that hit this level do recover by
                # end of day if held. This is the firm "close it now" trigger;
                # the -20% warning above is just an earlier heads-up, not a stop.
                if credit and cur_debit >= credit * STOP_LOSS_MULT and 'stop_loss' not in milestones_hit:
                    milestones_hit.append('stop_loss')
                    alerts.append(
                        f"🛑 **{ticker_name} {tid} — STOP LOSS HIT**\n"
                        f"{ticker_name} ${spot:.2f} | P&L: **${pnl_now:+.2f}**\n"
                        f"Cost to close (${cur_debit:.2f}) has reached {STOP_LOSS_MULT}x your credit (${credit:.2f}).\n"
                        f"Close now on Wealthsimple — buy back at **${cur_debit:.2f}** debit to cap the loss here."
                    )

                # ── Strike approach warning ─────────────────────────────────
                near_strike = (isBull and spot <= short_s + 0.75) or (not isBull and spot >= short_s - 0.75)
                if near_strike and 'near_strike' not in milestones_hit:
                    milestones_hit.append('near_strike')
                    alerts.append(
                        f"⚠️ **{ticker_name} {tid} — Strike breach risk!**\n"
                        f"{ticker_name} **${spot:.2f}** is within $0.75 of your short strike **${short_s:.0f}**\n"
                        f"P&L now: **${pnl_now:+.2f}** | Close debit: **${cur_debit:.2f}**\n"
                        f"Recommend closing immediately to cap loss."
                    )

                # ── Regular P&L update every 15 min ────────────────────────
                if now_ts - last_update_ts >= UPDATE_INTERVAL:
                    trade['last_update_ts'] = now_ts
                    bar   = '█' * int(max(0, profit_pct) / 10) + '░' * (10 - int(max(0, profit_pct) / 10))
                    emoji = '📈' if profit_pct > 0 else '📉'
                    alerts.append(
                        f"{emoji} **{ticker_name} Position Update — {tid}**\n"
                        f"{dir_name} ${short_s:.0f}/${long_s:.0f} | {ticker_name} ${spot:.2f}\n"
                        f"`{bar}` **{profit_pct:+.1f}%** | P&L: **${pnl_now:+.2f}**\n"
                        f"Close cost: **${cur_debit:.2f}** debit | Expires: {expiry}"
                    )

            except Exception as e:
                logger.warning(f'monitor trade {tid}: {e}')

        # ── EOD warning 3:30 PM ET ──────────────────────────────────────────
        if now_et.hour == 15 and 25 <= now_et.minute <= 35:
            open_ids = [t['trade_id'] for t in open_trades]
            if open_ids:
                alerts.append(f"⏰ **EOD — Close all spreads NOW!**\nOpen positions: {', '.join(open_ids)}\nMarket closes in ~30 min — do not let 0DTE expire ITM.")

        save_state(state)
    except Exception as e:
        logger.warning(f'check_trades: {e}')

    return alerts

# ── Health ping ───────────────────────────────────────────────────────────────
def send_health_ping(state: dict) -> None:
    """9:25 AM ET pre-market ping — confirms bot is live and shows today's schedule."""
    vix = fetch_vix()
    today    = et_now().strftime('%Y-%m-%d')
    today_pnl = state.get('pnl', {}).get(today, 0)
    vix_ok   = (VIX_MIN <= vix <= VIX_MAX) if vix is not None else True
    if vix is not None:
        if vix < 15:
            vix_label = f'VIX {vix:.1f} 😴 low vol'
        elif vix < 20:
            vix_label = f'VIX {vix:.1f} ✅ ideal zone'
        elif vix < 25:
            vix_label = f'VIX {vix:.1f} 🟡 elevated'
        else:
            vix_label = f'VIX {vix:.1f} 🔴 high — may skip'
    else:
        vix_label = 'VIX: unavailable'
    sched_str = ' → '.join(mst for _, _, _, mst in SCHEDULES)
    status_icon = '✅' if vix_ok else '⚠️'
    goal_needed = 74.0 - today_pnl
    discord(
        f"☀️ **SPY Bot Ready — {et_now().strftime('%a %b %-d')}**\n"
        f"{status_icon} {vix_label} "
        f"{'(signals active)' if vix_ok else f'(signals may pause — outside [{VIX_MIN}–{VIX_MAX}])'}\n"
        f"📅 5 signals: {sched_str}\n"
        f"💰 Today P&L: **${today_pnl:+.2f}** | Goal: ${goal_needed:.2f} more to reach $74 (~$100 CAD)\n"
        f"🛑 Daily loss limit: ${DAILY_LOSS_LIMIT_USD} | Hard close: 3:30 PM ET"
    )


# ── Signal runner ─────────────────────────────────────────────────────────────
def run_signal(label: str, state: dict) -> dict:
    # ── VIX go/no-go ─────────────────────────────────────────────────────────
    vix = fetch_vix()
    if vix is not None:
        logger.info(f'[{label}] VIX={vix:.1f}')
        if vix < VIX_MIN:
            msg = (f"⏭️ **SPY Skipped — {label}**\n"
                   f"VIX **{vix:.1f}** is below {VIX_MIN} — spreads pay near nothing. "
                   f"Waiting for higher vol before selling premium.")
            discord(msg)
            logger.info(f'[{label}] Skipped — VIX too low ({vix:.1f} < {VIX_MIN})')
            return state
        if vix > VIX_MAX:
            msg = (f"⏭️ **SPY Skipped — {label}**\n"
                   f"VIX **{vix:.1f}** above {VIX_MAX} — 0DTE too volatile. "
                   f"Max 1 contract if you trade manually today.")
            discord(msg)
            logger.info(f'[{label}] Skipped — VIX too high ({vix:.1f} > {VIX_MAX})')
            return state

    # ── Daily loss limit ─────────────────────────────────────────────────────
    today     = et_now().strftime('%Y-%m-%d')
    today_pnl = state.get('pnl', {}).get(today, 0)
    if today_pnl <= DAILY_LOSS_LIMIT_USD:
        loss_key = f'{today}_loss_limit_alerted'
        if not state['fired'].get(loss_key):
            state['fired'][loss_key] = True
            save_state(state)
            discord(
                f"🛑 **SPY Daily Loss Limit — Signals Paused**\n"
                f"Today's P&L: **${today_pnl:+.2f}** (limit: ${DAILY_LOSS_LIMIT_USD} ≈ -$150 CAD)\n"
                f"No more signals today. Bot resumes tomorrow at 9:25 AM ET."
            )
        logger.info(f'[{label}] Skipped — daily loss limit (${today_pnl:+.2f} ≤ ${DAILY_LOSS_LIMIT_USD})')
        return state

    logger.info(f'[{label}] Fetching SPY data...')
    data = fetch_spy_chain()
    if not data:
        logger.error(f'[{label}] No data available')
        return state

    spy_trend = fetch_spy_daily_trend()
    sig = build_signal(data, label, vix=vix, spy_trend=spy_trend)
    if not sig:
        logger.warning(f'[{label}] Could not build signal (no liquid spreads)')
        return state

    sig['market_event'] = today_market_event()

    open_trade = next((t for t in state.get('trades', []) if t.get('status') == 'open'), None)
    sig['open_trade'] = open_trade
    save_signal(sig)
    discord(format_discord(sig))

    s = sig['suggested']
    logger.info(f'[{label}] {sig["direction"]} ${s["short_strike"]:.0f}/${s["long_strike"]:.0f} credit ${s["net_credit"]:.2f}')
    return state

# ── Trigger handler ───────────────────────────────────────────────────────────
def handle_trigger(trigger: dict, state: dict) -> dict:
    action = trigger.get('action', '')

    if action == 'calculate':
        state = run_signal('on_demand', state)

    elif action == 'register_trade':
        t      = trigger.get('trade', {})
        today  = et_now().strftime('%Y-%m-%d')
        t['trade_id']      = f"{today}-{len(state['trades'])+1}"
        t['status']        = 'open'
        t['expiry']        = t.get('expiry', today)
        t['registered_at'] = et_now().isoformat()
        state['trades'].append(t)
        save_state(state)
        sig = load_signal()
        sig['open_trade'] = t
        save_signal(sig)
        direction = t.get('direction', '')
        short_s   = t.get('short_strike', 0)
        long_s    = t.get('long_strike', 0)
        credit    = t.get('credit', 0)
        contracts = t.get('contracts', 1)
        tk_name = t.get('ticker', 'SPY')
        discord(
            f"📝 **{tk_name} Trade Registered**\n"
            f"{'Bull Put' if direction == 'BULL_PUT' else 'Bear Call'} "
            f"${short_s:.0f}/{long_s:.0f} | Credit ${credit:.2f} × {contracts} contracts\n"
            f"Max profit: ${credit*100*contracts:.0f} | Close at: ${round(credit*0.20,2):.2f} debit"
        )
        logger.info(f'Trade registered: {t["trade_id"]}')

    elif action == 'edit_trade':
        tid = trigger.get('trade_id')
        edits = trigger.get('trade', {})
        for t in state['trades']:
            if t['trade_id'] == tid and t['status'] == 'open':
                for field in ('short_strike', 'long_strike', 'credit', 'contracts', 'expiry'):
                    if field in edits and edits[field] not in (None, ''):
                        t[field] = edits[field]
                # Strikes/credit changed — old profit milestones no longer apply to the
                # corrected numbers, so clear them and let fresh ones fire from here.
                t['milestones_hit'] = []
                t.pop('current_debit', None)
                t.pop('profit_pct', None)
                t.pop('pnl_now', None)
                save_state(state)
                sig = load_signal()
                sig['open_trade'] = t
                save_signal(sig)
                direction = t.get('direction', '')
                short_s   = t.get('short_strike', 0)
                long_s    = t.get('long_strike', 0)
                credit    = t.get('credit', 0)
                contracts = t.get('contracts', 1)
                tk_name   = t.get('ticker', 'SPY')
                discord(
                    f"✏️ **{tk_name} Trade Corrected**\n"
                    f"{'Bull Put' if direction == 'BULL_PUT' else 'Bear Call'} "
                    f"${short_s:.0f}/{long_s:.0f} | Credit ${credit:.2f} × {contracts} contracts\n"
                    f"Max profit: ${credit*100*contracts:.0f} | Close at: ${round(credit*0.20,2):.2f} debit\n"
                    f"Profit tracking reset to match the corrected numbers."
                )
                logger.info(f'Trade edited: {tid}')
                break

    elif action == 'close_trade':
        tid   = trigger.get('trade_id')
        pnl   = float(trigger.get('pnl', 0))
        today = et_now().strftime('%Y-%m-%d')
        for t in state['trades']:
            if t['trade_id'] == tid and t['status'] == 'open':
                t['status']    = 'closed'
                t['pnl']       = round(pnl, 2)
                t['closed_at'] = et_now().isoformat()
                state['pnl'][today] = round(state['pnl'].get(today, 0) + pnl, 2)
                discord(f"{'🟢' if pnl >= 0 else '🔴'} **SPY Trade Closed** — Net P&L: **${pnl:+.2f}**")
                break
        sig = load_signal()
        sig['open_trade'] = None
        save_signal(sig)
        save_state(state)

    return state

# ── HTTP API server ───────────────────────────────────────────────────────────
class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """Plain HTTPServer handles one connection at a time with no read/write
    timeout — a single stuck or slow client (flaky mobile network, backgrounded
    app) blocks every other request indefinitely. Threading + a per-connection
    timeout means one bad client can only ever wedge itself, never the API."""
    daemon_threads = True

class APIHandler(BaseHTTPRequestHandler):
    timeout = 15  # seconds — abort a connection that stalls mid-request

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = self.path.split('?')[0].rstrip('/')
        if path == '/api/signal':
            body = open(SIGNAL_FILE).read() if os.path.exists(SIGNAL_FILE) else '{}'
        elif path == '/api/state':
            body = open(STATE_FILE).read() if os.path.exists(STATE_FILE) else '{}'
        elif path == '/api/next':
            body = json.dumps(next_signal_info())
        else:
            self.send_response(404); self.end_headers(); return

        self.send_response(200)
        self._cors()
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(body.encode())

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body   = self.rfile.read(length)
        try:
            data = json.loads(body)
            # Run calculate immediately in background thread — don't wait for main loop
            if data.get('action') == 'calculate':
                def _calc():
                    state = load_state()
                    run_signal('on_demand', state)
                threading.Thread(target=_calc, daemon=True).start()
            else:
                json.dump(data, open(TRIGGER_FILE, 'w'))
            self.send_response(200)
            self._cors()
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        except Exception as e:
            self.send_response(500); self._cors(); self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode())

    def log_message(self, *_): pass  # silence logs

def next_signal_info() -> dict:
    now = et_now()
    for (h, m, lbl, mst) in SCHEDULES:
        t = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if t > now:
            secs = int((t - now).total_seconds())
            return {'label': lbl, 'mst': mst, 'seconds_until': secs, 'et_time': f'{h:02d}:{m:02d} ET'}
    return {'label': 'none', 'mst': 'No more signals today', 'seconds_until': 0, 'et_time': ''}

# ── Crypto 0DTE — pure-python indicators (no pandas/ta dependency in this file) ─
def _crypto_ema(values, period):
    alpha = 2.0/(period+1)
    out = [None]*len(values)
    if not values: return out
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = alpha*values[i] + (1-alpha)*out[i-1]
    return out

def _crypto_wilder(values, period):
    alpha = 1.0/period
    out = [None]*len(values)
    if len(values) < period: return out
    out[period-1] = sum(values[:period])/period
    for i in range(period, len(values)):
        out[i] = alpha*values[i] + (1-alpha)*out[i-1]
    return out

def _crypto_get(base: str, path: str, params: dict = None) -> dict:
    r = requests.get(base + path, params=params or {}, timeout=15)
    r.raise_for_status()
    return r.json()

def crypto_get_klines(symbol: str, interval='1h', limit=200) -> list:
    raw = _crypto_get(FUTURES_BASE_URL, '/fapi/v1/klines', {'symbol': symbol, 'interval': interval, 'limit': limit})
    return [{'time': k[0], 'open': float(k[1]), 'high': float(k[2]), 'low': float(k[3]),
              'close': float(k[4]), 'volume': float(k[5])} for k in raw]

def crypto_get_trend(symbol: str) -> str:
    """Same ADX/EMA trend logic Apex uses live, hand-rolled here to avoid
    adding a pandas/ta dependency to this file."""
    try:
        candles = crypto_get_klines(symbol, '1h', 200)
        closes = [c['close'] for c in candles]; highs = [c['high'] for c in candles]; lows = [c['low'] for c in candles]
        n = len(candles)
        plus_dm=[0.0]*n; minus_dm=[0.0]*n
        for i in range(1,n):
            up = highs[i]-highs[i-1]; down = lows[i-1]-lows[i]
            plus_dm[i] = up if (up>down and up>0) else 0.0
            minus_dm[i] = down if (down>up and down>0) else 0.0
        tr=[0.0]*n
        for i in range(n):
            tr[i] = highs[i]-lows[i] if i==0 else max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        sm_tr=_crypto_wilder(tr,14); sm_pdm=_crypto_wilder(plus_dm,14); sm_mdm=_crypto_wilder(minus_dm,14)
        pdi = 100*sm_pdm[-1]/sm_tr[-1] if sm_tr[-1] else 0
        mdi = 100*sm_mdm[-1]/sm_tr[-1] if sm_tr[-1] else 0
        dx=[None]*n
        for i in range(n):
            if sm_tr[i] and sm_pdm[i] is not None and sm_mdm[i] is not None:
                p = 100*sm_pdm[i]/sm_tr[i]; m = 100*sm_mdm[i]/sm_tr[i]
                if p+m>0: dx[i] = 100*abs(p-m)/(p+m)
        dx_clean=[d if d is not None else 0.0 for d in dx]
        adx = _crypto_wilder(dx_clean,14)[-1]
        ema21 = _crypto_ema(closes,21)[-1]; ema50 = _crypto_ema(closes,50)[-1]
        if adx is not None and adx >= CRYPTO_ADX_MIN and pdi > mdi and ema21 > ema50:
            return 'BULLISH'
        if adx is not None and adx >= CRYPTO_ADX_MIN and mdi > pdi and ema21 < ema50:
            return 'BEARISH'
        return 'CHOPPY'
    except Exception as e:
        logger.warning(f'crypto_get_trend[{symbol}] failed: {e}, defaulting to CHOPPY')
        return 'CHOPPY'

def crypto_get_index_price(underlying: str) -> float:
    d = _crypto_get(OPTIONS_BASE_URL, '/eapi/v1/index', {'underlying': underlying})
    return float(d['indexPrice'])

_crypto_chain_cache = {}
def crypto_get_option_chain(opt_prefix: str) -> dict:
    now = time.time()
    cached = _crypto_chain_cache.get(opt_prefix)
    if cached and (now - cached['ts']) < CRYPTO_CHAIN_CACHE_TTL:
        return cached['data']
    info = _crypto_get(OPTIONS_BASE_URL, '/eapi/v1/exchangeInfo')
    syms = [s for s in info.get('optionSymbols', []) if s['symbol'].startswith(opt_prefix + '-')]
    date_codes = sorted(set(s['symbol'].split('-')[1] for s in syms))
    if not date_codes:
        raise RuntimeError(f'no listed option contracts for {opt_prefix}')
    soonest = date_codes[0]
    calls = sorted(float(s['symbol'].split('-')[2]) for s in syms if s['symbol'].split('-')[1]==soonest and s['side']=='CALL')
    puts  = sorted(float(s['symbol'].split('-')[2]) for s in syms if s['symbol'].split('-')[1]==soonest and s['side']=='PUT')
    expiry_ms = next(s['expiryDate'] for s in syms if s['symbol'].split('-')[1] == soonest)
    data = {'date_code': soonest, 'expiry_ms': expiry_ms, 'strikes': {'C': calls, 'P': puts}}
    _crypto_chain_cache[opt_prefix] = {'data': data, 'ts': now}
    return data

def crypto_get_quotes(symbols: list) -> dict:
    if not symbols: return {}
    tickers = _crypto_get(OPTIONS_BASE_URL, '/eapi/v1/ticker')
    marks   = _crypto_get(OPTIONS_BASE_URL, '/eapi/v1/mark')
    want = set(symbols)
    tmap = {t['symbol']: t for t in tickers if t['symbol'] in want}
    mmap = {m['symbol']: m for m in marks if m['symbol'] in want}
    out = {}
    for sym in symbols:
        t, m = tmap.get(sym), mmap.get(sym)
        if not t: continue
        out[sym] = {'bid': float(t['bidPrice']), 'ask': float(t['askPrice']),
                     'mark_iv': float(m['markIV']) if m else None,
                     'delta': float(m['delta']) if m and m.get('delta') is not None else None}
    return out

def _crypto_nearest_strike(strikes: list, target: float) -> float:
    return min(strikes, key=lambda s: abs(s-target))

def _crypto_fmt_strike(s):
    return str(int(s)) if s == int(s) else str(s)

def crypto_build_spread(opt_prefix, date_code, chain, direction, spot, sigma_move):
    target_short = CRYPTO_K1 * sigma_move
    if direction == 'BULL_PUT':
        side = 'P'
        below = [s for s in chain['strikes']['P'] if s < spot]
        if not below: return None
        short_strike = _crypto_nearest_strike(below, spot - target_short)
        further = [s for s in below if s < short_strike]
        if not further: return None
        long_strike = _crypto_nearest_strike(further, short_strike - CRYPTO_K2*sigma_move)
    else:
        side = 'C'
        above = [s for s in chain['strikes']['C'] if s > spot]
        if not above: return None
        short_strike = _crypto_nearest_strike(above, spot + target_short)
        further = [s for s in above if s > short_strike]
        if not further: return None
        long_strike = _crypto_nearest_strike(further, short_strike + CRYPTO_K2*sigma_move)

    short_sym = f'{opt_prefix}-{date_code}-{_crypto_fmt_strike(short_strike)}-{side}'
    long_sym  = f'{opt_prefix}-{date_code}-{_crypto_fmt_strike(long_strike)}-{side}'
    quotes = crypto_get_quotes([short_sym, long_sym])
    if short_sym not in quotes or long_sym not in quotes: return None
    short_bid = quotes[short_sym]['bid']; long_ask = quotes[long_sym]['ask']
    credit = round(short_bid - long_ask, 4)
    width = abs(long_strike - short_strike)
    if credit <= 0 or width <= 0 or credit < width*MIN_CREDIT_WIDTH_RATIO: return None
    max_loss_per_unit = width - credit
    if max_loss_per_unit <= 0: return None
    contracts = round(CRYPTO_TRADE_RISK_USD / max_loss_per_unit, 2)
    if contracts < 0.01: return None
    breakeven = round(short_strike - credit, 4) if direction == 'BULL_PUT' else round(short_strike + credit, 4)
    return {'direction': direction, 'short_symbol': short_sym, 'long_symbol': long_sym,
            'short_strike': short_strike, 'long_strike': long_strike, 'breakeven': breakeven,
            'credit': credit, 'width': width, 'contracts': contracts,
            'max_loss': round(max_loss_per_unit*contracts, 2), 'max_profit': round(credit*contracts, 2)}

def crypto_vertical_payoff(direction, K_short, K_long, settle):
    if direction == 'BULL_PUT':
        return max(0, K_short-settle) - max(0, K_long-settle)
    return max(0, settle-K_short) - max(0, settle-K_long)

def crypto_current_option_date() -> str:
    now = datetime.now(timezone.utc)
    d = now.date() if now.hour >= 8 else (now.date() - timedelta(days=1))
    return d.isoformat()

def crypto_settle_open_positions(symbol, cfg, sym_state):
    base = cfg['base']
    positions = sym_state.get('open_positions', [])
    if not positions: return
    try:
        settle_price = crypto_get_index_price(symbol)
    except Exception as e:
        notify(f'❌ **Crypto 0DTE Paper [{base}] ERROR**\n\nsettle: could not fetch index price: {e}')
        return
    for pos in positions:
        owed = crypto_vertical_payoff(pos['direction'], pos['short_strike'], pos['long_strike'], settle_price)
        pnl = round((pos['credit'] - owed) * pos['contracts'], 2)
        trade = dict(pos, settle_price=settle_price, pnl=pnl, win=pnl>0,
                     closed_at=datetime.now(timezone.utc).isoformat(), date=pos.get('opened_date'))
        sym_state.setdefault('trades', []).append(trade)
        emoji = '🟢' if pnl>=0 else '🔴'
        notify(
            f'{emoji} **Crypto 0DTE Paper [{base}] — {pos["direction"]} SETTLED**\n\n'
            f'Short {pos["short_symbol"]} / Long {pos["long_symbol"]}\n'
            f'Settle: ${settle_price:,.2f}\n'
            f'Credit: ${pos["credit"]:.2f} | Contracts: {pos["contracts"]}\n'
            f'Net P&L: **${pnl:+.2f}** (paper)'
        )
        logger.info(f'[Crypto0DTE:{base}] Settled {pos["direction"]} pnl={pnl:+.2f} settle={settle_price:.2f}')
    sym_state['open_positions'] = []

def crypto_open_new_positions(symbol, cfg, sym_state, chain):
    base = cfg['base']
    try:
        spot = crypto_get_index_price(symbol)
        trend = crypto_get_trend(symbol)
        if not chain['strikes']['C']:
            logger.warning(f'[Crypto0DTE:{base}] no call strikes listed, skipping'); return
        atm_strike = _crypto_nearest_strike(chain['strikes']['C'], spot)
        atm_sym = f"{cfg['opt_prefix']}-{chain['date_code']}-{_crypto_fmt_strike(atm_strike)}-C"
        atm_quote = crypto_get_quotes([atm_sym]).get(atm_sym)
        iv = atm_quote['mark_iv'] if atm_quote and atm_quote.get('mark_iv') else 0.5
        # size the strike distance off the ACTUAL time remaining to this contract's
        # real expiry, not a hardcoded 1-day assumption - matters once DTE > ~1 day
        hours_to_expiry = max((chain['expiry_ms'] - datetime.now(timezone.utc).timestamp()*1000) / 3600000.0, 1.0)
        sigma_move = iv * math.sqrt(hours_to_expiry/24.0/365) * spot

        sides = ['BULL_PUT'] if trend=='BULLISH' else ['BEAR_CALL'] if trend=='BEARISH' else ['BULL_PUT','BEAR_CALL']
        opened_date = crypto_current_option_date()
        for direction in sides:
            spread = crypto_build_spread(cfg['opt_prefix'], chain['date_code'], chain, direction, spot, sigma_move)
            if spread is None:
                logger.info(f'[Crypto0DTE:{base}] {direction}: no qualifying spread this cycle'); continue
            spread['opened_date'] = opened_date
            spread['opened_at'] = datetime.now(timezone.utc).isoformat()
            spread['expiry_ms'] = chain['expiry_ms']
            spread['trend'] = trend
            sym_state.setdefault('open_positions', []).append(spread)
            notify(
                f'🎯 **Crypto 0DTE Paper [{base}] — {direction} OPENED**\n\n'
                f'Short {spread["short_symbol"]} / Long {spread["long_symbol"]}\n'
                f'Credit: ${spread["credit"]:.2f} | Width: ${spread["width"]:.2f} | Contracts: {spread["contracts"]}\n'
                f'Breakeven: ${spread["breakeven"]:,.2f}\n'
                f'Max profit: ${spread["max_profit"]:.2f} | Max loss: ${spread["max_loss"]:.2f} (paper, capped)\n'
                f'📊 Trend: {trend} | Spot: ${spot:,.2f} | IV: {iv*100:.1f}% | DTE: {hours_to_expiry/24:.1f}d'
            )
            logger.info(f'[Crypto0DTE:{base}] Opened {direction} credit={spread["credit"]:.2f} contracts={spread["contracts"]}')
    except Exception as e:
        logger.error(f'[Crypto0DTE:{base}] open_new_positions failed: {e}', exc_info=True)
        notify(f'❌ **Crypto 0DTE Paper [{base}] ERROR**\n\nopen_new_positions failed: {e}')

# How far out the SOONEST listed contract may be for us to still call it "0DTE" and
# trade it. Binance's near-term listing calendar isn't a fixed weekday pattern (it can
# skip days, and what's skipped today might not be skipped next week if they change
# the schedule) - so this is deliberately NOT keyed to any hardcoded day-of-week.
# Instead we just check the actual gap to the real next expiry, live, every cycle.
CRYPTO_MAX_DTE_HOURS = 30   # ~24h cadence + buffer for listing-time jitter

def crypto_check_rollover(symbol, cfg, crypto_state):
    sym_state = crypto_state['symbols'][symbol]
    base = cfg['base']
    now_ms = datetime.now(timezone.utc).timestamp() * 1000

    # Still holding a position whose REAL contract hasn't actually expired yet -
    # nothing to do (this is what stops us from "settling" a Friday-expiring
    # contract early just because a calendar day ticked over on Wednesday/Thursday).
    held_expiry_ms = sym_state.get('held_expiry_ms')
    if held_expiry_ms is not None and now_ms < held_expiry_ms:
        return

    if sym_state.get('open_positions'):
        logger.info(f'[Crypto0DTE:{base}] Held contract has passed real expiry - settling')
        crypto_settle_open_positions(symbol, cfg, sym_state)
        sym_state['held_expiry_ms'] = None
        save_crypto_state(crypto_state)

    try:
        chain = crypto_get_option_chain(cfg['opt_prefix'])
    except Exception as e:
        logger.warning(f'[Crypto0DTE:{base}] option chain fetch failed: {e}')
        return

    hours_to_expiry = (chain['expiry_ms'] - now_ms) / 3600000.0
    if hours_to_expiry > CRYPTO_MAX_DTE_HOURS:
        logger.info(f'[Crypto0DTE:{base}] no genuine 0DTE listed right now — soonest expiry is '
                    f'{hours_to_expiry/24:.1f} days out (>{CRYPTO_MAX_DTE_HOURS}h threshold). '
                    f'Skipping this cycle, will re-check in 5 min.')
        return  # deliberately do NOT set held_expiry_ms - keep re-checking every cycle
                # so we catch it the moment Binance lists something closer

    crypto_open_new_positions(symbol, cfg, sym_state, chain)
    sym_state['held_expiry_ms'] = chain['expiry_ms']
    sym_state['current_option_date'] = crypto_current_option_date()
    save_crypto_state(crypto_state)

def crypto_positions_with_live_pnl(all_positions):
    """Fetch current bid/ask for every open position's legs in one batch call
    and attach unrealized_pnl (and the live cost-to-close) to each. Credit was
    received selling the spread; closing now means buying back the short leg
    (pay ask) and selling the long leg (receive bid)."""
    legs = set()
    for pos in all_positions:
        legs.add(pos['short_symbol']); legs.add(pos['long_symbol'])
    if not legs:
        return
    try:
        quotes = crypto_get_quotes(list(legs))
    except Exception as e:
        logger.warning(f'crypto_positions_with_live_pnl: quote fetch failed: {e}')
        quotes = {}
    for pos in all_positions:
        if pos.get('breakeven') is None:  # backfill for positions opened before this field existed
            pos['breakeven'] = round(pos['short_strike'] - pos['credit'], 4) if pos['direction'] == 'BULL_PUT' \
                else round(pos['short_strike'] + pos['credit'], 4)
        sq, lq = quotes.get(pos['short_symbol']), quotes.get(pos['long_symbol'])
        if not sq or not lq:
            pos['unrealized_pnl'] = None
            continue
        cost_to_close = sq['ask'] - lq['bid']
        pos['cost_to_close'] = round(cost_to_close, 4)
        pos['unrealized_pnl'] = round((pos['credit'] - cost_to_close) * pos['contracts'], 2)
        # Short leg's delta approximates its live risk-neutral probability of
        # finishing in-the-money (the standard options-trading heuristic) - so
        # 1-|delta| is a real, market-implied probability the spread expires
        # at max profit, updating every cycle as price/time/IV move.
        if sq.get('delta') is not None:
            pos['prob_max_profit'] = round((1 - abs(sq['delta'])) * 100, 1)
        else:
            pos['prob_max_profit'] = None

def crypto_write_dashboard(crypto_state):
    symbols_payload = {}
    all_open = [p for s in crypto_state['symbols'].values() for p in s.get('open_positions', [])]
    crypto_positions_with_live_pnl(all_open)
    for symbol, cfg in CRYPTO_SYMBOLS_CONFIG.items():
        base = cfg['base']
        sym_state = crypto_state['symbols'][symbol]
        trades = sym_state.get('trades', [])
        wins = [t for t in trades if t['win']]; losses = [t for t in trades if not t['win']]
        total = len(trades)
        daily_pnl = {}
        for t in trades:
            d = t.get('date') or (t.get('closed_at') or '')[:10]
            if d: daily_pnl[d] = round(daily_pnl.get(d,0) + t['pnl'], 2)
        try:
            spot_price = crypto_get_index_price(symbol)
        except Exception as e:
            logger.warning(f'crypto_write_dashboard: spot fetch failed for {symbol}: {e}')
            spot_price = None
        try:
            current_trend = crypto_get_trend(symbol)
        except Exception as e:
            logger.warning(f'crypto_write_dashboard: trend fetch failed for {symbol}: {e}')
            current_trend = None
        symbols_payload[base] = {
            'symbol': symbol,
            'spot_price': spot_price,
            'current_trend': current_trend,
            'open_positions': sym_state.get('open_positions', []),
            'trade_risk_usd': CRYPTO_TRADE_RISK_USD,
            'performance': {
                'total': total, 'wins': len(wins), 'losses': len(losses),
                'win_rate': round(len(wins)/total*100, 1) if total else 0,
                'net_pnl': round(sum(t['pnl'] for t in trades), 2),
            },
            'daily_pnl': daily_pnl,
            'trades': list(reversed(trades[-30:])),
        }
    payload = {'generated_at': datetime.now(timezone.utc).isoformat(), 'paper_trading': True, 'symbols': symbols_payload}
    try:
        with open(CRYPTO_DASHBOARD_FILE, 'w') as f:
            json.dump(payload, f, indent=2)
    except Exception as e:
        logger.warning(f'crypto_write_dashboard failed: {e}')

def run_crypto_0dte_cycle(crypto_state):
    for symbol, cfg in CRYPTO_SYMBOLS_CONFIG.items():
        try:
            crypto_check_rollover(symbol, cfg, crypto_state)
        except Exception as e:
            logger.error(f'[Crypto0DTE:{cfg["base"]}] rollover error: {e}', exc_info=True)
            notify(f'❌ **Crypto 0DTE Paper [{cfg["base"]}] ERROR**\n\n{e}')
    crypto_write_dashboard(crypto_state)


def start_api():
    while True:
        try:
            server = ThreadingHTTPServer(('0.0.0.0', API_PORT), APIHandler)
            logger.info(f'API listening on port {API_PORT}')
            server.serve_forever()
        except Exception as e:
            logger.error(f'API server crashed: {e} — restarting in 5s')
            time.sleep(5)

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    try:
        import yfinance as yf
        logger.info(f'yfinance {yf.__version__} OK')
    except ImportError:
        logger.error('yfinance not installed — run: pip install yfinance --break-system-packages')
        return

    logger.info(f'🎯 SPY Options Bot starting — API on port {API_PORT}')
    logger.info(f'🚀 Crypto 0DTE Paper (BTC+ETH) starting — ${CRYPTO_TRADE_RISK_USD}/spread, PAPER ONLY, no real orders')
    notify(
        f'🚀 **Crypto 0DTE Paper Trading Started**\n\n'
        f'📊 Strategy: 0DTE credit spreads, trend-tilted (BULL_PUT/BEAR_CALL/iron condor when choppy)\n'
        f'💵 ${CRYPTO_TRADE_RISK_USD}/spread max-loss sizing — PAPER ONLY, no real orders\n'
        f'⏱ Rollover check every {CRYPTO_CHECK_INTERVAL_SEC//60} min | New position each option-day (~08:00 UTC)'
    )
    threading.Thread(target=start_api, daemon=True).start()

    state        = load_state()
    last_monitor = 0.0
    crypto_state = load_crypto_state()
    last_crypto_check = 0.0

    while True:
        try:
            # Crypto 0DTE paper trading — 24/7, independent of US market hours
            if time.time() - last_crypto_check > CRYPTO_CHECK_INTERVAL_SEC:
                run_crypto_0dte_cycle(crypto_state)
                last_crypto_check = time.time()

            # Handle trigger file
            if os.path.exists(TRIGGER_FILE):
                try:
                    trigger = json.load(open(TRIGGER_FILE))
                    os.remove(TRIGGER_FILE)
                    state = handle_trigger(trigger, state)
                except Exception as e:
                    logger.warning(f'trigger error: {e}')
                    try: os.remove(TRIGGER_FILE)
                    except: pass

            if is_market_day():
                now_et = et_now()
                today  = now_et.strftime('%Y-%m-%d')

                # Health ping at 9:25 AM ET — pre-market readiness check
                health_key    = f'{today}_health'
                health_target = now_et.replace(hour=9, minute=25, second=0, microsecond=0)
                if (not state['fired'].get(health_key)
                        and abs((now_et - health_target).total_seconds()) < 90):
                    send_health_ping(state)
                    state['fired'][health_key] = True
                    save_state(state)

                # Scheduled signals (fire within 90s window)
                for (h, m, lbl, _) in SCHEDULES:
                    key    = f'{today}_{lbl}'
                    target = now_et.replace(hour=h, minute=m, second=0, microsecond=0)
                    if not state['fired'].get(key) and abs((now_et - target).total_seconds()) < 90:
                        state = run_signal(lbl, state)
                        state['fired'][key] = True
                        save_state(state)

                # Monitor open trades every 5 min during market hours
                if is_market_open() and time.time() - last_monitor > 300:
                    alerts = check_trades(state)
                    for a in alerts:
                        discord(a)
                    if alerts:
                        sig = load_signal()
                        open_trade = next((t for t in state.get('trades', []) if t.get('status') == 'open'), None)
                        sig['open_trade'] = open_trade
                        save_signal(sig)
                    last_monitor = time.time()

            time.sleep(30)

        except KeyboardInterrupt:
            logger.info('Shutting down')
            break
        except Exception as e:
            logger.error(f'main loop: {e}')
            time.sleep(60)

if __name__ == '__main__':
    main()
