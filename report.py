#!/usr/bin/env python3
"""report.py - summarize the usage ledger ($AGENT_USAGE_DIR/logs/*.jsonl). Local only.

  report.py                      last 24h, table by bot x provider x model
  report.py --day 2026-10-18     one calendar day (local time)
  report.py --hours 6            last N hours
  report.py --by provider,model  choose grouping (fields: bot provider model script endpoint)
  report.py --bot research       filter one agent (substring)
  report.py --json               machine-readable
  report.py --errors-since 30    NEW errors in the last 30 min (deduped via a state file); prints
                                 nothing and exits 0 when there are none. --peek = don't mark seen.
Codex CLI sessions are imported first (codex_import.py) unless --no-codex.
"""
import argparse
import fcntl
import glob
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import usage_log as U  # noqa: E402

SEEN = os.path.join(U.STATE_DIR, "errors_seen.json")


def load(since, until=None):
    out = []
    d0 = (since - timedelta(days=1)).date()
    for fp in sorted(glob.glob(os.path.join(U.LOG_DIR, "*.jsonl"))):
        try:
            if datetime.strptime(os.path.basename(fp)[:10], "%Y-%m-%d").date() < d0:
                continue
        except ValueError:
            continue
        with open(fp, errors="replace") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            lines = f.readlines()
            fcntl.flock(f, fcntl.LOCK_UN)
        for ln in lines:
            try:
                r = json.loads(ln)
                t = datetime.fromisoformat(r["ts"])
            except (ValueError, KeyError, TypeError):
                continue
            if t < since or (until and t >= until):
                continue
            r["_t"] = t
            out.append(r)
    return out


def fmt_n(x):
    return f"{x:,}" if x else "0"


def summary(recs, keys, as_json):
    g = defaultdict(lambda: {"calls": 0, "ok": 0, "errors": 0, "tokens_in": 0, "tokens_out": 0,
                             "lat": 0.0, "nlat": 0, "cost": defaultdict(float)})
    for r in recs:
        a = g[tuple(str(r.get(k) or "-") for k in keys)]
        a["calls"] += 1
        a["ok" if r.get("ok") else "errors"] += 1
        a["tokens_in"] += r.get("tokens_in") or 0
        a["tokens_out"] += r.get("tokens_out") or 0
        if r.get("latency_s") is not None:
            a["lat"] += r["latency_s"]
            a["nlat"] += 1
        ch = r.get("cost_hint")
        if isinstance(ch, dict):
            for k, v in ch.items():
                if isinstance(v, (int, float)):
                    a["cost"][k] += v
    rows = sorted(g.items(), key=lambda kv: (-kv[1]["calls"], kv[0]))
    if as_json:
        print(json.dumps([dict(zip(keys, k), **{x: v for x, v in a.items() if x not in ("lat", "nlat")},
                               avg_latency_s=round(a["lat"] / a["nlat"], 1) if a["nlat"] else None)
                          for k, a in rows], ensure_ascii=False, indent=1))
        return
    hdr = [*keys, "calls", "err", "tok_in", "tok_out", "avg_s", "cost_hint"]
    table = [hdr]
    for k, a in rows:
        extra = " ".join(f"{n}={v:g}" for n, v in sorted(a["cost"].items()) if v)
        table.append([*k, str(a["calls"]), str(a["errors"]), fmt_n(a["tokens_in"]), fmt_n(a["tokens_out"]),
                      f"{a['lat'] / a['nlat']:.1f}" if a["nlat"] else "-", extra])
    tot = [sum(a[x] for _, a in rows) for x in ("calls", "errors", "tokens_in", "tokens_out")]
    table.append(["TOTAL"] + [""] * (len(keys) - 1) + [str(tot[0]), str(tot[1]), fmt_n(tot[2]), fmt_n(tot[3]), "", ""])
    w = [max(len(row[i]) for row in table) for i in range(len(hdr))]
    for i, row in enumerate(table):
        print("  ".join(c.ljust(w[j]) if j < len(keys) else c.rjust(w[j]) for j, c in enumerate(row)).rstrip())
        if i == 0 or i == len(table) - 2:
            print("  ".join("-" * x for x in w))


def errors_since(minutes, peek, bot):
    now = datetime.now().astimezone()
    recs = [r for r in load(now - timedelta(minutes=minutes)) if not r.get("ok")]
    if bot:
        recs = [r for r in recs if bot in (r.get("bot") or "")]
    os.makedirs(os.path.dirname(SEEN), exist_ok=True)
    with open(SEEN + ".lock", "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            with open(SEEN) as f:
                seen = json.load(f)
        except (OSError, ValueError):
            seen = {}
        new = [r for r in recs if r.get("id") and r["id"] not in seen]
        if new and not peek:
            cutoff = time.time() - 8 * 86400
            seen = {k: v for k, v in seen.items() if v >= cutoff}
            for r in new:
                seen[r["id"]] = time.time()
            tmp = SEEN + ".tmp"
            with open(tmp, "w") as f:
                json.dump(seen, f)
            os.replace(tmp, SEEN)
    if not new:
        return
    print(f"[usage] {len(new)} new API error(s) in last {minutes:g} min:")
    for r in sorted(new, key=lambda r: r["_t"]):
        print(f"- {r['_t']:%m-%d %H:%M:%S} bot={r.get('bot')} {r.get('provider')}/{r.get('model')} "
              f"{r.get('endpoint') or ''} status={r.get('status')} script={r.get('script')}: "
              f"{(r.get('error') or '').replace(chr(10), ' ')[:300]}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--day")
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--by", default="bot,provider,model")
    ap.add_argument("--bot")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-codex", action="store_true")
    ap.add_argument("--errors-since", type=float, metavar="MIN")
    ap.add_argument("--peek", action="store_true")
    a = ap.parse_args(argv)
    if not a.no_codex and a.errors_since is None:
        try:
            import codex_import
            codex_import.run()
        except Exception as e:
            print(f"(codex import skipped: {type(e).__name__})", file=sys.stderr)
    if a.errors_since is not None:
        errors_since(a.errors_since, a.peek, a.bot)
        return
    if a.day:
        since = datetime.strptime(a.day, "%Y-%m-%d").astimezone()
        until = since + timedelta(days=1)
        title = f"day {a.day} (local time)"
    else:
        until = None
        since = datetime.now().astimezone() - timedelta(hours=a.hours)
        title = f"last {a.hours:g}h since {since:%Y-%m-%d %H:%M %z}"
    recs = load(since, until)
    if a.bot:
        recs = [r for r in recs if a.bot in (r.get("bot") or "")]
    if not a.json:
        print(f"# API usage, {title}: {len(recs)} call(s)")
    summary(recs, [k.strip() for k in a.by.split(",") if k.strip()], a.json)


if __name__ == "__main__":
    main()
