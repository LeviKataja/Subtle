"""Offline tests: everything here runs against the mock provider, so no key and no cost.

    python -m unittest discover -s tests -v
"""
import json
import os
import re
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

os.environ["SUBTLE_HOME"] = tempfile.mkdtemp(prefix="subtle-test-")

from subtle import formats, qa  # noqa: E402
from subtle.cache import Cache  # noqa: E402
from subtle.chunker import make_chunks  # noqa: E402
from subtle.engine import BudgetExceeded, Options, Translator, estimate, retranslate, translate  # noqa: E402
from subtle.prep import prepare, wire_text  # noqa: E402
from subtle.prompt import clean_title, glossary_hits, system_prompt, user_message  # noqa: E402
from subtle.providers import Cancelled, MockProvider, pseudo  # noqa: E402
from subtle.wire import parse  # noqa: E402
from subtle.wrap import wrap  # noqa: E402

SAMPLE = (Path(__file__).parent.parent / "samples" / "lighthouse.en.srt").read_bytes()
FI = dict(target_code="fi", target_name="Finnish")


def stamp(ms):
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def long_srt(n=300):
    lines = ["We should check the north gate before the tide comes in.", "I never said the map was accurate.",
             "- Did you lock the workshop?\n- I thought you had the key.", "Seven.", "[door creaks]"]
    out, t = [], 1000
    for i in range(n):
        # numbered, so no two chunks are identical (identical chunks are served from the cache)
        out.append(f"{i + 1}\n{stamp(t)} --> {stamp(t + 2000)}\n{lines[i % len(lines)]} No. {i}.\n")
        t += 2000 + (9000 if i % 50 == 49 else 300)
    return "\n".join(out)


class Formats(unittest.TestCase):
    def test_srt_round_trip_keeps_every_timestamp(self):
        doc = formats.parse("a.srt", SAMPLE)
        self.assertEqual(len(doc.cues), 26)
        self.assertEqual(doc.cues[12].text, "1912.")  # a number-only line is text, not a counter
        self.assertEqual(doc.cues[3].text, "{\\an8}KEEPER'S LOG, DAY 412")
        translate(doc, Options(**FI), MockProvider())
        timing = re.compile(r"^\d\d:\d\d:\d\d,\d{3} --> .*$", re.M)
        self.assertEqual(timing.findall(formats.render(doc)), timing.findall(SAMPLE.decode()))

    def test_srt_survives_sloppy_files(self):
        text = "\ufeff1\r\n00:00:01,000 --> 00:00:02,000\r\nHello\r\n2\r\n00:00:03,000 --> 00:00:04,500 X1:10\r\nThere\r\n\r\n\r\n"
        doc = formats.parse("a.srt", text.encode())
        self.assertEqual([c.text for c in doc.cues], ["Hello", "There"])
        self.assertEqual((doc.cues[1].start, doc.cues[1].end, doc.cues[1].meta["tail"]), (3000, 4500, "X1:10"))

    def test_vtt_keeps_notes_ids_and_settings(self):
        text = ("WEBVTT - demo\n\nNOTE keep me\n\nintro\n00:01.000 --> 00:03.000 line:0 position:20%\n"
                "<v Ilse>Hello <c.loud>there</c>\n\n00:00:04.000 --> 00:00:05.000\nBye\n")
        doc = formats.parse("a.vtt", text.encode())
        self.assertEqual(len(doc.cues), 2)
        self.assertEqual(doc.cues[0].start, 1000)
        translate(doc, Options(**FI), MockProvider())
        out = formats.render(doc)
        self.assertTrue(out.startswith("WEBVTT - demo\n\nNOTE keep me\n\nintro\n00:00:01.000 --> 00:00:03.000 line:0 position:20%\n"))
        self.assertIn(pseudo("Hello there"), out)

    def test_ass_only_touches_the_text_field(self):
        text = ("[Script Info]\nTitle: x\n\n[Events]\n"
                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                "Comment: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,note\n"
                "Dialogue: 0,0:00:01.50,0:00:03.00,Default,,0,0,0,,{\\an8}Hello, world\\Nand you\n")
        doc = formats.parse("a.ass", text.encode())
        self.assertEqual(len(doc.cues), 1)
        self.assertEqual((doc.cues[0].start, doc.cues[0].text), (1500, "{\\an8}Hello, world\nand you"))
        translate(doc, Options(**FI), MockProvider())
        out = formats.render(doc).split("\n")
        self.assertEqual(out[5], "Comment: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,note")
        self.assertEqual(out[6], "Dialogue: 0,0:00:01.50,0:00:03.00,Default,,0,0,0,,{\\an8}" + pseudo("Hello, world and you"))

    def test_plain_text_keeps_blank_lines_and_indent(self):
        doc = formats.parse("a.txt", b"Title\n\n  Indented line.\n42\n")
        translate(doc, Options(**FI), MockProvider())
        self.assertEqual(formats.render(doc), f"{pseudo('Title')}\n\n  {pseudo('Indented line.')}\n42\n")

    def test_output_name(self):
        self.assertEqual(formats.output_name("Show.S01E01.en.sdh.srt", "fi"), "Show.S01E01.fi.srt")
        self.assertEqual(formats.output_name("movie.srt", "pt-BR"), "movie.pt-BR.srt")
        self.assertEqual(clean_title("Harbour.Town.S01E03.1080p.WEB.x264-GRP.en.srt"), "Harbour Town S01E03")


class Prep(unittest.TestCase):
    def units(self, text, **kw):
        doc = formats.parse("a.srt", f"1\n00:00:01,000 --> 00:00:03,000\n{text}\n".encode())
        return prepare(doc, **kw)

    def test_speakers_become_separate_lines(self):
        units, plans = self.units("- You're late.\n- The ferry was late.")
        self.assertEqual([wire_text(u) for u in units], ["- You're late.", "- The ferry was late."])

    def test_wrapped_sentence_is_one_line_and_italics_never_reach_the_model(self):
        units, plans = self.units("<i>The lamp went out,\nand nobody noticed.</i>")
        self.assertEqual([u.src for u in units], ["The lamp went out, and nobody noticed."])
        self.assertEqual(plans[0].wraps, [("<i>", "</i>")])

    def test_nothing_to_translate_costs_nothing(self):
        for text in ("♪♪", "1912.", "...", "- ?"):
            units, _ = self.units(text)
            self.assertEqual([u.kind for u in units], ["keep"], text)

    def test_sdh_strip(self):
        units, plans = self.units("ILSE: (SIGHS) Did you check [clears throat] the line?", sdh="strip")
        self.assertEqual([u.src for u in units], ["Did you check the line?"])
        units, plans = self.units("[wind howling]", sdh="strip")
        self.assertTrue(plans[0].drop and not units)

    def test_lyrics_are_left_alone_unless_asked(self):
        self.assertEqual(self.units("♪ la la la ♪")[0][0].kind, "keep")
        self.assertEqual(self.units("♪ la la la ♪", lyrics="translate")[0][0].kind, "text")


class Wire(unittest.TestCase):
    def test_parser_ignores_everything_but_numbered_lines(self):
        reply = "Sure! Here you go:\n```\n1|yksi\n 2 | kaksi\n2|again\n9|out of range\n3｜kolme\n```\nHope that helps."
        self.assertEqual(parse(reply, 4), {0: "yksi", 1: "kaksi", 2: "kolme"})

    def test_a_cut_off_reply_loses_only_its_unfinished_last_line(self):
        self.assertEqual(parse("1|yksi\n2|kaksi\n3|kol", 4, cut=True), {0: "yksi", 1: "kaksi"})
        self.assertEqual(parse("1|yksi\n2|kaksi\n", 4, cut=True), {0: "yksi", 1: "kaksi"})  # ended on a line break
        self.assertEqual(parse("1|yksi\n2|kaksi\n3|kol", 4), {0: "yksi", 1: "kaksi", 2: "kol"})

    def test_prompt_is_small_and_glossary_is_filtered(self):
        self.assertLess(len(system_prompt("Finnish", title="Some Show S01E02")), 1500)
        hits = glossary_hits([["North Rock", "Pohjoiskari"], ["Marta", "Martta"], ["rock", "kivi"]],
                             ["north rock here.", "Rocky road"])
        self.assertEqual(hits, [("North Rock", "Pohjoiskari"), ("rock", "kivi")])
        self.assertTrue(user_message(["a", "b"]).endswith("Translate:\n1|a\n2|b"))


class Wrap(unittest.TestCase):
    def test_short_stays_on_one_line(self):
        self.assertEqual(wrap("Kotona.", 42), ["Kotona."])

    def test_two_balanced_lines_within_the_limit(self):
        lines = wrap("We have three left. Three bulbs between us and forty fishing boats.", 42)
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(len(l) <= 42 for l in lines))
        self.assertEqual(lines[0], "We have three left. Three bulbs")

    def test_very_long_text_never_loses_words(self):
        text = "word " * 40
        self.assertEqual(" ".join(wrap(text, 42)), text.strip())
        self.assertEqual(wrap("这是一个没有空格的很长的句子，需要被分成两行才能显示。", 42)[0][-1], "，")


class Chunks(unittest.TestCase):
    def test_boundaries_fall_on_scene_changes(self):
        doc = formats.parse("a.srt", long_srt(300).encode())
        units, _ = prepare(doc)
        ids = [i for i, u in enumerate(units) if u.kind == "text"]
        chunks = make_chunks(ids, units, doc, target=120)
        self.assertEqual([i for c in chunks for i in c], ids)
        for chunk in chunks[:-1]:
            last = doc.cues[units[chunk[-1]].cue]
            nxt = doc.cues[units[chunk[-1]].cue + 1]
            self.assertGreaterEqual(nxt.start - last.end, 9000)  # the long pauses in the fixture


class Engine(unittest.TestCase):
    def doc(self):
        return formats.parse("lighthouse.en.srt", SAMPLE)

    def test_one_request_for_a_short_file(self):
        doc, p = self.doc(), MockProvider(chatter=True)
        stats = translate(doc, Options(**FI), p)
        self.assertEqual((p.calls, stats.failed, stats.units), (1, 0, 27))
        self.assertNotIn("-->", p.requests[0])  # timing never leaves the machine
        self.assertEqual(doc.cues[2].translation, f"- {pseudo('You')}'{pseudo('re late again, Marta.')}\n- "
                         + pseudo("The ferry was late. I wasn't."))
        self.assertEqual(doc.cues[8].translation, "♪♪")

    def test_skipped_lines_are_repaired_with_small_requests(self):
        doc, p = self.doc(), MockProvider(drop=(5, 6, 20))
        stats = translate(doc, Options(**FI), p)
        self.assertEqual((stats.failed, stats.repaired, p.calls), (0, 3, 3))
        self.assertLess(len(p.requests[1]), len(p.requests[0]) / 2)
        self.assertTrue(all(c.translation for c in doc.cues))

    def test_a_reply_cut_off_mid_line_does_not_keep_the_half_line(self):
        clean, whole = self.doc(), MockProvider()
        translate(clean, Options(**FI), whole)
        last = whole.requests[0].rsplit("Translate:\n", 1)[-1].count("\n") + 1
        for cut in (20, last):  # in the middle, and inside the very last line of the request
            with self.subTest(cut=cut):
                doc, p = self.doc(), MockProvider(cut=cut)
                stats = translate(doc, Options(**FI), p)
                self.assertEqual([c.translation for c in doc.cues], [c.translation for c in clean.cues])
                self.assertEqual((stats.failed, p.calls), (0, 2))  # one small request for what was lost
                self.assertLess(len(p.requests[1]), len(p.requests[0]))

    def test_a_refused_line_is_isolated_and_flagged(self):
        doc, p = self.doc(), MockProvider(refuse="every flare we own")
        stats = translate(doc, Options(**FI), p)
        refused = [i for i, c in enumerate(doc.cues) if "refused" in c.flags]
        self.assertTrue(20 in refused and len(refused) <= 3, refused)
        self.assertEqual(doc.cues[20].translation, doc.cues[20].text)  # the source stands in
        self.assertEqual(sum(1 for c in doc.cues if c.translation and c.translation != c.text and c.text.strip()),
                         sum(1 for i, c in enumerate(doc.cues) if i not in refused and c.text not in ("♪♪", "1912.")))
        self.assertLess(p.calls, 12)

    def test_budget_cap_stops_the_run_and_keeps_what_was_done(self):
        doc = formats.parse("long.srt", long_srt(300).encode())
        with self.assertRaises(BudgetExceeded):
            translate(doc, Options(**FI, budget=0.0005), MockProvider())
        done = sum(1 for c in doc.cues if c.translation is not None)
        self.assertTrue(0 < done < len(doc.cues), done)

    def test_cancel_keeps_partial_work_and_a_rerun_resumes_from_cache(self):
        cache = Cache(Path(tempfile.mkdtemp()) / "c.db")
        doc = formats.parse("long.srt", long_srt(300).encode())
        calls = {"n": 0}

        def cancel():
            calls["n"] += 1
            return calls["n"] > 150

        with self.assertRaises(Cancelled):
            translate(doc, Options(**FI), MockProvider(), cache, cancel=cancel)
        self.assertTrue(any(c.translation for c in doc.cues) and any(c.translation is None for c in doc.cues))
        doc2, p2 = formats.parse("long.srt", long_srt(300).encode()), MockProvider()
        stats = translate(doc2, Options(**FI), p2, cache)
        self.assertGreater(stats.cached, 0)
        self.assertEqual(stats.cached + sum(len(parse(r.rsplit("Translate:\n", 1)[-1], 999)) for r in p2.requests), stats.units)
        doc3, p3 = formats.parse("long.srt", long_srt(300).encode()), MockProvider()
        self.assertEqual((translate(doc3, Options(**FI), p3, cache).cached, p3.calls), (stats.units, 0))

    def test_estimate_is_in_the_right_ballpark(self):
        doc = formats.parse("long.srt", long_srt(300).encode())
        est = estimate(doc, Options(**FI))
        stats = translate(doc, Options(**FI), MockProvider())
        self.assertLess(abs(est["inp"] - stats.inp) / stats.inp, 0.5)
        self.assertLess(est["cost"], 0.01)  # a few hundred lines on the default model is well under a cent

    def test_qa_flags(self):
        doc = self.doc()
        translate(doc, Options(**FI), MockProvider())
        self.assertEqual([c.flags for c in doc.cues if c.flags], [])
        cue = doc.cues[18]  # "How long overdue?", 1.4 s
        cue.translation = "Kuinka kauan se on ollut myöhässä ja miksi kukaan ei kertonut minulle siitä mitään?"
        self.assertEqual(qa.check(cue, "fi"), ["long", "fast"])
        cue.translation = cue.text
        self.assertEqual(qa.check(cue, "fi"), ["untranslated"])
        cue.translation = "I think that they would have known about this"
        self.assertEqual(qa.check(cue, "fi"), ["english", "long", "fast"])
        long_cue = doc.cues[23]  # a 79-character source line
        long_cue.translation = "Ei."
        self.assertEqual(qa.check(long_cue, "fi"), ["ratio"])

    def test_retranslate_touches_only_the_chosen_cues(self):
        doc, p = self.doc(), MockProvider()
        translate(doc, Options(**FI), p)
        before = [c.translation for c in doc.cues]
        doc.cues[18].translation = "x"
        stats = retranslate(doc, [18], Options(**FI), p, instruction="more casual", condense=True)
        self.assertEqual([c.translation for c in doc.cues], before)
        self.assertEqual(stats.requests, 1)
        self.assertIn("more casual", p.requests[-1])
        self.assertIn("character limits: 1: ", p.requests[-1])
        self.assertTrue(p.requests[-1].endswith("Translate:\n1|How long overdue?"))


class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        from subtle.server import Handler, Manager

        os.environ["SUBTLE_HOME"] = tempfile.mkdtemp(prefix="subtle-test-")  # its own settings and queue
        cls.mgr = Handler.mgr = Manager(mock=True)
        cls.mgr._provider, cls.mgr._provider_for = MockProvider(), (True, "none", None)
        Handler.open_host, Handler.password = False, ""
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def call(self, path, body=None, method=None, headers=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"X-Subtle": "1", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                payload = r.read()
                return r.status, (json.loads(payload) if "json" in r.headers["Content-Type"] else payload.decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def wait(self, job_id, until=("done", "error", "stopped")):
        for _ in range(200):
            _, job = self.call(f"/api/jobs/{job_id}")
            if job["status"] in until and not job["busy"]:
                return job
            time.sleep(0.05)
        self.fail("job did not finish")

    def test_requests_from_other_pages_are_refused(self):
        req = urllib.request.Request(self.base + "/api/start", data=b"{}", method="POST")  # no X-Subtle header
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 403)
        self.assertEqual(self.call("/api/state", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.call("/api/state", headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.call("/api/state")[0], 200)
        self.assertEqual(self.call("/../config.json")[0], 404)

    def test_full_run_edit_fix_and_download(self):
        self.assertEqual(self.call("/api/start", {})[0], 400)  # no language chosen yet
        code, state = self.call("/api/config", {"target": "finnish"})
        self.assertEqual((state["config"]["target_code"], state["config"]["target_name"]), ("fi", "Finnish"))
        self.assertNotIn("api_key", state["config"])

        folder = Path(tempfile.mkdtemp())
        (folder / "Show.S01E01.en.srt").write_bytes(SAMPLE)
        (folder / "Show.S01E01.sv.srt").write_bytes(SAMPLE)
        _, found = self.call("/api/scan", {"path": str(folder)})
        self.assertEqual({f["name"]: f["state"] for f in found["files"]},
                         {"Show.S01E01.en.srt": "new", "Show.S01E01.sv.srt": "other"})
        _, added = self.call("/api/add-paths", {"paths": [str(folder / "Show.S01E01.en.srt")]})
        job_id = added["added"][0]["id"]
        self.assertGreater(added["added"][0]["est"]["cost"], 0)

        self.assertEqual(self.call(f"/api/jobs/{job_id}/start", {})[1], {"started": 1})
        job = self.wait(job_id)
        self.assertEqual((job["status"], job["flagged"], job["spent"]["requests"]), ("done", 0, 1))
        saved = folder / "Show.S01E01.fi.srt"
        self.assertEqual(job["saved_to"], str(saved))
        self.assertEqual(saved.read_text("utf-8").count("-->"), 26)

        long_line = "Kuinka kauan se on ollut myöhässä ja miksi kukaan ei kertonut minulle siitä mitään?"
        _, row = self.call(f"/api/jobs/{job_id}/cue/18", {"text": long_line})
        self.assertEqual((row["ed"], row["flags"]), (True, ["long", "fast"]))
        self.assertIn(long_line, saved.read_text("utf-8"))  # the file on disk follows edits

        self.assertEqual(self.call(f"/api/jobs/{job_id}/fix", {})[0], 400)  # hand-edited lines are left alone
        self.mgr.jobs[job_id].doc.cues[18].edited = False
        self.assertEqual(self.call(f"/api/jobs/{job_id}/fix", {})[1], {"again": 0, "shorter": 1})
        job = self.wait(job_id)
        self.assertEqual((job["flagged"], job["spent"]["requests"]), (0, 2))

        code, text = self.call(f"/api/jobs/{job_id}/download?bilingual=1")
        self.assertIn(pseudo("How long overdue?") + "\nHow long overdue?", text)
        self.assertEqual(self.call(f"/api/jobs/{job_id}", method="DELETE")[0], 200)
        self.assertEqual(self.call(f"/api/jobs/{job_id}")[0], 404)

    def test_upload_rejects_files_without_subtitles(self):
        code, body = self.call("/api/upload?name=empty.srt", raw=b"\n\n")
        self.assertEqual(code, 400)
        code, job = self.call("/api/upload?name=..%2F..%2Fevil.srt", raw=SAMPLE)
        self.assertEqual((code, job["name"]), (200, "evil.srt"))
        self.call(f"/api/jobs/{job['id']}", method="DELETE")


if __name__ == "__main__":
    unittest.main()
