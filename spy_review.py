#!/usr/bin/env python3
"""spy_review.py -- daily Hermes analysis of the SPY 0DTE credit-spread bot's
trade history and recent market sentiment (changed from weekly to daily
2026-09-17, on request, specifically so Hermes has a day-by-day memory of
what conditions looked like and how signals behaved, not just a weekly
rollup). Run via plain system crontab (NOT `hermes cron` -- that wrapper was
found to silently route a script's own HTTP calls back through Hermes's
flaky session layer even in --no-agent mode, see project memory).

Calls the 5 providers directly, same as bot_review.py / hermes_veto_service.py.
Unlike Apex's bot-review, SPY has no auto-tune/override system to feed
candidates into -- this is read-only: a plain-English report written here,
pulled and delivered by spy_options_bot.py over its own Telegram channel.
"""
import json
import os
import sys
import time
import requests

HERMES_HOME     = os.path.expanduser('~/.hermes')
SNAPSHOT_PATH   = os.path.join(HERMES_HOME, 'spy_snapshot.json')
REPORT_PATH     = os.path.join(HERMES_HOME, 'spy_report.json')
ANALYSIS_LOG    = os.path.join(HERMES_HOME, 'spy_review_analysis.log')
PROVIDER_TIMEOUT_SECONDS = 30

PROVIDERS = [
    {'name': 'groq', 'key_env': 'GROQ_API_KEY', 'model': 'openai/gpt-oss-120b',
     'url': 'https://api.groq.com/openai/v1/chat/completions'},
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
    # No API key needed (anonymous access) -- added 2026-09-18 as an extra
    # fallback after the providers above all hit their daily/rate limits the
    # same day. See hermes_veto_service.py's PROVIDERS list for the full note.
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


load_env_file(os.path.join(HERMES_HOME, '.env'))


def ask_llm(prompt, max_tokens=1800):
    for p in PROVIDERS:
        key_env = p.get('key_env')
        key = os.environ.get(key_env, '').strip() if key_env else None
        if key_env and not key:
            continue
        headers = {'Content-Type': 'application/json'}
        if key:
            headers['Authorization'] = f'Bearer {key}'
        try:
            resp = requests.post(
                p['url'],
                headers=headers,
                json={'model': p['model'], 'messages': [{'role': 'user', 'content': prompt}],
                      'max_tokens': max_tokens, 'temperature': 0.3},
                timeout=PROVIDER_TIMEOUT_SECONDS,
            )
            if resp.status_code != 200:
                continue
            content = resp.json()['choices'][0]['message'].get('content', '').strip()
            if content:
                return content, p['name']
        except Exception:
            continue
    return None, None


def main():
    if not os.path.exists(SNAPSHOT_PATH):
        print("No spy_snapshot.json yet -- SPY bot hasn't pushed data. Nothing to analyze.")
        return

    with open(SNAPSHOT_PATH) as f:
        snapshot = f.read()

    analysis_prompt = f"""You are reviewing a trading system's recent activity to find concrete,
actionable improvements. It has two parts:
1. SPY 0DTE credit spreads (BULL_PUT / BEAR_CALL), same-day expiry, real trades based on gamma
   exposure walls and options chain positioning -- the trader executes these manually.
2. BTC/ETH 0DTE credit spreads -- PAPER TRADING ONLY right now, no real money. The plan is to go
   live with real money after a few weeks of validation, so this part of your review is
   specifically a readiness check: is the strategy showing a real, consistent edge, or is it too
   early / too inconsistent to trust with real capital yet?

Here is the recent trade summary and current config for both:
---
{snapshot}
---

If present, "spy.recent_market_sentiment" summarizes the last 7 days of SIGNALS (not just trades
taken) -- VIX range, GEX-regime split (positive/negative), direction split (BULL_PUT/BEAR_CALL),
and average confidence score, across every signal generated whether or not it was traded. Since
this analysis now runs daily, use it to build a running sense of what conditions have looked like
recently and flag anything notable (e.g. VIX drifting toward a gate boundary, GEX regime flipping,
confidence trending down) -- this is memory across days, so a change from what you'd expect based
on prior context is worth naming explicitly, not just today's snapshot in isolation.

SPECIFIC CONCERN TO ANALYZE, raised after a real incident: on 2026-09-17 the signal flipped
BULL_PUT -> BEAR_CALL -> BULL_PUT across three consecutive scheduled slots the same day, and a
trade taken on the middle (BEAR_CALL) signal lost money once the next slot reversed back. If
present, "spy.signal_timing_stability" gives you real structured data to analyze this with:
"by_label" breaks down average confidence and direction split for each of the 5 daily slots
(market-open, mid-morning, late-morning, pre-noon, last-call) over the last 14 days;
"days_direction_flipped" vs "days_with_2plus_signals" tells you how often the direction actually
reverses within a single day; "early_slot_later_reversed" vs "early_slot_signals_checked" tells
you specifically how often an early-session signal (market-open or mid-morning) got contradicted
later the same day. Using this real data (not speculation), answer directly: are early-session
signals meaningfully less reliable than later ones? Is there a confidence threshold below which a
signal is more likely to flip later that day? Propose one concrete, testable approach -- e.g. a
minimum confidence bar before trading the first signal or two of the day, or a "wait one more
slot for confirmation" rule -- but be honest if the sample (only a couple weeks so far) is still
too thin to conclude anything with real confidence; say that plainly rather than overfitting to
a small number of days.

For the SPY section, look specifically for:
- Whether one direction (BULL_PUT vs BEAR_CALL) is meaningfully outperforming or underperforming
  the other, and whether that suggests a directional bias worth adjusting
- Whether losses cluster around specific conditions (particular strike distance/buffer, time of
  day, day of week, or how close price got to the short strike before the trade was closed)
- Whether the stop-loss/profit-taking thresholds seem to be cutting winners short or letting
  losers run, based on the profit_pct and pnl values across trades
- Anything about position sizing (contracts) that looks worth reconsidering given the results
- If present, "spy.shadow_simulation.SPY.by_variant" shows SIMULATED win rate and per-contract P&L
  for each of the 3 tiers (Conservative/Suggested/Aggressive) the bot generates every signal,
  whether or not that tier was actually traded -- this is real signal about which tier is actually
  profitable, not just which one happened to get traded. If one tier is clearly ahead with a
  reasonable sample size, say so explicitly and recommend it as the new default. If the sample is
  still thin, say that plainly instead of overstating a lead from a handful of signals.
- "spy.shadow_simulation.SPX.by_variant" is the same simulation for the SPX parallel spread the bot
  already computes on every signal. SPX is NOT currently traded -- treat this purely as forward
  prep, e.g. "SPX Aggressive is showing an early edge, worth revisiting once SPX trading starts,"
  never as something to act on today.

For the crypto (BTC/ETH) paper-trading section, look specifically for:
- Win rate and net P&L per symbol -- is either showing a real edge yet?
- Whether the sample is anywhere near large enough to justify going live, and if not, roughly what
  would need to be true (more trades, a cleaner win rate, fewer big losses) before it would be
- Any early warning sign (a single large loss dominating the total, a losing streak, one-sided
  direction performance) that should delay going live even if the overall number looks okay
- If present, each symbol's "diagnostics" section breaks losses down by weekday, by trend regime
  (BULLISH/BEARISH/CHOPPY at entry), by how far out-of-the-money the short strike was, and gives an
  explicit win/loss streak count -- use these to name a SPECIFIC likely cause of losses (e.g. "most
  losses come from CHOPPY-regime entries" or "one 4-trade losing streak accounts for most of the
  drawdown") instead of a generic "win rate is low." Treat any bucket with a small n as a hint, not
  a conclusion.

Be honest if the sample size is too small to conclude anything confidently -- say so rather than
overstating a pattern from a handful of trades, especially for the crypto go/no-go call. Write a
concise, plain-English report (a short paragraph per section) suitable for a Telegram message to
the trader. Give specific, concrete suggestions where you have them (e.g. "widen the buffer by $1"
or "not ready to go live yet -- win rate needs more data"). If there isn't enough data yet for a
real conclusion on either part, say that plainly instead of inventing a pattern."""

    analysis, provider = ask_llm(analysis_prompt)
    if not analysis:
        print("ERROR: analysis call failed on every provider", file=sys.stderr)
        sys.exit(1)

    with open(ANALYSIS_LOG, 'w') as f:
        f.write(analysis)

    now_iso = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    report = {'generated_at': now_iso, 'report_summary': analysis.strip(), 'provider': provider}
    with open(REPORT_PATH, 'w') as f:
        json.dump(report, f)
    print(f"Wrote {REPORT_PATH} (via {provider})")


if __name__ == '__main__':
    main()
