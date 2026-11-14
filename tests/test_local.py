"""Local models, against a stand-in server.

The stand-in answers the way Ollama's documentation describes its own API (/api/tags, /api/chat)
and the way OpenAI-style servers answer (/v1/models, /v1/chat/completions, as LM Studio and
llama.cpp do). Nothing in this file proves that a real Ollama, LM Studio or llama.cpp behaves
the same way, or says anything about how well a local model translates.
"""
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault("SUBTLE_HOME", tempfile.mkdtemp(prefix="subtle-local-"))

from subtle import config, formats  # noqa: E402
from subtle.engine import Options, Translator, proof_size, proofread, request_size  # noqa: E402
from subtle.providers import (DEFAULT_MODEL, LOCAL, Cancelled, LocalProvider, MockProvider,  # noqa: E402
                              ProviderError, Router)

SAMPLE = (Path(__file__).parent.parent / "samples" / "lighthouse.en.srt").read_bytes()
MODEL = LOCAL + "gemma3:12b"


def stamp(ms):
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def long_srt(n=300):
    out, t = [], 1000
    for i in range(n):
        out.append(f"{i + 1}\n{stamp(t)} --> {stamp(t + 2000)}\nWe should check the north gate before the tide. No. {i}.\n")
        t += 2300
    return "\n".join(out)


class FakeModels(BaseHTTPRequestHandler):
    """A model server. It "translates" like the mock provider does, a few characters at a time."""

    kind = "ollama"  # or "openai"
    names = ["gemma3:12b", "qwen3:8b"]
    seen: list = []  # every chat request it got
    refuse: tuple = ()  # request fields it answers with a 400
    think = False  # put a reasoning block in front of each reply
    hold = None  # an Event to wait for before answering

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        cls = type(self)
        if self.path == "/api/tags" and cls.kind == "ollama":
            return self._json(200, {"models": [{"name": n, "model": n} for n in cls.names]})
        if self.path == "/v1/models" and cls.kind == "openai":
            return self._json(200, {"object": "list", "data": [{"id": n, "object": "model"} for n in cls.names]})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        cls = type(self)
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        cls.seen.append({"path": self.path, "auth": self.headers.get("Authorization"), **body})
        bad = [f for f in cls.refuse if f in body]
        if bad:
            return self._json(400, {"error": {"message": f"unknown field {bad[0]}"}})
        if body["model"] not in cls.names:
            return self._json(404, {"error": f"model '{body['model']}' not found"})
        if cls.hold:
            cls.hold.wait(10)
        system, user = (m["content"] for m in body["messages"])
        text = MockProvider().complete("", system, user).text
        if cls.think:
            text = "<think>\nThe first line could be:\n1|not this\n</think>\n\n" + text
        pieces = [text[i:i + 7] for i in range(0, len(text), 7)]
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson" if self.path == "/api/chat" else "text/event-stream")
        self.end_headers()
        try:
            if self.path == "/api/chat":
                for p in pieces:
                    self.wfile.write(json.dumps({"message": {"role": "assistant", "content": p}, "done": False}).encode() + b"\n")
                self.wfile.write(json.dumps({"message": {"role": "assistant", "content": ""}, "done": True,
                                             "done_reason": "stop", "prompt_eval_count": 111, "eval_count": 222}).encode() + b"\n")
            else:
                for p in pieces:
                    self.wfile.write(b"data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": p}}]}).encode() + b"\n\n")
                self.wfile.write(b"data: " + json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}).encode() + b"\n\n")
                if "stream_options" in body:
                    usage = {"prompt_tokens": 111, "completion_tokens": 222}
                    self.wfile.write(b"data: " + json.dumps({"choices": [], "usage": usage}).encode() + b"\n\n")
                self.wfile.write(b"data: [DONE]\n\n")
        except OSError:
            pass  # the client hung up, as it does when a job is stopped


def start(handler):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


class Local(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd, cls.url = start(FakeModels)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        FakeModels.kind, FakeModels.refuse, FakeModels.think, FakeModels.hold = "ollama", (), False, None
        FakeModels.seen = []

    def opts(self, **over):
        return Options(**{"target_code": "fi", "target_name": "Finnish", "model": MODEL, **over})

    def router(self, ctx=4096, key=""):
        return Router(None, {"url": self.url, "key": key, "ctx": ctx})

    def assert_translated(self, doc):
        """Every line came out as the mock provider would have written it."""
        ref = formats.parse("lighthouse.en.srt", SAMPLE)
        Translator(ref, Options(target_code="fi", target_name="Finnish"), MockProvider()).run()
        self.assertEqual([c.translation for c in doc.cues], [c.translation for c in ref.cues])
        self.assertFalse([c.flags for c in doc.cues if c.flags])

    def test_ollama_is_spoken_to_through_its_own_api(self):
        self.assertEqual(LocalProvider(self.url).describe(), {"kind": "ollama", "models": ["gemma3:12b", "qwen3:8b"]})
        doc, router, shown = formats.parse("lighthouse.en.srt", SAMPLE), self.router(ctx=8192), []
        stats = Translator(doc, self.opts(local_ctx=8192), router, emit=shown.append).run()
        self.assert_translated(doc)
        self.assertTrue(any(e["type"] == "cue" for e in shown))  # lines appear while the reply streams
        request = FakeModels.seen[0]
        self.assertEqual((request["path"], request["model"], request["stream"]), ("/api/chat", "gemma3:12b", True))
        # The request says how much context it needs (Ollama's default would cut a long prompt
        # short) and that it wants no reasoning.
        self.assertEqual((request["options"]["num_ctx"], request["options"]["num_predict"], request["think"]),
                         (8192, 4096, False))
        self.assertEqual((stats.requests, stats.inp, stats.out, stats.cost), (1, 111, 222, 0.0))
        self.assertIsNone(router._claude)  # no Claude client was made: a local model needs no API key

    def test_a_model_that_cannot_take_the_think_switch_still_works(self):
        FakeModels.refuse = ("think",)
        router = self.router()
        for _ in range(2):
            doc = formats.parse("lighthouse.en.srt", SAMPLE)
            Translator(doc, self.opts(), router).run()
            self.assert_translated(doc)
        # refused once, sent again without it, and not offered the second time
        self.assertEqual(["think" in r for r in FakeModels.seen], [True, False, False])

    def test_other_servers_are_spoken_to_the_openai_way(self):
        FakeModels.kind, FakeModels.think = "openai", True
        for url in (self.url, self.url + "/v1", self.url + "/v1/chat/completions"):
            self.assertEqual(LocalProvider(url).describe()["kind"], "openai")
        doc = formats.parse("lighthouse.en.srt", SAMPLE)
        stats = Translator(doc, self.opts(), self.router(key="sk-local")).run()
        self.assert_translated(doc)  # the reasoning block, with its line that looks like an answer, is left out
        request = FakeModels.seen[0]
        self.assertEqual((request["path"], request["auth"], request["max_tokens"]),
                         ("/v1/chat/completions", "Bearer sk-local", 2048))
        self.assertEqual((stats.inp, stats.out, stats.cost), (111, 222, 0.0))

        FakeModels.seen, FakeModels.refuse = [], ("stream_options",)
        doc = formats.parse("lighthouse.en.srt", SAMPLE)
        stats = Translator(doc, self.opts(), self.router()).run()
        self.assert_translated(doc)
        self.assertEqual(len(FakeModels.seen), 2)  # one refusal, one go without the optional field
        self.assertGreater(stats.inp, 0)  # the server gave no token counts, so they are estimated

    def test_requests_are_sized_to_fit_the_context(self):
        self.assertEqual(request_size(Options(chunk_lines=120)), (120, 9000))  # Claude: as the settings say
        self.assertEqual(request_size(self.opts(local_ctx=4096)), (63, 2716))
        self.assertEqual(request_size(self.opts(local_ctx=32768)), (120, 9000))
        self.assertEqual(proof_size(self.opts(), DEFAULT_MODEL)[0], 2000)  # Claude reads a film in one go
        self.assertEqual(proof_size(self.opts(local_ctx=4096), MODEL)[0], 39)

        doc = formats.parse("long.srt", long_srt(300).encode())
        Translator(doc, self.opts(local_ctx=4096), self.router()).run()
        self.assertTrue(all(c.translation for c in doc.cues))
        sizes = [r["messages"][1]["content"].rsplit("Translate:\n", 1)[-1].count("\n") + 1 for r in FakeModels.seen]
        self.assertGreaterEqual(len(sizes), 5)
        self.assertEqual(sum(sizes), 300)

        def fits(request, both=False):
            """Prompt plus the longest likely reply, at three characters a token in and two out."""
            asked = sum(len(m["content"]) for m in request["messages"])
            lines = request["messages"][1]["content"].rsplit(":\n", 1)[-1]
            reply = len(lines) * (0.55 if both else 1.1)  # proofreading sends both languages, returns one
            return asked / 3 + reply / 2 < 4096 and reply / 2 < request["options"]["num_predict"]

        self.assertTrue(all(fits(r) for r in FakeModels.seen))

        FakeModels.seen = []
        stats, summary = proofread(doc, self.opts(local_ctx=4096), self.router(), MODEL)
        self.assertEqual((summary["model"], summary["lines"], stats.cost), (MODEL, 300, 0.0))
        self.assertGreaterEqual(len(FakeModels.seen), 5)  # read a stretch at a time, not whole
        self.assertTrue(all(fits(r, both=True) for r in FakeModels.seen))  # even if every line came back rewritten
        self.assertGreater(len(summary["changed"]), 0)

    def test_stop_reaches_a_model_that_has_not_answered_yet(self):
        FakeModels.hold = threading.Event()
        stop = {"now": False}
        threading.Timer(0.3, stop.update, [{"now": True}]).start()
        started = time.time()
        try:
            with self.assertRaises(Cancelled):
                LocalProvider(self.url).complete("gemma3:12b", "system", "Translate:\n1|Hello.", cancel=lambda: stop["now"])
        finally:
            FakeModels.hold.set()
        self.assertLess(time.time() - started, 5)

    def test_errors_say_what_is_wrong(self):
        with self.assertRaises(ProviderError) as ctx:
            LocalProvider(self.url).complete("no-such-model", "system", "Translate:\n1|Hello.")
        self.assertEqual(ctx.exception.kind, "model")
        self.assertIn("no model called no-such-model", str(ctx.exception))

        with socket.socket() as s:  # a port nothing listens on
            s.bind(("127.0.0.1", 0))
            dead = f"http://127.0.0.1:{s.getsockname()[1]}"
        for call in (LocalProvider(dead).describe, lambda: LocalProvider(dead).complete("x", "s", "u")):
            with self.assertRaises(ProviderError) as ctx:
                call()
            self.assertEqual(ctx.exception.kind, "network")
            self.assertIn("Is it running?", str(ctx.exception))

        with self.assertRaises(ProviderError) as ctx:
            Router(None, None).complete(MODEL, "system", "user")
        self.assertIn("No local model server is set up", str(ctx.exception))


class LocalInTheApp(unittest.TestCase):
    """The settings, the model list and a whole job, with nothing but a local model."""

    @classmethod
    def setUpClass(cls):
        cls.httpd, cls.url = start(FakeModels)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        FakeModels.kind, FakeModels.refuse, FakeModels.think, FakeModels.hold = "ollama", (), False, None
        FakeModels.seen = []
        self.home = os.environ.get("SUBTLE_HOME")
        os.environ["SUBTLE_HOME"] = tempfile.mkdtemp(prefix="subtle-local-app-")
        self.mock = os.environ.pop("SUBTLE_MOCK", None)

    def tearDown(self):
        os.environ["SUBTLE_HOME"] = self.home
        if self.mock is not None:
            os.environ["SUBTLE_MOCK"] = self.mock

    def test_a_job_runs_on_a_local_model_alone(self):
        from subtle.server import Manager

        mgr = Manager(mock=False)
        self.assertEqual(mgr.local_test(self.url), {"kind": "ollama", "models": ["gemma3:12b", "qwen3:8b"]})
        self.assertEqual([m["id"] for m in mgr.state()["models"]][-1], "claude-opus-5-5")  # nothing local until it is set

        state = mgr.set_config({"target": "fi", "local_url": self.url, "local_model": "gemma3:12b",
                                "local_ctx": 8192, "local_key": "sk-local"})
        local = state["models"][-1]
        self.assertEqual((local["id"], local["label"], local["inp"], local["local"]), (MODEL, "gemma3:12b", 0.0, True))
        self.assertNotIn("local_key", state["config"])  # the key never goes back to the page
        self.assertTrue(state["local_key_set"])

        state = mgr.set_config({"model": MODEL})
        job = mgr.add("lighthouse.en.srt", SAMPLE)
        self.assertEqual(job.est["cost"], 0)
        mgr.start([job.id])
        for _ in range(400):
            if job.status in ("done", "error", "stopped"):
                break
            time.sleep(0.02)
        self.assertEqual((job.status, job.error), ("done", ""))
        self.assertEqual((job.used["model"], job.proofed["model"], job.spent["cost"]), (MODEL, MODEL, 0.0))
        self.assertTrue(all(c.translation for c in job.doc.cues))
        self.assertEqual({r["options"]["num_ctx"] for r in FakeModels.seen}, {8192})
        self.assertEqual(len(FakeModels.seen), 2)  # translated, then proofread, both by the local model

        # A Claude model can be asked to read what the local one wrote, and the other way round.
        self.assertIn(MODEL, mgr.detail(job)["proof_est"])
        with self.assertRaises(ValueError):
            mgr.proofread(job, "local:something-else")

        # Renaming the model takes the choice along; removing the server falls back to Claude.
        state = mgr.set_config({"local_model": "qwen3:8b", "proof_model": LOCAL + "qwen3:8b"})
        self.assertEqual((state["config"]["model"], state["config"]["proof_model"]), (LOCAL + "qwen3:8b",) * 2)
        state = mgr.set_config({"local_url": ""})
        self.assertEqual((state["config"]["model"], state["config"]["proof_model"]), (DEFAULT_MODEL, ""))
        self.assertFalse(state["local_key_set"])
        self.assertNotIn(True, [m.get("local", False) for m in state["models"]])

    def test_proofreading_is_on_unless_switched_off(self):
        self.assertTrue(config.load()["proofread"])
        # A settings file from before proofreading became the default says "off" only because
        # every key is written out. It is switched on once.
        (config.home() / "config.json").write_text(json.dumps({"proofread": False, "target_code": "fi"}), "utf-8")
        cfg = config.load()
        self.assertEqual((cfg["proofread"], cfg["target_code"]), (True, "fi"))
        cfg["proofread"] = False  # switched off after that: this is a choice, and it sticks
        config.save(cfg)
        self.assertFalse(config.load()["proofread"])

    def test_the_command_line_proofreads_unless_told_not_to(self):
        import contextlib
        import io

        from subtle.cli import main

        folder = Path(tempfile.mkdtemp(prefix="subtle-cli-"))
        (folder / "a.en.srt").write_bytes(SAMPLE)
        home = os.environ["SUBTLE_HOME"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["translate", str(folder / "a.en.srt"), "--to", "fi", "--mock", "--data", home]), 0)
        self.assertRegex(out.getvalue(), r"proofreading changed \d+")
        read = (folder / "a.fi.srt").read_text("utf-8")

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["translate", str(folder / "a.en.srt"), "--to", "fi", "--mock", "--force",
                                   "--no-proofread", "--data", home]), 0)
        self.assertNotIn("proofreading", out.getvalue())
        self.assertNotEqual((folder / "a.fi.srt").read_text("utf-8"), read)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(["estimate", str(folder / "a.en.srt"), "--to", "fi", "--data", home])
        self.assertIn("proofreading included", out.getvalue())


if __name__ == "__main__":
    unittest.main()
