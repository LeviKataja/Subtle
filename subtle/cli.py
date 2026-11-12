"""Command line.

    subtle                       open the web UI
    subtle translate PATH... --to fi
    subtle estimate PATH...
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from . import __version__, config, formats, media
from .cache import Cache
from .engine import BudgetExceeded, Options, Stats, Translator, estimate, proofread
from .languages import resolve, variants
from .prompt import clean_title
from .providers import ALIASES, MODELS, Cancelled, ProviderError, build

_SCAN = (".srt", ".vtt", ".ass", ".ssa")


def _utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def money(v: float) -> str:
    return "$0" if not v else f"${v:.4f}" if v < 0.01 else f"${v:.3f}" if v < 1 else f"${v:.2f}"


def collect(paths: list[str], code: str) -> list[Path]:
    """Files given directly, plus what is found under any folders given.

    In a folder that means subtitle files, and (with ffmpeg installed) videos that have no
    subtitle file of their own, whose subtitles are read from inside the video.
    """
    out: list[Path] = []
    ours = variants(code)
    videos = media.available()
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if not f.is_file():
                    continue
                ext = f.suffix.lower()
                tags = {t.lower() for t in f.name.split(".")[1:-1]}
                if ext in _SCAN and not tags & ours:
                    out.append(f)
                elif ext in media.VIDEO_EXT and videos and not media.find_sidecar(f) and not media.has_target(f, code):
                    out.append(f)
        elif p.is_file():
            out.append(p)
        else:
            print(f"  skipped (not found): {raw}", file=sys.stderr)
    return out


def _options(args, cfg: dict) -> Options:
    code, name = resolve(args.to) if getattr(args, "to", None) else (cfg["target_code"], cfg["target_name"])
    over = {"target_code": code, "target_name": name}
    if getattr(args, "model", None):
        over["model"] = ALIASES.get(args.model, args.model)
    for key in ("sdh", "lyrics", "register", "notes", "budget"):
        if getattr(args, key, None) not in (None, ""):
            over[key] = getattr(args, key)
    return Options.from_dict(cfg, **over)


def cmd_translate(args) -> int:
    cfg = config.load()
    opts = _options(args, cfg)
    dry = args.command == "estimate" or args.dry_run
    if not opts.target_code and not dry:
        print("Say which language to translate into, e.g. --to fi", file=sys.stderr)
        return 2
    files = collect(args.paths, opts.target_code or "\0")
    if not files:
        print("No subtitle files found." + ("" if media.available() else
              " (ffmpeg is not installed, so video files were not looked into.)"), file=sys.stderr)
        return 1

    provider = cache = None
    if not dry:
        mock = args.mock or bool(os.environ.get("SUBTLE_MOCK"))
        try:
            provider = build(cfg["api_key"] if config.key_source(cfg) == "saved" else None, mock)
        except ProviderError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        cache = None if args.no_cache else Cache(config.home() / "cache.db")

    label = MODELS.get(opts.model, {}).get("label", opts.model)
    print(f"{len(files)} file(s) -> {opts.target_name or '?'} with {label}")
    total, est_total, failures, skipped, written = Stats(), 0.0, 0, 0, 0
    for path in files:
        name, data = path.name, None
        if path.suffix.lower() in media.VIDEO_EXT:
            try:
                name, data, _, how = media.source_for(path)
            except media.MediaError as e:
                skipped += 1  # a video with nothing usable in it is not an error in a folder run
                print(f"  {path.name}: skipped - {e}")
                continue
            print(f"  {path.name}: using the {how}")
        doc = formats.parse(name, data if data is not None else path.read_bytes())
        opts.title = clean_title(name)
        est = estimate(doc, opts)
        est_total += est["cost"]
        out_dir = Path(args.out) if getattr(args, "out", None) else path.parent
        out = out_dir / formats.output_name(name, opts.target_code)
        if dry:
            print(f"  {path.name}: {est['units']} lines, ~{est['inp'] + est['out']:,} tokens, ~{money(est['cost'])}")
            continue
        if out.exists() and not args.force:
            print(f"  {path.name}: {out.name} already exists (use --force to redo)")
            continue

        def emit(event: dict, name=path.name) -> None:
            if event["type"] == "progress" and sys.stdout.isatty():
                pct = 100 * event["done"] // max(event["total"], 1)
                print(f"\r  {name}: {pct:3d}%  {money(event['cost'])}   ", end="", flush=True)

        started, error, fatal = time.time(), "", False
        tr = Translator(doc, opts, provider, cache, emit)
        try:
            tr.run()
        except KeyboardInterrupt:
            print("\nInterrupted. Finished chunks are cached; run again to resume.")
            return 130
        except (BudgetExceeded, ProviderError, Cancelled) as e:
            # A bad key or model fails every file the same way: stop instead of repeating it.
            error, fatal = str(e), isinstance(e, ProviderError) and e.kind in ("auth", "model")
        stats = tr.stats
        total.add(stats)
        if error:
            failures += 1
            print(f"\r  {path.name}: FAILED - {error}")
            if fatal:
                return 1
            continue
        proofed = ""
        if args.proofread or args.proofread_model or cfg.get("proofread"):
            reader = ALIASES.get(args.proofread_model, args.proofread_model) or cfg.get("proof_model") or ""
            try:
                extra, summary = proofread(doc, opts, provider, reader)
                total.add(extra)
                stats.add(extra)
                proofed = f", proofreading changed {len(summary['changed'])}"
            except KeyboardInterrupt:
                proofed = ", proofreading interrupted"
            except (BudgetExceeded, ProviderError, Cancelled) as e:
                proofed = f", proofreading failed ({e})"  # the translation is still written
        out_dir.mkdir(parents=True, exist_ok=True)
        out.write_text(formats.render(doc, args.bilingual), "utf-8")
        written += 1
        flagged = sum(1 for c in doc.cues if c.flags)
        note = f", {flagged} flagged" if flagged else ""
        cached = f", {stats.cached} from cache" if stats.cached else ""
        print(f"\r  {path.name} -> {out.name}: {stats.units} lines{note}{cached}{proofed}, "
              f"{stats.inp + stats.out:,} tokens, {money(stats.cost)}, {time.time() - started:.1f}s")
    if dry:
        print(f"Estimated total: ~{money(est_total)}")
    else:
        print(f"Total: {total.requests} requests, {total.inp:,} in / {total.out:,} out tokens, {money(total.cost)}")
    return 1 if failures or (skipped and not written and not dry) else 0


def cmd_serve(args) -> int:
    from .server import serve

    return serve(args.host, args.port, not args.no_open, args.mock, args.password or "")


def cmd_cache(args) -> int:
    print(f"Removed {Cache(config.home() / 'cache.db').clear()} cached chunks.")
    return 0


def main(argv: list[str] | None = None) -> int:
    _utf8()
    parser = argparse.ArgumentParser(prog="subtle", description="Token-lean subtitle translation.")
    parser.add_argument("--version", action="version", version=f"subtle {__version__}")
    sub = parser.add_subparsers(dest="command")

    def serve_args(p):
        p.add_argument("--host", default="127.0.0.1", help="address to listen on (default: this machine only)")
        p.add_argument("--port", type=int, default=8756)
        p.add_argument("--no-open", action="store_true", help="don't open a browser tab")
        p.add_argument("--mock", action="store_true", help="offline demo: fake translations, no API key, no cost")
        p.add_argument("--data", help="folder for settings, cache and queue (default: ~/.subtle)")
        p.add_argument("--password", help="ask for this password in the browser (or set SUBTLE_PASSWORD)")

    serve_args(parser)
    serve_args(sub.add_parser("serve", help="run the web UI (the default)"))

    for name, text in (("translate", "translate files or folders"), ("estimate", "show what it would cost")):
        p = sub.add_parser(name, help=text)
        p.add_argument("paths", nargs="+", help="subtitle files, or folders to search")
        p.add_argument("--to", help="target language, e.g. fi or Finnish (default: the one saved in settings)")
        p.add_argument("--model", help="haiku (default), sonnet, opus, or a full model id")
        p.add_argument("--sdh", choices=("keep", "strip"), help="strip removes [sounds] and SPEAKER: labels")
        p.add_argument("--lyrics", choices=("keep", "translate"))
        p.add_argument("--register", choices=("auto", "informal", "formal"))
        p.add_argument("--notes", help="a sentence of context for the translator")
        p.add_argument("--data", help="folder for settings and cache (default: ~/.subtle)")
        if name == "translate":
            p.add_argument("--out", help="output folder (default: next to each source file)")
            p.add_argument("--force", action="store_true", help="overwrite existing translations")
            p.add_argument("--dry-run", action="store_true", help="only show the estimate")
            p.add_argument("--bilingual", action="store_true", help="keep the original line under each translation")
            p.add_argument("--budget", type=float, help="stop a file once it has cost this many dollars")
            p.add_argument("--proofread", action="store_true",
                           help="read each finished translation once more and correct the lines that need it")
            p.add_argument("--proofread-model", default="", metavar="MODEL",
                           help="proofread with this model instead of the one that translated (implies --proofread)")
            p.add_argument("--no-cache", action="store_true")
            p.add_argument("--mock", action="store_true", help="fake translations, for trying things out")
        else:
            p.set_defaults(dry_run=True, force=False, bilingual=False, mock=False, no_cache=True,
                           proofread=False, proofread_model="")

    cache = sub.add_parser("cache", help="manage cached translations")
    cache.add_argument("action", choices=("clear",))
    cache.add_argument("--data", help="folder for settings and cache (default: ~/.subtle)")

    args = parser.parse_args(argv)
    if getattr(args, "data", None):
        os.environ["SUBTLE_HOME"] = args.data
    if args.command in ("translate", "estimate"):
        return cmd_translate(args)
    if args.command == "cache":
        return cmd_cache(args)
    return cmd_serve(args)
