# agent-usage-ledger

**A tiny, shared, per-agent ledger for LLM/API calls: who called what, how many tokens, what failed.**

When several agents (bots, cron jobs, Codex sessions, research scripts) share one machine and a
handful of API keys, "where did the quota go?" and "which bot has been getting 429s all night?"
become hard questions. agent-usage-ledger answers them with almost no machinery:

- `usage_log.py`: one function, `log_call(...)`, appends one JSON line per call to
  `logs/YYYY-MM-DD.jsonl` under an exclusive `fcntl` lock (safe across processes and agents). A
  `Tracker` accumulates usage from streaming events and logs once. `usage_from()` understands
  OpenAI Chat Completions, OpenAI Responses and Gemini usage shapes (incl. reasoning/cached tokens).
  It **never raises** (accounting must not break the caller) and **never stores keys**: error
  text is redacted and truncated.
- `report.py`: tables by agent x provider x model (or any grouping), JSON output, and
  `--errors-since N`, which prints only *new* errors (deduplicated), for cron alerts.
- `codex_import.py`: imports Codex CLI token usage from `~/.codex/sessions` (read-only), so
  subscription usage appears next to API usage.
- `error_alert.sh`: a rate-limited wrapper that hands new errors to any notifier you like.

Pure standard-library Python 3.9+, no network, no daemon. It only *records* calls: it never
retries, fails over, or switches providers.

## Install

```bash
git clone https://github.com/sdpwh7-ux/agent-usage-ledger.git
export PYTHONPATH="$PWD/agent-usage-ledger:$PYTHONPATH"   # or copy usage_log.py next to your scripts
```

## Usage

Tell the ledger who is calling (once per agent/process):

```bash
export AGENT_NAME=research-bot
```

Log a call:

```python
import time
from usage_log import log_call, usage_from, Tracker

t0 = time.time(); resp = client_call(...)                  # your call, any SDK
ti, to, extra = usage_from(resp)                           # OpenAI chat / Responses / Gemini shapes
log_call("openai", "gpt-4o-mini", tokens_in=ti, tokens_out=to, ok=True, status=200,
         latency=time.time() - t0, endpoint="/chat/completions", **extra)

# streaming
tr = Tracker("openai", "my-model", "/v1/responses")
for ev in stream: tr.feed(ev)
tr.done(ok=True, status=200)
```

Optional `cost_hint={"usd": 0.004}` (or any numeric keys such as `pages`) is summed by the report.
See `examples/wrap_call.py` for a complete stdlib example.

Reports:

```bash
python3 report.py                                  # last 24h, agent x provider x model
python3 report.py --day 2026-10-18                 # one calendar day (local time)
python3 report.py --hours 6 --by provider,model --bot research --json
python3 report.py --errors-since 15                # new errors only; silent when none
```

```
# API usage, last 24h since 2026-10-21 09:00 -0400: 4 call(s)
bot          provider  model  calls  err  tok_in  tok_out  avg_s  cost_hint
-----------  --------  -----  -----  ---  ------  -------  -----  ---------
research-bot openai    m1         3    0     300       30    2.0
other        deepseek  m2         1    1       0        0      -
-----------  --------  -----  -----  ---  ------  -------  -----  ---------
TOTAL                             4    1     300       30
```

Alerts from cron (at most one every 2 h and 3 per day by default; errors in between are batched):

```cron
*/15 * * * * ALERT_CMD='notify-send "$TITLE" "$BODY"' /path/to/agent-usage-ledger/error_alert.sh
```

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `AGENT_NAME` | `unknown:<script>` | agent name recorded with each call |
| `AGENT_USAGE_DIR` | `~/.local/share/agent-usage` | ledger root (`logs/` inside) |
| `AGENT_USAGE_STATE` | `~/.local/state/agent-usage` | dedupe/import state |
| `AGENT_USAGE_LOG` | `1` | `0` disables logging |
| `AGENT_USAGE_KEY_FILES` | - | `:`-separated key files whose contents are scrubbed from errors |
| `AGENT_USAGE_SECRET_ENV` | common `*_API_KEY` names | env vars whose values are scrubbed |
| `CODEX_HOME` | `~/.codex` | where Codex CLI sessions live |
| `ALERT_CMD`, `ALERT_MIN_INTERVAL`, `ALERT_MAX_PER_DAY`, `ALERT_WINDOW_MIN` | - / 7200 / 3 / 180 | `error_alert.sh` |

Record fields: `ts id bot script provider model endpoint ok status tokens_in tokens_out cost_hint
latency_s error pid`, plus extras (`reasoning_tokens`, `cached_tokens`, `resp_model`, `stream`, ...).

## Security notes for agent tooling

- **Credentials.** The ledger never asks for keys. To redact error text it reads the env vars in
  `AGENT_USAGE_SECRET_ENV` and the files in `AGENT_USAGE_KEY_FILES` *in memory only*; on top of
  that it masks `Bearer ...` and any 32+ character token-like string. Redaction is best-effort:
  do not log request bodies or prompts in `error`.
- **Files.** Log files are created with mode 640 under your umask. They contain model names,
  token counts and (redacted) error text, but no prompts or outputs. Still, treat them as internal.
- **Network.** None. `report.py` and `codex_import.py` are read-only over local files
  (`codex_import.py` reads `~/.codex/sessions/**` and writes only its own state file).
- **Shell.** `error_alert.sh` runs `ALERT_CMD` with `bash -c`; the error text is passed through the
  environment (`$BODY`), never interpolated into the command string. Quote `"$BODY"` in your command.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT
