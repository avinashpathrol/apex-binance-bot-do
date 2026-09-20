#!/bin/bash
# macro_briefing.sh -- two-step LLM chain for the daily macro briefing.
#
# Split into two separate Hermes calls deliberately: a single combined
# "search AND write JSON" prompt was observed skipping the actual search
# and writing lazy placeholder defaults instead (technically valid JSON,
# factually empty). Splitting removes the incentive to shortcut -- the
# research call has no file-writing pressure, and the formatting call has
# no search tools to reach for, just the transcript already gathered.
# The file write itself happens here in bash, not via an LLM tool call,
# so a lazy or malformed response can only fail loudly (see python step
# below), never silently overwrite a good file with garbage.
#
# The formatting call gets 2 attempts -- observed one transient internal
# Hermes error ("context compression temporarily paused") on this step
# during testing; a bare retry cleared it.
#
# --provider gemini -m gemini-flash-latest on both calls below: added
# 2026-09-18 after this script silently degraded to a useless "data
# collection failed" briefing for at least a day. Root cause: the CLI's
# default model (openai/gpt-oss-120b via Groq) started hard-rejecting every
# request with "Reasoning is mandatory for this endpoint and cannot be
# disabled" (HTTP 400) -- a provider-side API contract change colliding with
# this install's global `reasoning_effort: "none"` default in config.yaml
# (itself set deliberately for an earlier, different Groq issue -- see that
# file's comment). The fallback chain then also failed over across every
# other provider without ever producing real output, so the script's own
# `[ -z "$RESEARCH" ]` guard never caught it (the error text itself is
# non-empty). Forcing gemini here sidesteps the broken Groq/reasoning
# interaction entirely -- confirmed working, including real web search,
# 2026-09-18. This is scoped to just this script's two calls, not a change
# to the global CLI default, since other hermes usage may depend on that.
set -uo pipefail

TODAY_ET=$(TZ=America/New_York date +%Y-%m-%d)
NOW_ISO=$(date -u +%Y-%m-%dT%H:%M:%SZ)
HERMES_HOME=/root/.hermes

RESEARCH=$(hermes -z "Today's real date is $TODAY_ET (Eastern Time). Use web search tools now, and answer these two questions precisely -- do not skip searching, and do not guess:
1. Search specifically for the Federal Reserve's official FOMC meeting calendar (federalreserve.gov) and the US Bureau of Labor Statistics release schedule (bls.gov) for this month. From those official sources, state: is a US CPI report, PPI report, FOMC/Federal Reserve interest rate decision, or major jobs report (Non-Farm Payrolls) scheduled for today, tomorrow, or later this week? Give exact dates/times ET, and cite the specific source URL for each date you report. If two searches disagree, prefer the .gov source and say so.
2. What is the current overall US equity market sentiment, and specifically semiconductor/tech/AI sector sentiment (relevant to NBIS, AMD, APP, SOXL, CRCL, ASTS, TSLA)?
Report your findings in plain text with sources for every date you claim. Do not write any file -- just report what you found." --ignore-rules --provider gemini -m gemini-flash-latest 2>&1)

if [ -z "$RESEARCH" ] || echo "$RESEARCH" | grep -qi "no reply.*fallback chain\|kept failing over"; then
    echo "ERROR: research call returned nothing usable (empty or a provider-failure message masquerading as content) -- keeping previous briefing file" >&2
    echo "$RESEARCH" > "$HERMES_HOME/macro_briefing_research.log"
    exit 1
fi
echo "$RESEARCH" > "$HERMES_HOME/macro_briefing_research.log"

FORMAT_PROMPT="You are given this research, already gathered -- do not search again, just use it:
---
$RESEARCH
---
Convert this into EXACTLY this JSON schema. Output ONLY the raw JSON on its own, nothing else -- no markdown fences, no commentary, no leading or trailing text:
{\"date\": \"$TODAY_ET\", \"generated_at\": \"$NOW_ISO\", \"high_impact_today_or_tomorrow\": true or false, \"events\": [{\"name\": \"...\", \"date\": \"YYYY-MM-DD\", \"time_et\": \"HH:MM or unknown\", \"impact\": \"high or medium\"}], \"overall_sentiment\": \"risk-on or risk-off or neutral\", \"sentiment_notes\": \"one or two sentence summary\", \"caution_flag\": true or false, \"caution_reason\": \"short reason if caution_flag is true, else empty string\"}"

WROTE=0
for attempt in 1 2; do
    hermes -z "$FORMAT_PROMPT" --ignore-rules --provider gemini -m gemini-flash-latest > "$HERMES_HOME/macro_briefing_raw.txt" 2>&1

    if python3 - "$HERMES_HOME/macro_briefing_raw.txt" "$HERMES_HOME/macro_briefing.json" << 'PYEOF'
import json, re, sys
raw_path, out_path = sys.argv[1], sys.argv[2]
raw = open(raw_path).read()
m = re.search(r'\{.*\}', raw, re.DOTALL)
if not m:
    print('ERROR: no JSON object found in model output', file=sys.stderr)
    sys.exit(1)
obj = json.loads(m.group(0))
with open(out_path, 'w') as f:
    json.dump(obj, f)
print(f'Wrote {out_path}: {len(json.dumps(obj))} bytes')
PYEOF
    then
        WROTE=1
        break
    fi
    echo "Format attempt $attempt failed, retrying..." >&2
    sleep 3
done

if [ "$WROTE" -ne 1 ]; then
    echo "ERROR: both formatting attempts failed -- keeping previous briefing file" >&2
    exit 1
fi
