"""Sonarr and Radarr webhooks and library listings, and the Bazarr API.

Sonarr/Radarr say "this file was just imported" and list what is in the library. Bazarr is
asked first whether it can find real subtitles in the target language, and told to rescan
once a translation is written.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class ArrError(Exception):
    pass


def _join(base: str, rel: str) -> str:
    if not base or not rel:
        return ""
    sep = "\\" if "\\" in base and "/" not in base else "/"
    return base.rstrip("/\\") + sep + rel.lstrip("/\\")


def parse_webhook(kind: str, payload: dict) -> dict | None:
    """Reduce a webhook body to what matters. None for events that need no action."""
    event = str(payload.get("eventType", ""))
    if event == "Test":
        return {"event": "test"}
    if event != "Download":  # Download covers both a first import and an upgrade
        return None
    if kind == "sonarr":
        series, file = payload.get("series") or {}, payload.get("episodeFile") or {}
        episodes = payload.get("episodes") or [{}]
        path = file.get("path") or _join(series.get("path", ""), file.get("relativePath", ""))
        ref = {"kind": "episode", "series": series.get("id"), "id": episodes[0].get("id")}
    else:
        movie, file = payload.get("movie") or {}, payload.get("movieFile") or {}
        path = file.get("path") or _join(movie.get("folderPath", ""), file.get("relativePath", ""))
        ref = {"kind": "movie", "id": movie.get("id")}
    if not path:
        return None
    return {"event": "import", "path": path, "ref": ref if ref.get("id") is not None else None}


def map_path(path: str, maps: list) -> str:
    """Translate a path as Sonarr/Radarr see it into one this machine can open."""
    p = path.replace("\\", "/")
    for remote, local in sorted((m for m in maps or [] if len(m) == 2 and m[0] and m[1]),
                                key=lambda m: -len(m[0])):
        r = str(remote).replace("\\", "/").rstrip("/")
        if p.lower() == r.lower() or p.lower().startswith(r.lower() + "/"):
            rest = [part for part in p[len(r):].split("/") if part]
            return str(Path(str(local)).joinpath(*rest))
    return path


def child_of(root: str, path: str) -> str:
    """The first folder name below `root` in `path` ('' if path is not under root). Either slash style."""
    r, p = root.replace("\\", "/").rstrip("/"), path.replace("\\", "/")
    if not p.lower().startswith(r.lower() + "/"):
        return ""
    return p[len(r):].strip("/").split("/")[0]


class Arr:
    """Sonarr or Radarr: the few v3 API calls that let Subtle connect itself.

    It checks the address and key, reads where the library is, and adds (or updates, or
    removes) its own webhook, so nobody has to click through Settings -> Connect.
    Routes and field names follow Sonarr.Api.V3 and Radarr.Api.V3 in their source.
    """

    HOOK = "Subtle"

    def __init__(self, kind: str, url: str, key: str):
        self.kind = kind
        self.name = "Sonarr" if kind == "sonarr" else "Radarr"
        base = url.strip().rstrip("/")
        if base and "://" not in base:
            base = "http://" + base
        for tail in ("/api/v3", "/api"):
            if base.lower().endswith(tail):
                base = base[:-len(tail)]
        self.base, self.key = base, key.strip()

    def _call(self, method: str, path: str, body=None, timeout: int = 30):
        req = urllib.request.Request(
            f"{self.base}/api/v3/{path}", method=method,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={"X-Api-Key": self.key, "Accept": "application/json", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                raw = res.read()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise ArrError(f"{self.name} rejected the API key.") from e
            raise ArrError(self._failures(e.read()) or f"{self.name} answered {e.code} to {path}.") from e
        except TimeoutError as e:
            raise ArrError(f"{self.name} at {self.base} did not answer in {timeout} seconds.") from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            if isinstance(getattr(e, "reason", None), TimeoutError):
                raise ArrError(f"{self.name} at {self.base} did not answer in {timeout} seconds.") from e
            raise ArrError(f"Could not reach {self.name} at {self.base} ({getattr(e, 'reason', e)}).") from e
        try:
            return json.loads(raw) if raw.strip() else None
        except ValueError as e:
            raise ArrError(f"{self.base} answered, but not like {self.name} does. Is that the right address?") from e

    @staticmethod
    def _failures(raw: bytes) -> str:
        """The messages out of a validation error body: [{"propertyName": ..., "errorMessage": ...}]."""
        try:
            data = json.loads(raw)
        except ValueError:
            return ""
        items = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
        found = [str(i.get("errorMessage") or i.get("message") or "") for i in items if isinstance(i, dict)]
        return " ".join(m for m in found if m)[:400]

    def status(self) -> dict:
        data = self._call("GET", "system/status")
        if not isinstance(data, dict) or "version" not in data:
            raise ArrError(f"{self.base} answered, but not like {self.name} does. Is that the right address?")
        app = str(data.get("appName") or self.name)
        if app.lower() != self.name.lower():
            raise ArrError(f"That address is {app}, not {self.name}.")
        return {"app": app, "version": str(data.get("version", ""))}

    def roots(self) -> list[str]:
        """The library folders, as this app names them."""
        return [str(r["path"]) for r in self._call("GET", "rootfolder") or [] if isinstance(r, dict) and r.get("path")]

    def folders(self) -> list[str]:
        """The folder of every series or movie, as this app names them."""
        items = self._call("GET", "series" if self.kind == "sonarr" else "movie", timeout=90) or []
        return [str(i["path"]) for i in items if isinstance(i, dict) and i.get("path")]

    @staticmethod
    def _inside(file: dict | None) -> list[str] | None:
        """Languages of the subtitle tracks inside a file, as the app's own scan lists them ("eng/fin").

        None when the app has not scanned the file, which is not the same as finding no tracks.
        """
        info = (file or {}).get("mediaInfo")
        if not isinstance(info, dict):
            return None
        return [code.strip().lower() for code in str(info.get("subtitles") or "").split("/") if code.strip()]

    def titles(self) -> list[dict]:
        """Every show (Sonarr) or movie (Radarr): id, title, year, and for a movie its file."""
        out = []
        for i in self._call("GET", "series" if self.kind == "sonarr" else "movie", timeout=90) or []:
            if not isinstance(i, dict) or i.get("id") is None:
                continue
            item = {"id": int(i["id"]), "title": str(i.get("title") or ""), "year": i.get("year") or None}
            if self.kind == "sonarr":
                item["files"] = int((i.get("statistics") or {}).get("episodeFileCount") or 0)
            else:
                file = i.get("movieFile") if isinstance(i.get("movieFile"), dict) else None
                item["path"] = str((file or {}).get("path") or _join(str(i.get("path") or ""),
                                                                    str((file or {}).get("relativePath") or "")))
                item["inside"] = self._inside(file)
            out.append(item)
        return sorted(out, key=lambda x: x["title"].lower())

    def episodes(self, series: int) -> list[dict]:
        """The episodes of one show that have a file: id, season, number, title, path, languages inside."""
        files = {f["id"]: f for f in self._call("GET", f"episodefile?seriesId={int(series)}", timeout=90) or []
                 if isinstance(f, dict) and f.get("id") is not None}
        out = []
        for e in self._call("GET", f"episode?seriesId={int(series)}", timeout=90) or []:
            file = files.get(e.get("episodeFileId")) if isinstance(e, dict) else None
            if not file or not file.get("path"):
                continue
            out.append({"id": int(e["id"]), "season": int(e.get("seasonNumber") or 0),
                        "number": int(e.get("episodeNumber") or 0), "title": str(e.get("title") or ""),
                        "path": str(file["path"]), "inside": self._inside(file)})
        return sorted(out, key=lambda x: (x["season"], x["number"]))

    def _hook(self) -> dict | None:
        for n in self._call("GET", "notification") or []:
            if isinstance(n, dict) and n.get("implementation") == "Webhook" and str(n.get("name", "")).lower() == self.HOOK.lower():
                return n
        return None

    @staticmethod
    def _field(resource: dict, name: str):
        return next((f.get("value") for f in resource.get("fields") or [] if f.get("name") == name), None)

    def hook_url(self) -> str:
        """Where the webhook this class added currently points ('' if there is none)."""
        hook = self._hook()
        return str(self._field(hook, "url") or "") if hook else ""

    def ensure_hook(self, url: str) -> str:
        """Add the webhook, or point the existing one at `url`. Returns added | updated | kept.

        The app tests the address before saving: it sends a test event to `url` and refuses
        (ArrError) if nothing answers there. A stored webhook is therefore one that works.
        """
        hook = self._hook()
        if hook and self._field(hook, "url") == url and hook.get("onDownload") and hook.get("onUpgrade"):
            return "kept"
        if hook is None:
            templates = self._call("GET", "notification/schema") or []
            hook = next((dict(t) for t in templates if isinstance(t, dict) and t.get("implementation") == "Webhook"), None)
            if hook is None:
                raise ArrError(f"This {self.name} offers no Webhook connection.")
            hook.pop("presets", None)
            hook.pop("id", None)
            hook["name"] = self.HOOK
            for key in [k for k in hook if k.startswith("on") and isinstance(hook[k], bool)]:
                hook[key] = False  # only imports and upgrades are of interest
        hook["onDownload"] = hook["onUpgrade"] = True
        hook["enable"] = True  # ignored by versions that have no such switch
        hook["fields"] = [{**f, "value": url} if f.get("name") == "url" else f for f in hook.get("fields") or []]
        if hook.get("id"):
            self._call("PUT", f"notification/{hook['id']}", hook, 90)
            return "updated"
        self._call("POST", "notification", hook, 90)
        return "added"

    def remove_hook(self) -> bool:
        hook = self._hook()
        if hook and hook.get("id"):
            self._call("DELETE", f"notification/{hook['id']}")
            return True
        return False


# Bazarr's own two-letter codes where they differ from ours.
_BAZARR_CODES = {"pt-br": "pb", "zh-tw": "zt", "es-419": "ea"}


class Bazarr:
    """The handful of Bazarr API calls needed. Paths and parameters follow bazarr/api in its source."""

    def __init__(self, url: str, key: str):
        base = url.strip().rstrip("/")
        self.base = base[:-4] if base.lower().endswith("/api") else base
        self.key = key.strip()

    def _call(self, method: str, path: str, params: dict, timeout: int = 30):
        url = f"{self.base}/api/{path}?{urllib.parse.urlencode(params, doseq=True)}"
        req = urllib.request.Request(url, method=method, headers={"X-API-KEY": self.key, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                body = res.read()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise ArrError("Bazarr rejected the API key.") from e
            raise ArrError(f"Bazarr answered {e.code} to {path}.") from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise ArrError(f"Could not reach Bazarr at {self.base} ({getattr(e, 'reason', e)}).") from e
        try:
            return json.loads(body) if body.strip() else None
        except ValueError:
            return None

    @staticmethod
    def code(target: str) -> str:
        return _BAZARR_CODES.get(target.lower(), target.split("-")[0].lower())

    def version(self) -> str:
        data = self._call("GET", "system/status", {}) or {}
        return str((data.get("data") or {}).get("bazarr_version", "")) if isinstance(data, dict) else ""

    def has(self, ref: dict, target: str) -> bool | None:
        """Does Bazarr list subtitles in this language? None if it does not know the item (yet)."""
        if ref["kind"] == "episode":
            data = self._call("GET", "episodes", {"episodeid[]": ref["id"]})
        else:
            data = self._call("GET", "movies", {"radarrid[]": ref["id"]})
        items = (data or {}).get("data") if isinstance(data, dict) else None
        if not items:
            return None
        want = self.code(target)
        for sub in items[0].get("subtitles") or []:
            if str(sub.get("code2", "")).lower() == want and not sub.get("forced"):
                return True
        return False

    def search(self, ref: dict, target: str) -> None:
        """Ask Bazarr to look for and download subtitles now. It answers once it has finished."""
        params = {"language": self.code(target), "forced": "false", "hi": "false"}
        if ref["kind"] == "episode":
            self._call("PATCH", "episodes/subtitles", {"seriesid": ref["series"], "episodeid": ref["id"], **params}, 300)
        else:
            self._call("PATCH", "movies/subtitles", {"radarrid": ref["id"], **params}, 300)

    def rescan(self, ref: dict) -> None:
        """Have Bazarr notice a subtitle file that was just written next to the video."""
        if ref["kind"] == "episode":
            self._call("PATCH", "series", {"seriesid": ref["series"], "action": "scan-disk"}, 120)
        else:
            self._call("PATCH", "movies", {"radarrid": ref["id"], "action": "scan-disk"}, 120)
