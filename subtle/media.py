"""Find the subtitles to translate for a video file.

A subtitle file sitting next to the video wins; otherwise a text track inside the
container is pulled out with ffmpeg. Needs ffmpeg and ffprobe on the PATH for the latter.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .languages import variants

VIDEO_EXT = (".mkv", ".mp4", ".m4v", ".webm", ".mov", ".avi")
SUB_EXT = (".srt", ".vtt", ".ass", ".ssa")
TEXT_CODECS = {"subrip": "SubRip", "srt": "SubRip", "ass": "ASS", "ssa": "SSA", "webvtt": "WebVTT",
               "mov_text": "MP4 text", "text": "text"}
IMAGE_CODECS = {"hdmv_pgs_subtitle": "PGS", "dvd_subtitle": "VobSub", "dvb_subtitle": "DVB", "xsub": "XSUB"}
_SDH = re.compile(r"\b(sdh|cc|hearing|hi)\b", re.I)
_FORCED = re.compile(r"\b(forced|signs?|songs?)\b", re.I)
_HINTS = {"sdh", "hi", "cc"}


class MediaError(Exception):
    pass


_found: dict = {"at": 0.0, "tools": (None, None)}


def _elsewhere() -> list[Path]:
    """Places ffmpeg may live that are not on this process's PATH.

    A program started before ffmpeg was installed keeps its old PATH, so a fresh
    `winget install ffmpeg` would otherwise go unnoticed until every terminal is reopened.
    """
    dirs: list[Path] = []
    override = os.environ.get("SUBTLE_FFMPEG")
    if override:
        dirs.append(Path(override) if Path(override).is_dir() else Path(override).parent)
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        dirs += sorted(local.glob("Microsoft/WinGet/Packages/Gyan.FFmpeg*/*/bin"), reverse=True)
        dirs += [local / "Microsoft" / "WinGet" / "Links", Path("C:/ffmpeg/bin"),
                 Path(os.environ.get("ProgramFiles") or "C:/Program Files") / "ffmpeg" / "bin"]
    return dirs


def tools() -> tuple[str | None, str | None]:
    """Paths of ffmpeg and ffprobe, or None for whichever is missing."""
    if all(_found["tools"]) or time.time() - _found["at"] < 10:
        return _found["tools"]
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not (ffmpeg and ffprobe):
        for folder in _elsewhere():
            a, b = shutil.which("ffmpeg", path=str(folder)), shutil.which("ffprobe", path=str(folder))
            if a and b:
                ffmpeg, ffprobe = a, b
                break
    _found.update(at=time.time(), tools=(ffmpeg, ffprobe))
    return ffmpeg, ffprobe


def available() -> bool:
    return all(tools())


def run(cmd: list[str], timeout: int) -> bytes:
    """Run ffmpeg/ffprobe and return stdout. Kept separate so tests can stand in for it."""
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
                              creationflags=flags)
    except subprocess.TimeoutExpired as e:
        raise MediaError(f"{Path(cmd[0]).stem} took longer than {timeout} s and was stopped.") from e
    except OSError as e:
        raise MediaError(f"Could not run {Path(cmd[0]).name}: {e}") from e
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or ["no error message"]
        raise MediaError(f"{Path(cmd[0]).stem} failed: {tail[0][:200]}")
    return proc.stdout


def probe(path: Path | str) -> list[dict]:
    """The subtitle tracks inside a video file."""
    ffprobe = tools()[1]
    if not ffprobe:
        raise MediaError("ffmpeg is not installed, so subtitles inside video files cannot be read.")
    out = run([ffprobe, "-v", "error", "-print_format", "json", "-show_streams", "-select_streams", "s",
               str(path)], 60)
    try:
        streams = json.loads(out or b"{}").get("streams", [])
    except ValueError as e:
        raise MediaError("ffprobe returned something unreadable.") from e
    tracks = []
    for s in streams:
        tags = {k.lower(): str(v) for k, v in (s.get("tags") or {}).items()}
        disp = s.get("disposition") or {}
        title = tags.get("title", "")
        frames = next((v for k, v in tags.items() if k.startswith("number_of_frames")), "0")
        tracks.append({
            "index": s.get("index"), "codec": s.get("codec_name", ""), "lang": tags.get("language", "").lower(),
            "title": title, "default": bool(disp.get("default")),
            "forced": bool(disp.get("forced")) or bool(_FORCED.search(title)),
            "sdh": bool(disp.get("hearing_impaired")) or bool(_SDH.search(title)),
            "frames": int(frames) if frames.isdigit() else 0,
        })
    return tracks


def describe(track: dict) -> str:
    kind = TEXT_CODECS.get(track["codec"]) or IMAGE_CODECS.get(track["codec"]) or track["codec"] or "unknown"
    return f"{track['lang'] or 'unlabelled'} {kind}" + (" forced" if track["forced"] else "")


def pick(tracks: list[dict], source: str = "en") -> dict:
    """The best track to translate from: full dialogue, in the source language, as text."""
    want = variants(source)
    text = [t for t in tracks if t["codec"] in TEXT_CODECS]
    matches = [t for t in text if t["lang"] in want]
    if not matches:  # a single unlabelled text track is almost always the one
        unlabelled = [t for t in text if t["lang"] in ("", "und")]
        matches = unlabelled if len(unlabelled) == 1 or (unlabelled and not any(t["lang"] in want for t in tracks)) else []
    if not matches:
        if not tracks:
            raise MediaError("There are no subtitle tracks inside this file.")
        found = ", ".join(describe(t) for t in tracks)
        if any(t["lang"] in want and t["codec"] in IMAGE_CODECS for t in tracks):
            raise MediaError(f"The only matching subtitles are pictures, not text, so they cannot be translated ({found}).")
        raise MediaError(f"No text subtitles in the source language inside this file (it has: {found}).")
    return max(matches, key=lambda t: (not t["forced"], not t["sdh"], t["frames"], t["default"], -t["index"]))


def extract(path: Path | str, track: dict) -> bytes:
    """One text track as SRT."""
    ffmpeg = tools()[0]
    if not ffmpeg:
        raise MediaError("ffmpeg is not installed, so subtitles inside video files cannot be read.")
    data = run([ffmpeg, "-nostdin", "-v", "error", "-i", str(path), "-map", f"0:{track['index']}",
                "-c:s", "srt", "-f", "srt", "pipe:1"], 900)
    if not data.strip():
        raise MediaError("The subtitle track inside this file is empty.")
    return data


def sidecars(video: Path) -> list[tuple[Path, list[str]]]:
    """Subtitle files that belong to this video, with the tags between its name and the extension."""
    out = []
    prefix = video.stem.lower() + "."
    try:
        siblings = sorted(video.parent.iterdir())
    except OSError:
        return out
    for p in siblings:
        if p.suffix.lower() in SUB_EXT and p.name.lower().startswith(prefix) and p.is_file():
            out.append((p, [t for t in p.name[len(prefix):].lower().split(".")[:-1] if t]))
    return out


def find_sidecar(video: Path, source: str = "en") -> Path | None:
    """A subtitle file next to the video in the source language (or with no language tag at all)."""
    want = variants(source)
    best = None
    for path, tags in sidecars(video):
        if any(t not in want and t not in _HINTS for t in tags):
            continue  # another language, or forced
        rank = (any(t in want for t in tags), not any(t in _HINTS for t in tags), path.suffix.lower() == ".srt")
        if best is None or rank > best[0]:
            best = (rank, path)
    return best[1] if best else None


def has_target(video: Path, target: str, tracks: list[dict] | None = None) -> str:
    """Why this video needs no translation ('' if it does): a sidecar or a track already in the target language."""
    want = variants(target)
    for path, tags in sidecars(video):
        if any(t in want for t in tags) and "forced" not in tags:
            return f"{path.name} is already there"
    for t in tracks or []:
        if t["lang"] in want and not t["forced"]:
            return "the file already carries a track in that language"
    return ""


def source_for(video: Path | str, source: str = "en",
               tracks: list[dict] | None = None) -> tuple[str, bytes, str, str]:
    """(job name, subtitle bytes, path the translation is saved beside, where it came from)."""
    video = Path(video)
    side = find_sidecar(video, source)
    if side:
        return side.name, side.read_bytes(), str(side), f"from {side.name}"
    track = pick(tracks if tracks is not None else probe(video), source)
    return video.stem + ".srt", extract(video, track), str(video), f"track inside {video.name} ({describe(track)})"
