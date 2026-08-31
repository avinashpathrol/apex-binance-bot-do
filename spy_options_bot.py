#!/usr/bin/env python3
"""SPY Options Signal Bot — GEX-based daily spread signals with dashboard API."""

import os, json, math, logging, time, threading
from datetime import datetime, date, timezone, timedelta
from math import log, sqrt, exp, pi
from http.server import HTTPServer, BaseHTTPRequestHandler
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
# CBOE comparison uses no API key — public CDN endpoint

# SPY alerts go to their own channel (DISCORD_SPY_WEBHOOK).
# Falls back to the main webhook if not set.
DISCORD_SPY_WEBHOOK  = os.getenv('DISCORD_SPY_WEBHOOK', '')
DISCORD_WEBHOOK      = os.getenv('DISCORD_WEBHOOK_URL', '')
TELEGRAM_BOT_TOKEN   = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID     = os.getenv('TELEGRAM_CHAT_ID', '')

# Signal schedule (ET): (hour, minute, label, mst_display)
SCHEDULES = [
    (8,  30, 'premarket',   '6:30 AM MST'),
    (11, 15, 'midmorning',  '9:15 AM MST'),
    (11, 50, 'late',        '9:50 AM MST'),
]

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
        call_map: dict = {}
        put_map: dict  = {}

        def _safe_float(v, default=0.0):
            # float(NaN or 0) stays NaN because NaN is truthy — detect it explicitly
            try:
                f = float(v) if v is not None else default
                return default if f != f else f  # f != f is True only for NaN
            except (TypeError, ValueError):
                return default

        for _, r in calls.iterrows():
            s   = _safe_float(r['strike'])
            iv  = _safe_float(r.get('impliedVolatility'))
            g   = bs_gamma(float(spot), s, T_years, iv)
            oi  = _safe_float(r.get('openInterest'))
            gex[s] = gex.get(s, 0) + g * oi * 100 * float(spot)
            call_map[s] = {
                'bid': _safe_float(r.get('bid')),
                'ask': _safe_float(r.get('ask')),
                'oi':  int(oi),
            }

        for _, r in puts.iterrows():
            s   = _safe_float(r['strike'])
            iv  = _safe_float(r.get('impliedVolatility'))
            g   = bs_gamma(float(spot), s, T_years, iv)
            oi  = _safe_float(r.get('openInterest'))
            gex[s] = gex.get(s, 0) - g * oi * 100 * float(spot)
            put_map[s] = {
                'bid': _safe_float(r.get('bid')),
                'ask': _safe_float(r.get('ask')),
                'oi':  int(oi),
            }

        return {
            'spot':      round(float(spot), 2),
            'expiry':    target,
            'gex':       gex,
            'call_map':  call_map,
            'put_map':   put_map,
            'total_gex': sum(gex.values()),
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
        short_bid = pm.get(short_s, {}).get('bid', 0)
        long_ask  = pm.get(long_s,  {}).get('ask', 0)
    else:
        long_s    = short_s + w
        short_bid = cm.get(short_s, {}).get('bid', 0)
        long_ask  = cm.get(long_s,  {}).get('ask', 0)

    credit = round(float(short_bid) - float(long_ask), 2)
    if credit <= 0.05:
        return None

    be = round(short_s - credit, 2) if direction == 'BULL_PUT' else round(short_s + credit, 2)
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

        spreads = []
        for (delta, lbl) in variants:
            short_s = float(base_s + delta)
            long_s  = float(short_s - w) if direction == 'BULL_PUT' else float(short_s + w)
            try:
                if direction == 'BULL_PUT':
                    short_bid = float(pm.get(short_s, {}).get('bid', 0) or 0)
                    long_ask  = float(pm.get(long_s,  {}).get('ask', 0) or 0)
                else:
                    short_bid = float(cm.get(short_s, {}).get('bid', 0) or 0)
                    long_ask  = float(cm.get(long_s,  {}).get('ask', 0) or 0)
                credit = round(short_bid - long_ask, 2)
                if credit <= 0.10:
                    continue
                be = round(short_s - credit, 2) if direction == 'BULL_PUT' else round(short_s + credit, 2)
                spreads.append({
                    'label':        lbl,
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
                })
            except Exception:
                continue

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

def build_signal(data: dict, label: str) -> Optional[dict]:
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

    # 3 strike variants
    spreads = []
    if direction == 'BULL_PUT':
        base = math.floor(lower_wall) - 1
        variants = [(+2, 'Conservative'), (0, 'Suggested'), (-2, 'Aggressive')]
    else:
        base = math.ceil(upper_wall) + 1
        variants = [(-2, 'Conservative'), (0, 'Suggested'), (+2, 'Aggressive')]

    for (delta, lbl) in variants:
        short_s = float(base + delta) if direction == 'BEAR_CALL' else float(base - delta)
        sp = make_spread(data, direction, short_s)
        if sp:
            sp['label'] = lbl
            spreads.append(sp)

    # Fallback: GEX wall may be far from spot (low premium there).
    # Scan between the wall and spot for liquid strikes.
    if not spreads:
        candidates = []
        if direction == 'BULL_PUT':
            # Walk from just below spot downward to the GEX wall
            for s in range(int(spot), int(lower_wall) - 1, -1):
                sp = make_spread(data, direction, float(s))
                if sp:
                    candidates.append(sp)
                    if len(candidates) >= 3:
                        break
            candidates.reverse()  # lowest premium first → Conservative
        else:
            # Walk from just above spot upward to the GEX wall
            for s in range(int(spot) + 1, int(upper_wall) + 2):
                sp = make_spread(data, direction, float(s))
                if sp:
                    candidates.append(sp)
                    if len(candidates) >= 3:
                        break
            candidates.reverse()  # lowest premium first → Conservative
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
        'open_trade':   None,
    }

# ── Discord ───────────────────────────────────────────────────────────────────
def _discord_fmt(msg: str) -> str:
    """Discord uses **bold** — return as-is."""
    return msg

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
    mst = {'premarket': '6:30 AM MST ☀️', 'midmorning': '9:15 AM MST 📊', 'late': '9:50 AM MST 🕙', 'on_demand': 'On Demand 🔄'}
    regime_txt = '📌 Pinning (range-bound)' if sig['gex_regime'] == 'positive' else '⚡ Trending (volatile)'
    dir_emoji  = '🐂' if sig['direction'] == 'BULL_PUT' else '🐻'
    dir_name   = 'Bull Put Spread' if sig['direction'] == 'BULL_PUT' else 'Bear Call Spread'
    close_at   = round(s['net_credit'] * 0.20, 2)

    lines = [
        f"**📈 SPY Options — {mst.get(sig['label'], sig['label'])}**",
        f"",
        f"SPY **${sig['spy_price']:.2f}** | Expiry **{sig['expiry']}**",
        f"GEX: {regime_txt} | Net GEX: **${sig['total_gex_b']:.1f}B**",
        f"📌 Pin **${sig['pin_strike']:.0f}** | Support **${sig['lower_wall']:.0f}** | Resist **${sig['upper_wall']:.0f}**",
    ]
    cmp = sig.get('gex_compare')
    if cmp:
        agree_icon = '✅' if cmp['direction_agree'] else '⚠️'
        lines.append(
            f"{agree_icon} AV cross-check: GEX **${cmp['av_total_gex_b']:.1f}B** "
            f"| diff **${cmp['diff_b']:+.2f}B** "
            f"| dir {'agrees' if cmp['direction_agree'] else '**DISAGREES**'}"
        )
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
PROFIT_MILESTONES = [25, 50, 75, 80]   # alert at each of these %
LOSS_WARN_PCT     = -20                 # warn when loss exceeds this
UPDATE_INTERVAL   = 900                 # send regular P&L update every 15 min

def check_trades(state: dict) -> list:
    open_trades = [t for t in state.get('trades', []) if t.get('status') == 'open']
    if not open_trades:
        return []

    alerts = []
    try:
        import yfinance as yf
        ticker = yf.Ticker('SPY')
        fi     = ticker.fast_info
        spot   = float(fi.get('lastPrice') or fi.get('previousClose') or 0)
        now_et = et_now()
        now_ts = time.time()

        for trade in open_trades:
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
                chain = ticker.option_chain(expiry)
                if isBull:
                    pm        = {float(r['strike']): r for _, r in chain.puts.iterrows()}
                    short_ask = float(pm.get(short_s, {}).get('ask', 0) or 0)
                    long_bid  = float(pm.get(long_s,  {}).get('bid', 0) or 0)
                else:
                    cm        = {float(r['strike']): r for _, r in chain.calls.iterrows()}
                    short_ask = float(cm.get(short_s, {}).get('ask', 0) or 0)
                    long_bid  = float(cm.get(long_s,  {}).get('bid', 0) or 0)

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
                        emoji  = '🚀' if m >= 75 else '✅' if m >= 50 else '💰'
                        advice = 'Strong close signal — lock in gains!' if m >= 75 else \
                                 'Good time to close — solid profit secured' if m >= 50 else \
                                 'Consider closing half if you want to play it safe'
                        close_cost = round(cur_debit * 100 * contracts, 2)
                        alerts.append(
                            f"{emoji} **SPY {tid} — {m}% profit reached!**\n"
                            f"{dir_name} ${short_s:.0f}/${long_s:.0f} | SPY ${spot:.2f}\n"
                            f"Close now: buy back at **${cur_debit:.2f}** debit\n"
                            f"P&L if closed now: **+${pnl_now:.2f}** (${close_cost:.2f} cost × {contracts} contract{'s' if contracts>1 else ''})\n"
                            f"💡 {advice}"
                        )

                # ── Afternoon rule: suggest close at 30%+ after 2 PM ET ────
                if now_et.hour >= 14 and profit_pct >= 30 and 'afternoon_close' not in milestones_hit:
                    milestones_hit.append('afternoon_close')
                    alerts.append(
                        f"🕑 **SPY {tid} — Afternoon close suggestion**\n"
                        f"It's after 2 PM ET. Profit at **{profit_pct:.0f}%** (${pnl_now:+.2f}).\n"
                        f"Close debit: **${cur_debit:.2f}** | Time decay risk increasing — consider locking in."
                    )

                # ── Loss warning ────────────────────────────────────────────
                if profit_pct <= LOSS_WARN_PCT and 'loss_warn' not in milestones_hit:
                    milestones_hit.append('loss_warn')
                    danger = isBull and spot <= short_s + 1.0 or not isBull and spot >= short_s - 1.0
                    alerts.append(
                        f"🔴 **SPY {tid} — Loss warning {profit_pct:.0f}%**\n"
                        f"SPY ${spot:.2f} | P&L: **${pnl_now:+.2f}**\n"
                        f"{'⚠️ SPY approaching your short strike $'+str(int(short_s))+' — close to limit damage' if danger else 'Consider cutting loss now before it gets worse'}"
                    )

                # ── Strike approach warning ─────────────────────────────────
                near_strike = (isBull and spot <= short_s + 0.75) or (not isBull and spot >= short_s - 0.75)
                if near_strike and 'near_strike' not in milestones_hit:
                    milestones_hit.append('near_strike')
                    alerts.append(
                        f"⚠️ **SPY {tid} — Strike breach risk!**\n"
                        f"SPY **${spot:.2f}** is within $0.75 of your short strike **${short_s:.0f}**\n"
                        f"P&L now: **${pnl_now:+.2f}** | Close debit: **${cur_debit:.2f}**\n"
                        f"Recommend closing immediately to cap loss."
                    )

                # ── Regular P&L update every 15 min ────────────────────────
                if now_ts - last_update_ts >= UPDATE_INTERVAL:
                    trade['last_update_ts'] = now_ts
                    bar   = '█' * int(max(0, profit_pct) / 10) + '░' * (10 - int(max(0, profit_pct) / 10))
                    emoji = '📈' if profit_pct > 0 else '📉'
                    alerts.append(
                        f"{emoji} **SPY Position Update — {tid}**\n"
                        f"{dir_name} ${short_s:.0f}/${long_s:.0f} | SPY ${spot:.2f}\n"
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

# ── Signal runner ─────────────────────────────────────────────────────────────
def run_signal(label: str, state: dict) -> dict:
    logger.info(f'[{label}] Fetching SPY data...')
    data = fetch_spy_chain()
    if not data:
        logger.error(f'[{label}] No data available')
        return state

    sig = build_signal(data, label)
    if not sig:
        logger.warning(f'[{label}] Could not build signal (no liquid spreads)')
        return state

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
        discord(
            f"📝 **SPY Trade Registered**\n"
            f"{'Bull Put' if direction == 'BULL_PUT' else 'Bear Call'} "
            f"${short_s:.0f}/{long_s:.0f} | Credit ${credit:.2f} × {contracts} contracts\n"
            f"Max profit: ${credit*100*contracts:.0f} | Close at: ${round(credit*0.20,2):.2f} debit"
        )
        logger.info(f'Trade registered: {t["trade_id"]}')

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
class APIHandler(BaseHTTPRequestHandler):
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

def start_api():
    while True:
        try:
            server = HTTPServer(('0.0.0.0', API_PORT), APIHandler)
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
    threading.Thread(target=start_api, daemon=True).start()

    state        = load_state()
    last_monitor = 0.0

    while True:
        try:
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
