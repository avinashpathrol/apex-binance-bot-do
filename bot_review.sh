#!/bin/bash
# bot_review.sh -- weekly Hermes analysis of Apex's trade history, live config,
# auto-tune history, and Hermes's own past veto-check decisions. Produces
# candidate parameter suggestions (NOT direct config changes -- Apex's
# existing statistical backtest + rollback gate is still the sole thing that
# actually decides what reaches live config; see check_hermes_veto /
# pull_hermes_suggestions in trading_bot_futures.py).
#
# Same two-step, bash-writes-the-file pattern as macro_briefing.sh, for the
# same reason: a single combined "analyze AND write JSON" prompt was observed
# (on that job) skipping the real work and writing lazy defaults. Splitting
# analysis from formatting removes the incentive to shortcut.
set -uo pipefail

HERMES_HOME=/root/.hermes
NOW_ISO=$(date -u +%Y-%m-%dT%H:%M:%SZ)
SNAPSHOT_PATH="$HERMES_HOME/apex_snapshot.json"
VETO_LOG_PATH="$HERMES_HOME/veto_decisions.jsonl"

if [ ! -f "$SNAPSHOT_PATH" ]; then
    echo "No apex_snapshot.json yet -- Apex hasn't pushed data. Nothing to analyze." >&2
    exit 0
fi

SNAPSHOT=$(cat "$SNAPSHOT_PATH")
VETO_HISTORY=$( [ -f "$VETO_LOG_PATH" ] && tail -n 200 "$VETO_LOG_PATH" || echo "(no veto decisions logged yet)" )

ANALYSIS=$(hermes -z "You are reviewing a systematic futures trading bot's recent performance to find
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

Here is the bot's current snapshot (recent trade log, current effective config per symbol,
auto-tune change history):
---
$SNAPSHOT
---

Here is the log of this same system's own recent pre-trade veto/approve decisions (each entry:
symbol, proposed action, the bot's technical confidence/reason, the decision made, and why):
---
$VETO_HISTORY
---

Analyze this. Look specifically for: symbols with a high win rate but flat or negative net P&L
(suggests exit parameters cutting winners short or letting losers run), symbols where a recent
auto-tune change hasn't been validated yet or was reverted, entry types or times of day that
consistently underperform, and whether past veto/approve calls look justified in hindsight given
what you can tell from the data. For each concern worth testing, propose ONE specific new
parameter value (not a range) with a one-sentence reason. Do not propose changes to a symbol
you have no real signal on. Write your findings as a plain-text report." --ignore-rules 2>&1)

if [ -z "$ANALYSIS" ]; then
    echo "ERROR: analysis call returned nothing -- keeping previous suggestions file" >&2
    exit 1
fi
echo "$ANALYSIS" > "$HERMES_HOME/bot_review_analysis.log"

FORMAT_PROMPT="You are given this trading-bot analysis, already written -- do not analyze again, just
convert it:
---
$ANALYSIS
---
Convert this into EXACTLY this JSON schema. Output ONLY the raw JSON on its own, nothing else --
no markdown fences, no commentary, no leading or trailing text. If the analysis proposed no
concrete parameter changes, candidate_suggestions should be an empty list -- do not invent one to
fill the schema.
{\"generated_at\": \"$NOW_ISO\", \"report_summary\": \"2-4 sentence plain-English summary of the
overall findings, suitable for a Telegram message\", \"candidate_suggestions\": [{\"symbol\":
\"e.g. TSLAUSDT\", \"param\": \"one of hard_sl_atr/trail_activate_atr/trail_dist_atr/rsi_long_min/
rsi_long_max/rsi_short_min/rsi_short_max/pullback_zone_pct\", \"value\": 1.23, \"reasoning\": \"one
sentence\"}]}"

WROTE=0
for attempt in 1 2; do
    hermes -z "$FORMAT_PROMPT" --ignore-rules --reasoning none > "$HERMES_HOME/bot_review_raw.txt" 2>&1

    if python3 - "$HERMES_HOME/bot_review_raw.txt" "$HERMES_HOME/bot_suggestions.json" << 'PYEOF'
import json, re, sys
raw_path, out_path = sys.argv[1], sys.argv[2]
raw = open(raw_path).read()
m = re.search(r'\{.*\}', raw, re.DOTALL)
if not m:
    print('ERROR: no JSON object found in model output', file=sys.stderr)
    sys.exit(1)
obj = json.loads(m.group(0))
obj.setdefault('candidate_suggestions', [])
with open(out_path, 'w') as f:
    json.dump(obj, f)
print(f"Wrote {out_path}: {len(obj['candidate_suggestions'])} suggestions")
PYEOF
    then
        WROTE=1
        break
    fi
    echo "Format attempt $attempt failed, retrying..." >&2
    sleep 3
done

if [ "$WROTE" -ne 1 ]; then
    echo "ERROR: both formatting attempts failed -- keeping previous suggestions file" >&2
    exit 1
fi
