"""A report on how Subtle has been doing on this machine, for whoever is improving it.

    subtle report                 writes subtle-report.txt in the current folder
    subtle report --lines 5       also quotes up to five lines for each kind of problem
    subtle report -o -            prints it instead of writing a file

It reads what Subtle already keeps (the settings, the record of the files it saved, the queue,
the automation log) and the subtitle files it wrote. What comes out is numbers, model names and
error messages. File and show names become "file 1", "file 2"; keys, addresses, folders, the
note for the translator and the glossary are left out. Subtitle text is quoted only when
--lines asks for it. Nothing is sent anywhere: the report is a text file, to be read before it
is passed on.
"""
from __future__ import annotations

import json
import platform
import re
import sqlite3
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

from . import __version__, config, formats, media, qa
from .languages import LANGUAGES
from .providers import LOCAL, MODELS

NAME = "subtle-report.txt"
_SUBS = ("srt", "vtt", "ass", "ssa", "sub", "txt")
_EXTS = sorted({e.lstrip(".").lower() for e in media.VIDEO_EXT} | set(_SUBS))
# The bits between a name and its extension that say which language or kind a subtitle file is.
_TAGS = {c.lower() for c, _ in LANGUAGES} | {"en", "eng", "fin", "swe", "ger", "deu", "fre", "fra", "spa", "ita",
                                             "rus", "sdh", "forced", "cc", "hi", "default"}
_FLAG = {"failed": "not translated", "refused": "declined", "untranslated": "same as source", "english": "English?",
         "fast": "too fast", "long": "long line", "ratio": "length", "tags": "tags", "empty": "no words"}


def usd(v: float) -> str:
    return "$0" if not v else f"${v:.4f}" if v < 1 else f"${v:.2f}"


def count(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def model_name(model: str) -> str:
    if not model:
        return "-"
    return MODELS.get(model, {}).get("label") or model.removeprefix(LOCAL) + (" (local)" if model.startswith(LOCAL) else "")


class Names:
    """Keeps names out of the report.

    A file it is told about gets a label, the same one for a video, its source subtitles and
    its translation. In free text (an error message, a log note) those names are swapped for
    the label, and whatever else looks like a path, an address or a key for a placeholder.
    """

    _EXT = "|".join(_EXTS)
    # What may follow a name it knows: Show.S01E01 also stands for Show.S01E01.fi.srt.
    _TAIL = r"(?:\.(?:%s))*(?![A-Za-z0-9])" % "|".join(sorted(_TAGS | set(_EXTS), key=len, reverse=True))
    # A path runs on over a space when the next word has a separator in it ("D:\TV\Some Show\Season 1").
    _ON = r"""(?:[ ]+[^\s"'<>|\\/]*[\\/][^\s"'<>|]*)*"""
    _SCRUB = (
        (re.compile(r"""<folder>[\\/][^"'<>|\n]*?\.(?:%s)\b""" % _EXT, re.I), "<file>"),  # a file in a folder it knows
        (re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s\"'<>]+", re.I), "<address>"),
        # A whole path to a file, spaces and all, when it ends in an extension Subtle deals with.
        (re.compile(r"""(?:\b[A-Za-z]:[\\/]|\\\\)[^"'<>|\n]*?\.(?:%s)\b""" % _EXT, re.I), "<file>"),
        (re.compile(r"""(?<![\w<.:/])/[^\s"'<>|/]+/[^"'<>|\n]*?\.(?:%s)\b""" % _EXT, re.I), "<file>"),
        (re.compile(r"""(?:\b[A-Za-z]:[\\/]|\\\\)[^\s"'<>|]*""" + _ON), "<folder>"),
        (re.compile(r"""(?<![\w<.:/])/[^\s"'<>|/]+(?:/[^\s"'<>|]*)*""" + _ON), "<folder>"),
        (re.compile(r"\b(?=[\w.-]*[A-Za-z.])[\w.-]+:\d{2,5}\b"), "<address>"),
        (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "<address>"),
        (re.compile(r"\b(?:sk-[\w-]{8,}|[A-Fa-f0-9]{32,}|[\w-]{40,})\b"), "<key>"),
        (re.compile(r"""[^\s"'<>|/\\]+\.(?:%s)\b""" % _EXT, re.I), "<file>"),
    )

    def __init__(self) -> None:
        self._labels: dict[str, str] = {}
        self._known: dict[str, str] = {}
        self._folders: set[str] = set()
        self.folder(Path.home())

    def folder(self, path) -> None:
        """A folder to keep out of the report, with every folder above it."""
        p = str(path).rstrip("\\/")
        while len(p) > 3 and p not in self._folders:  # down to, but not including, a drive or the root
            self._folders.add(p)
            cut = max(p.rfind("/"), p.rfind("\\"))
            if cut <= 0:
                break
            p = p[:cut]

    @staticmethod
    def _stem(base: str) -> str:
        parts = base.split(".")
        if len(parts) > 1 and parts[-1].lower() in _EXTS:
            subs = parts.pop().lower() in _SUBS
            for _ in range(2):  # Show.S01E01.en.sdh.srt and Show.S01E01.mkv are the same episode
                if subs and len(parts) > 1 and parts[-1].lower() in _TAGS:
                    parts.pop()
        return ".".join(parts)

    def label(self, name: str) -> str:
        base = str(name).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        if not base:
            return "a file"
        stem = self._stem(base)
        label = self._labels.setdefault(stem.lower(), f"file {len(self._labels) + 1}")
        for known in (str(name), base, stem):
            if len(known) >= 6 or known == base:  # a short stem ("Up") could be an ordinary word
                self._known[known] = label
        if len(str(name).rstrip("\\/")) > len(base):
            self.folder(str(name).rstrip("\\/")[:-len(base)])
        return label

    def clean(self, text: str) -> str:
        text = str(text or "")
        for known in sorted(self._known, key=len, reverse=True):
            text = re.sub(re.escape(known) + self._TAIL, lambda m, label=self._known[known]: label, text, flags=re.I)
        for known in sorted(self._folders, key=len, reverse=True):
            text = re.sub(re.escape(known), "<folder>", text, flags=re.I)
        for pattern, word in self._SCRUB:
            text = pattern.sub(word, text)
        return " ".join(text.split())


def table(head: tuple, rows: list[tuple]) -> list[str]:
    cells = [tuple(str(c) for c in row) for row in [head, *rows]]
    widths = [max(len(row[i]) for row in cells) for i in range(len(head))]
    return ["  " + "  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip() for row in cells]


# ── what is set up ──────────────────────────────────────────────────────────

def setup(cfg: dict) -> list[str]:
    yes = lambda v: "yes" if v else "no"  # noqa: E731
    where = platform.system() + (", in a container" if Path("/.dockerenv").exists() else "")
    shows = "every show" if cfg["auto_all"] else count(len(cfg["auto_series"]), "show")
    return [
        f"Subtle {__version__} · Python {platform.python_version()} · {where} · ffmpeg {'found' if media.available() else 'not found'}",
        f"Translating into {cfg['target_name'] or 'nothing yet'} ({cfg['target_code'] or '-'}) with {model_name(cfg['model'])}"
        f" · API key: {config.key_source(cfg)}",
        f"Proofreading: {'on' if cfg['proofread'] else 'off'}, read by {model_name(cfg['proof_model']) if cfg['proof_model'] else 'the model that translated'}",
        f"Per request {cfg['chunk_lines']} lines · {cfg['concurrency']} files at once · stop a file at "
        f"{usd(cfg['budget']) if cfg['budget'] else 'four times the estimate'} · failing lines retried with {model_name(cfg['escalate_model'])}",
        f"Longest line {cfg['max_chars']} characters · speed limit {cfg['max_cps']} per second",
        f"Tone {cfg['register']} · sound descriptions {cfg['sdh']} · lyrics {cfg['lyrics']} · note for the translator: "
        f"{len(cfg['notes'])} characters · glossary: {count(len(cfg['glossary']), 'term')}",
        f"Saved next to the source: {yes(cfg['save_next_to_source'])} · original kept under each line: {yes(cfg['bilingual'])}",
        "Local model: " + (f"{cfg['local_model']}, context {cfg['local_ctx']}, key set: {yes(cfg['local_key'])}"
                           if cfg["local_url"] and cfg["local_model"] else "none"),
        f"Sonarr: {'connected' if cfg['sonarr_key'] else 'no'} · Radarr: {'connected' if cfg['radarr_key'] else 'no'} · "
        f"Bazarr: {'set, waits %s minutes' % cfg['bazarr_wait'] if cfg['bazarr_url'] else 'no'} · "
        f"path mappings: {len(cfg['path_maps'])}",
        f"Translated without asking: {shows}, movies: {yes(cfg['auto_movies'])} · daily limit "
        f"{usd(cfg['auto_daily_budget']) if cfg['auto_daily_budget'] else 'none'}",
    ]


# ── the files Subtle saved ──────────────────────────────────────────────────

def measure(path: Path, cfg: dict) -> dict | None:
    """What a saved subtitle file holds now: how many of its lines are over each limit."""
    try:
        doc = formats.parse(path.name, path.read_bytes())
    except Exception:  # noqa: BLE001  a file that cannot be read is simply not measured
        return None
    cues = fast = long = 0
    for c in doc.cues:
        text = qa.visible(c.text).strip()
        if not text:
            continue
        cues += 1
        long += any(len(line) > cfg["max_chars"] for line in text.split("\n"))
        if c.start is not None and c.end is not None and c.end - c.start > 300:
            fast += len(" ".join(text.split())) / ((c.end - c.start) / 1000) > cfg["max_cps"]
    return {"cues": cues, "fast": fast, "long": long}


def _records(home: Path) -> list[tuple] | None:
    """The rows of records.db, oldest first; None when there is no such file."""
    db = home / "records.db"
    if not db.exists():
        return None
    con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)  # read only: Subtle may be running
    try:
        return con.execute("SELECT out, size, model, reader, cost, lines, flagged, changed, at FROM made ORDER BY at").fetchall()
    finally:
        con.close()


def saved(home: Path, cfg: dict, names: Names) -> list[str]:
    rows = _records(home)
    if rows is None:
        return ["No record of saved files here (records.db is missing)."]
    if not rows:
        return ["Subtle has not saved a file next to a video or a source file yet."]
    listing, groups = [], {}
    for out, size, model, reader, cost, lines, flagged, changed, at in rows:
        path, now, state = Path(out), None, "gone"
        try:
            state = "as written" if path.stat().st_size == size else "replaced"
        except OSError:
            pass
        if state == "as written":
            now = measure(path, cfg)
        listing.append((names.label(out), time.strftime("%Y-%m-%d", time.localtime(at)), model_name(model), model_name(reader),
                        lines, usd(cost), flagged, changed if reader else "-", state,
                        now["fast"] if now else "-", now["long"] if now else "-"))
        g = groups.setdefault((model_name(model), model_name(reader)), {"files": 0, "lines": 0, "cost": 0.0, "flagged": 0,
                                                                     "changed": 0, "rates": [], "clean": 0, "fast": 0, "cues": 0})
        g["files"] += 1
        g["lines"] += lines
        g["cost"] += cost
        g["flagged"] += flagged
        g["changed"] += changed
        g["clean"] += not flagged
        if lines:
            g["rates"].append(1000 * cost / lines)
        if now:
            g["fast"] += now["fast"]
            g["cues"] += now["cues"]
    per = lambda n, d, k=100: f"{k * n / d:.1f}" if d else "-"  # noqa: E731
    summary = []
    for (model, reader), g in groups.items():
        rates = sorted(g["rates"]) or [0.0]
        summary.append((model, reader, g["files"], g["lines"], usd(g["cost"]),
                        f"{usd(statistics.median(rates))} ({usd(rates[0])} to {usd(rates[-1])})",
                        per(g["flagged"], g["lines"]), g["clean"], per(g["changed"], g["lines"]) if reader != "-" else "-",
                        per(g["fast"], g["cues"])))
    return [f"{count(len(rows), 'file')}.", "", "By model:",
            *table(("translated by", "read by", "files", "lines", "cost", "per 1000 lines (range)", "flagged /100",
                    "unflagged files", "changed /100", "fast now /100"), summary),
            "", "Each file. \"Fast now\" and \"long now\" count the lines over the speed and length limits in the",
            "file as it is today, whether or not they were flagged; /100 above means per hundred lines.",
            *table(("file", "saved", "translated by", "read by", "lines", "cost", "flagged", "changed", "on disk",
                    "fast now", "long now"), listing)]


# ── the queue ───────────────────────────────────────────────────────────────

def _jobs(home: Path) -> list[dict]:
    out = []
    for path in sorted((home / "jobs").glob("*.json"), key=lambda p: p.stat().st_mtime):
        try:
            out.append(json.loads(path.read_text("utf-8")))
        except (OSError, ValueError):
            continue
    return out


def queue(jobs: list[dict], names: Names) -> list[str]:
    if not jobs:
        return ["The queue is empty."]
    lines, flags_all, est_all, spent_all = [f"{count(len(jobs), 'file')}.", ""], Counter(), 0.0, 0.0
    for j in jobs:
        label = names.label(j.get("src_path") or j.get("name") or "")
        cues = [c for c in j["doc"]["cues"] if (c.get("t") or "").strip()]
        flags = Counter(f for c in cues for f in c.get("f") or [])
        used, est, spent, last, proofed = (j.get(k) or {} for k in ("used", "est", "spent", "last", "proofed"))
        src = sum(len(qa.visible(c["t"])) for c in cues)
        out = sum(len(qa.visible(c.get("tr") or "")) for c in cues)
        how = "started by a webhook" if j.get("auto") else "added from the Library" if j.get("beside") else \
            "added from a folder" if j.get("src_path") else "uploaded"
        lines.append(f"{label} · {j['doc'].get('fmt', '?')} · {count(len(cues), 'line')} · {j.get('status', '?')} · "
                     f"{model_name(used.get('model', ''))}, read by {model_name(proofed.get('model', ''))} · {how}")
        if j.get("via"):
            lines.append(f"  source: {names.clean(j['via'])}")
        lines.append(f"  cost: estimated {usd(est.get('cost', 0))}"
                     + (f" (of which proofreading {usd(est['proof'])})" if est.get("proof") else "")
                     + f" · spent {usd(spent.get('cost', 0))} · {count(spent.get('requests', 0), 'request')} · "
                     f"{spent.get('inp', 0):,} tokens in, {spent.get('out', 0):,} out")
        if last:
            lines.append(f"  last run: {count(last.get('units', 0), 'line')}, {last.get('cached', 0)} from cache, {last.get('repaired', 0)} repaired, "
                         f"{last.get('failed', 0)} failed, {last.get('inp', 0):,} tokens in, {last.get('out', 0):,} out · "
                         f"text: {src:,} characters in, {out:,} out")
        if proofed:
            lines.append(f"  proofreading: read {count(proofed.get('lines', 0), 'line')}, changed {proofed.get('changed', 0)}, "
                         f"discarded {proofed.get('dropped', 0)}, {usd(proofed.get('cost', 0))}")
        marks = [f"{_FLAG.get(f, f)} {n}" for f, n in flags.most_common()]
        edited = sum(1 for c in cues if c.get("ed"))
        lines.append("  flags: " + (", ".join(marks) or "none")
                     + (f" · {count(edited, 'line')} edited by hand" if edited else ""))
        if j.get("error"):
            lines.append(f"  message: {names.clean(j['error'])}")
        flags_all.update(flags)
        if j.get("status") == "done" and est.get("cost") and spent.get("cost"):
            est_all += est["cost"]
            spent_all += spent["cost"]
    lines.append("")
    if est_all:
        lines.append(f"Finished files that had an estimate: estimated {usd(est_all)}, spent {usd(spent_all)} "
                     f"({100 * spent_all / est_all:.0f}% of the estimate). Spending includes anything redone by hand.")
    lines.append("All flags in the queue: " + (", ".join(f"{_FLAG.get(f, f)} {n}" for f, n in flags_all.most_common()) or "none"))
    return lines


def quoted(jobs: list[dict], limit: int) -> list[str]:
    """Up to `limit` lines for each kind of flag, and as many proofreading changes."""
    flat = lambda s: " / ".join((s or "").split("\n"))  # noqa: E731
    seen: Counter = Counter()
    lines = []
    for j in jobs:
        for c in j["doc"]["cues"]:
            for f in c.get("f") or []:
                if seen[f] < limit:
                    seen[f] += 1
                    speed = ""
                    if c.get("s") is not None and c.get("e") is not None and c["e"] > c["s"] and c.get("tr"):
                        speed = f", {len(' '.join(qa.visible(c['tr']).split())) / ((c['e'] - c['s']) / 1000):.0f} per second"
                    lines += [f"[{_FLAG.get(f, f)}{speed}]", f"  original:    {flat(c.get('t'))}", f"  translation: {flat(c.get('tr'))}"]
    for j in jobs:
        for c in j["doc"]["cues"]:
            if c.get("pv") is not None and seen["proofread"] < limit:
                seen["proofread"] += 1
                lines += ["[changed by proofreading]", f"  original: {flat(c.get('t'))}", f"  before:   {flat(c['pv'])}",
                          f"  after:    {flat(c.get('tr'))}"]
    return lines or ["Nothing in the queue is flagged or was changed by proofreading."]


# ── the automation log ──────────────────────────────────────────────────────

def _log(home: Path) -> list[dict]:
    try:
        return json.loads((home / "activity.json").read_text("utf-8")) or []
    except (OSError, ValueError):
        return []


def activity(home: Path, names: Names) -> list[str]:
    log = _log(home)
    if not log:
        return ["Nothing has been logged."]
    by = Counter((a.get("origin", "?"), a.get("status", "?")) for a in log)
    first = time.strftime("%Y-%m-%d", time.localtime(min(a.get("t", 0) for a in log)))
    lines = [f"{len(log)} {'entry' if len(log) == 1 else 'entries'} since {first} (the log keeps the last 200), {usd(sum(a.get('cost') or 0 for a in log))} spent.",
             *table(("from", "outcome", "times"), [(o, s, n) for (o, s), n in by.most_common()])]
    # Why things were skipped or failed, with the names taken out so that equal reasons add up.
    why = Counter((a["status"], names.clean(a["note"])) for a in log
                  if a.get("status") in ("error", "skipped", "waiting") and a.get("note"))
    if why:
        lines += ["", "Reasons given:", *table(("outcome", "times", "note"), [(s, n, note) for (s, note), n in why.most_common(40)])]
    return lines


# ── putting it together ─────────────────────────────────────────────────────

def build(lines: int = 0) -> str:
    """The whole report as text. A part that cannot be read says so and the rest still comes out."""
    home, cfg, names = config.home(), config.load(), Names()
    try:
        jobs = _jobs(home)
    except OSError:
        jobs = []
    # Every name and folder is learned before anything is written, so that a file has one label
    # throughout and no part can let through a name that only another part knows.
    try:
        for pair in cfg["path_maps"]:
            for folder in pair:
                names.folder(folder)
        known = [row[0] for row in _records(home) or []]
        known += [name for j in jobs for name in (j.get("src_path") or j.get("name"), j.get("name"), j.get("saved_to"))]
        known += [a.get("file") for a in _log(home)]
        for name in known:
            if name:
                names.label(name)
    except Exception:  # noqa: BLE001  the part that cannot be read says so itself, below
        pass
    parts = [
        ("How it is set up", lambda: setup(cfg)),
        ("Files Subtle saved", lambda: saved(home, cfg, names)),
        ("The queue", lambda: queue(jobs, names)),
        ("The automation log", lambda: activity(home, names)),
    ]
    if lines > 0:
        parts.append((f"Lines from the queue (up to {lines} of each kind)", lambda: quoted(jobs, lines)))
    out = [
        f"Subtle report, {time.strftime('%Y-%m-%d %H:%M')}",
        "",
        "Numbers, model names and error messages from this machine. File and show names are",
        "replaced by labels (file 1, file 2, ...); keys, addresses, folders, the note for the",
        "translator and the glossary are left out.",
        "The last part quotes subtitle lines, because --lines asked for them." if lines > 0 else "No subtitle text is included.",
        "Read it through before passing it on.",
    ]
    for title, make in parts:
        out += ["", f"== {title} ==", ""]
        try:
            out += make()
        except Exception as e:  # noqa: BLE001  one unreadable part must not cost the rest
            out.append(f"This part could not be put together: {type(e).__name__}: {names.clean(str(e))}")
    out += [
        "", "== What only you can add ==", "",
        "1. Lines that read wrong, a handful is plenty. For each: the original line, what Subtle wrote,",
        "   and what it should have said.",
        "2. Anything that failed or looked broken, and what you were doing when it did.",
        "3. Whether the translations were good enough to watch with, and which shows were worse than others.",
        "",
    ]
    return "\n".join(out)


def main(args) -> int:
    text = build(max(0, args.lines or 0))
    if args.out == "-":
        print(text)
        return 0
    path = Path(args.out or NAME)
    path.write_text(text, "utf-8")
    print(f"Wrote {path} ({text.count(chr(10))} lines). Read it through before passing it on:", file=sys.stderr)
    print("it should hold no file names, keys or addresses" + (", but it does quote subtitle lines." if args.lines else "."), file=sys.stderr)
    return 0
