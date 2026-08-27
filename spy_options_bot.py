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
SPREAD_WIDTH      = 2       # default $2 wide spread
# CBOE comparison uses no API key — public CDN endpoint

# SPY alerts go to their own channel (DISCORD_SPY_WEBHOOK).
# Falls back to the main webhook if not set.
DISCORD_SPY_WEBHOOK = os.getenv('DISCORD_SPY_WEBHOOK', '')
DISCORD_WEBHOOK     = (
    os.getenv('DISCORD_WEBHOOK_URL') or
    os.getenv('TELEGRAM_WEBHOOK_URL', '')
)

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

        for _, r in calls.iterrows():
            s   = float(r['strike'])
            iv  = float(r.get('impliedVolatility') or 0)
            g   = bs_gamma(float(spot), s, T_years, iv)
            oi  = float(r.get('openInterest') or 0)
            gex[s] = gex.get(s, 0) + g * oi * 100 * float(spot)
            call_map[s] = {
                'bid': float(r.get('bid') or 0),
                'ask': float(r.get('ask') or 0),
                'oi':  int(oi),
            }

        for _, r in puts.iterrows():
            s   = float(r['strike'])
            iv  = float(r.get('impliedVolatility') or 0)
            g   = bs_gamma(float(spot), s, T_years, iv)
            oi  = float(r.get('openInterest') or 0)
            gex[s] = gex.get(s, 0) - g * oi * 100 * float(spot)
            put_map[s] = {
                'bid': float(r.get('bid') or 0),
                'ask': float(r.get('ask') or 0),
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
        'open_trade':   None,
    }

# ── Discord ───────────────────────────────────────────────────────────────────
def discord(msg: str):
    """Send to dedicated SPY channel, falling back to main webhook."""
    target = DISCORD_SPY_WEBHOOK or DISCORD_WEBHOOK
    if not target:
        return
    try:
        requests.post(target, json={'content': msg}, timeout=10)
    except Exception as e:
        logger.warning(f'discord: {e}')

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
        f"",
        f"👇 Log trade at your SPY dashboard",
    ]
    return '\n'.join(lines)

# ── Trade monitoring ──────────────────────────────────────────────────────────
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

        for trade in open_trades:
            direction = trade.get('direction')
            short_s   = float(trade.get('short_strike', 0))
            long_s    = float(trade.get('long_strike', 0))
            credit    = float(trade.get('credit', 0))
            expiry    = trade.get('expiry', date.today().strftime('%Y-%m-%d'))
            tid       = trade.get('trade_id', '?')

            try:
                chain = ticker.option_chain(expiry)
                if direction == 'BULL_PUT':
                    pm = {float(r['strike']): r for _, r in chain.puts.iterrows()}
                    short_ask  = float(pm.get(short_s, {}).get('ask', 0) or 0)
                    long_bid   = float(pm.get(long_s,  {}).get('bid', 0) or 0)
                    cur_debit  = round(short_ask - long_bid, 2)
                    profit_pct = round((credit - cur_debit) / credit * 100, 1) if credit else 0
                    trade['current_debit'] = cur_debit
                    trade['profit_pct']    = profit_pct

                    if profit_pct >= 80:
                        alerts.append(f"✅ **SPY {tid}** — 80% profit! Close for debit ${cur_debit:.2f} (${round(cur_debit*100):.0f}/contract)")
                    elif spot <= short_s + 0.5:
                        alerts.append(f"⚠️ **SPY {tid}** — SPY ${spot:.2f} approaching short strike ${short_s:.0f} — consider closing!")

                elif direction == 'BEAR_CALL':
                    cm = {float(r['strike']): r for _, r in chain.calls.iterrows()}
                    short_ask  = float(cm.get(short_s, {}).get('ask', 0) or 0)
                    long_bid   = float(cm.get(long_s,  {}).get('bid', 0) or 0)
                    cur_debit  = round(short_ask - long_bid, 2)
                    profit_pct = round((credit - cur_debit) / credit * 100, 1) if credit else 0
                    trade['current_debit'] = cur_debit
                    trade['profit_pct']    = profit_pct

                    if profit_pct >= 80:
                        alerts.append(f"✅ **SPY {tid}** — 80% profit! Close for debit ${cur_debit:.2f}")
                    elif spot >= short_s - 0.5:
                        alerts.append(f"⚠️ **SPY {tid}** — SPY ${spot:.2f} approaching short strike ${short_s:.0f} — consider closing!")

            except Exception as e:
                logger.warning(f'monitor trade {tid}: {e}')

        if now_et.hour == 15 and now_et.minute >= 30:
            alerts.append(f"⏰ **EOD Warning** — close all open SPY spreads now! Market closes in {90 - now_et.minute + 30:.0f} min")

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
    server = HTTPServer(('0.0.0.0', API_PORT), APIHandler)
    server.serve_forever()

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

                # Monitor open trades every 30 min during market hours
                if is_market_open() and time.time() - last_monitor > 1800:
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
