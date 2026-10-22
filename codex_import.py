#!/usr/bin/env python3
"""codex_import.py - import Codex CLI token usage from ~/.codex/sessions into the usage ledger.

Codex is a binary you do not wrap, so instead this reads its own session files (local,
read-only): per session, the last `token_count.total_token_usage` minus what was already imported
(state file) becomes one record: provider=codex, model from turn_context, bot="codex@<cwd basename>".
Idempotent; run automatically by report.py (disable with --no-codex). No network.
"""
import glob
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import usage_log as U  # noqa: E402

SESS = os.path.expanduser(os.environ.get("CODEX_HOME", "~/.codex"))
STATE = os.path.join(U.STATE_DIR, "codex_import.json")


def scan(path):
    sid = cwd = model = last = ts = orig = None
    with open(path, errors="replace") as f:
        for line in f:
            if '"session_meta"' not in line and '"turn_context"' not in line and '"token_count"' not in line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            p = o.get("payload") or {}
            if o.get("type") == "session_meta":
                sid, cwd, orig = p.get("id") or p.get("session_id"), p.get("cwd"), p.get("originator")
            elif o.get("type") == "turn_context":
                model = p.get("model") or model
            elif p.get("type") == "token_count" and (p.get("info") or {}).get("total_token_usage"):
                last, ts = p["info"]["total_token_usage"], o.get("timestamp")
    return sid, cwd, model, last, ts, orig


def _load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def run(quiet=True):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    st = _load_state()
    files = glob.glob(os.path.join(SESS, "sessions", "**", "*.jsonl"), recursive=True) + \
        glob.glob(os.path.join(SESS, "archived_sessions", "**", "*.jsonl"), recursive=True)
    n = 0
    for fp in files:
        try:
            mt = os.path.getmtime(fp)
        except OSError:
            continue
        if st.get("_mtime", {}).get(fp) == mt:
            continue
        sid, cwd, model, tot, ts, orig = scan(fp)
        st.setdefault("_mtime", {})[fp] = mt
        if not sid or not tot:
            continue
        prev = st.get(sid, {})
        d_in = (tot.get("input_tokens") or 0) - prev.get("in", 0)
        d_out = (tot.get("output_tokens") or 0) - prev.get("out", 0)
        if d_in <= 0 and d_out <= 0:
            continue
        d_cached = (tot.get("cached_input_tokens") or 0) - prev.get("cached", 0)
        d_reason = (tot.get("reasoning_output_tokens") or 0) - prev.get("reason", 0)
        try:
            when = datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None
        except ValueError:
            when = None
        U.log_call("codex", model, "codex@" + os.path.basename((cwd or "").rstrip("/") or "?"), d_in, d_out,
                   None, True, None, None, None, "codex " + (orig or ""), "codex", ts=when,
                   session=sid, cached_tokens=d_cached or None, reasoning_tokens=d_reason or None, cwd=cwd)
        st[sid] = {"in": tot.get("input_tokens") or 0, "out": tot.get("output_tokens") or 0,
                   "cached": tot.get("cached_input_tokens") or 0, "reason": tot.get("reasoning_output_tokens") or 0}
        n += 1
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, STATE)
    if not quiet:
        print(f"codex_import: {n} session delta(s) imported")
    return n


if __name__ == "__main__":
    run(quiet=False)
