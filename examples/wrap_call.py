"""Example: account a non-streamed OpenAI-compatible call (stdlib only)."""
import json
import os
import time
import urllib.request

from usage_log import log_call, usage_from

base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
body = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}
req = urllib.request.Request(base + "/chat/completions", data=json.dumps(body).encode(),
                             headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"],
                                      "Content-Type": "application/json"})
t0 = time.time()
try:
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.load(r)
    ti, to, ex = usage_from(d)
    log_call("openai", body["model"], tokens_in=ti, tokens_out=to, ok=True, status=200,
             latency=time.time() - t0, endpoint="/chat/completions", **ex)
except Exception as e:
    log_call("openai", body["model"], ok=False, status=getattr(e, "code", None), error=repr(e),
             latency=time.time() - t0, endpoint="/chat/completions")
    raise
