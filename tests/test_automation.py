"""Video files, Sonarr/Radarr webhooks and Bazarr, all offline.

ffmpeg and Bazarr are stand-ins here: `media.run` is replaced by a function that answers like
ffprobe/ffmpeg would, and Bazarr is a tiny local HTTP server. Nothing in this file proves that
a real ffmpeg or a real Bazarr behaves the same way.
"""
import base64
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from subtle import media
from subtle.arr import ArrError, Bazarr, map_path, parse_webhook
from subtle.providers import MockProvider

SAMPLE = (Path(__file__).parent.parent / "samples" / "lighthouse.en.srt").read_bytes()


def track(index, codec="subrip", lang="eng", title="", forced=0, sdh=0, frames=0):
    tags = {"language": lang, "title": title}
    if frames:
        tags["NUMBER_OF_FRAMES-eng"] = str(frames)
    return {"index": index, "codec_name": codec, "tags": tags,
            "disposition": {"default": 0, "forced": forced, "hearing_impaired": sdh}}


class FakeFfmpeg:
    """Answers ffprobe with tracks chosen by file name, and ffmpeg with the sample subtitles."""

    def __init__(self):
        self.extracted: list[list[str]] = []

    def tools(self):
        return "ffmpeg", "ffprobe"

    def run(self, cmd, timeout):
        name = Path(next(a for a in cmd if a.lower().endswith(media.VIDEO_EXT))).name
        if cmd[0] == "ffprobe":
            if "pgsonly" in name:
                streams = [track(2, "hdmv_pgs_subtitle")]
            else:
                streams = [track(2, title="Forced", forced=1), track(3, title="English", frames=800),
                           track(4, title="English SDH", sdh=1, frames=900), track(5, "hdmv_pgs_subtitle", "spa")]
                if "hasfin" in name:
                    streams.append(track(6, lang="fin"))
            return json.dumps({"streams": streams}).encode()
        self.extracted.append(cmd)
        return SAMPLE


class Media(unittest.TestCase):
    def setUp(self):
        self.fake = FakeFfmpeg()
        self.saved = (media.tools, media.run)
        media.tools, media.run = self.fake.tools, self.fake.run
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        media.tools, media.run = self.saved

    def video(self, name):
        path = self.dir / name
        path.write_bytes(b"not really a video")
        return path

    def test_picks_the_full_dialogue_track(self):
        tracks = media.probe(self.video("Show.S01E01.mkv"))
        self.assertEqual([t["index"] for t in tracks], [2, 3, 4, 5])
        chosen = media.pick(tracks)
        self.assertEqual((chosen["index"], media.describe(chosen)), (3, "eng SubRip"))  # not forced, not SDH
        self.assertEqual(media.pick([t for t in tracks if t["index"] != 3])["index"], 4)  # SDH beats forced

    def test_extract_asks_ffmpeg_for_that_track_as_srt(self):
        name, data, beside, how = media.source_for(self.video("Show.S01E01.mkv"))
        self.assertEqual((name, data, beside), ("Show.S01E01.srt", SAMPLE, str(self.dir / "Show.S01E01.mkv")))
        self.assertEqual(how, "track inside Show.S01E01.mkv (eng SubRip)")
        cmd = self.fake.extracted[0]
        self.assertEqual(cmd[cmd.index("-map") + 1], "0:3")
        self.assertEqual(cmd[-5:], ["-c:s", "srt", "-f", "srt", "pipe:1"])

    def test_a_subtitle_file_next_to_the_video_wins(self):
        video = self.video("Movie (2020).mkv")
        (self.dir / "Movie (2020).en.sdh.srt").write_bytes(b"sdh")
        (self.dir / "Movie (2020).en.srt").write_bytes(SAMPLE)
        (self.dir / "Movie (2020).de.srt").write_bytes(b"german")
        (self.dir / "Movie (2020).en.forced.srt").write_bytes(b"forced")
        name, data, beside, how = media.source_for(video)
        self.assertEqual((name, data, how), ("Movie (2020).en.srt", SAMPLE, "from Movie (2020).en.srt"))
        self.assertEqual(self.fake.extracted, [])

    def test_knows_when_there_is_nothing_to_do(self):
        video = self.video("Film.mkv")
        self.assertEqual(media.has_target(video, "fi", media.probe(video)), "")
        (self.dir / "Film.fin.srt").write_bytes(b"x")
        self.assertEqual(media.has_target(video, "fi"), "Film.fin.srt is already there")
        other = self.video("Other.hasfin.mkv")
        self.assertIn("already carries", media.has_target(other, "fi", media.probe(other)))

    def test_explains_what_it_cannot_use(self):
        with self.assertRaises(media.MediaError) as ctx:
            media.source_for(self.video("Film.pgsonly.mkv"))
        self.assertIn("pictures, not text", str(ctx.exception))
        with self.assertRaises(media.MediaError) as ctx:
            media.pick([])
        self.assertIn("no subtitle tracks", str(ctx.exception))
        self.assertEqual(media.pick([{"index": 1, "codec": "ass", "lang": "", "title": "", "default": True,
                                      "forced": False, "sdh": False, "frames": 0}])["index"], 1)

    def test_without_ffmpeg_the_message_says_so(self):
        media.tools = lambda: (None, None)
        with self.assertRaises(media.MediaError) as ctx:
            media.source_for(self.video("Show.mkv"))
        self.assertIn("ffmpeg is not installed", str(ctx.exception))


class Webhooks(unittest.TestCase):
    def test_sonarr_import(self):
        body = {"eventType": "Download", "series": {"id": 7, "path": "/tv/Show"}, "episodes": [{"id": 70}, {"id": 71}],
                "episodeFile": {"relativePath": "Season 01/Show.S01E01.mkv"}, "isUpgrade": False}
        self.assertEqual(parse_webhook("sonarr", body), {
            "event": "import", "path": "/tv/Show/Season 01/Show.S01E01.mkv",
            "ref": {"kind": "episode", "series": 7, "id": 70}})
        body["episodeFile"]["path"] = "/data/tv/Show/x.mkv"  # a full path, when given, is used as it is
        self.assertEqual(parse_webhook("sonarr", body)["path"], "/data/tv/Show/x.mkv")

    def test_radarr_import_and_other_events(self):
        body = {"eventType": "Download", "movie": {"id": 3, "folderPath": "D:\\Movies\\Film (2020)"},
                "movieFile": {"relativePath": "Film (2020).mkv"}}
        self.assertEqual(parse_webhook("radarr", body), {
            "event": "import", "path": "D:\\Movies\\Film (2020)\\Film (2020).mkv", "ref": {"kind": "movie", "id": 3}})
        self.assertEqual(parse_webhook("radarr", {"eventType": "Test"}), {"event": "test"})
        self.assertIsNone(parse_webhook("sonarr", {"eventType": "Grab"}))
        self.assertIsNone(parse_webhook("sonarr", {"eventType": "Download"}))

    def test_path_mapping(self):
        maps = [["/tv", "X:/Media/TV"], ["/tv/anime", "Y:/Anime"], ["", "ignored"]]
        self.assertEqual(Path(map_path("/tv/Show/S01/e.mkv", maps)), Path("X:/Media/TV/Show/S01/e.mkv"))
        self.assertEqual(Path(map_path("/tv/anime/Show/e.mkv", maps)), Path("Y:/Anime/Show/e.mkv"))  # longest wins
        self.assertEqual(map_path("/tvshows/e.mkv", maps), "/tvshows/e.mkv")  # a prefix must end at a folder
        self.assertEqual(Path(map_path("D:\\Lib\\Show\\e.mkv", [["d:/lib", "/mnt/lib"]])), Path("/mnt/lib/Show/e.mkv"))


class FakeBazarr(BaseHTTPRequestHandler):
    calls: list = []
    state: dict = {}

    def log_message(self, *args):
        pass

    def _reply(self, code, obj=None):
        body = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self):
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        FakeBazarr.calls.append((self.command, url.path, query))
        st = FakeBazarr.state
        if self.headers.get("X-API-KEY") != "bz-key":
            return self._reply(401, {"error": "nope"})
        if url.path == "/api/system/status":
            return self._reply(200, {"data": {"bazarr_version": "1.5.1"}})
        if self.command == "GET" and url.path in ("/api/episodes", "/api/movies"):
            item = [{"subtitles": st["subs"], "missing_subtitles": []}] if st["known"] else []
            return self._reply(200, {"data": item})
        if self.command == "PATCH" and url.path.endswith("/subtitles"):
            if st["finds"]:
                st["subs"].append({"name": "Finnish", "code2": query["language"], "code3": "fin", "forced": False, "hi": False})
            return self._reply(204)
        if self.command == "PATCH":
            return self._reply(204)
        self._reply(404)

    do_GET = do_PATCH = _handle


def start(handler):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


class BazarrClient(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd, cls.url = start(FakeBazarr)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        FakeBazarr.calls.clear()
        FakeBazarr.state = {"known": True, "subs": [], "finds": True}

    def test_calls_match_the_bazarr_api(self):
        bz, ep, movie = Bazarr(self.url + "/api/", "bz-key"), {"kind": "episode", "series": 7, "id": 70}, {"kind": "movie", "id": 3}
        self.assertEqual(bz.version(), "1.5.1")
        self.assertIs(bz.has(ep, "fi"), False)
        bz.search(ep, "fi")
        self.assertIs(bz.has(ep, "fi"), True)
        bz.rescan(ep)
        bz.search(movie, "pt-BR")
        bz.rescan(movie)
        self.assertEqual(FakeBazarr.calls[1:], [
            ("GET", "/api/episodes", {"episodeid[]": "70"}),
            ("PATCH", "/api/episodes/subtitles", {"seriesid": "7", "episodeid": "70", "language": "fi", "forced": "false", "hi": "false"}),
            ("GET", "/api/episodes", {"episodeid[]": "70"}),
            ("PATCH", "/api/series", {"seriesid": "7", "action": "scan-disk"}),
            ("PATCH", "/api/movies/subtitles", {"radarrid": "3", "language": "pb", "forced": "false", "hi": "false"}),
            ("PATCH", "/api/movies", {"radarrid": "3", "action": "scan-disk"}),
        ])

    def test_unknown_items_and_bad_keys(self):
        FakeBazarr.state["known"] = False
        self.assertIsNone(Bazarr(self.url, "bz-key").has({"kind": "movie", "id": 3}, "fi"))
        with self.assertRaises(ArrError) as ctx:
            Bazarr(self.url, "wrong").version()
        self.assertIn("rejected the API key", str(ctx.exception))
        with self.assertRaises(ArrError) as ctx:
            Bazarr("http://127.0.0.1:9", "k").version()
        self.assertIn("Could not reach Bazarr", str(ctx.exception))


class Automation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from subtle.server import Handler, Manager

        os.environ["SUBTLE_HOME"] = tempfile.mkdtemp(prefix="subtle-auto-")
        cls.fake = FakeFfmpeg()
        cls.saved = (media.tools, media.run)
        media.tools, media.run = cls.fake.tools, cls.fake.run
        cls.bz_httpd, cls.bz_url = start(FakeBazarr)
        cls.lib = Path(tempfile.mkdtemp(prefix="subtle-lib-"))
        cls.Handler = Handler
        cls.mgr = Handler.mgr = Manager(mock=True)
        cls.mgr._provider, cls.mgr._provider_for = MockProvider(), (True, "none", None)
        cls.mgr.bazarr_poll, cls.mgr.bazarr_sync_wait = 0.01, 0.05
        cls.mgr.set_config({"target": "fi", "path_maps": [["/tv", str(cls.lib / "tv")], ["/movies", str(cls.lib / "movies")]],
                            "bazarr_url": cls.bz_url, "bazarr_key": "bz-key", "auto_daily_budget": 5})
        Handler.open_host, Handler.password = False, ""
        cls.httpd, cls.base = start(Handler)
        cls.secret = cls.mgr.cfg["webhook_secret"]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.bz_httpd.shutdown()
        cls.Handler.password = ""
        media.tools, media.run = cls.saved

    def setUp(self):
        FakeBazarr.calls.clear()
        FakeBazarr.state = {"known": True, "subs": [], "finds": False}
        self.fake.extracted.clear()

    def post(self, path, body, headers=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST", headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def video(self, rel):
        path = self.lib / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not really a video")
        return path

    def sonarr(self, rel, episode=70):
        return {"eventType": "Download", "series": {"id": 7, "path": "/tv/Show"}, "episodes": [{"id": episode}],
                "episodeFile": {"relativePath": rel.split("Show/", 1)[1], "path": "/" + rel}}

    def outcome(self, file):
        for _ in range(400):
            entry = next((e for e in reversed(self.mgr.activity) if e["file"] == file), None)
            if entry and entry["status"] in ("translated", "bazarr", "skipped", "error"):
                return entry
            time.sleep(0.02)
        self.fail(f"no outcome for {file}: {self.mgr.activity[-3:]}")

    def test_an_import_is_translated_and_saved_next_to_the_video(self):
        video = self.video("tv/Show/Season 01/Show.S01E01.mkv")
        code, body = self.post(f"/webhook/sonarr?secret={self.secret}", self.sonarr("tv/Show/Season 01/Show.S01E01.mkv"))
        self.assertEqual((code, body), (200, {"ok": True, "action": "queued"}))
        entry = self.outcome("Show.S01E01.mkv")
        self.assertEqual((entry["status"], entry["origin"]), ("translated", "Sonarr"))
        self.assertEqual(entry["note"], "Saved Show.S01E01.fi.srt.")
        self.assertGreater(entry["cost"], 0)
        out = video.with_name("Show.S01E01.fi.srt")
        self.assertEqual(out.read_text("utf-8").count("-->"), 26)
        self.assertEqual(len(self.fake.extracted), 1)
        # Bazarr was asked first, searched once, and told to rescan afterwards.
        self.assertEqual([(m, p) for m, p, _ in FakeBazarr.calls], [
            ("GET", "/api/episodes"), ("PATCH", "/api/episodes/subtitles"), ("GET", "/api/episodes"), ("PATCH", "/api/series")])
        job = next(j for j in self.mgr.jobs.values() if j.name == "Show.S01E01.srt")
        self.assertEqual(job.via, "Sonarr · track inside Show.S01E01.mkv (eng SubRip)")

        # The same import reported again: the translation is there, so nothing is spent.
        self.post(f"/webhook/sonarr?secret={self.secret}", self.sonarr("tv/Show/Season 01/Show.S01E01.mkv"))
        time.sleep(0.2)
        again = self.outcome("Show.S01E01.mkv")
        self.assertEqual((again["status"], again["note"]), ("skipped", "Nothing to do: Show.S01E01.fi.srt is already there."))

    def test_bazarr_gets_the_first_chance(self):
        FakeBazarr.state["finds"] = True
        video = self.video("tv/Show/Season 01/Show.S01E02.mkv")
        jobs_before = len(self.mgr.jobs)
        self.post("/webhook/sonarr", self.sonarr("tv/Show/Season 01/Show.S01E02.mkv", 71), {"X-Webhook-Secret": self.secret})
        entry = self.outcome("Show.S01E02.mkv")
        self.assertEqual((entry["status"], entry["cost"]), ("bazarr", 0.0))
        self.assertEqual(len(self.mgr.jobs), jobs_before)
        self.assertFalse(video.with_name("Show.S01E02.fi.srt").exists())
        self.assertEqual(self.fake.extracted, [])

    def test_an_item_bazarr_has_not_heard_of_is_translated_anyway(self):
        FakeBazarr.state["known"] = False
        self.video("movies/Film (2020)/Film (2020).mkv")
        body = {"eventType": "Download", "movie": {"id": 3, "folderPath": "/movies/Film (2020)"},
                "movieFile": {"relativePath": "Film (2020).mkv"}}
        self.assertEqual(self.post(f"/webhook/radarr?secret={self.secret}", body)[0], 200)
        entry = self.outcome("Film (2020).mkv")
        self.assertEqual((entry["status"], entry["origin"]), ("translated", "Radarr"))
        self.assertNotIn(("PATCH", "/api/movies/subtitles"), [(m, p) for m, p, _ in FakeBazarr.calls])
        self.assertEqual(FakeBazarr.calls[-1], ("PATCH", "/api/movies", {"radarrid": "3", "action": "scan-disk"}))

    def test_files_that_need_nothing_or_cannot_be_used(self):
        self.video("tv/Show/Season 01/Show.S01E03.hasfin.mkv")
        self.post(f"/webhook/sonarr?secret={self.secret}", self.sonarr("tv/Show/Season 01/Show.S01E03.hasfin.mkv"))
        self.assertIn("already carries a track", self.outcome("Show.S01E03.hasfin.mkv")["note"])

        self.video("tv/Show/Season 01/Show.S01E04.pgsonly.mkv")
        self.post(f"/webhook/sonarr?secret={self.secret}", self.sonarr("tv/Show/Season 01/Show.S01E04.pgsonly.mkv"))
        entry = self.outcome("Show.S01E04.pgsonly.mkv")
        self.assertEqual(entry["status"], "error")
        self.assertIn("pictures, not text", entry["note"])

        self.post(f"/webhook/sonarr?secret={self.secret}", self.sonarr("tv/Show/Season 09/Missing.mkv"))
        entry = self.outcome("Missing.mkv")
        self.assertEqual(entry["status"], "error")
        self.assertIn("Check the path mapping", entry["note"])

    def test_daily_limit_stops_automatic_spending(self):
        self.video("tv/Show/Season 01/Show.S01E05.mkv")
        self.mgr.log("Sonarr", "earlier.mkv", "translated")["cost"] = 5.0
        self.post(f"/webhook/sonarr?secret={self.secret}", self.sonarr("tv/Show/Season 01/Show.S01E05.mkv"))
        entry = self.outcome("Show.S01E05.mkv")
        self.assertEqual(entry["status"], "skipped")
        self.assertIn("limit for automatic translations ($5.00) is used up", entry["note"])
        self.mgr.activity[:] = [e for e in self.mgr.activity if e["file"] != "earlier.mkv"]

    def test_webhooks_need_the_secret_and_ignore_other_events(self):
        self.assertEqual(self.post("/webhook/sonarr?secret=wrong", {"eventType": "Test"})[0], 403)
        self.assertEqual(self.post("/webhook/sonarr", {"eventType": "Test"})[0], 403)
        self.assertEqual(self.post(f"/webhook/plex?secret={self.secret}", {})[0], 404)
        self.assertEqual(self.post(f"/webhook/radarr?secret={self.secret}", {"eventType": "Test"})[1]["action"], "test")
        self.assertEqual(self.mgr.activity[-1]["status"], "test")
        self.assertEqual(self.post(f"/webhook/sonarr?secret={self.secret}", {"eventType": "Grab"})[1]["action"], "ignored")

    def test_password_protects_the_ui_but_not_the_webhooks(self):
        self.Handler.password = "open sesame"
        try:
            req = urllib.request.Request(self.base + "/api/state")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual((ctx.exception.code, ctx.exception.headers["WWW-Authenticate"]), (401, 'Basic realm="Subtle"'))
            token = base64.b64encode(b"anyone:open sesame").decode()
            req = urllib.request.Request(self.base + "/api/state", headers={"Authorization": "Basic " + token})
            with urllib.request.urlopen(req, timeout=5) as r:
                state = json.loads(r.read())
            self.assertNotIn("bazarr_key", state["config"])
            self.assertTrue(state["bazarr_key_set"] and state["ffmpeg"])
            self.assertEqual(self.post(f"/webhook/sonarr?secret={self.secret}", {"eventType": "Test"})[0], 200)
        finally:
            self.Handler.password = ""

    def test_folder_scan_lists_videos_with_what_was_found_inside(self):
        folder = self.lib / "scan"
        for name in ("A.mkv", "B.pgsonly.mkv", "C.mkv", "D.hasfin.mkv"):
            self.video(f"scan/{name}")
        (folder / "C.en.srt").write_bytes(SAMPLE)
        found = self.mgr.scan(str(folder))
        self.assertEqual({f["name"]: f["state"] for f in found["files"]},
                         {"A.mkv": "video", "B.pgsonly.mkv": "nosub", "C.en.srt": "new", "D.hasfin.mkv": "done"})
        self.assertEqual(found["files"][0]["note"], "track inside the video: eng SubRip")
        result = self.mgr.add_paths([str(folder / "A.mkv"), str(folder / "B.pgsonly.mkv")])
        self.assertEqual(([j["name"] for j in result["added"]], [f["name"] for f in result["failed"]]),
                         (["A.srt"], ["B.pgsonly.mkv"]))
        self.assertEqual(result["added"][0]["via"], "track inside A.mkv (eng SubRip)")
        for job in result["added"]:
            self.mgr.remove(self.mgr.jobs[job["id"]])


class FakeArr(BaseHTTPRequestHandler):
    """Sonarr/Radarr's v3 API as far as Subtle uses it, including the test call made before a webhook is saved."""
    app, KEY = "Sonarr", "arr-test-key"
    roots: list = []
    items: list = []
    hooks: list = []
    calls: list = []

    def log_message(self, *args):
        pass

    def _reply(self, code, obj=None):
        body = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _tested(self, hook) -> str:
        url = next((f.get("value") for f in hook.get("fields", []) if f.get("name") == "url"), "")
        req = urllib.request.Request(url, data=b'{"eventType": "Test"}', method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5):
                return ""
        except Exception as e:
            return f"Unable to post to webhook: {e}"

    def _handle(self):
        cls, path = type(self), urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        cls.calls.append((self.command, path))
        if self.headers.get("X-Api-Key") != cls.KEY:
            return self._reply(401, {"error": "Unauthorized"})
        if path == "/api/v3/system/status":
            return self._reply(200, {"appName": cls.app, "version": "4.0.9.2244"})
        if path == "/api/v3/rootfolder":
            return self._reply(200, [{"id": i, "path": p, "accessible": True} for i, p in enumerate(cls.roots, 1)])
        if path in ("/api/v3/series", "/api/v3/movie"):
            if path.endswith("series") != (cls.app == "Sonarr"):
                return self._reply(404)
            return self._reply(200, [{"id": i, "path": p} for i, p in enumerate(cls.items, 1)])
        if path == "/api/v3/notification/schema":
            flags = {"onGrab": True, "onDownload": True, "onUpgrade": True, "onRename": True, "onHealthIssue": False,
                     "includeHealthWarnings": False, "supportsOnGrab": True, "supportsOnDownload": True}
            return self._reply(200, [
                {"id": 0, "name": "", "implementation": "Email", "configContract": "EmailSettings", "fields": [], **flags},
                {"id": 0, "name": "", "implementation": "Webhook", "implementationName": "Webhook",
                 "configContract": "WebhookSettings", "tags": [], "presets": [], **flags,
                 "fields": [{"order": 0, "name": "url"}, {"order": 1, "name": "method", "value": 1},
                            {"order": 2, "name": "username"}, {"order": 3, "name": "password"},
                            {"order": 4, "name": "headers", "value": []}]}])
        if path == "/api/v3/notification" and self.command == "GET":
            return self._reply(200, cls.hooks)
        if path == "/api/v3/notification" and self.command == "POST":
            problem = self._tested(body)
            if problem:
                return self._reply(400, [{"propertyName": "Url", "errorMessage": problem, "severity": "error"}])
            body["id"] = 1 + max([h["id"] for h in cls.hooks], default=0)
            cls.hooks.append(body)
            return self._reply(201, body)
        if path.startswith("/api/v3/notification/"):
            wanted = int(path.rsplit("/", 1)[1])
            if self.command == "DELETE":
                cls.hooks[:] = [h for h in cls.hooks if h["id"] != wanted]
                return self._reply(200, {})
            problem = self._tested(body)
            if problem:
                return self._reply(400, [{"propertyName": "Url", "errorMessage": problem, "severity": "error"}])
            cls.hooks[:] = [body if h["id"] == wanted else h for h in cls.hooks]
            return self._reply(202, body)
        self._reply(404)

    do_GET = do_POST = do_PUT = do_DELETE = _handle


class FakeSonarr(FakeArr):
    app, hooks, calls = "Sonarr", [], []


class FakeRadarr(FakeArr):
    app, hooks, calls = "Radarr", [], []


class Setup(unittest.TestCase):
    """The setup screen's Connect button, against a stand-in Sonarr and Radarr."""

    @classmethod
    def setUpClass(cls):
        from subtle.server import Handler, Manager

        os.environ["SUBTLE_HOME"] = tempfile.mkdtemp(prefix="subtle-setup-")

        class Own(Handler):  # its own manager, whatever other test classes do to Handler
            pass

        cls.mgr = Own.mgr = Manager(mock=True)
        Own.open_host, Own.password = False, ""
        cls.httpd, cls.base = start(Own)
        cls.mgr.listen = {"open": False, "port": cls.httpd.server_address[1]}
        cls.lib = Path(tempfile.mkdtemp(prefix="subtle-setup-lib-"))
        for folder in ("shows/Show A", "shows/Show B", "elsewhere", "films/Film (2020)"):
            (cls.lib / folder).mkdir(parents=True)
        cls.sonarr_httpd, cls.sonarr = start(FakeSonarr)
        cls.radarr_httpd, cls.radarr = start(FakeRadarr)
        FakeSonarr.roots, FakeSonarr.items = ["/tv"], ["/tv/Show A", "/tv/Show B", "/tv/Show C"]
        FakeRadarr.roots = [str(cls.lib / "films")]
        FakeRadarr.items = [str(cls.lib / "films" / "Film (2020)")]

    @classmethod
    def tearDownClass(cls):
        for httpd in (cls.httpd, cls.sonarr_httpd, cls.radarr_httpd):
            httpd.shutdown()

    def setUp(self):
        for fake in (FakeSonarr, FakeRadarr):
            fake.hooks.clear()
            fake.calls.clear()
        self.mgr.cfg.update(path_maps=[], reach_url="", sonarr_url="", sonarr_key="", radarr_url="", radarr_key="")
        self.mgr.activity.clear()

    def connect(self, kind, url, key=FakeArr.KEY, **more):
        req = urllib.request.Request(self.base + "/api/arr/connect", method="POST",
                                     data=json.dumps({"kind": kind, "url": url, "key": key, **more}).encode(),
                                     headers={"X-Subtle": "1", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_connecting_adds_a_tested_webhook_and_keeps_the_key_out_of_the_page(self):
        code, body = self.connect("sonarr", self.sonarr)
        self.assertEqual((code, body["app"], body["version"]), (200, "Sonarr", "4.0.9.2244"))
        self.assertEqual((body["hook"]["ok"], body["hook"]["state"]), (True, "added"))
        self.assertNotIn("secret", json.dumps(body["hook"]))
        hook, = FakeSonarr.hooks
        url = next(f["value"] for f in hook["fields"] if f["name"] == "url")
        self.assertTrue(url.endswith(f"/webhook/sonarr?secret={self.mgr.cfg['webhook_secret']}"), url)
        self.assertEqual((hook["name"], hook["onDownload"], hook["onUpgrade"], hook["onGrab"], hook["onRename"]),
                         ("Subtle", True, True, False, False))
        self.assertNotIn("presets", hook)
        # Sonarr's own test event arrived, which is the proof that it can reach Subtle.
        self.assertEqual([(e["origin"], e["status"]) for e in self.mgr.activity], [("Sonarr", "test")])
        state = body["state"]
        self.assertEqual(state["arr"]["sonarr"], {"url": self.sonarr, "key_set": True})
        self.assertNotIn(FakeArr.KEY, json.dumps(state))

        # Pressing Connect again changes nothing over there.
        code, body = self.connect("sonarr", "", "")  # the saved address and key are used
        self.assertEqual((code, body["hook"]["state"], len(FakeSonarr.hooks)), (200, "kept", 1))
        self.assertEqual([c for c in FakeSonarr.calls if c[0] in ("POST", "PUT")], [("POST", "/api/v3/notification")])

    def test_a_library_folder_that_is_elsewhere_here_is_asked_for_and_checked(self):
        folder = self.connect("sonarr", self.sonarr)[1]["folders"][0]
        self.assertEqual((folder["remote"], folder["state"], folder["total"]), ("/tv", "missing", 3))

        wrong = self.connect("sonarr", self.sonarr, local={"/tv": str(self.lib / "elsewhere")})[1]["folders"][0]
        self.assertEqual(wrong["state"], "missing")
        self.assertIn("None of the 3 folders Sonarr lists under /tv", wrong["error"])
        self.assertEqual(self.mgr.cfg["path_maps"], [])

        right = self.connect("sonarr", self.sonarr, local={"/tv": str(self.lib / "shows")})[1]["folders"][0]
        self.assertEqual((right["state"], right["found"], right["total"], right["error"]), ("mapped", 2, 3, ""))
        self.assertEqual(self.mgr.cfg["path_maps"], [["/tv", str(self.lib / "shows")]])
        from subtle import config
        self.assertEqual(config.load()["path_maps"], [["/tv", str(self.lib / "shows")]])  # written to disk

    def test_a_library_at_the_same_path_needs_no_mapping(self):
        code, body = self.connect("radarr", self.radarr)
        folder, = body["folders"]
        self.assertEqual((code, folder["state"], folder["found"], folder["total"]), (200, "same", 1, 1))
        self.assertEqual((self.mgr.cfg["path_maps"], body["hook"]["state"], len(FakeRadarr.hooks)), ([], "added", 1))
        self.assertEqual(self.mgr.activity[-1]["origin"], "Radarr")

    def test_a_wrong_key_app_or_address_saves_nothing(self):
        for kind, url, key, expected in (
                ("sonarr", self.sonarr, "not-the-key", "Sonarr rejected the API key."),
                ("radarr", self.sonarr, FakeArr.KEY, "That address is Sonarr, not Radarr."),
                ("sonarr", "http://127.0.0.1:9", FakeArr.KEY, "Could not reach Sonarr at http://127.0.0.1:9"),
                ("sonarr", "", "", "Enter Sonarr's address and API key first.")):
            with self.subTest(expected=expected):
                code, body = self.connect(kind, url, key)
                self.assertIn(code, (400, 502))
                self.assertIn(expected, body["error"])
        self.assertEqual((self.mgr.cfg["sonarr_url"], self.mgr.cfg["radarr_url"], FakeSonarr.hooks), ("", "", []))

    def test_an_address_the_app_cannot_reach_is_reported_and_nothing_is_added(self):
        code, body = self.connect("sonarr", self.sonarr, reach="http://127.0.0.1:9")
        self.assertEqual((code, body["hook"]["ok"], FakeSonarr.hooks), (200, False, []))
        self.assertIn("Sonarr could not use http://127.0.0.1:9 to reach Subtle", body["hook"]["error"])
        self.assertEqual(self.mgr.cfg["sonarr_url"], self.sonarr)  # the connection itself is kept

        # Clearing the address goes back to working it out.
        code, body = self.connect("sonarr", self.sonarr, reach="")
        self.assertEqual((body["hook"]["ok"], len(FakeSonarr.hooks)), (True, 1))

    def test_disconnecting_takes_the_webhook_away_again(self):
        self.connect("sonarr", self.sonarr)
        self.assertEqual(len(FakeSonarr.hooks), 1)
        req = urllib.request.Request(self.base + "/api/arr/disconnect", method="POST", data=b'{"kind": "sonarr"}',
                                     headers={"X-Subtle": "1", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.loads(r.read())
        self.assertEqual((FakeSonarr.hooks, body["note"], body["state"]["arr"]["sonarr"]),
                         ([], "", {"url": "", "key_set": False}))


if __name__ == "__main__":
    unittest.main()
