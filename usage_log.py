"""usage_log.py - tiny shared per-agent usage ledger + error log for model/API calls.

    from usage_log import log_call
    log_call("openai", "gpt-4o-mini", tokens_in=12, tokens_out=3, ok=True, status=200, latency=1.2)

Appends one JSON line per call to $AGENT_USAGE_DIR/logs/YYYY-MM-DD.jsonl (local date) under an
exclusive fcntl lock, so many processes and many agents can share one ledger. Never raises
(accounting must not break the caller). Never stores keys: error text is redacted (known key
files/env vars + token-like strings) and truncated.

Agent name: argument > $AGENT_NAME > "unknown:<script name>". Disable with AGENT_USAGE_LOG=0.
Pure stdlib, no network.
"""
import fcntl
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime

USAGE_DIR = os.path.expanduser(os.environ.get("AGENT_USAGE_DIR") or "~/.local/share/agent-usage")
LOG_DIR = os.path.join(USAGE_DIR, "logs")
STATE_DIR = os.path.expanduser(os.environ.get("AGENT_USAGE_STATE") or "~/.local/state/agent-usage")
# Files / env vars whose contents are treated as secrets and scrubbed from error text.
_KEY_FILES = [p for p in os.environ.get("AGENT_USAGE_KEY_FILES", "").split(os.pathsep) if p]
_SECRET_ENV = [v.strip() for v in os.environ.get(
    "AGENT_USAGE_SECRET_ENV",
    "OPENAI_API_KEY,ANTHROPIC_API_KEY,GEMINI_API_KEY,DEEPSEEK_API_KEY,MOONSHOT_API_KEY,KIMI_API_KEY,"
    "XAI_API_KEY,OPENROUTER_API_KEY,EXA_API_KEY,TAVILY_API_KEY,JINA_API_KEY,GITHUB_TOKEN,GH_TOKEN").split(",") if v.strip()]
_secrets = None


def _known_secrets():
    global _secrets
    if _secrets is None:
        _secrets = []
        for p in _KEY_FILES:
            try:
                with open(os.path.expanduser(p)) as f:
                    k = f.read().strip()
                if len(k) >= 8:
                    _secrets.append(k)
            except OSError:
                pass
        for v in _SECRET_ENV:
            if len(os.environ.get(v, "")) >= 8:
                _secrets.append(os.environ[v])
    return _secrets


def redact(s, limit=600):
    s = "" if s is None else str(s)
    for k in _known_secrets():
        s = s.replace(k, "***")
    s = re.sub(r"(?i)(bearer\s+)\S+", r"\1***", s)
    s = re.sub(r"\b(sk-|ak-|pk-)?[A-Za-z0-9_\-]{32,}\b", "***", s)
    return s[:limit]


def script_name():
    try:
        return os.path.basename(sys.argv[0]) or "python"
    except Exception:
        return "python"


def bot_name(bot=None):
    return bot or os.environ.get("AGENT_NAME") or ("unknown:" + script_name())


def _num(x):
    try:
        return int(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def usage_from(obj):
    """Extract (tokens_in, tokens_out, extra) from OpenAI chat / Responses / Gemini usage shapes.
    Accepts a full response, a stream event, or a bare usage dict. Returns (None, None, {}) if absent."""
    if not isinstance(obj, dict):
        return None, None, {}
    u = obj.get("usage") or obj.get("usageMetadata")
    if u is None and isinstance(obj.get("response"), dict):  # responses stream: response.completed
        u = obj["response"].get("usage")
    if u is None and any(k in obj for k in ("prompt_tokens", "input_tokens", "promptTokenCount")):
        u = obj
    if not isinstance(u, dict):
        return None, None, {}
    extra = {}
    if "promptTokenCount" in u or "candidatesTokenCount" in u:  # Gemini
        ti = _num(u.get("promptTokenCount"))
        to = (_num(u.get("candidatesTokenCount")) or 0) + (_num(u.get("thoughtsTokenCount")) or 0)
        if u.get("thoughtsTokenCount"):
            extra["reasoning_tokens"] = _num(u["thoughtsTokenCount"])
        if u.get("cachedContentTokenCount"):
            extra["cached_tokens"] = _num(u["cachedContentTokenCount"])
        return ti, to, extra
    ti = _num(u.get("input_tokens", u.get("prompt_tokens")))
    to = _num(u.get("output_tokens", u.get("completion_tokens")))
    det = u.get("output_tokens_details") or u.get("completion_tokens_details") or {}
    if isinstance(det, dict) and det.get("reasoning_tokens"):
        extra["reasoning_tokens"] = _num(det["reasoning_tokens"])
    idet = u.get("input_tokens_details") or u.get("prompt_tokens_details") or {}
    if isinstance(idet, dict) and idet.get("cached_tokens"):
        extra["cached_tokens"] = _num(idet["cached_tokens"])
    if ti is None and to is None and u.get("total_tokens") is not None:
        extra["total_tokens"] = _num(u["total_tokens"])
    return ti, to, extra


PROVIDERS = [("openai.com", "openai"), ("anthropic", "anthropic"), ("deepseek", "deepseek"), ("moonshot", "moonshot"),
             ("kimi.com", "moonshot"), ("x.ai", "xai"), ("openrouter", "openrouter"), ("googleapis", "google"),
             ("exa.ai", "exa"), ("tavily", "tavily"), ("jina.ai", "jina"), ("github.com", "github"), ("baidu", "baidu")]


def provider_from_url(url):
    h = (re.sub(r"^\w+://", "", url or "").split("/")[0]).split(":")[0].lower()
    for needle, name in PROVIDERS:
        if needle in h:
            return name
    return h or "unknown"


def http_error_body(e, n=600):
    """Read an urllib HTTPError body once, and make e.read() return it again for the caller."""
    try:
        data = e.read()
    except Exception:
        return ""
    try:
        e.read = lambda *a, _d=data: _d if not a or a[0] is None or a[0] < 0 else _d[:a[0]]
    except Exception:
        pass
    return data[:n].decode("utf-8", "replace") if isinstance(data, (bytes, bytearray)) else str(data)[:n]


def log_call(provider, model=None, bot=None, tokens_in=None, tokens_out=None, cost_hint=None, ok=True,
             status=None, error=None, latency=None, endpoint=None, script=None, ts=None, **extra):
    """Append one call record. Returns the record (or None if disabled/failed). Never raises.

    cost_hint: optional dict such as {"usd": 0.002} or {"pages": 3}; summed by report.py."""
    try:
        if os.environ.get("AGENT_USAGE_LOG", "1") == "0":
            return None
        now = ts.astimezone() if isinstance(ts, datetime) else datetime.now().astimezone()
        rec = {"ts": now.isoformat(timespec="seconds"), "id": uuid.uuid4().hex[:16],
               "bot": bot_name(bot), "script": script or script_name(), "provider": provider or "unknown",
               "model": model, "endpoint": endpoint, "ok": bool(ok), "status": status,
               "tokens_in": _num(tokens_in), "tokens_out": _num(tokens_out),
               "cost_hint": cost_hint, "latency_s": round(float(latency), 2) if latency is not None else None,
               "error": redact(error) if error else None, "pid": os.getpid()}
        for k, v in extra.items():
            if v is not None:
                rec[k] = v
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, now.strftime("%Y-%m-%d") + ".jsonl")
        line = json.dumps(rec, ensure_ascii=False, default=str) + "\n"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.write(fd, line.encode("utf-8"))
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        return rec
    except Exception as e:  # accounting must never break the caller
        try:
            print(f"[usage_log] warning: {type(e).__name__}", file=sys.stderr)
        except Exception:
            pass
        return None


class Tracker:
    """Accumulate usage from stream events, then log once:
        tr = Tracker("openai", model, endpoint="/v1/responses"); tr.feed(ev) ...; tr.done(ok=..., status=...)"""

    def __init__(self, provider, model=None, endpoint=None, bot=None, **extra):
        self.provider, self.model, self.endpoint, self.bot, self.extra = provider, model, endpoint, bot, extra
        self.t0 = time.time()
        self.tin = self.tout = None
        self.uextra = {}
        self.stream_error = None
        self.logged = False
        self.resp_model = None

    def feed(self, ev):
        try:
            if not isinstance(ev, dict):
                return
            ti, to, ex = usage_from(ev)
            if ti is not None or to is not None:  # last usage wins (stream totals are cumulative)
                self.tin, self.tout, self.uextra = ti, to, ex
            t = ev.get("type", "")
            if t in ("response.failed", "error") or (ev.get("error") and not t.startswith("response.output")):
                self.stream_error = json.dumps(ev.get("error") or ev.get("response", {}).get("error") or ev,
                                               ensure_ascii=False, default=str)[:500]
            r = ev.get("response") if isinstance(ev.get("response"), dict) else ev
            m = r.get("model") or r.get("modelVersion")
            if isinstance(m, str):
                self.resp_model = m
        except Exception:
            pass

    def done(self, ok=True, status=None, error=None, **extra):
        if self.logged:
            return None
        self.logged = True
        if self.stream_error and ok:
            ok, error = False, error or ("stream error: " + self.stream_error)
        ex = dict(self.extra)
        ex.update(self.uextra)
        ex.update(extra)
        if self.resp_model and self.resp_model != self.model:
            ex["resp_model"] = self.resp_model
        return log_call(self.provider, self.model, self.bot, self.tin, self.tout, ex.pop("cost_hint", None), ok,
                        status, error, time.time() - self.t0, self.endpoint, **ex)
