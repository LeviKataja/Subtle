"""The translation pipeline.

    prepare -> chunk at scene changes -> one small request per chunk -> validate locally
            -> re-ask only for the lines that came back wrong -> re-wrap -> QA flags

Where the tokens go: the words of the subtitle, about two tokens of framing per line and a
~300-token prompt per request. There is no JSON, no timing data and no reasoning budget, and a
hard spend cap stops a file that goes wrong.

Proofreading is a separate, optional pass (see `proofread`): the finished script is read once,
whole, and only the lines that need correcting come back.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Callable

from . import qa
from .chunker import make_chunks
from .model import Document
from .prep import (Unit, assemble, disassemble, flat, has_words, prepare, ready, strip_dash, wire_text,
                   wire_tgt)
from .prompt import PROMPT_VERSION, glossary_hits, proof_message, proof_prompt, system_prompt, user_message
from .providers import MODELS, Cancelled, DEFAULT_MODEL, price
from .wire import StreamParser, parse


class BudgetExceeded(Exception):
    pass


@dataclass
class Options:
    target_code: str = ""
    target_name: str = ""
    source_name: str = "English"
    model: str = DEFAULT_MODEL
    escalate_model: str = ""
    register: str = "auto"
    sdh: str = "keep"
    lyrics: str = "keep"
    max_chars: int = 42
    max_cps: int = 21
    chunk_lines: int = 120
    notes: str = ""
    title: str = ""
    glossary: list = field(default_factory=list)
    budget: float = 0.0

    @classmethod
    def from_dict(cls, d: dict, **over) -> "Options":
        known = {k: v for k, v in {**d, **over}.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class Stats:
    units: int = 0
    done: int = 0
    requests: int = 0
    inp: int = 0
    out: int = 0
    cost: float = 0.0
    cached: int = 0
    repaired: int = 0
    failed: int = 0
    seconds: float = 0.0

    def add(self, other: "Stats") -> None:
        for k in ("requests", "inp", "out", "cost", "repaired"):
            setattr(self, k, getattr(self, k) + getattr(other, k))


def estimate(doc: Document, opts: Options) -> dict:
    """Rough token and cost forecast, computed locally (no API call)."""
    units, _ = prepare(doc, opts.sdh, opts.lyrics)
    todo = [u for u in units if u.kind == "text"]
    n, chars = len(todo), sum(len(u.src) for u in todo)
    chunks = math.ceil(n / max(opts.chunk_lines, 1))
    system = system_prompt(opts.target_name or "the target language", opts.source_name, opts.register,
                           opts.notes, opts.title, doc.fmt == "txt")
    inp = chars / 3.3 + n * 3 + chunks * (len(system) / 3.3 + 70)
    out = chars / 2.3 + n * 3
    if MODELS.get(opts.model, {}).get("thinks"):
        out *= 1.2  # these models cannot switch reasoning off completely
    pin, pout = price(opts.model)
    return {"units": n, "skipped": len(units) - n, "chars": chars, "chunks": chunks,
            "inp": int(inp), "out": int(out), "cost": (inp * pin + out * pout) / 1e6}


def _unecho(text: str, src: str) -> str:
    """Drop a repeated "original => " from the front of a reply line.

    The context lines in a request are written "original => translation", and a model now and
    then answers in the same shape. Only cut when what precedes the arrow really is the original.
    """
    head, arrow, tail = text.partition("=>")
    same = " ".join(head.split()).lower() == " ".join(src.split()).lower()
    return tail.strip() if arrow and same else text


def _windows(missing: list[int], n: int, radius: int = 1) -> list[list[int]]:
    """Missing line numbers -> merged [first, last] spans, each padded with a neighbour either side."""
    spans: list[list[int]] = []
    for i in sorted(missing):
        a, b = max(0, i - radius), min(n - 1, i + radius)
        if spans and a <= spans[-1][1] + 1:
            spans[-1][1] = max(spans[-1][1], b)
        else:
            spans.append([a, b])
    return spans


class Translator:
    def __init__(self, doc: Document, opts: Options, provider, cache=None,
                 emit: Callable[[dict], None] | None = None, cancel: Callable[[], bool] | None = None):
        self.doc, self.opts, self.provider, self.cache = doc, opts, provider, cache
        self.emit = emit or (lambda event: None)
        self.cancel = cancel or (lambda: False)
        self.units, self.plans = prepare(doc, opts.sdh, opts.lyrics)
        self.todo = [i for i, u in enumerate(self.units) if u.kind == "text"]
        self.system = system_prompt(opts.target_name, opts.source_name, opts.register, opts.notes,
                                    opts.title, doc.fmt == "txt")
        self.stats = Stats(units=len(self.todo))
        self.budget = opts.budget or estimate(doc, opts)["cost"] * 4 + 0.02
        self._last_emit = 0.0

    # --- driving ------------------------------------------------------------

    def run(self) -> Stats:
        started = time.time()
        chunks = make_chunks(self.todo, self.units, self.doc, self.opts.chunk_lines)
        self.emit({"type": "plan", "units": len(self.todo), "chunks": len(chunks), "budget": round(self.budget, 4)})
        context: list[tuple[str, str]] = []
        try:
            for ids in chunks:
                self._chunk(ids, context)
                context = [(wire_text(u), wire_tgt(u)) for u in (self.units[i] for i in ids[-3:])
                           if u.tgt and u.state in ("ok", "cached")]
        finally:
            self.stats.seconds = time.time() - started
            self.finish()  # keep whatever was translated, even when stopped half way
        return self.stats

    def finish(self) -> None:
        o = self.opts
        self.stats.failed = 0
        for cue, plan in zip(self.doc.cues, self.plans):
            mine = [self.units[i] for i in plan.units]
            if any(u.kind == "text" and u.tgt is None and u.state not in ("failed", "refused") for u in mine):
                cue.translation, cue.flags = None, []  # never reached
                continue
            carry = tuple(s for s in ("refused", "failed") if any(u.state == s for u in mine))
            self.stats.failed += bool(carry)
            cue.translation = assemble(cue, plan, self.units, self.doc.fmt, o.max_chars)
            cue.edited, cue.prev = False, None
            cue.flags = qa.check(cue, o.target_code, self.doc.fmt, o.max_chars, o.max_cps, carry)
        self._progress(force=True)

    # --- one chunk ----------------------------------------------------------

    def _chunk(self, ids: list[int], context: list[tuple[str, str]]) -> None:
        us = [self.units[i] for i in ids]
        lines = [wire_text(u) for u in us]
        key = None
        if self.cache:
            key = self.cache.key(PROMPT_VERSION, self.opts.model, self.system, lines,
                                 glossary_hits(self.opts.glossary, lines))
            hit = self.cache.get(key)
            if hit and len(hit) == len(us):
                for u, text in zip(us, hit):
                    self._set(u, text, "cached")
                self.stats.cached += len(us)
                self._progress(force=True)
                return
        got = self._solve(us, context)
        for j, u in enumerate(us):
            if j in got:
                self._set(u, got[j], "ok")
            else:
                u.tgt, u.state = None, ("refused" if u.state == "refused" else "failed")
        if key and len(got) == len(us):
            self.cache.put(key, [got[j] for j in range(len(us))])
        self._progress(force=True)

    def _solve(self, us: list[Unit], context: list, depth: int = 0) -> dict[int, str]:
        """Translate these lines, repairing only what comes back wrong. Returns {index: text}."""
        n = len(us)
        got, refused = self._ask(us, context, self.opts.model, live=True)

        if (refused and not got) or len(got) < n / 2:
            if n > 6 and depth < 4:
                # A refusal or a garbled reply: halve the batch to isolate the lines responsible.
                mid = n // 2
                while mid < n - 1 and us[mid].cue == us[mid - 1].cue:
                    mid += 1
                a = self._solve(us[:mid], context, depth + 1)
                tail = [(wire_text(us[i]), self._dashed(us[i], a[i])) for i in range(max(0, mid - 3), mid) if i in a]
                b = self._solve(us[mid:], tail or context, depth + 1)
                return {**a, **{mid + j: t for j, t in b.items()}}
            if refused and not got:
                esc = self.opts.escalate_model
                if esc and esc != self.opts.model:
                    got, refused = self._ask(us, context, esc)
                if refused and not got:
                    for u in us:
                        u.state = "refused"
                    return got

        for attempt in range(2):
            missing = [i for i in range(n) if i not in got]
            if not missing:
                break
            esc = self.opts.escalate_model
            model = esc if attempt and esc else self.opts.model
            for a, b in _windows(missing, n):
                # Re-translate the hole together with its neighbours and replace all of them:
                # a skipped line usually means its words were folded into the line next to it.
                ctx = [(wire_text(us[i]), self._dashed(us[i], got[i])) for i in range(max(0, a - 3), a) if i in got]
                fresh, _ = self._ask(us[a:b + 1], ctx or (context if a == 0 else []), model)
                self.stats.repaired += sum(1 for j in fresh if a + j in missing)
                for j, text in fresh.items():
                    got[a + j] = text
        return got

    def _ask(self, us: list[Unit], context: list, model: str, live: bool = False,
             extra: str = "") -> tuple[dict[int, str], bool]:
        """One request. Returns ({index: translation}, was_refused)."""
        if self.cancel():
            raise Cancelled()
        if self.stats.cost > self.budget:
            raise BudgetExceeded(f"Stopped at ${self.stats.cost:.2f}: over the ${self.budget:.2f} cap for this file.")
        lines = [wire_text(u) for u in us]
        user = user_message(lines, context, glossary_hits(self.opts.glossary, lines), extra)
        feed = StreamParser(len(us), lambda i, text: self._preview(us[i], text)).feed if live else None
        reply = self.provider.complete(model, self.system, user, on_text=feed, cancel=self.cancel)
        self._spend(reply, model)
        cut = reply.stop == "max_tokens"  # ran out of room: the last line is not to be trusted
        got = {i: self._clean(us[i], _unecho(text, wire_text(us[i])))
               for i, text in parse(reply.text, len(us), cut).items()}
        # A line that comes back as "." had its words folded into a neighbour. Counting it as
        # missing sends it through the same repair as a skipped line.
        return {i: t for i, t in got.items() if t and has_words(t)}, reply.stop == "refusal"

    # --- bookkeeping --------------------------------------------------------

    def _spend(self, reply, model: str) -> None:
        pin, pout = price(model)
        s = self.stats
        s.requests += 1
        s.inp += reply.inp + reply.cache_read + reply.cache_write
        s.out += reply.out
        s.cost += (reply.inp * pin + reply.cache_read * pin * 0.1 + reply.cache_write * pin * 1.25
                   + reply.out * pout) / 1e6

    @staticmethod
    def _dashed(u: Unit, text: str) -> str:
        return ("- " if u.dash else "") + text

    @staticmethod
    def _clean(u: Unit, text: str) -> str:
        text = text.strip()
        return strip_dash(text).strip() if u.dash else text

    def _preview(self, u: Unit, text: str) -> None:
        u.tgt = self._clean(u, _unecho(text, wire_text(u))) or None
        self._show(u.cue)
        self._progress()

    def _set(self, u: Unit, text: str, state: str) -> None:
        u.tgt, u.state = text, state
        self._show(u.cue)

    def _show(self, ci: int) -> None:
        plan = self.plans[ci]
        if ready(plan, self.units):
            cue = self.doc.cues[ci]
            text = assemble(cue, plan, self.units, self.doc.fmt, self.opts.max_chars)
            if text != cue.translation:  # already shown while streaming, or unchanged since the last run
                cue.translation = text
                self.emit({"type": "cue", "i": ci, "text": text})

    def _progress(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_emit < 0.25:
            return
        self._last_emit = now
        s = self.stats
        s.done = sum(1 for i in self.todo if self.units[i].tgt is not None)
        self.emit({"type": "progress", "done": s.done, "total": s.units, "inp": s.inp, "out": s.out,
                   "cost": round(s.cost, 6), "requests": s.requests, "cached": s.cached})


def translate(doc: Document, opts: Options, provider, cache=None, emit=None, cancel=None) -> Stats:
    return Translator(doc, opts, provider, cache, emit, cancel).run()


def retranslate(doc: Document, cue_ids: list[int], opts: Options, provider, instruction: str = "",
                condense: bool = False, emit=None, cancel=None) -> Stats:
    """Redo a few cues in place, optionally with an instruction or a reading-speed limit."""
    tr = Translator(doc, opts, provider, None, None, cancel)
    tr.budget = max(tr.budget, 0.25)
    emit = emit or (lambda event: None)
    wanted = sorted({i for i in cue_ids if 0 <= i < len(doc.cues)})
    runs: list[list[int]] = []
    for i in wanted:
        if runs and i == runs[-1][-1] + 1 and len(runs[-1]) < 40:
            runs[-1].append(i)
        else:
            runs.append([i])

    for run in runs:
        us = [tr.units[ui] for ci in run for ui in tr.plans[ci].units if tr.units[ui].kind == "text"]
        if not us:
            continue
        context = []
        for ci in range(max(0, run[0] - 3), run[0]):
            c = doc.cues[ci]
            if c.text.strip() and c.translation and c.translation.strip():
                context.append((flat(c.text), flat(c.translation)))
        extra = f"Extra instruction for these lines: {instruction.strip()}" if instruction.strip() else ""
        if condense:
            limits = []
            for n, u in enumerate(us, 1):
                cue = doc.cues[u.cue]
                share = max(1, sum(1 for x in tr.plans[u.cue].units if tr.units[x].kind == "text"))
                seconds = ((cue.end or 0) - (cue.start or 0)) / 1000 or 2.0
                limits.append(f"{n}: {max(10, min(int(seconds * opts.max_cps / share), opts.max_chars * 2))}")
            extra = (extra + "\n" if extra else "") + (
                "These lines are on screen too briefly for a full translation. Translate each one as "
                "concisely as possible, dropping filler but not meaning, within these character limits: "
                + ", ".join(limits) + ".")
        got, _ = tr._ask(us, context, opts.model, extra=extra)
        for j, u in enumerate(us):
            if j in got:
                u.tgt, u.state = got[j], "ok"
        for ci in run:
            plan = tr.plans[ci]
            if ready(plan, tr.units) and any(tr.units[x].kind == "text" for x in plan.units):
                cue = doc.cues[ci]
                cue.translation = assemble(cue, plan, tr.units, doc.fmt, opts.max_chars)
                cue.edited, cue.prev = False, None
                cue.flags = qa.check(cue, opts.target_code, doc.fmt, opts.max_chars, opts.max_cps)
                emit({"type": "cue", "i": ci, "text": cue.translation, "flags": cue.flags})
    return tr.stats


# --- proofreading -------------------------------------------------------------

PROOF_LINES = 2000  # lines per request: an episode or a film is read in one go
_UNDOES = {"empty", "untranslated", "english", "long", "ratio", "tags"}  # a correction may not cause these


def _proofable(cue) -> bool:
    return bool(cue.text.strip() and cue.translation and cue.translation.strip()) and not set(cue.flags) & qa.ERRORS


def estimate_proof(doc: Document, opts: Options, model: str = "") -> dict:
    """Forecast for one proofreading pass: both languages go in, only the corrections come out."""
    model = model or opts.model
    cues = [c for c in doc.cues if _proofable(c)]
    n = len(cues)
    src, tgt = sum(len(flat(c.text)) for c in cues), sum(len(flat(c.translation)) for c in cues)
    inp = src / 3.3 + tgt / 2.2 + n * 5 + 600
    out = (tgt / 2.2 + n * 3) * 0.1  # one line in ten rewritten
    if MODELS.get(model, {}).get("thinks"):
        out *= 1.2
    pin, pout = price(model)
    return {"lines": n, "inp": int(inp), "out": int(out), "cost": (inp * pin + out * pout) / 1e6}


def proofread(doc: Document, opts: Options, provider, model: str = "", emit=None, cancel=None) -> tuple[Stats, dict]:
    """Read the finished translation once more, whole, and correct only the lines that need it.

    The translator works a scene at a time, so it cannot see that a term is rendered two ways an
    hour apart, and it does not get a second look at its own slips. This pass does both. Lines
    edited by hand are shown to the model but never changed, and a correction that breaks a local
    check (no words, tags lost, far too long) is thrown away. Returns (stats, summary).
    """
    model = model or opts.model
    emit = emit or (lambda event: None)
    tr = Translator(doc, opts, provider, None, None, cancel)
    listed: list[int] = []
    for cue, plan in zip(doc.cues, tr.plans):
        if _proofable(cue) and disassemble(cue, plan, tr.units, doc.fmt):
            listed += [i for i in plan.units if tr.units[i].kind == "text"]
    summary = {"model": model, "lines": len(listed), "changed": [], "dropped": 0}
    if not listed:
        return tr.stats, summary

    system = proof_prompt(opts.target_name, opts.source_name, opts.register, opts.notes, opts.title)
    cap = opts.budget or estimate_proof(doc, opts, model)["cost"] * 4 + 0.02
    for ids in make_chunks(listed, tr.units, doc, PROOF_LINES, max_chars=10 ** 9):
        if cancel and cancel():
            raise Cancelled()
        if tr.stats.cost > cap:
            raise BudgetExceeded(f"Stopped proofreading at ${tr.stats.cost:.2f}: over the ${cap:.2f} cap for this file.")
        us = [tr.units[i] for i in ids]
        sources = [wire_text(u) for u in us]
        user = proof_message(list(zip(sources, (wire_tgt(u) for u in us))), glossary_hits(opts.glossary, sources))
        reply = provider.complete(model, system, user, cancel=cancel)
        tr._spend(reply, model)

        by_cue: dict[int, list[tuple[Unit, str]]] = {}
        for j, text in parse(reply.text, len(us), reply.stop == "max_tokens").items():
            u = us[j]
            # The request shows "original => translation", and a reply sometimes repeats that shape
            # instead of giving the correction alone. Only what follows the arrow is the correction.
            text = tr._clean(u, text.rsplit("=>", 1)[-1])
            if not text or text == u.tgt:
                continue
            if len(u.src.split()) >= 4 and u.src.lower() in text.lower():
                summary["dropped"] += 1  # the original pasted into the translation some other way
                continue
            by_cue.setdefault(u.cue, []).append((u, text))
        for ci, fixes in sorted(by_cue.items()):
            cue, plan = doc.cues[ci], tr.plans[ci]
            if cue.edited:
                continue  # a person's wording stands
            was = [(u, u.tgt) for u, _ in fixes]
            for u, text in fixes:
                u.tgt = text
            before, cue.translation = cue.translation, assemble(cue, plan, tr.units, doc.fmt, opts.max_chars)
            flags = qa.check(cue, opts.target_code, doc.fmt, opts.max_chars, opts.max_cps)
            if cue.translation == before or (set(flags) - set(cue.flags)) & _UNDOES:
                summary["dropped"] += cue.translation != before
                cue.translation = before
                for u, old in was:
                    u.tgt = old
                continue
            original = before if cue.prev is None else cue.prev
            cue.prev = None if cue.translation == original else original
            cue.flags = flags
            summary["changed"].append(ci)
            emit({"type": "cue", "i": ci, "text": cue.translation, "flags": flags})
    return tr.stats, summary
