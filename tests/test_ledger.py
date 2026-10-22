import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE_KEY = "fake-key-for-tests-0123456789"


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = dict(os.environ, AGENT_USAGE_DIR=os.path.join(self.tmp.name, "u"),
                        AGENT_USAGE_STATE=os.path.join(self.tmp.name, "s"), CODEX_HOME=os.path.join(self.tmp.name, "codex"),
                        AGENT_NAME="tester", OPENAI_API_KEY=FAKE_KEY, PYTHONPATH=ROOT)

    def tearDown(self):
        self.tmp.cleanup()

    def py(self, code):
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def run_report(self, *args):
        return subprocess.run([sys.executable, os.path.join(ROOT, "report.py"), *args], capture_output=True, text=True,
                              env=self.env, timeout=60)

    def records(self):
        d = os.path.join(self.tmp.name, "u", "logs")
        out = []
        for fn in sorted(os.listdir(d)):
            with open(os.path.join(d, fn)) as f:
                out += [json.loads(x) for x in f]
        return out

    def test_log_and_redact(self):
        self.py(f"""
import usage_log as U
U.log_call("openai", "m1", tokens_in=10, tokens_out=5, ok=True, status=200, latency=1.234, cost_hint={{"usd": 0.01}})
U.log_call("openai", "m1", ok=False, status=401, error="bad key {FAKE_KEY} Bearer abc.def")
tr = U.Tracker("google", "g1", "/v1beta")
tr.feed({{"usageMetadata": {{"promptTokenCount": 7, "candidatesTokenCount": 2, "thoughtsTokenCount": 3}}, "modelVersion": "g1-002"}})
tr.done(ok=True, status=200)
""")
        recs = self.records()
        self.assertEqual(len(recs), 3)
        self.assertEqual(recs[0]["bot"], "tester")
        self.assertEqual((recs[0]["tokens_in"], recs[0]["tokens_out"]), (10, 5))
        self.assertNotIn(FAKE_KEY, json.dumps(recs))
        self.assertNotIn("abc.def", json.dumps(recs))
        self.assertEqual((recs[2]["tokens_in"], recs[2]["tokens_out"], recs[2]["reasoning_tokens"]), (7, 5, 3))
        self.assertEqual(recs[2]["resp_model"], "g1-002")

    def test_usage_shapes(self):
        out = self.py("""
import usage_log as U, json
print(json.dumps([U.usage_from({"usage": {"prompt_tokens": 1, "completion_tokens": 2, "completion_tokens_details": {"reasoning_tokens": 1}}}),
                  U.usage_from({"type": "response.completed", "response": {"usage": {"input_tokens": 3, "output_tokens": 4,
                                "input_tokens_details": {"cached_tokens": 2}}}}),
                  U.usage_from("nope"), U.provider_from_url("https://api.openai.com/v1/x"), U.provider_from_url("http://localhost:8080/v1")]))
""")
        self.assertEqual(json.loads(out), [[1, 2, {"reasoning_tokens": 1}], [3, 4, {"cached_tokens": 2}], [None, None, {}],
                                           "openai", "localhost"])

    def test_disabled(self):
        self.env["AGENT_USAGE_LOG"] = "0"
        self.py("import usage_log as U; assert U.log_call('x') is None")
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "u", "logs")))

    def test_report_and_errors(self):
        self.py("""
import usage_log as U
for i in range(3): U.log_call("openai", "m1", tokens_in=100, tokens_out=10, latency=2)
U.log_call("deepseek", "m2", ok=False, status=500, error="boom", bot="other")
""")
        r = self.run_report("--no-codex")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("4 call(s)", r.stdout)
        self.assertIn("300", r.stdout)
        j = json.loads(self.run_report("--no-codex", "--json", "--by", "provider").stdout)
        self.assertEqual({x["provider"]: x["calls"] for x in j}, {"openai": 3, "deepseek": 1})
        e1 = self.run_report("--errors-since", "10").stdout
        self.assertIn("1 new API error", e1)
        self.assertEqual(self.run_report("--errors-since", "10").stdout, "")  # deduped

    def test_codex_import(self):
        d = os.path.join(self.tmp.name, "codex", "sessions", "2026")
        os.makedirs(d)
        lines = [{"type": "session_meta", "payload": {"id": "s1", "cwd": "/src/proj", "originator": "codex_exec"}},
                 {"type": "turn_context", "payload": {"model": "codex-model"}},
                 {"type": "event_msg", "timestamp": "2026-10-20T01:00:00Z",
                  "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 50, "output_tokens": 5}}}}]
        with open(os.path.join(d, "a.jsonl"), "w") as f:
            f.write("\n".join(json.dumps(x) for x in lines) + "\n")
        self.py("import codex_import as C; assert C.run() == 1; assert C.run() == 0")
        rec = self.records()[0]
        self.assertEqual((rec["provider"], rec["bot"], rec["model"], rec["tokens_in"]), ("codex", "codex@proj", "codex-model", 50))


if __name__ == "__main__":
    unittest.main()
