"""The local web UI: a small threaded HTTP server, JSON endpoints and one event stream.

Standard library only. It listens on this machine by default and refuses requests that
did not come from its own page, because it can read and write subtitle files on disk.
"""
from __future__ import annotations

import base64
import hmac
import io
import json
import os
import queue
import re
import secrets
import socket
import threading
import time
import uuid
import webbrowser
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from . import __version__, config, formats, media, qa
from .arr import Arr, ArrError, Bazarr, child_of, map_path, parse_webhook
from .cache import Cache
from .engine import BudgetExceeded, Options, Translator, estimate, retranslate
from .languages import LANGUAGES, resolve, variants
from .model import Document
from .prompt import clean_title
from .providers import MODELS, Cancelled, ProviderError, build

WEB = Path(__file__).parent / "web"
STATIC = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
          "/app.css": ("app.css", "text/css"), "/favicon.svg": ("favicon.svg", "image/svg+xml")}
MAX_UPLOAD = 8 * 1024 * 1024
SCAN_EXT = (".srt", ".vtt", ".ass", ".ssa")
OTHER_LANGS = {c.lower() for c, _ in LANGUAGES} - {"en"} | {"fin", "swe", "ger", "deu", "fre", "fra", "spa", "ita", "rus"}


class Job:
    def __init__(self, name: str, doc: Document, src_path: str = ""):
        self.id = uuid.uuid4().hex[:10]
        self.name, self.doc, self.src_path = name, doc, src_path
        self.status = "ready"  # ready | queued | running | done | stopped | error
        self.error = ""
        self.created = time.time()
        self.est: dict = {}
        self.progress = [0, 0]
        self.spent = {"cost": 0.0, "inp": 0, "out": 0, "requests": 0}
        self.last: dict = {}  # stats of the latest run
        self.used: dict = {}  # target and model of the latest run
        self.saved_to = ""
        self.cancel = False
        self.busy = False  # a retranslation is in flight
        self.via = ""  # how the source was found, e.g. "Sonarr · track inside x.mkv (eng SubRip)"
        self.auto = False  # started by a webhook rather than a click
        self.ref: dict | None = None  # Sonarr/Radarr ids, for Bazarr
        self.entry: dict | None = None  # its line in the activity log

    def summary(self) -> dict:
        cues = self.doc.cues
        return {"id": self.id, "name": self.name, "fmt": self.doc.fmt, "status": self.status, "error": self.error,
                "cues": len(cues), "est": self.est, "progress": self.progress, "spent": self.spent,
                "last": self.last, "used": self.used, "saved_to": self.saved_to, "src_path": self.src_path,
                "flagged": sum(1 for c in cues if c.flags),
                "errors": sum(1 for c in cues if set(c.flags) & qa.ERRORS),
                "edited": sum(1 for c in cues if c.edited), "created": self.created, "busy": self.busy,
                "via": self.via}

    def row(self, i: int) -> dict:
        c = self.doc.cues[i]
        speed = qa.cps(c)
        return {"i": i, "s": c.start, "e": c.end, "src": c.text, "tr": c.translation, "flags": c.flags,
                "ed": c.edited, "cps": round(speed, 1) if speed is not None else None}

    def detail(self) -> dict:
        rows = [self.row(i) for i, c in enumerate(self.doc.cues) if c.text.strip()]
        return {**self.summary(), "rows": rows}

    def to_json(self) -> dict:
        keep = ("id", "name", "src_path", "status", "error", "created", "est", "progress", "spent", "last",
                "used", "saved_to", "via", "auto", "ref")
        return {**{k: getattr(self, k) for k in keep}, "doc": self.doc.to_json()}

    @classmethod
    def from_json(cls, d: dict) -> "Job":
        job = cls(d["name"], Document.from_json(d["doc"]), d.get("src_path", ""))
        for k in ("id", "status", "error", "created", "est", "progress", "spent", "last", "used", "saved_to",
                  "via", "auto", "ref"):
            if k in d:
                setattr(job, k, d[k])
        if job.status in ("running", "queued"):  # the server was closed mid-run
            job.status, job.error = "stopped", "Interrupted. Start again to resume; finished parts are cached."
        return job


class Manager:
    def __init__(self, mock: bool = False):
        self.cfg = config.load()
        self.mock = mock or bool(os.environ.get("SUBTLE_MOCK"))
        self.cache = Cache(config.home() / "cache.db")
        self.jobs: dict[str, Job] = {}
        self.subs: set[queue.Queue] = set()
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=self._workers())
        self._provider = None
        self._provider_for = None
        self.listen = {"open": False, "port": 0}  # filled in by serve()
        self.bazarr_poll, self.bazarr_sync_wait = 15, 90  # seconds; see _bazarr_first
        self.activity: list[dict] = []
        try:
            self.activity = json.loads((config.home() / "activity.json").read_text("utf-8"))[-200:]
        except (OSError, ValueError):
            pass
        if not self.cfg.get("webhook_secret"):
            self.cfg["webhook_secret"] = secrets.token_urlsafe(18)
            config.save(self.cfg)
        self.dir = config.home() / "jobs"
        self.dir.mkdir(exist_ok=True)
        for path in sorted(self.dir.glob("*.json"), key=lambda p: p.stat().st_mtime):
            try:
                job = Job.from_json(json.loads(path.read_text("utf-8")))
                self.jobs[job.id] = job
            except (OSError, ValueError, KeyError):
                continue

    # --- plumbing -----------------------------------------------------------

    def _workers(self) -> int:
        return max(1, min(int(self.cfg.get("concurrency") or 3), 8))

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=20000)
        with self.lock:
            self.subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            self.subs.discard(q)

    def publish(self, event: dict) -> None:
        with self.lock:
            subs = list(self.subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

    def _announce(self, job: Job) -> None:
        self.publish({"type": "job", "job": job.summary()})

    def _save(self, job: Job) -> None:
        tmp = self.dir / f"{job.id}.tmp"
        tmp.write_text(json.dumps(job.to_json(), ensure_ascii=False), "utf-8")
        tmp.replace(self.dir / f"{job.id}.json")

    def provider(self):
        source = config.key_source(self.cfg)
        key = self.cfg.get("api_key") if source == "saved" else None
        ident = (self.mock, source, key)
        if self._provider is None or self._provider_for != ident:
            self._provider, self._provider_for = build(key, self.mock), ident
        return self._provider

    def options(self, job: Job, use_run_settings: bool = False) -> Options:
        over = {"title": clean_title(job.name)}
        if use_run_settings and job.used:
            over.update(job.used)
        return Options.from_dict(self.cfg, **over)

    def get(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if not job:
            raise KeyError("That file is no longer in the queue.")
        return job

    # --- state --------------------------------------------------------------

    def state(self) -> dict:
        cfg = {k: v for k, v in self.cfg.items() if k not in ("api_key", "bazarr_key", "sonarr_key", "radarr_key")}
        key = self.cfg.get("api_key") or ""
        return {
            "version": __version__, "mock": self.mock, "home": str(config.home()),
            "arr": {k: {"url": self.cfg.get(f"{k}_url", ""), "key_set": bool(self.cfg.get(f"{k}_key"))}
                    for k in ("sonarr", "radarr")},
            "ffmpeg": media.available(), "listen": self.listen, "bazarr_key_set": bool(self.cfg.get("bazarr_key")),
            "activity": self.activity[-30:], "auto_spent_today": round(self.auto_spent_today(), 4),
            "config": cfg, "key_source": "mock" if self.mock else config.key_source(self.cfg),
            "key_hint": f"…{key[-4:]}" if len(key) > 8 else "",
            "models": [{"id": k, **{f: v[f] for f in ("label", "inp", "out", "blurb")}} for k, v in MODELS.items()],
            "languages": [{"code": c, "name": n} for c, n in LANGUAGES],
            "labels": qa.LABELS, "errors": sorted(qa.ERRORS),
            "jobs": [j.summary() for j in self.jobs.values()],
        }

    def set_config(self, patch: dict) -> dict:
        cfg = self.cfg
        if "target" in patch:
            cfg["target_code"], cfg["target_name"] = resolve(str(patch["target"]))
        for key, kind in (("model", str), ("escalate_model", str), ("register", str), ("sdh", str),
                          ("lyrics", str), ("notes", str), ("max_chars", int), ("max_cps", int),
                          ("chunk_lines", int), ("budget", float), ("concurrency", int),
                          ("save_next_to_source", bool), ("bilingual", bool), ("api_key", str),
                          ("bazarr_url", str), ("bazarr_wait", int), ("auto_daily_budget", float),
                          ("setup_seen", bool), ("reach_url", str)):
            if key in patch:
                cfg[key] = kind(patch[key]) if patch[key] is not None else config.DEFAULTS[key]
        if "glossary" in patch:
            cfg["glossary"] = [[str(a).strip(), str(b).strip()] for a, b in patch["glossary"]
                               if str(a).strip() and str(b).strip()][:500]
        if "path_maps" in patch:
            cfg["path_maps"] = [[str(a).strip(), str(b).strip()] for a, b in patch["path_maps"]
                                if str(a).strip() and str(b).strip()][:50]
        if patch.get("bazarr_key"):  # an empty field means "keep the saved one"
            cfg["bazarr_key"] = str(patch["bazarr_key"]).strip()
        if not cfg["bazarr_url"].strip():
            cfg["bazarr_url"] = cfg["bazarr_key"] = ""
        cfg["bazarr_wait"] = max(0, min(cfg["bazarr_wait"], 7 * 24 * 60))
        cfg["auto_daily_budget"] = max(0.0, cfg["auto_daily_budget"])
        cfg["max_chars"] = max(20, min(cfg["max_chars"], 80))
        cfg["max_cps"] = max(8, min(cfg["max_cps"], 40))
        cfg["chunk_lines"] = max(20, min(cfg["chunk_lines"], 400))
        cfg["api_key"] = cfg["api_key"].strip()
        config.save(cfg)
        if "concurrency" in patch:
            self.pool = ThreadPoolExecutor(max_workers=self._workers())
        for job in self.jobs.values():
            if job.status in ("ready", "stopped", "error"):
                job.est = estimate(job.doc, self.options(job))
        return self.state()

    # --- adding files -------------------------------------------------------

    def add(self, name: str, data: bytes, src_path: str = "", via: str = "") -> Job:
        name = os.path.basename(name.replace("\\", "/")) or "subtitles.srt"
        doc = formats.parse(name, data)
        if not any(c.text.strip() for c in doc.cues):
            raise ValueError(f"{name} has no subtitle lines in it.")
        job = Job(name, doc, src_path)
        job.via = via
        job.est = estimate(doc, self.options(job))
        with self.lock:
            self.jobs[job.id] = job
        self._save(job)
        self._announce(job)
        return job

    def scan(self, raw: str) -> dict:
        root = Path(raw.strip().strip('"')).expanduser()
        if root.is_file():
            files = [root]
        elif root.is_dir():
            wanted = SCAN_EXT + media.VIDEO_EXT
            files = sorted(p for p in root.rglob("*") if p.suffix.lower() in wanted and p.is_file())[:6000]
        else:
            raise ValueError("That folder or file does not exist.")
        code = self.cfg["target_code"]
        ours = variants(code)
        queued = {j.src_path for j in self.jobs.values() if j.src_path}
        out, videos = [], []
        for p in files:
            if p.suffix.lower() in media.VIDEO_EXT:
                videos.append(p)
                continue
            tags = {t.lower() for t in p.name.split(".")[1:-1]}
            if tags & ours:
                continue  # that is a translation, not a source
            target = p.with_name(formats.output_name(p.name, code)) if code else None
            state = ("queued" if str(p) in queued else "done" if target and target.exists() and target != p
                     else "other" if tags & OTHER_LANGS else "new")
            out.append({"path": str(p), "name": p.name, "dir": str(p.parent), "state": state, "note": ""})

        def look(video: Path) -> dict | None:
            if media.find_sidecar(video):
                return None  # its subtitle file is already in the list
            entry = {"path": str(video), "name": video.name, "dir": str(video.parent), "state": "video", "note": ""}
            if str(video) in queued:
                entry["state"] = "queued"
                return entry
            try:
                tracks = media.probe(video)
                if code and media.has_target(video, code, tracks):
                    entry["state"] = "done"
                else:
                    entry["note"] = "track inside the video: " + media.describe(media.pick(tracks))
            except media.MediaError as e:
                entry["state"], entry["note"] = "nosub", str(e)
            return entry

        can_read_video = media.available()
        if can_read_video and videos:
            with ThreadPoolExecutor(max_workers=8) as pool:
                out += [e for e in pool.map(look, videos[:500]) if e]
        skipped = 0 if can_read_video else sum(1 for v in videos if not media.find_sidecar(v))
        return {"root": str(root), "files": sorted(out, key=lambda e: e["path"]), "videos_skipped": skipped}

    def add_paths(self, paths: list[str]) -> dict:
        added, failed = [], []
        for raw in paths[:500]:
            p = Path(raw)
            try:
                if p.suffix.lower() in media.VIDEO_EXT and p.is_file():
                    name, data, beside, how = media.source_for(p)
                    added.append(self.add(name, data, beside, how).summary())
                elif p.suffix.lower() in SCAN_EXT + (".txt",) and p.is_file() and p.stat().st_size <= MAX_UPLOAD:
                    added.append(self.add(p.name, p.read_bytes(), str(p)).summary())
            except (OSError, ValueError, media.MediaError) as e:
                failed.append({"name": p.name, "error": str(e)})
        return {"added": added, "failed": failed}

    # --- automation: Sonarr/Radarr webhooks, Bazarr ---------------------------

    def bazarr(self) -> Bazarr | None:
        url, key = self.cfg.get("bazarr_url"), self.cfg.get("bazarr_key")
        return Bazarr(url, key) if url and key else None

    # --- setup: connecting Sonarr and Radarr -----------------------------------

    def _reach(self, arr: Arr) -> list[str]:
        """Addresses Sonarr/Radarr might reach Subtle at, best guess first."""
        port = self.listen.get("port") or 8756
        saved = (self.cfg.get("reach_url") or "").strip().rstrip("/")
        if saved:
            return [saved if "://" in saved else "http://" + saved]
        host = (urlparse(arr.base).hostname or "").lower()
        names: list[str] = []
        if host in ("localhost", "127.0.0.1", "::1"):
            # Same machine. If the app runs in Docker, "localhost" there is the container itself.
            names += ["127.0.0.1", "host.docker.internal"]
            host = "10.255.255.255"  # any routable address: finds this machine's network address
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((host, 9))  # UDP: picks the outgoing interface, sends nothing
                names.append(s.getsockname()[0])
        except OSError:
            pass
        return [f"http://{n}:{port}" for n in dict.fromkeys(names) if n and n != "0.0.0.0"]

    def _folders(self, arr: Arr, given: dict) -> list[dict]:
        """Can this machine open the library folders the app reports? Saves mappings that check out."""
        roots = arr.roots()
        items = arr.folders() if roots else []
        maps = [m for m in self.cfg.get("path_maps") or [] if len(m) == 2]
        out = []
        for root in roots:
            names = sorted({n for n in (child_of(root, p) for p in items) if n})[:40]
            entry = {"remote": root, "local": "", "state": "missing", "found": 0, "total": len(names), "error": ""}

            def count(folder: Path) -> int:
                return sum(1 for n in names if (folder / n).is_dir())

            offered = entry["offered"] = str(given.get(root) or "").strip().strip('"')
            if offered:
                folder = Path(offered).expanduser()
                if not folder.is_dir():
                    entry["error"] = f"{offered} is not a folder this machine can open."
                elif names and not count(folder):
                    entry["error"] = (f"None of the {len(names)} folders {arr.name} lists under {root} "
                                      f"are in {offered}. Is it the right folder?")
                else:
                    maps = [m for m in maps if m[0].replace("\\", "/").rstrip("/").lower()
                            != root.replace("\\", "/").rstrip("/").lower()] + [[root, str(folder)]]
            local = Path(map_path(root, maps))
            if local.is_dir():
                same = str(local).replace("\\", "/").rstrip("/").lower() == root.replace("\\", "/").rstrip("/").lower()
                entry.update(local=str(local), state="same" if same else "mapped", found=count(local))
            out.append(entry)
        if maps != self.cfg.get("path_maps"):
            self.cfg["path_maps"] = maps[:50]
        return out

    def connect_arr(self, kind: str, url: str = "", key: str = "", given: dict | None = None,
                    reach: str | None = None) -> dict:
        """Everything the setup screen's Connect button does for Sonarr or Radarr."""
        if kind not in ("sonarr", "radarr"):
            raise KeyError("Not found.")
        url = (url or self.cfg.get(f"{kind}_url") or "").strip()
        key = (key or self.cfg.get(f"{kind}_key") or "").strip()
        arr = Arr(kind, url, key)
        if not arr.base or not key:
            raise ValueError(f"Enter {arr.name}'s address and API key first.")
        info = arr.status()  # ArrError if the address or key is wrong: nothing is saved
        self.cfg[f"{kind}_url"], self.cfg[f"{kind}_key"] = arr.base, key
        if reach is not None:
            self.cfg["reach_url"] = reach.strip().rstrip("/")
        folders = self._folders(arr, given or {})
        hook = {"ok": False, "state": "", "url": "", "error": ""}
        tried = self._reach(arr)
        far = (urlparse(arr.base).hostname or "").lower() not in ("localhost", "127.0.0.1", "::1")
        if far and not self.listen.get("open") and not self.cfg.get("reach_url"):
            hook["error"] = (f"Subtle only accepts connections from this computer, and {arr.name} is on another one. "
                             "Start Subtle with --host 0.0.0.0 (and --password), then connect again.")
        else:
            for base in tried:
                try:
                    state = arr.ensure_hook(f"{base}/webhook/{kind}?secret={self.cfg['webhook_secret']}")
                    hook.update(ok=True, state=state, url=base, error="")
                    break
                except ArrError as e:
                    hook.update(url=base, error=f"{arr.name} could not use {base} to reach Subtle: {e}")
            if not tried:
                hook["error"] = "Could not work out this machine's network address. Enter it below."
        config.save(self.cfg)
        return {**info, "kind": kind, "folders": folders, "hook": hook, "state": self.state()}

    def disconnect_arr(self, kind: str) -> dict:
        if kind not in ("sonarr", "radarr"):
            raise KeyError("Not found.")
        url, key, note = self.cfg.get(f"{kind}_url"), self.cfg.get(f"{kind}_key"), ""
        if url and key:
            try:
                Arr(kind, url, key).remove_hook()
            except ArrError as e:
                note = f"Its webhook could not be removed ({e}); delete it there under Settings, Connect."
        self.cfg[f"{kind}_url"] = self.cfg[f"{kind}_key"] = ""
        config.save(self.cfg)
        return {"note": note, "state": self.state()}

    def log(self, origin: str, file: str, status: str, note: str = "") -> dict:
        entry = {"t": time.time(), "origin": origin, "file": file, "status": status, "note": note, "cost": 0.0}
        with self.lock:
            self.activity.append(entry)
            del self.activity[:-200]
        self._touch(entry)
        return entry

    def _touch(self, entry: dict, **changes) -> None:
        entry.update(changes)
        with self.lock:
            tmp = config.home() / "activity.tmp"
            tmp.write_text(json.dumps(self.activity, ensure_ascii=False), "utf-8")
            tmp.replace(config.home() / "activity.json")
        self.publish({"type": "activity", "list": self.activity[-30:], "spent": round(self.auto_spent_today(), 4)})

    def auto_spent_today(self) -> float:
        midnight = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        return sum(e.get("cost", 0.0) for e in self.activity if e["t"] >= midnight)

    def webhook(self, kind: str, payload: dict) -> dict:
        origin = "Sonarr" if kind == "sonarr" else "Radarr"
        info = parse_webhook(kind, payload)
        if info is None:
            return {"ok": True, "action": "ignored"}
        if info["event"] == "test":
            self.log(origin, "", "test", "Connection test received.")
            return {"ok": True, "action": "test"}
        threading.Thread(target=self._automate, args=(origin, info), daemon=True).start()
        return {"ok": True, "action": "queued"}

    def _automate(self, origin: str, info: dict) -> None:
        """An import was reported: translate it unless subtitles exist or Bazarr can supply them."""
        local = Path(map_path(info["path"], self.cfg.get("path_maps")))
        entry = self.log(origin, local.name, "checking")
        try:
            code, lang = self.cfg["target_code"], self.cfg["target_name"]
            if not code:
                return self._touch(entry, status="skipped", note="No language to translate into has been chosen.")
            if local.suffix.lower() not in media.VIDEO_EXT:
                return self._touch(entry, status="skipped", note="Not a video file.")
            if not local.is_file():
                return self._touch(entry, status="error", note=f"Cannot open {local}. Check the path mapping.")
            tracks = None
            if media.available():
                try:
                    tracks = media.probe(local)
                except media.MediaError:
                    tracks = None
            why = media.has_target(local, code, tracks)
            if why:
                return self._touch(entry, status="skipped", note=f"Nothing to do: {why}.")
            note = ""
            bazarr, ref = self.bazarr(), info.get("ref")
            if bazarr and ref:
                try:
                    if self._bazarr_first(bazarr, ref, local, code, entry):
                        return self._touch(entry, status="bazarr", note=f"Bazarr supplied {lang} subtitles.")
                except ArrError as e:
                    note = f" (Bazarr could not be asked: {e})"
            budget = float(self.cfg.get("auto_daily_budget") or 0)
            if budget and self.auto_spent_today() >= budget:
                return self._touch(entry, status="skipped",
                                   note=f"Today's limit for automatic translations (${budget:.2f}) is used up.")
            name, data, beside, how = media.source_for(local, tracks=tracks)
            job = self.add(name, data, beside, f"{origin} · {how}")
            job.auto, job.ref, job.entry = True, ref, entry
            self._touch(entry, status="translating", note=how + note)
            self.start([job.id])
        except (media.MediaError, ValueError, OSError) as e:
            self._touch(entry, status="error", note=str(e))
        except Exception as e:  # a webhook must never take the server down
            self._touch(entry, status="error", note=f"{type(e).__name__}: {e}")

    def _bazarr_first(self, bazarr: Bazarr, ref: dict, local: Path, code: str, entry: dict) -> bool:
        """Give Bazarr its chance. True if the target-language subtitles now exist."""
        # A file imported seconds ago may not have reached Bazarr yet: allow it a short moment.
        learn_by = time.time() + self.bazarr_sync_wait
        known = bazarr.has(ref, code)
        while known is None and time.time() < learn_by:
            time.sleep(self.bazarr_poll)
            known = bazarr.has(ref, code)
        if known:
            return True
        if known is False:
            bazarr.search(ref, code)  # returns when Bazarr has finished looking
            if bazarr.has(ref, code) or media.has_target(local, code):
                return True
        deadline = time.time() + int(self.cfg.get("bazarr_wait") or 0) * 60
        while time.time() < deadline:
            until = time.strftime("%H:%M", time.localtime(deadline))
            self._touch(entry, status="waiting", note=f"Bazarr has until {until} to find subtitles.")
            time.sleep(max(0.01, min(60, deadline - time.time())))
            if media.has_target(local, code) or bazarr.has(ref, code):
                return True
        return False

    def _after_auto(self, job: Job) -> None:
        entry = job.entry if job.entry is not None else self.log("Automation", job.name, "checking")
        if job.status != "done":
            return self._touch(entry, status="error", note=job.error or "Stopped.", cost=job.spent["cost"])
        note = f"Saved {Path(job.saved_to).name}" if job.saved_to else "Translated but not saved"
        flagged = sum(1 for c in job.doc.cues if c.flags)
        if flagged:
            note += f", {flagged} lines to check"
        bazarr = self.bazarr()
        if bazarr and job.ref:
            try:
                bazarr.rescan(job.ref)
            except ArrError as e:
                note += f" (Bazarr rescan failed: {e})"
        self._touch(entry, status="translated", note=note + ".", cost=job.spent["cost"])

    # --- running ------------------------------------------------------------

    def start(self, ids: list[str] | None = None) -> int:
        if not self.cfg["target_code"]:
            raise ValueError("Choose a language to translate into first.")
        n = 0
        for job in list(self.jobs.values()):
            if (ids is None or job.id in ids) and job.status in ("ready", "stopped", "error"):
                job.status, job.error, job.cancel = "queued", "", False
                self._announce(job)
                self.pool.submit(self._run, job)
                n += 1
        return n

    def _run(self, job: Job) -> None:
        if job.cancel or job.id not in self.jobs:
            job.status, job.cancel = "stopped", False
            self._announce(job)
            return
        opts = self.options(job)
        job.status = "running"
        job.used = {"target_code": opts.target_code, "target_name": opts.target_name, "model": opts.model}
        job.progress = [0, 0]
        base = dict(job.spent)
        self._announce(job)

        def emit(event: dict) -> None:
            if event["type"] == "progress":
                job.progress = [event["done"], event["total"]]
                job.spent = {"cost": base["cost"] + event["cost"], "inp": base["inp"] + event["inp"],
                             "out": base["out"] + event["out"], "requests": base["requests"] + event["requests"]}
                self._announce(job)
            elif event["type"] == "cue":
                self.publish({"type": "cue", "job": job.id, "i": event["i"], "text": event["text"]})

        tr = None
        try:
            tr = Translator(job.doc, opts, self.provider(), self.cache, emit, lambda: job.cancel)
            tr.run()
            job.status = "done"
        except Cancelled:
            job.status, job.error = "stopped", "Stopped. Start again to resume; finished parts are cached."
        except BudgetExceeded as e:
            job.status, job.error = "stopped", str(e)
        except ProviderError as e:
            job.status, job.error = "error", str(e)
        except Exception as e:  # keep the queue alive whatever a single file does
            job.status, job.error = "error", f"{type(e).__name__}: {e}"
        finally:
            job.cancel = False
            if tr:
                job.last = asdict(tr.stats)
            if job.status == "done":
                self._autosave(job)
            self._save(job)
            self._announce(job)
            if job.auto:
                self._after_auto(job)

    def _autosave(self, job: Job) -> None:
        """Write name.<lang>.srt beside the source, for files that were added from disk."""
        wanted = self.cfg.get("save_next_to_source") or job.auto
        if not (job.src_path and wanted and job.used.get("target_code")):
            return
        try:
            out = Path(job.src_path).with_name(formats.output_name(job.name, job.used["target_code"]))
            if out != Path(job.src_path):
                out.write_text(formats.render(job.doc, bool(self.cfg.get("bilingual"))), "utf-8")
                job.saved_to = str(out)
        except OSError as e:
            job.error = f"Translated, but could not save next to the source: {e}"

    def stop(self, job: Job) -> None:
        if job.status in ("queued", "running"):
            job.cancel = True

    def reset(self, job: Job) -> None:
        if job.status in ("queued", "running"):
            raise ValueError("Stop it first.")
        for cue in job.doc.cues:
            cue.translation, cue.flags, cue.edited = None, [], False
        job.status, job.error, job.progress, job.saved_to = "ready", "", [0, 0], ""
        job.est = estimate(job.doc, self.options(job))
        self._save(job)
        self._announce(job)

    def remove(self, job: Job) -> None:
        job.cancel = True
        with self.lock:
            self.jobs.pop(job.id, None)
        (self.dir / f"{job.id}.json").unlink(missing_ok=True)
        self.publish({"type": "removed", "id": job.id})

    def clear(self, which: str) -> None:
        for job in list(self.jobs.values()):
            if which == "all" or job.status == "done":
                if job.status not in ("queued", "running"):
                    self.remove(job)

    # --- reviewing ----------------------------------------------------------

    def _idle(self, job: Job) -> None:
        if job.status in ("queued", "running") or job.busy:
            raise ValueError("This file is being worked on. Try again in a moment.")

    def edit(self, job: Job, i: int, text: str) -> dict:
        self._idle(job)
        cue = job.doc.cues[i]
        text = "\n".join(l.rstrip() for l in text.replace("\r", "").strip().split("\n"))
        if text != (cue.translation or ""):
            o = self.options(job, True)
            cue.translation, cue.edited = text, True
            cue.flags = qa.check(cue, o.target_code, job.doc.fmt, o.max_chars, o.max_cps)
            self._save(job)
            self._autosave(job) if job.saved_to else None
            self._announce(job)
        return job.row(i)

    def flagged(self, job: Job) -> tuple[list[int], list[int]]:
        """(cues to translate again, cues to shorten) among the flagged ones a human has not touched."""
        again, shorter = [], []
        for i, cue in enumerate(job.doc.cues):
            if cue.edited or not cue.flags:
                continue
            if set(cue.flags) & {"failed", "untranslated", "english"}:
                again.append(i)
            elif set(cue.flags) & {"fast", "long"}:
                shorter.append(i)
        return again, shorter

    def rework(self, job: Job, cues: list[int], instruction: str = "", condense: bool = False,
               also_condense: list[int] | None = None) -> None:
        """Re-translate some cues in the background; results arrive as events."""
        self._idle(job)
        if not job.used.get("target_code") and not self.cfg["target_code"]:
            raise ValueError("Choose a language to translate into first.")
        job.busy = True
        self._announce(job)
        batches = [(cues, condense)] + ([(also_condense, True)] if also_condense else [])

        def work() -> None:
            error = ""
            try:
                opts = self.options(job, True)
                for ids, short in batches:
                    if not ids:
                        continue

                    def emit(event: dict) -> None:
                        self.publish({"type": "cue", "job": job.id, "row": job.row(event["i"])})

                    stats = retranslate(job.doc, ids, opts, self.provider(), instruction, short, emit)
                    for k in ("cost", "inp", "out", "requests"):
                        job.spent[k] += getattr(stats, k)
            except (ProviderError, BudgetExceeded) as e:
                error = str(e)
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
            finally:
                job.busy = False
                self._save(job)
                if job.saved_to:
                    self._autosave(job)
                self.publish({"type": "reworked", "id": job.id, "cues": [i for ids, _ in batches for i in ids or []],
                              "error": error})
                self._announce(job)

        threading.Thread(target=work, daemon=True).start()

    def rendered(self, job: Job, bilingual: bool = False) -> tuple[str, str]:
        code = job.used.get("target_code") or self.cfg["target_code"] or "xx"
        return formats.output_name(job.name, code), formats.render(job.doc, bilingual)


_JOB = re.compile(r"^/api/jobs/([0-9a-f]{6,32})(?:/([a-z-]+)(?:/(\d+))?)?$")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Subtle"
    mgr: Manager = None  # set by serve()
    open_host = False
    password = ""  # when set, the UI and API ask for it (HTTP Basic); webhooks use their own secret

    def log_message(self, *args) -> None:  # quiet
        pass

    # --- helpers ------------------------------------------------------------

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") or "json" in ctype else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json")

    def _allowed(self, mutating: bool) -> bool:
        host = self.headers.get("Host") or ""
        name = host[1:host.index("]")] if host.startswith("[") and "]" in host else host.rsplit(":", 1)[0]
        if not self.open_host and name.lower() not in ("localhost", "127.0.0.1", "::1"):
            return False  # DNS rebinding
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != host:
            return False  # another site's page
        return not mutating or self.headers.get("X-Subtle") == "1"

    def _authorised(self) -> bool:
        if not self.password:
            return True
        head = self.headers.get("Authorization") or ""
        try:
            supplied = base64.b64decode(head[6:]).decode("utf-8").split(":", 1)[1] if head.startswith("Basic ") else ""
        except (ValueError, IndexError):
            supplied = ""
        return hmac.compare_digest(supplied.encode("utf-8"), self.password.encode("utf-8"))

    def _refuse(self, code: int, message: str, extra: dict | None = None) -> None:
        """Turn a request away. Its body is taken off the wire first, or the reply never arrives."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if 0 < length <= MAX_UPLOAD:
            self.rfile.read(length)
        elif length:
            self.close_connection = True
        self._send(code, json.dumps({"error": message}).encode("utf-8"), "application/json", extra)

    def _webhook(self, url) -> None:
        kind = url.path.rsplit("/", 1)[-1]
        given = (parse_qs(url.query).get("secret") or [self.headers.get("X-Webhook-Secret") or ""])[0]
        expected = self.mgr.cfg.get("webhook_secret") or ""
        if self.command != "POST" or kind not in ("sonarr", "radarr"):
            return self._refuse(404, "Not found.")
        if not expected or not hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8")):
            return self._refuse(403, "Wrong or missing secret.")
        self._json(self.mgr.webhook(kind, self._payload()))

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            raise ValueError("That file is too large to be a subtitle file.")
        return self.rfile.read(length) if length else b""

    def _payload(self) -> dict:
        raw = self._body()
        data = json.loads(raw.decode("utf-8")) if raw else {}
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object.")
        return data

    def _attachment(self, name: str, body: bytes, ctype: str) -> None:
        ascii_name = name.encode("ascii", "replace").decode().replace('"', "'")
        self._send(200, body, ctype, {
            "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"})

    # --- routing ------------------------------------------------------------

    def do_GET(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        self._route("POST")

    def do_DELETE(self) -> None:
        self._route("DELETE")

    def _route(self, method: str) -> None:
        url = urlparse(self.path)
        try:
            if url.path.startswith("/webhook/"):
                return self._webhook(url)
            if not self._authorised():
                return self._refuse(401, "Password required.", {"WWW-Authenticate": 'Basic realm="Subtle"'})
            if not self._allowed(method != "GET"):
                return self._refuse(403, "Request refused.")
            if method == "GET" and url.path in STATIC:
                fname, ctype = STATIC[url.path]
                return self._send(200, (WEB / fname).read_bytes(), ctype)
            if url.path == "/api/events" and method == "GET":
                return self._events()
            result = self._api(method, url.path, parse_qs(url.query))
            if result is not None:
                self._json(result)
        except KeyError as e:
            self._json({"error": str(e).strip("'\"")}, 404)
        except (ValueError, IndexError) as e:
            self._json({"error": str(e) or "Bad request."}, 400)
        except (ProviderError, ArrError) as e:
            self._json({"error": str(e)}, 502)
        except media.MediaError as e:
            self._json({"error": str(e)}, 400)
        except (BrokenPipeError, ConnectionError):
            pass

    def _api(self, method: str, path: str, query: dict):
        mgr = self.mgr
        if method == "GET":
            if path == "/api/state":
                return mgr.state()
            if path == "/api/download.zip":
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                    seen: set[str] = set()
                    for job in mgr.jobs.values():
                        if job.status == "done":
                            name, text = mgr.rendered(job, bool(mgr.cfg.get("bilingual")))
                            while name in seen:
                                name = "_" + name
                            seen.add(name)
                            z.writestr(name, text)
                return self._attachment("subtitles.zip", buf.getvalue(), "application/zip")
            m = _JOB.match(path)
            if m and not m.group(2):
                return mgr.get(m.group(1)).detail()
            if m and m.group(2) == "download":
                name, text = mgr.rendered(mgr.get(m.group(1)), query.get("bilingual", ["0"])[0] == "1")
                return self._attachment(name, text.encode("utf-8"), "text/plain")
            raise KeyError("Not found.")

        if method == "DELETE":
            m = _JOB.match(path)
            if m and not m.group(2):
                mgr.remove(mgr.get(m.group(1)))
                return {"ok": True}
            raise KeyError("Not found.")

        if path == "/api/upload":
            name = (query.get("name") or ["subtitles.srt"])[0]
            return mgr.add(name, self._body()).summary()
        data = self._payload()
        if path == "/api/config":
            return mgr.set_config(data)
        if path == "/api/scan":
            return mgr.scan(str(data.get("path", "")))
        if path == "/api/add-paths":
            return mgr.add_paths([str(p) for p in data.get("paths", [])])
        if path == "/api/bazarr/test":
            url, key = str(data.get("url") or mgr.cfg["bazarr_url"]), str(data.get("key") or mgr.cfg["bazarr_key"])
            if not url or not key:
                raise ValueError("Enter Bazarr's address and API key first.")
            return {"version": Bazarr(url, key).version()}
        if path == "/api/arr/connect":
            given = data.get("local") if isinstance(data.get("local"), dict) else {}
            reach = str(data["reach"]) if isinstance(data.get("reach"), str) else None
            return mgr.connect_arr(str(data.get("kind", "")), str(data.get("url") or ""), str(data.get("key") or ""),
                                   given, reach)
        if path == "/api/arr/disconnect":
            return mgr.disconnect_arr(str(data.get("kind", "")))
        if path == "/api/start":
            return {"started": mgr.start(data.get("ids"))}
        if path == "/api/clear":
            mgr.clear(str(data.get("which", "done")))
            return {"ok": True}
        if path == "/api/cache/clear":
            return {"removed": mgr.cache.clear()}
        m = _JOB.match(path)
        if not m:
            raise KeyError("Not found.")
        job, action = mgr.get(m.group(1)), m.group(2)
        if action == "start":
            return {"started": mgr.start([job.id])}
        if action == "cancel":
            mgr.stop(job)
        elif action == "reset":
            mgr.reset(job)
        elif action == "cue":
            return mgr.edit(job, int(m.group(3)), str(data.get("text", "")))
        elif action == "retranslate":
            cues = [int(i) for i in data.get("cues", [])][:400]
            mgr.rework(job, cues, str(data.get("instruction", ""))[:300], bool(data.get("condense")))
        elif action == "fix":
            again, shorter = mgr.flagged(job)
            if not again and not shorter:
                raise ValueError("Nothing here can be fixed automatically.")
            mgr.rework(job, again, also_condense=shorter)
            return {"again": len(again), "shorter": len(shorter)}
        else:
            raise KeyError("Not found.")
        return {"ok": True}

    def _events(self) -> None:
        q = self.mgr.subscribe()
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    batch = [q.get(timeout=15)]
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                while len(batch) < 300:
                    try:
                        batch.append(q.get_nowait())
                    except queue.Empty:
                        break
                self.wfile.write(b"data: " + json.dumps(batch, ensure_ascii=False).encode("utf-8") + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionError, OSError):
            pass
        finally:
            self.mgr.unsubscribe(q)


def serve(host: str = "127.0.0.1", port: int = 8756, open_browser: bool = True, mock: bool = False,
          password: str = "") -> int:
    Handler.mgr = Manager(mock)
    Handler.open_host = host not in ("127.0.0.1", "localhost", "::1")
    Handler.password = password or os.environ.get("SUBTLE_PASSWORD", "")
    Handler.mgr.listen = {"open": Handler.open_host, "port": port}
    ThreadingHTTPServer.daemon_threads = True
    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        print(f"Could not listen on {host}:{port} ({e}). Try --port with another number.")
        return 1
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    url = f"http://{shown}:{port}"
    print(f"Subtle {__version__} is running at {url}" + ("  (mock mode: no API calls)" if Handler.mgr.mock else ""))
    if Handler.open_host and not Handler.password:
        print("Listening beyond this machine with no password. Anyone who can reach this port can use it;")
        print("add --password to require one.")
    if not media.available():
        print("ffmpeg was not found: subtitle files work, subtitles inside video files do not.")
    print("Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.4, webbrowser.open, [url]).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
    return 0
