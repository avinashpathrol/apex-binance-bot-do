#!/usr/bin/env python3
"""bot_review.py -- daily Hermes analysis of Apex trade history, config, and
Hermes's own veto-call history (changed from weekly to daily 2026-09-17, on
request, so Hermes has a day-by-day memory of signal behavior -- note this
is analysis/memory only; actual auto-tune decisions stay on their own
slower, evidence-gated cadence in trading_bot_futures.py, unaffected by this
change). Run via plain system crontab, same reasoning as spy_review.py
(NOT `hermes cron` -- despite what an earlier version of this docstring
said, that wrapper was found to silently route this script's own HTTP calls
back through Hermes's flaky session layer even in --no-agent mode).

Calls the 5 providers directly over HTTP, same as hermes_veto_service.py,
deliberately bypassing the `hermes` CLI's agent/session layer. The CLI-based
version of this job (via `hermes -z`) failed twice in testing with an
internal "context compression temporarily paused" error and total
fallback-chain exhaustion, unrelated to prompt size (happened again after
trimming the payload from 120KB to 22KB) -- a real reliability problem with
that layer on this installation, not something prompt engineering fixes.
This job doesn't need web search (macro_briefing.sh still uses the full CLI
for that, where it's actually needed), so bypassing it entirely is a clean
fix, not a workaround.
"""
import json
import os
import re
import sys
import time
import requests

HERMES_HOME      = os.path.expanduser('~/.hermes')
SNAPSHOT_PATH    = os.path.join(HERMES_HOME, 'apex_snapshot.json')
VETO_LOG_PATH    = os.path.join(HERMES_HOME, 'veto_decisions.jsonl')
SUGGESTIONS_PATH = os.path.join(HERMES_HOME, 'bot_suggestions.json')
ANALYSIS_LOG     = os.path.join(HERMES_HOME, 'bot_review_analysis.log')
PROVIDER_TIMEOUT_SECONDS = 30  # slow scheduled job, not a hot path -- generous budget

# Same 5-provider chain as config.yaml's fallback_providers / hermes_veto_service.py.
# Keep all three in sync if the chain ever changes.
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


def ask_llm(prompt, max_tokens=1500):
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
        print("No apex_snapshot.json yet -- Apex hasn't pushed data. Nothing to analyze.")
        return

    with open(SNAPSHOT_PATH) as f:
        snapshot = f.read()

    if os.path.exists(VETO_LOG_PATH):
        with open(VETO_LOG_PATH) as f:
            veto_history = ''.join(f.readlines()[-200:])
    else:
        veto_history = '(no veto decisions logged yet)'

    analysis_prompt = f"""You are reviewing a systematic futures trading bot's recent performance to find
concrete, testable improvements. You do NOT set live config values yourself -- your job is to
identify patterns and propose SPECIFIC candidate parameter values worth backtesting. A separate
system will backtest each candidate against real historical price data and only apply it if it
passes a statistical consistency check across multiple time chunks -- so it's fine, and expected,
to propose ideas that might not pan out.

Tunable params and their sane bounds (never propose outside these):
- hard_sl_atr: 0.3 to 3.0 (hard stop-loss distance, in ATR multiples)
- trail_activate_atr: 0.3 to 3.0 (profit distance, in ATR, before trailing stop activates)
- trail_dist_atr: 0.1 to 1.0 (trailing stop distance, in ATR, once active)
- rsi_long_min / rsi_long_max: 10-50 / 50-90 (RSI band for LONG entries)
- rsi_short_min / rsi_short_max: 10-50 / 50-90 (RSI band for SHORT entries)
- pullback_zone_pct: 0.005 to 0.05 (how close to EMA21 counts as a valid pullback entry)

Here is the bot's current snapshot (pre-aggregated per-symbol performance, config, auto-tune history):
---
{snapshot}
---

Here is the log of this same system's own recent pre-trade veto/approve decisions:
---
{veto_history}
---

If present, "recent_market_sentiment" summarizes the last 7 days of regime/trend samples across
all symbols (ADX range, how often each regime/trend/action occurred, plus a per-symbol trend
breakdown) -- sampled hourly, not just at trade time, so it reflects general market conditions
even during periods with no trades. Since this analysis now runs daily, use it to build a running
sense of what conditions have looked like recently and flag anything notable (e.g. ADX trending
into/out of the tradeable range, a symbol's trend flipping) -- this is memory across days, worth
naming explicitly when today's picture differs from what recent days showed.

This bot also runs a completely separate strategy on Micron (MUUSDT): buy at US market close, sell
at next market open, once per weeknight. See the "mu_overnight" key in the snapshot (its current $
collateral / leverage sizing, and aggregated win/loss/net performance). It is NOT one of the
symbols above and does not use hard_sl_atr/trail/RSI params -- a separate deterministic mechanism
(not you) decides any sizing changes for it, gated on real trade evidence. Give your qualitative
read on whether MU's current sizing looks appropriate given its win rate and average net P&L per
trade so far, as part of your plain-text report_summary narrative only -- do not propose an MU
parameter change in candidate_suggestions, since none of the tunable params below apply to it.

Analyze this. Look specifically for: symbols with a high win rate but flat or negative net P&L
(suggests exit parameters cutting winners short or letting losers run), symbols where a recent
auto-tune change hasn't been validated yet or was reverted, entry types/exit reasons/hours that
consistently underperform, and whether past veto/approve calls look justified in hindsight. For
each concern worth testing, propose ONE specific new parameter value (not a range) with a
one-sentence reason. Do not propose changes to a symbol you have no real signal on. Write your
findings as a plain-text report."""

    analysis, provider1 = ask_llm(analysis_prompt, max_tokens=1200)
    if not analysis:
        print("ERROR: analysis call failed on every provider", file=sys.stderr)
        sys.exit(1)
    with open(ANALYSIS_LOG, 'w') as f:
        f.write(analysis)

    now_iso = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    format_prompt = f"""You are given this trading-bot analysis, already written -- do not analyze again,
just convert it:
---
{analysis}
---
Convert this into EXACTLY this JSON schema. Output ONLY the raw JSON, nothing else -- no markdown
fences, no commentary. If the analysis proposed no concrete parameter changes, candidate_suggestions
should be an empty list -- do not invent one to fill the schema.
{{"generated_at": "{now_iso}", "report_summary": "2-4 sentence plain-English summary suitable for a
Telegram message", "candidate_suggestions": [{{"symbol": "e.g. TSLAUSDT", "param": "one of
hard_sl_atr/trail_activate_atr/trail_dist_atr/rsi_long_min/rsi_long_max/rsi_short_min/
rsi_short_max/pullback_zone_pct", "value": 1.23, "reasoning": "one sentence"}}]}}"""

    raw, provider2 = ask_llm(format_prompt, max_tokens=1500)
    if not raw:
        print("ERROR: format call failed on every provider -- keeping previous suggestions file",
              file=sys.stderr)
        sys.exit(1)

    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if not m:
        print(f"ERROR: no JSON object found in format output: {raw[:300]!r}", file=sys.stderr)
        sys.exit(1)
    try:
        obj = json.loads(m.group(0))
    except Exception as e:
        print(f"ERROR: JSON parse failed ({e}) -- keeping previous suggestions file", file=sys.stderr)
        sys.exit(1)

    obj.setdefault('candidate_suggestions', [])
    obj.setdefault('generated_at', now_iso)
    with open(SUGGESTIONS_PATH, 'w') as f:
        json.dump(obj, f)
    print(f"Wrote {SUGGESTIONS_PATH}: {len(obj['candidate_suggestions'])} suggestions "
          f"(analysis via {provider1}, format via {provider2})")


if __name__ == '__main__':
    main()
