#!/usr/bin/env python3
"""Hermes veto-check service -- answers Apex's pre-trade sanity check.

Runs on the Hermes droplet, bound to the DigitalOcean private network only
(10.122.0.3), so it is unreachable from the public internet. Reads the
cached daily macro briefing (written separately by the `macro-briefing`
Hermes cron job) and combines it with the specific trade's technicals, then
asks the LLM for an APPROVE/VETO call.

Fail-open by design at every layer: Apex enforces its own client-side
timeout and treats any failure reaching this service as approval, and this
service itself treats any internal failure (missing briefing, LLM timeout,
malformed response) as approval too. This check can only ever make Apex
more cautious when it clearly works -- never block a trade by being broken.

Deliberately calls the 5 LLM providers directly over HTTP instead of
shelling out to the `hermes` CLI. Measured `hermes -z` at a consistent
~12s floor even for a trivial no-search prompt (plugin discovery + skill
indexing cold-starts on every invocation) -- too slow and eats the entire
per-trade timeout budget before any real network/model latency. A direct
call to the same providers/models/keys already in ~/.hermes/.env typically
answers in 1-3s. The slower, research-heavy macro-briefing job (see
macro_briefing.sh) keeps using the full Hermes CLI/cron, where that
overhead doesn't matter.
"""
import json
import os
import re
import time
import requests
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

BIND_HOST = '10.122.0.3'
BIND_PORT = 8787
BRIEFING_PATH = os.path.expanduser('~/.hermes/macro_briefing.json')
BRIEFING_MAX_AGE_HOURS = 30  # a bit over 24h so a slightly-late cron run isn't discarded
PROVIDER_TIMEOUT_SECONDS = 4  # per-provider; cascades through the chain below on failure

VETO_LOG_PATH      = os.path.expanduser('~/.hermes/veto_decisions.jsonl')
PICK_LOG_PATH      = os.path.expanduser('~/.hermes/pick_decisions.jsonl')
# Longer timeout/tokens than the veto-check's single-word verdict -- a trade
# pick or check-in is a multi-field JSON response with real reasoning text.
PICK_PROVIDER_TIMEOUT_SECONDS = 20
PICK_MAX_TOKENS    = 700
CHECKIN_MAX_TOKENS = 300
SNAPSHOT_PATH       = os.path.expanduser('~/.hermes/apex_snapshot.json')
SUGGESTIONS_PATH    = os.path.expanduser('~/.hermes/bot_suggestions.json')
SUGGESTIONS_MAX_AGE_HOURS = 24 * 10  # stale beyond ~10 days is worse than useless -- don't hand out

# SPY options bot -- kept fully separate from the Apex paths above (different
# bot, different data shape). SPY has no auto-tune/override system to feed
# candidates into, so this is read-only reporting: a report lands here for
# spy_options_bot.py to pull and deliver over its own Telegram alert channel,
# not structured candidates for another system to backtest and apply.
SPY_SNAPSHOT_PATH    = os.path.expanduser('~/.hermes/spy_snapshot.json')
SPY_REPORT_PATH      = os.path.expanduser('~/.hermes/spy_report.json')
SPY_REPORT_MAX_AGE_HOURS = 24 * 10

# Dashboard chat -- a live Q&A panel embedded in the trader's dashboard.
# Answers only; it cannot place trades, change trading parameters, or edit
# dashboard code from here (see build_chat_prompt's explicit scope note --
# those actions already have their own dedicated, reviewed write paths on
# the dashboard itself, this endpoint is read-only by design).
CHAT_LOG_PATH = os.path.expanduser('~/.hermes/dashboard_chat.jsonl')
CHAT_PROVIDER_TIMEOUT_SECONDS = 20
CHAT_MAX_TOKENS = 700  # bumped from 500 same day/reason as ask_llm's veto default -- see there
CHAT_CONTEXT_CHAR_BUDGET = 6000
# Real command power for the chat, added 2026-09-18 -- deliberately scoped to
# the EXACT same actions the dashboard's own buttons already trigger (close a
# position, lock a trailing stop, pause/resume the bot), nothing new. This is
# NOT "Hermes can change trading parameters or edit dashboard code from
# chat" -- those are a different risk profile (unbounded config drift /
# arbitrary code execution) and were explicitly kept out. The actual write
# happens on the Apex side (config_api.py), using the identical
# bot_config.json flags the buttons use, so this can only ever do what a
# human clicking a button could already do -- never a new capability.
CHAT_ACTION_TYPES = ('close_position', 'force_trail', 'pause_bot', 'resume_bot')
CHAT_ACTION_SYMBOLS = ('NBISUSDT', 'AMDUSDT', 'APPUSDT', 'SOXLUSDT', 'CRCLUSDT',
                        'ASTSUSDT', 'TSLAUSDT', 'MUUSDT')

# Same 5-provider fallback chain as ~/.hermes/config.yaml's fallback_providers,
# called directly here for speed. Keep these two lists in sync if the config
# chain ever changes.
PROVIDERS = [
    # 'extra' is merged into this provider's request body only (see ask_llm).
    # reasoning_effort=low added 2026-09-19: replaying a real failed daily-pick
    # request showed this model burning 590-698 of the 700-token budget on
    # hidden reasoning (leaving nothing / a truncated JSON -> "Unparseable
    # response"); with low effort the same request used ~250 tokens total and
    # returned a valid decision every time. Groq-only on purpose -- other
    # providers may reject an unknown parameter, and ask_llm treats any
    # non-200 as "skip to the next provider".
    {'name': 'groq', 'key_env': 'GROQ_API_KEY', 'model': 'openai/gpt-oss-120b',
     'url': 'https://api.groq.com/openai/v1/chat/completions',
     'extra': {'reasoning_effort': 'low'}},
    {'name': 'gemini', 'key_env': 'GOOGLE_API_KEY', 'model': 'gemini-flash-latest',
     'url': 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'},
    {'name': 'cloudflare', 'key_env': 'CLOUDFLARE_API_KEY',
     'model': '@cf/meta/llama-3.3-70b-instruct-fp8-fast',
     'url': 'https://api.cloudflare.com/client/v4/accounts/3fc84986ae02f1809bc3178caf0a2797/ai/v1/chat/completions'},
    {'name': 'openrouter-glm', 'key_env': 'OPENROUTER_API_KEY', 'model': 'z-ai/glm-5.2:free',
     'url': 'https://openrouter.ai/api/v1/chat/completions'},
    {'name': 'openrouter-nemotron', 'key_env': 'OPENROUTER_API_KEY',
     'model': 'nvidia/nemotron-3-super-120b-a12b:free',
     'url': 'https://openrouter.ai/api/v1/chat/completions'},
    # Added 2026-09-18 after Groq/Gemini/Cloudflare/OpenRouter all hit their
    # daily/rate limits the same day (confirmed live: Cloudflare's 10K
    # neurons/day fully exhausted, Gemini and OpenRouter both 429'd) -- a
    # real gap where every provider above shares exposure to a single heavy
    # day of usage. LLM7.io needs no API key (anonymous access), so it adds
    # a genuinely independent fallback with zero new secrets. Tested live:
    # clean, fast, correctly-formatted responses, no reasoning-token issue.
    # key_env deliberately None -- see the "no key needed" handling below.
    # Anonymous tier is tight (10 RPM / 60 req/hr per their docs) so this
    # stays last in the chain, a safety net rather than a primary.
    {'name': 'llm7', 'key_env': None, 'model': 'mistral-Nemo-Instruct-2407',
     'url': 'https://api.llm7.io/v1/chat/completions'},
]


def load_env_file(path):
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, _, v = line.partition('=')
                k, v = k.strip(), v.strip()
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception:
        pass


load_env_file(os.path.expanduser('~/.hermes/.env'))


def load_briefing():
    try:
        st = os.stat(BRIEFING_PATH)
        age_hours = (time.time() - st.st_mtime) / 3600.0
        if age_hours > BRIEFING_MAX_AGE_HOURS:
            return None
        with open(BRIEFING_PATH) as f:
            return json.load(f)
    except Exception:
        return None


def build_prompt(trade, briefing):
    lines = [
        "You are a pre-trade sanity check for a systematic futures trading bot.",
        "The bot's technical strategy already decided to enter this trade. Your job is ONLY "
        "to catch a bad reason to avoid it right now -- not to re-judge the technical setup.",
        "",
        f"Proposed trade: {trade.get('action')} {trade.get('symbol')} @ {trade.get('price')}",
        f"Confidence: {trade.get('confidence')}% | Regime: {trade.get('regime')} | "
        f"Trend: {trade.get('trend_direction')}",
        f"Technical reason: {trade.get('reason')}",
        "",
    ]
    if briefing:
        lines.append(f"Today's macro briefing (as of {briefing.get('generated_at', 'unknown')}):")
        lines.append(f"- High-impact event today/tomorrow: {briefing.get('high_impact_today_or_tomorrow')}")
        for e in (briefing.get('events') or []):
            lines.append(f"  - {e.get('name')} on {e.get('date')} {e.get('time_et', '')} ET "
                         f"(impact: {e.get('impact')})")
        lines.append(f"- Overall sentiment: {briefing.get('overall_sentiment')}")
        lines.append(f"- Notes: {briefing.get('sentiment_notes')}")
        if briefing.get('caution_flag'):
            lines.append(f"- CAUTION FLAG: {briefing.get('caution_reason')}")
    else:
        lines.append("(No current macro briefing available -- proceed on technicals alone.)")
    lines += [
        "",
        "Veto ONLY for a clear, material reason -- e.g. entering right into a scheduled "
        "high-impact event in the next couple hours, or sentiment strongly and specifically "
        "opposing this exact trade direction. Do not veto on vague caution or because you'd "
        "personally prefer to wait.",
        "Respond with your verdict as the ENTIRE first line -- just the single word APPROVE "
        "or VETO, with no reasoning or preamble before it on that line. Second line: one "
        "short sentence why.",
    ]
    return "\n".join(lines)


def ask_llm(prompt, max_tokens=400, timeout=PROVIDER_TIMEOUT_SECONDS):
    """Try each provider in order, short timeout each. Returns (text, provider_name)
    or (None, None) if every provider failed -- caller treats that as fail-open.
    max_tokens bumped 150 -> 400 on 2026-09-18: Groq's openai/gpt-oss-120b
    started spending the bulk of any token budget on hidden chain-of-thought
    "reasoning" tokens before the actual answer (observed live: 132 of 150
    tokens went to reasoning, leaving too little for real content -- the
    empty/truncated result then silently falls through to the next provider
    below, which is correct/safe but wastes the fast primary provider and
    adds real latency). This is a hedge, not a full fix for a live, evolving
    provider-side behavior change; /pick_trade and /pick_checkin pass their
    own (larger) max_tokens/timeout since those responses are longer JSON."""
    for p in PROVIDERS:
        key_env = p.get('key_env')
        key = os.environ.get(key_env, '').strip() if key_env else None
        if key_env and not key:
            continue
        headers = {'Content-Type': 'application/json'}
        if key:
            headers['Authorization'] = f'Bearer {key}'
        try:
            body = {
                'model': p['model'],
                'messages': [{'role': 'user', 'content': prompt}],
                'max_tokens': max_tokens,
                'temperature': 0.2,
            }
            body.update(p.get('extra', {}))
            resp = requests.post(
                p['url'],
                headers=headers,
                json=body,
                timeout=timeout,
            )
            if resp.status_code != 200:
                continue
            content = resp.json()['choices'][0]['message'].get('content', '').strip()
            if content:
                return content, p['name']
        except Exception:
            continue
    return None, None


def log_veto_decision(trade, decision, reason, provider, elapsed):
    """Append a structured record of every veto call -- this is what the weekly
    Hermes analysis job reads to judge its own past calls (did a VETO actually
    save money, did an APPROVE that lost badly deserve more weight next time)."""
    record = {
        'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'symbol': trade.get('symbol'), 'action': trade.get('action'),
        'confidence': trade.get('confidence'), 'price': trade.get('price'),
        'reason': trade.get('reason'), 'decision': decision,
        'hermes_reason': reason, 'provider': provider, 'elapsed_seconds': round(elapsed, 1),
    }
    try:
        with open(VETO_LOG_PATH, 'a') as f:
            f.write(json.dumps(record) + '\n')
    except Exception:
        pass


def parse_decision(raw):
    """The prompt asks the model to lead with a clean single-word verdict,
    but free-tier models don't always comply. Two real production bugs
    found 2026-09-17, in order:

    1. Some models reason through the veto POLICY first (which itself uses
       the word "veto" while describing the RULE, not the decision) and only
       state their actual conclusion at the end. Checking only line 1 for the
       substring "VETO" matched that policy text and recorded a veto even
       when the conclusion was APPROVE (observed live, TSLAUSDT).
    2. The fix for #1 -- scan the whole text, take the LAST whole-word match
       -- broke a DIFFERENT real case: a model that DID comply (led with a
       clean "APPROVE") but then, in its explanation, used "veto" in a
       negated sentence ("...does not provide a reason to veto the trade").
       Since that mention came later in the text, "last match wins" flipped
       a correct APPROVE into VETO (observed live, NBISUSDT).

    Fix: two-tier. Trust a clean leading verdict immediately if the model
    complied with the format (the common case now that the prompt asks for
    it explicitly) -- everything after it is just explanation and never
    overrides it. Only fall back to whole-text scanning when line 1 doesn't
    cleanly state a verdict, for the non-compliant case #1 was about.
    """
    if not raw:
        return 'APPROVE', 'All providers unreachable or timed out -- fail-open'
    text = raw.strip()
    if not text:
        return 'APPROVE', 'Empty response -- fail-open'

    reason = ' '.join(text.split())  # collapse whitespace/newlines
    if len(reason) > 300:
        reason = reason[:300].rsplit(' ', 1)[0] + '…'  # trim at a word boundary, not mid-word
    reason = reason or 'No reason given'

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if lines:
        leading = re.match(r'^(VETO|APPROVE)\b', lines[0], re.IGNORECASE)
        if leading:
            return leading.group(1).upper(), reason

    # Non-compliant fallback: no clean leading verdict, so search the whole
    # response and take the LAST whole-word match -- models that reason
    # before answering usually conclude after the reasoning, not before.
    # This is a best-effort heuristic, not a guarantee, for phrasing this
    # text-only approach can't fully disambiguate (see case #2 above) --
    # but it only ever runs when the model already failed to comply with an
    # explicit formatting instruction, which should be the rarer path.
    matches = list(re.finditer(r'\b(VETO|APPROVE)\b', text, re.IGNORECASE))
    if not matches:
        return 'APPROVE', 'No clear verdict in response -- fail-open to APPROVE'
    return matches[-1].group(1).upper(), reason


# ── Hermes Daily Picks (paper only) -- Apex asks, this decides what to pick ───
def build_pick_prompt(candidates, briefing):
    lines = [
        "You are picking ONE daily paper-trading swing setup for a research experiment.",
        "No real money is ever at risk here -- this is purely simulated, so be honest and "
        "selective rather than forcing a pick. Concluding NONE of today's candidates are good "
        "enough is a legitimate, expected outcome, not a failure.",
        "",
        "Today's candidates (pre-filtered from Binance's full tokenized-equity perpetuals list "
        "by a deterministic breakout/momentum score -- these are already the strongest ones, "
        "not the whole universe):",
    ]
    for c in candidates:
        lines.append(
            f"- {c['symbol']}: price ${c['price']} | ADX {c['adx']} ({c['trend']}) | RSI {c['rsi']} | "
            f"1H ATR {c['atr']} | volume {c['vol_ratio']}x 20-bar avg | "
            f"Donchian breakout: {c.get('breakout') or 'none'} | "
            f"max real leverage for a $50 position: {c.get('max_leverage')}x"
        )
    lines.append("")
    if briefing:
        lines.append(f"Today's macro briefing (as of {briefing.get('generated_at', 'unknown')}):")
        lines.append(f"- High-impact event today/tomorrow: {briefing.get('high_impact_today_or_tomorrow')}")
        lines.append(f"- Overall sentiment: {briefing.get('overall_sentiment')}")
        if briefing.get('caution_flag'):
            lines.append(f"- CAUTION FLAG: {briefing.get('caution_reason')}")
    else:
        lines.append("(No current macro briefing available -- proceed on technicals alone.)")
    lines += [
        "",
        "Pick exactly ONE symbol from the list above, or none. If you pick one, give a specific "
        "entry zone (low/high), a target price, a stop price, and how many days you expect to "
        "hold (1-10) before this setup plays out. Explain your reasoning in 2-4 sentences -- this "
        "reasoning is shown to a human today verbatim, and reused verbatim again when the trade "
        "closes, so be specific, not generic.",
        "",
        "Respond with ONLY raw JSON, no markdown fences, no text before or after it, in EXACTLY "
        "this schema:",
        '{"decision": "TRADE" or "NO_TRADE", "symbol": "<one of the candidate symbols above, or '
        'null>", "direction": "LONG" or "SHORT" or null, "entry_low": <number or null>, '
        '"entry_high": <number or null>, "target": <number or null>, "stop": <number or null>, '
        '"expected_hold_days": <integer 1-10 or null>, "reasoning": "<2-4 sentences>"}',
    ]
    return "\n".join(lines)


def _extract_json_object(text):
    """Brace-depth walk, not a greedy regex -- bot_review.py's `re.search(r'\\{.*\\}', ...,
    re.DOTALL)` grabs from the first '{' to the LAST '}' in the whole text, which breaks
    if there's any trailing prose containing another '}' after the real JSON. This
    response drives an unattended (paper) trade decision, so it's worth the extra
    robustness. Also strips markdown code fences, since models don't reliably omit
    them despite being asked to."""
    if not text:
        return None
    cleaned = re.sub(r'^```(?:json)?|```$', '', text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(cleaned)
    except Exception:
        pass
    start = cleaned.find('{')
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(cleaned)):
        if cleaned[i] == '{':
            depth += 1
        elif cleaned[i] == '}':
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(cleaned[start:i + 1])
                except Exception:
                    return None
    return None


def parse_pick_decision(raw, candidates):
    """Fail-open to NO_TRADE on ANY ambiguity -- unlike the veto-check, where
    fail-open means 'let the already-decided real trade proceed', here the
    safe default is 'skip today, paper trade or not', never inventing or
    half-trusting a pick. Same core lesson as parse_decision()'s postmortem,
    applied to JSON: don't trust that the model followed the 'raw JSON only'
    instruction, and validate every field against the ACTUAL candidate list
    sent -- never trust a symbol name the model invents -- plus a
    directional sanity check (target/stop on the correct side of entry)
    before accepting anything."""
    def fail(reason):
        return {'decision': 'NO_TRADE', 'reasoning': reason}

    if not raw:
        return fail('All providers unreachable or timed out -- fail-open to no pick')
    obj = _extract_json_object(raw)
    if not isinstance(obj, dict):
        return fail('Unparseable response -- fail-open to no pick')

    decision = str(obj.get('decision', '')).strip().upper()
    if decision not in ('TRADE', 'NO_TRADE'):
        return fail('Invalid or missing decision field -- fail-open to no pick')
    if decision == 'NO_TRADE':
        return {'decision': 'NO_TRADE',
                'reasoning': (' '.join(str(obj.get('reasoning', '')).split()) or 'No reasoning given')[:600]}

    valid_symbols = {c['symbol'].upper() for c in candidates}
    symbol = str(obj.get('symbol', '')).strip().upper()
    if symbol not in valid_symbols:
        return fail(f'Model picked {symbol!r}, which is not in the candidate list -- fail-open to no pick')
    direction = str(obj.get('direction', '')).strip().upper()
    if direction not in ('LONG', 'SHORT'):
        return fail('Invalid or missing direction -- fail-open to no pick')
    try:
        entry_low, entry_high = float(obj['entry_low']), float(obj['entry_high'])
        target, stop = float(obj['target']), float(obj['stop'])
        hold_days = int(obj['expected_hold_days'])
    except (KeyError, TypeError, ValueError):
        return fail('Missing/non-numeric price or hold-day field -- fail-open to no pick')
    if not (1 <= hold_days <= 10):
        return fail(f'expected_hold_days {hold_days} outside the sane 1-10 range -- fail-open to no pick')
    if entry_low > entry_high:
        entry_low, entry_high = entry_high, entry_low
    ok = (target > entry_high > entry_low > stop) if direction == 'LONG' \
        else (target < entry_low < entry_high < stop)
    if not ok:
        return fail('Target/stop on the wrong side of entry for the stated direction -- fail-open to no pick')
    reasoning = (' '.join(str(obj.get('reasoning', '')).split()) or 'No reasoning given')[:600]
    return {'decision': 'TRADE', 'symbol': symbol, 'direction': direction,
            'entry_low': entry_low, 'entry_high': entry_high, 'target': target,
            'stop': stop, 'expected_hold_days': hold_days, 'reasoning': reasoning}


def build_checkin_prompt(info):
    is_long = info.get('direction') == 'LONG'
    moved = info.get('current_price', 0) - info.get('entry_price', 0)
    return "\n".join([
        "You picked this paper-trading swing setup a few days ago and it hasn't hit your "
        "target or stop yet, past your own expected hold time. No real money is at risk -- "
        "give an honest current read.",
        "",
        f"Symbol: {info.get('symbol')} | Direction: {info.get('direction')}",
        f"Entry: ${info.get('entry_price')} | Target: ${info.get('target')} | Stop: ${info.get('stop')}",
        f"Your original reasoning: {info.get('entry_reasoning')}",
        f"Expected hold: {info.get('expected_hold_days')} days | Actual so far: {info.get('days_held')} days",
        f"Current price: ${info.get('current_price')} ({'up' if moved >= 0 else 'down'} from entry, "
        f"{'in your favor' if (moved >= 0) == is_long else 'against you'})",
        "",
        "Given current price action, should this position be held longer or exited now? Explain "
        "your reasoning in 1-3 sentences -- this is shown to a human verbatim as the reason for "
        "whatever you decide.",
        "",
        "Respond with ONLY raw JSON, no markdown fences, no text before or after it, in EXACTLY "
        "this schema:",
        '{"decision": "HOLD" or "EXIT", "reasoning": "<1-3 sentences>"}',
    ])


def parse_checkin_decision(raw):
    """Fails open to None (not a forced verdict) -- the caller's deterministic
    max-hold-time backstop is what prevents an indefinite hang if this stays
    unparseable or every provider fails, so there is no need to guess here."""
    if not raw:
        return None
    obj = _extract_json_object(raw)
    if not isinstance(obj, dict):
        return None
    decision = str(obj.get('decision', '')).strip().upper()
    if decision not in ('HOLD', 'EXIT'):
        return None
    reasoning = (' '.join(str(obj.get('reasoning', '')).split()) or 'No reasoning given')[:600]
    return {'decision': decision, 'reasoning': reasoning}


def _read_capped(path, max_chars=CHAT_CONTEXT_CHAR_BUDGET):
    try:
        with open(path) as f:
            text = f.read()
        if len(text) > max_chars:
            text = text[:max_chars] + '\n…[truncated]'
        return text
    except Exception:
        return None


def build_chat_prompt(message):
    apex_snap = _read_capped(SNAPSHOT_PATH)
    spy_snap  = _read_capped(SPY_SNAPSHOT_PATH)
    apex_report = None
    try:
        with open(SUGGESTIONS_PATH) as f:
            apex_report = json.load(f).get('report_summary')
    except Exception:
        pass
    spy_report = None
    try:
        with open(SPY_REPORT_PATH) as f:
            spy_report = json.load(f).get('report_summary')
    except Exception:
        pass

    parts = [
        "You are Hermes, the trading assistant embedded in this trader's live dashboard. "
        "They are asking a question or making a request in a chat panel next to their real bot "
        "data. Answer directly and concretely using ONLY the real data given below -- never "
        "invent numbers, prices, or trade outcomes you don't actually have.",
        "",
        "COMMAND POWER (new, narrow, real): you CAN take exactly four actions if the trader "
        "clearly and explicitly asks for one right now -- close_position (a symbol's open "
        f"position), force_trail (lock a symbol's trailing stop), pause_bot, resume_bot. Valid "
        f"symbols: {', '.join(CHAT_ACTION_SYMBOLS)}. These are the SAME actions the dashboard's "
        "own buttons already trigger -- you are not getting any new power beyond what a button "
        "already does. Only set an action when the instruction is unambiguous (e.g. 'close NBIS', "
        "'lock the trail on TSLA', 'pause the bot'). If they're asking a question, making small "
        "talk, or you're not sure exactly which symbol/action they mean, set action to null and "
        "just answer -- never guess a symbol they didn't name, and never invent a 5th action type. "
        "You still cannot change trading parameters (leverage, stop distance, size) or edit the "
        "dashboard's code from here -- for a UI/code change request, acknowledge it in one "
        "sentence and say it's noted for the developer, since that needs an actual code change.",
        "",
        f"Trader's message: {message}",
        "",
    ]
    if apex_snap:
        parts.append(f"--- Apex futures bot: recent data snapshot ---\n{apex_snap}")
    if apex_report:
        parts.append(f"--- Apex futures bot: latest daily Hermes analysis ---\n{apex_report}")
    if spy_snap:
        parts.append(f"--- SPY 0DTE + crypto paper bot: recent data snapshot ---\n{spy_snap}")
    if spy_report:
        parts.append(f"--- SPY/crypto bot: latest daily Hermes analysis ---\n{spy_report}")
    if not any([apex_snap, apex_report, spy_snap, spy_report]):
        parts.append("(No cached bot data available right now -- say so plainly rather than guessing.)")
    parts.append(
        '\nOutput STRICT JSON only, nothing else -- no markdown fences, no text before or after:\n'
        '{"reply": "plain English, 2-4 sentences, no markdown headers -- renders in a small chat '
        'bubble", "action": null or {"type": "close_position|force_trail|pause_bot|resume_bot", '
        '"symbol": "<one of the valid symbols above, or null for pause_bot/resume_bot>"}}'
    )
    return "\n".join(parts)


def parse_chat_response(raw):
    """Fail-open on any ambiguity, same posture as parse_pick_decision: a
    malformed or half-compliant response degrades to 'just show the raw text
    as the reply, take no action' -- never a guessed action, never a crash."""
    if not raw:
        return {'reply': "Hermes didn't respond -- try again in a moment.", 'action': None}
    obj = _extract_json_object(raw)
    if not isinstance(obj, dict):
        return {'reply': raw.strip()[:1000], 'action': None}
    reply = obj.get('reply')
    reply = str(reply).strip()[:1000] if reply else "(no reply text)"
    action = obj.get('action')
    if not isinstance(action, dict):
        return {'reply': reply, 'action': None}
    a_type = action.get('type')
    if a_type not in CHAT_ACTION_TYPES:
        return {'reply': reply, 'action': None}
    symbol = action.get('symbol')
    if a_type in ('close_position', 'force_trail'):
        if symbol not in CHAT_ACTION_SYMBOLS:
            return {'reply': reply, 'action': None}
        return {'reply': reply, 'action': {'type': a_type, 'symbol': symbol}}
    return {'reply': reply, 'action': {'type': a_type, 'symbol': None}}


def log_dashboard_chat(message, reply, provider, elapsed, action=None):
    record = {'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'message': message,
              'reply': reply, 'action': action, 'provider': provider, 'elapsed_seconds': round(elapsed, 1)}
    try:
        with open(CHAT_LOG_PATH, 'a') as f:
            f.write(json.dumps(record) + '\n')
    except Exception:
        pass


def log_pick_decision(payload, result):
    """Same append-only-log pattern as log_veto_decision -- a real audit
    trail of every pick/no-pick/check-in call, independent of the compact
    summary that ends up in Apex's auto_tune_history."""
    record = {'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'request': payload, 'result': result}
    try:
        with open(PICK_LOG_PATH, 'a') as f:
            f.write(json.dumps(record) + '\n')
    except Exception:
        pass


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class VetoHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path == '/health':
            self._send_json(200, {'status': 'ok'})
        elif self.path == '/suggestions':
            self._handle_get_suggestions()
        elif self.path == '/report':
            self._handle_get_report()
        elif self.path == '/spy/report':
            self._handle_get_spy_report()
        else:
            self._send_json(404, {'error': 'not found'})

    def _handle_get_report(self):
        """Apex's own weekly narrative review -- same underlying file as
        _handle_get_suggestions (bot_suggestions.json), but this returns the
        plain-English report_summary that pull_hermes_suggestions() has
        always discarded (it only ever read candidate_suggestions). Added so
        Apex can show a human-readable analysis log, same as SPY already
        does via /spy/report. Same fail-open/staleness posture as everywhere
        else here."""
        try:
            st = os.stat(SUGGESTIONS_PATH)
            age_hours = (time.time() - st.st_mtime) / 3600.0
            if age_hours > SUGGESTIONS_MAX_AGE_HOURS:
                self._send_json(200, {'report_summary': None, 'reason': 'stale'})
                return
            with open(SUGGESTIONS_PATH) as f:
                data = json.load(f)
            self._send_json(200, {'report_summary': data.get('report_summary'),
                                   'generated_at': data.get('generated_at')})
        except Exception:
            self._send_json(200, {'report_summary': None, 'reason': 'none available'})

    def _handle_get_spy_report(self):
        """spy_options_bot.py pulls this weekly and delivers it over its own
        Telegram alert -- same fail-open posture as everything else: missing
        or stale (>10 days) resolves to no report, never an error, so a
        broken/not-yet-run analysis job just means silence, not a crash."""
        try:
            st = os.stat(SPY_REPORT_PATH)
            age_hours = (time.time() - st.st_mtime) / 3600.0
            if age_hours > SPY_REPORT_MAX_AGE_HOURS:
                self._send_json(200, {'report_summary': None, 'reason': 'stale'})
                return
            with open(SPY_REPORT_PATH) as f:
                data = json.load(f)
            self._send_json(200, {'report_summary': data.get('report_summary'),
                                   'generated_at': data.get('generated_at')})
        except Exception:
            self._send_json(200, {'report_summary': None, 'reason': 'none available'})

    def _handle_get_suggestions(self):
        """Apex pulls this at the start of run_weekly_auto_tune() -- whatever the
        most recent bot-review cron run produced. Empty/missing/stale (>10 days)
        all resolve to an empty list, same fail-open posture as everything else
        here: Apex's existing candidate search just runs unchanged if there's
        nothing fresh to add."""
        try:
            st = os.stat(SUGGESTIONS_PATH)
            age_hours = (time.time() - st.st_mtime) / 3600.0
            if age_hours > SUGGESTIONS_MAX_AGE_HOURS:
                self._send_json(200, {'suggestions': [], 'reason': 'stale'})
                return
            with open(SUGGESTIONS_PATH) as f:
                data = json.load(f)
            self._send_json(200, {'suggestions': data.get('candidate_suggestions', []),
                                   'generated_at': data.get('generated_at')})
        except Exception:
            self._send_json(200, {'suggestions': [], 'reason': 'none available'})

    def do_POST(self):
        if self.path == '/ingest_snapshot':
            self._handle_ingest_snapshot()
            return
        if self.path == '/spy/ingest_snapshot':
            self._handle_spy_ingest_snapshot()
            return
        if self.path == '/pick_trade':
            self._handle_pick_trade()
            return
        if self.path == '/pick_checkin':
            self._handle_pick_checkin()
            return
        if self.path == '/dashboard_chat':
            self._handle_dashboard_chat()
            return
        if self.path != '/veto_check':
            self._send_json(404, {'error': 'not found'})
            return
        try:
            length = int(self.headers.get('Content-Length', 0))
            trade = json.loads(self.rfile.read(length))
        except Exception as e:
            self._send_json(400, {'error': f'bad request: {e}'})
            return

        briefing = load_briefing()
        prompt = build_prompt(trade, briefing)
        t0 = time.time()
        raw, provider = ask_llm(prompt)
        decision, reason = parse_decision(raw)
        elapsed = time.time() - t0
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {trade.get('symbol')} "
              f"{trade.get('action')} -> {decision} via {provider} ({elapsed:.1f}s): {reason}", flush=True)
        log_veto_decision(trade, decision, reason, provider, elapsed)
        self._send_json(200, {'decision': decision, 'reason': reason,
                               'provider': provider, 'elapsed_seconds': round(elapsed, 1)})

    def _handle_pick_trade(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length))
        except Exception as e:
            self._send_json(400, {'error': f'bad request: {e}'})
            return
        candidates = body.get('candidates', [])
        briefing = load_briefing()
        prompt = build_pick_prompt(candidates, briefing)
        t0 = time.time()
        raw, provider = ask_llm(prompt, max_tokens=PICK_MAX_TOKENS, timeout=PICK_PROVIDER_TIMEOUT_SECONDS)
        result = parse_pick_decision(raw, candidates)
        result['provider'] = provider
        result['elapsed_seconds'] = round(time.time() - t0, 1)
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] pick_trade -> {result.get('decision')} "
              f"{result.get('symbol', '')} via {provider} ({result['elapsed_seconds']:.1f}s)", flush=True)
        log_pick_decision(body, result)
        self._send_json(200, result)

    def _handle_pick_checkin(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            info = json.loads(self.rfile.read(length))
        except Exception as e:
            self._send_json(400, {'error': f'bad request: {e}'})
            return
        prompt = build_checkin_prompt(info)
        t0 = time.time()
        raw, provider = ask_llm(prompt, max_tokens=CHECKIN_MAX_TOKENS, timeout=PICK_PROVIDER_TIMEOUT_SECONDS)
        result = parse_checkin_decision(raw) or {'decision': None, 'reasoning': 'Unparseable or unreachable'}
        result['provider'] = provider
        result['elapsed_seconds'] = round(time.time() - t0, 1)
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] pick_checkin[{info.get('symbol')}] -> "
              f"{result.get('decision')} via {provider} ({result['elapsed_seconds']:.1f}s)", flush=True)
        log_pick_decision(info, result)
        self._send_json(200, result)

    def _handle_dashboard_chat(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length))
        except Exception as e:
            self._send_json(400, {'error': f'bad request: {e}'})
            return
        message = str(body.get('message', '')).strip()[:2000]
        if not message:
            self._send_json(200, {'reply': "Didn't catch a question there -- try asking again.", 'action': None})
            return
        prompt = build_chat_prompt(message)
        t0 = time.time()
        raw, provider = ask_llm(prompt, max_tokens=CHAT_MAX_TOKENS, timeout=CHAT_PROVIDER_TIMEOUT_SECONDS)
        if raw:
            result = parse_chat_response(raw)
        else:
            result = {'reply': 'Hermes is unreachable right now -- all providers timed out or '
                                'failed. Nothing was changed, try again shortly.', 'action': None}
        elapsed = time.time() - t0
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] dashboard_chat -> via {provider} "
              f"({elapsed:.1f}s) action={result['action']}: {message[:80]!r}", flush=True)
        log_dashboard_chat(message, result['reply'], provider, elapsed, result['action'])
        self._send_json(200, {'reply': result['reply'], 'action': result['action'], 'provider': provider})

    def _handle_ingest_snapshot(self):
        """Apex pushes a data snapshot here periodically (trade log, current
        effective config, auto-tune history) -- overwrites the previous one.
        Deliberately push, not pull: Hermes never needs SSH/credentials to
        reach into Apex, Apex stays in control of exactly what it shares."""
        try:
            length = int(self.headers.get('Content-Length', 0))
            snapshot = json.loads(self.rfile.read(length))
            snapshot['_received_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
            with open(SNAPSHOT_PATH, 'w') as f:
                json.dump(snapshot, f)
            n_trades = len(snapshot.get('recent_trades', []))
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Received snapshot: "
                  f"{n_trades} trades, {len(snapshot.get('symbols', {}))} symbols", flush=True)
            self._send_json(200, {'status': 'ok', 'received_trades': n_trades})
        except Exception as e:
            self._send_json(400, {'error': f'bad snapshot: {e}'})

    def _handle_spy_ingest_snapshot(self):
        """SPY options bot pushes its trade summary here periodically. Same
        push-not-pull design as Apex's ingest_snapshot -- Hermes never needs
        credentials to reach into the SPY bot's droplet."""
        try:
            length = int(self.headers.get('Content-Length', 0))
            snapshot = json.loads(self.rfile.read(length))
            snapshot['_received_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
            with open(SPY_SNAPSHOT_PATH, 'w') as f:
                json.dump(snapshot, f)
            n_spy = len(snapshot.get('spy', {}).get('recent_trades', []))
            n_crypto = sum(len(v.get('recent_trades', []))
                           for v in snapshot.get('crypto_paper_trading', {}).get('by_symbol', {}).values())
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Received SPY snapshot: "
                  f"{n_spy} SPY + {n_crypto} crypto trades", flush=True)
            self._send_json(200, {'status': 'ok', 'received_trades': n_spy + n_crypto})
        except Exception as e:
            self._send_json(400, {'error': f'bad snapshot: {e}'})

    def _send_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    server = ThreadingHTTPServer((BIND_HOST, BIND_PORT), VetoHandler)
    print(f"Hermes veto service listening on {BIND_HOST}:{BIND_PORT}", flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
