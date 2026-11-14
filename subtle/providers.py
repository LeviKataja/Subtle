"""Model backends. The engine only needs `complete()`; everything else stays in here."""
from __future__ import annotations

import http.client
import json
import re
import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

# Prices are USD per million tokens (Anthropic first-party rates, checked 2026-11).
# `kwargs` keeps each model from spending tokens on reasoning a translation does not need:
# Haiku can switch thinking off outright; Sonnet and Opus cannot, so they run at low effort.
MODELS: dict[str, dict] = {
    "claude-haiku-5-5": {
        "label": "Haiku 5.5", "inp": 0.10, "out": 0.50, "blurb": "Fast and cheap. The default.",
        "kwargs": {"thinking": {"type": "disabled"}, "output_config": {"effort": "medium"}},
        "fallbacks": False, "thinks": False,
    },
    "claude-sonnet-5-5": {
        "label": "Sonnet 5.5", "inp": 2.00, "out": 10.00, "blurb": "For tricky wordplay or literary text.",
        "kwargs": {"output_config": {"effort": "low"}},
        "fallbacks": True, "thinks": True,
    },
    "claude-opus-5-5": {
        "label": "Opus 5.5", "inp": 4.00, "out": 20.00, "blurb": "Rarely worth it for subtitles.",
        "kwargs": {"output_config": {"effort": "low"}},
        "fallbacks": True, "thinks": True,
    },
}
ALIASES = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}
DEFAULT_MODEL = "claude-haiku-5-5"
LOCAL = "local:"  # a model id that starts like this names a model on the local server, e.g. local:gemma3:12b


def is_local(model: str) -> bool:
    return (model or "").startswith(LOCAL)


def price(model: str) -> tuple[float, float]:
    m = MODELS.get(model)
    return (m["inp"], m["out"]) if m else (0.0, 0.0)


@dataclass
class Completion:
    text: str
    stop: str  # end_turn | max_tokens | refusal | ...
    inp: int
    out: int
    cache_read: int = 0
    cache_write: int = 0


class ProviderError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind  # auth | model | rate | network | api


class Cancelled(Exception):
    pass


class AnthropicProvider:
    def __init__(self, api_key: str | None = None):
        try:
            import anthropic
        except ImportError as e:  # a setup that only ever used a local model may not have it
            raise ProviderError("auth", "Claude's models need the anthropic package: pip install anthropic") from e

        self._sdk = anthropic
        try:
            self.client = anthropic.Anthropic(api_key=api_key or None, max_retries=4)
        except Exception as e:  # the SDK raises when it finds no credentials at all
            raise ProviderError("auth", f"No API credentials found ({e}).") from e
        self._skip: dict[str, int] = {}  # per model: how many optional request variants to skip

    def _variants(self, model: str, base: dict) -> list[tuple[dict, bool]]:
        prof = MODELS.get(model, {})
        tuned = {**base, **prof.get("kwargs", {})}
        out = []
        if prof.get("fallbacks"):
            out.append((tuned, True))  # server-side refusal fallback, a beta
        if prof.get("kwargs"):
            out.append((tuned, False))
        out.append((base, False))
        return out

    def complete(self, model: str, system: str, user: str,
                 on_text: Callable[[str], None] | None = None,
                 cancel: Callable[[], bool] | None = None, max_tokens: int = 16000) -> Completion:
        sdk = self._sdk
        base = {"model": model, "max_tokens": max_tokens, "system": system,
                "messages": [{"role": "user", "content": user}]}
        variants = self._variants(model, base)
        i = min(self._skip.get(model, 0), len(variants) - 1)
        while True:
            kwargs, fallbacks = variants[i]
            try:
                return self._stream(kwargs, fallbacks, on_text, cancel)
            except sdk.BadRequestError as e:
                # An optional field this account or model rejects: drop it and remember.
                if i + 1 >= len(variants):
                    raise ProviderError("api", f"The API rejected the request: {e.message}") from e
                i += 1
                self._skip[model] = i
            except sdk.AuthenticationError as e:
                raise ProviderError("auth", "The API key was rejected.") from e
            except sdk.PermissionDeniedError as e:
                raise ProviderError("auth", f"This API key may not use {model}.") from e
            except sdk.NotFoundError as e:
                raise ProviderError("model", f"Model {model} was not found.") from e
            except sdk.RateLimitError as e:
                raise ProviderError("rate", "Rate limited by the API. Try again in a minute.") from e
            except sdk.APIStatusError as e:
                raise ProviderError("api", f"API error {e.status_code}: {e.message}") from e
            except sdk.APIConnectionError as e:
                raise ProviderError("network", "Could not reach the API. Check the connection.") from e
            except TypeError as e:  # the SDK's "could not resolve authentication" error
                raise ProviderError("auth", "No API key found. Set ANTHROPIC_API_KEY or add one in Settings.") from e

    def _stream(self, kwargs: dict, fallbacks: bool, on_text, cancel) -> Completion:
        if fallbacks:
            manager = self.client.beta.messages.stream(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
        else:
            manager = self.client.messages.stream(**kwargs)
        parts: list[str] = []
        with manager as stream:
            for text in stream.text_stream:
                parts.append(text)
                if on_text:
                    on_text(text)
                if cancel and cancel():
                    raise Cancelled()
            message = stream.get_final_message()
        u = message.usage
        return Completion("".join(parts), message.stop_reason or "", u.input_tokens or 0, u.output_tokens or 0,
                          getattr(u, "cache_read_input_tokens", 0) or 0,
                          getattr(u, "cache_creation_input_tokens", 0) or 0)


_THINK = re.compile(r"<think>.*?(?:</think>|$)", re.S)


def _visible(raw: str) -> str:
    """What has arrived so far, without reasoning blocks and without a tag that is only half here."""
    text = _THINK.sub("", raw)
    for n in range(min(6, len(text)), 0, -1):
        if "<think>".startswith(text[-n:]):
            return text[:-n]
    return text


def _said(raw: bytes) -> str:
    """The message out of an error body: {"error": "..."} (Ollama) or {"error": {"message": "..."}}."""
    try:
        data = json.loads(raw)
    except ValueError:
        return raw.decode("utf-8", "replace").strip()[:200]
    if not isinstance(data, dict):
        return ""
    err = data.get("error")
    if isinstance(err, dict):
        err = err.get("message")
    return str(err or data.get("message") or "")[:300]


class _Refused(Exception):
    """The server answered with an HTTP error."""

    def __init__(self, status: int, body: bytes):
        super().__init__(status)
        self.status, self.said = status, _said(body)


class LocalProvider:
    """A model on your own hardware.

    Ollama is spoken to through its own API, anything else through the OpenAI-style chat API
    (LM Studio, llama.cpp, vLLM, ...). Ollama gets its own path because only there can a
    request say how much context it needs and that it wants no reasoning: through its
    OpenAI-style route a long prompt is silently cut down to the server's default context.
    Requests go out one at a time, which is all such a server does anyway.
    """

    TIMEOUT = 600  # seconds without a byte; loading a model and reading a prompt on a CPU is slow

    def __init__(self, url: str, key: str = "", ctx: int = 4096):
        base = url.strip().rstrip("/")
        if base and "://" not in base:
            base = "http://" + base
        for tail in ("/chat/completions", "/api/chat", "/api"):
            if base.lower().endswith(tail):
                base = base[:-len(tail)]
        self.v1 = base if base.lower().endswith("/v1") else base + "/v1"
        self.root = self.v1[:-3]
        self.key, self.ctx = key.strip(), max(int(ctx or 0), 2048)
        self.kind = ""  # ollama | openai, found out on first use
        self._gate = threading.Lock()
        self._skip: dict[str, int] = {}  # per model: optional request fields it turned down

    # --- plumbing -----------------------------------------------------------

    def _call(self, url: str, body: dict | None, handle, cancel=None, timeout: float | None = None):
        """One request. `handle(response)` reads the answer; `cancel` turning true drops the connection."""
        parts = urlsplit(url)
        cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        conn = cls(parts.netloc, timeout=timeout or self.TIMEOUT)
        finished = threading.Event()

        def watch() -> None:  # a blocked read cannot look at `cancel` itself
            while not finished.wait(0.3):
                if cancel():
                    sock = conn.sock
                    try:
                        if sock:
                            sock.shutdown(socket.SHUT_RDWR)  # wakes a blocked read on Linux and macOS
                    except OSError:
                        pass
                    try:
                        if sock:
                            socket.close(sock.detach())  # ...and closing it under the read does on Windows
                    except OSError:
                        pass
                    return

        if cancel:
            threading.Thread(target=watch, daemon=True).start()
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        try:
            conn.request("POST" if body is not None else "GET", parts.path + ("?" + parts.query if parts.query else ""),
                         body=json.dumps(body).encode("utf-8") if body is not None else None, headers=headers)
            res = conn.getresponse()
            if res.status >= 400:
                raise _Refused(res.status, res.read(4000))
            return handle(res)
        except (Cancelled, ProviderError, _Refused):
            raise
        except (OSError, http.client.HTTPException, ValueError) as e:
            if cancel and cancel():
                raise Cancelled() from e
            if isinstance(e, TimeoutError):
                raise ProviderError("network", f"The local model at {self.root} did not answer within "
                                               f"{int(timeout or self.TIMEOUT) // 60 or 1} minutes.") from e
            if isinstance(e, ValueError):
                raise ProviderError("api", f"{self.root} answered, but not like a model server does.") from e
            raise ProviderError("network", f"Could not reach a model server at {self.root} ({e}). Is it running?") from e
        finally:
            finished.set()
            conn.close()

    def describe(self) -> dict:
        """What is at the address: {"kind": "ollama" | "openai", "models": [names]}."""
        def body(res):
            return json.loads(res.read())

        try:
            data = self._call(self.root + "/api/tags", None, body, timeout=15)
            if isinstance(data, dict) and isinstance(data.get("models"), list):
                self.kind = "ollama"
                names = [str(m.get("name") or m.get("model") or "") for m in data["models"] if isinstance(m, dict)]
                return {"kind": "ollama", "models": sorted(n for n in names if n)}
        except _Refused:
            pass  # not Ollama: ask the OpenAI way
        except ProviderError as e:
            if e.kind == "network":
                raise
        try:
            data = self._call(self.v1 + "/models", None, body, timeout=15)
        except _Refused as e:
            if e.status in (401, 403):
                raise ProviderError("auth", f"The server at {self.root} wants an API key, or rejected this one.") from e
            raise ProviderError("api", f"Something answers at {self.root}, but it does not list models the way "
                                       "Ollama or an OpenAI-style server does. Is that the right address?") from e
        items = data.get("data") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise ProviderError("api", f"{self.root} answered, but not like a model server does.")
        self.kind = "openai"
        return {"kind": "openai", "models": sorted(str(m["id"]) for m in items if isinstance(m, dict) and m.get("id"))}

    # --- one completion -----------------------------------------------------

    def complete(self, model: str, system: str, user: str,
                 on_text: Callable[[str], None] | None = None,
                 cancel: Callable[[], bool] | None = None, max_tokens: int = 16000) -> Completion:
        while not self._gate.acquire(timeout=0.3):
            if cancel and cancel():
                raise Cancelled()
        try:
            if not self.kind:
                try:
                    self.describe()
                except ProviderError as e:
                    if e.kind != "api":
                        raise
                    self.kind = "openai"  # no model list to go by; the chat route may still be there
            room = max(256, min(max_tokens, self.ctx // 2))  # the reply has to fit in the context too
            messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
            if self.kind == "ollama":
                base = {"model": model, "stream": True, "messages": messages,
                        "options": {"num_ctx": self.ctx, "num_predict": room, "temperature": 0.3}}
                url, variants = self.root + "/api/chat", [{**base, "think": False}, base]
            else:
                base = {"model": model, "stream": True, "messages": messages, "max_tokens": room}
                url = self.v1 + "/chat/completions"
                variants = [{**base, "temperature": 0.3, "stream_options": {"include_usage": True}},
                            {**base, "temperature": 0.3}, base]
            i = min(self._skip.get(model, 0), len(variants) - 1)
            while True:
                try:
                    done = self._call(url, variants[i], lambda res: self._read(res, on_text, cancel), cancel)
                    break
                except _Refused as e:
                    if e.status in (400, 422) and i + 1 < len(variants):
                        i += 1  # an optional field this server or model does not take: drop it and remember
                        self._skip[model] = i
                        continue
                    if e.status in (401, 403):
                        raise ProviderError("auth", "The local model server wants an API key, or rejected this one.") from e
                    if e.status == 404:
                        raise ProviderError("model", f"The server at {self.root} has no model called {model}"
                                                     + (f" ({e.said})." if e.said else ".")) from e
                    raise ProviderError("api", f"The local model server answered {e.status}"
                                               + (f": {e.said}" if e.said else ".")) from e
            if not done.inp:
                done.inp = (len(system) + len(user)) // 3
            if not done.out:
                done.out = len(done.text) // 3
            return done
        finally:
            self._gate.release()

    def _read(self, res, on_text, cancel) -> Completion:
        """Collect a streamed reply: one JSON object per line (Ollama) or `data:` lines (OpenAI style)."""
        raw, sent, stop, inp, out, plain = "", 0, "end_turn", 0, 0, []
        for line in res:
            if cancel and cancel():
                raise Cancelled()
            line = line.decode("utf-8", "replace").strip()
            if line.startswith("data:"):
                line = line[5:].strip()
            elif self.kind != "ollama":
                plain.append(line)  # not a stream after all: put the body back together below
                continue
            if not line or line == "[DONE]":
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            piece, last = self._piece(obj)
            if last.get("stop"):
                stop = last["stop"]
            inp, out = last.get("inp") or inp, last.get("out") or out
            if piece:
                raw += piece
                shown = _visible(raw)
                if on_text and len(shown) > sent:
                    on_text(shown[sent:])
                sent = max(sent, len(shown))
        if cancel and cancel():
            raise Cancelled()  # the dropped connection reads as an ordinary end of the reply
        if not raw and plain:
            try:
                piece, last = self._piece(json.loads("".join(plain)))
            except ValueError:
                piece, last = "", {}
            raw, stop = piece, last.get("stop") or stop
            inp, out = last.get("inp") or inp, last.get("out") or out
        text = _THINK.sub("", raw)
        if on_text and len(text) > sent:
            on_text(text[sent:])
        return Completion(text, stop, inp, out)

    @staticmethod
    def _piece(obj) -> tuple[str, dict]:
        """(text in this part of the reply, what it says about how the reply ended)."""
        if not isinstance(obj, dict):
            return "", {}
        if obj.get("error"):
            raise ProviderError("api", "The local model reported: " + (_said(json.dumps(obj).encode()) or "an error"))
        last: dict = {}
        if "choices" in obj or "usage" in obj:  # OpenAI style
            choice = (obj.get("choices") or [{}])[0] or {}
            part = choice.get("delta") or choice.get("message") or {}
            if choice.get("finish_reason") == "length":
                last["stop"] = "max_tokens"
            usage = obj.get("usage") or {}
            last["inp"], last["out"] = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
            return str(part.get("content") or ""), last
        if obj.get("done"):
            last["inp"], last["out"] = int(obj.get("prompt_eval_count") or 0), int(obj.get("eval_count") or 0)
            if obj.get("done_reason") == "length":
                last["stop"] = "max_tokens"
        return str((obj.get("message") or {}).get("content") or ""), last


class Router:
    """Sends each request to the backend its model belongs to: Claude, or the local server.

    One file can then be translated by one and proofread by the other. Claude's client is only
    made when a Claude model is first asked for, so a local model needs no API key at all.
    """

    def __init__(self, api_key: str | None = None, local: dict | None = None):
        self._key, self._claude, self._lock = api_key, None, threading.Lock()
        self.local = LocalProvider(local["url"], local.get("key", ""), local.get("ctx", 4096)) \
            if local and local.get("url") else None

    def complete(self, model: str, system: str, user: str, on_text=None, cancel=None,
                 max_tokens: int = 16000) -> Completion:
        if is_local(model):
            if not self.local:
                raise ProviderError("model", "No local model server is set up. Add its address in Settings.")
            return self.local.complete(model[len(LOCAL):], system, user, on_text, cancel, max_tokens)
        with self._lock:
            if self._claude is None:
                self._claude = AnthropicProvider(self._key)
        return self._claude.complete(model, system, user, on_text, cancel, max_tokens)


_PSEUDO = str.maketrans("aeiouAEIOU", "áéíöüÁÉÍÖÜ")


def pseudo(text: str) -> str:
    """Accent the vowels, leaving <tags> alone: visibly 'translated', same shape."""
    return "".join(p if p.startswith("<") else p.translate(_PSEUDO) for p in re.split(r"(<[^>]+>)", text))


class MockProvider:
    """Offline stand-in: no key, no network, no cost. Used by the tests and `--mock`.

    `drop` lists line numbers to leave out of the first reply, `refuse` makes any request
    containing that text come back as a refusal, `cut` makes the first reply run into
    the output limit half way through that line number, and `dot` lists line numbers that
    come back as a lone full stop the first time, to exercise the repair paths.

    A proofreading request gets the corrections in `fixes` ({line number: new text}). Without
    any, every eighth line of four words or more loses a word, so the demo has something to show.
    """

    def __init__(self, delay: float = 0.0, drop: tuple = (), refuse: str = "", chatter: bool = False,
                 cut: int = 0, dot: tuple = (), fixes: dict | None = None):
        self.delay, self.drop, self.refuse, self.chatter = delay, set(drop), refuse, chatter
        self.cut, self.dot, self.fixes = cut, set(dot), fixes
        self.calls = 0
        self.requests: list[str] = []

    def complete(self, model, system, user, on_text=None, cancel=None, max_tokens=16000) -> Completion:
        self.calls += 1
        self.requests.append(user)
        inp = (len(system) + len(user)) // 4
        if "Proofread:\n" in user:
            return self._proofread(user.rsplit("Proofread:\n", 1)[-1], inp, cancel)
        body = user.rsplit("Translate:\n", 1)[-1]
        if self.refuse and self.refuse in body:
            return Completion("", "refusal", inp, 0)
        lines = ["Here is the translation:", ""] if self.chatter else []
        stop = "end_turn"
        for line in body.split("\n"):
            m = re.match(r"(\d+)\|(.*)", line)
            if m and not (self.calls == 1 and int(m.group(1)) in self.drop):
                out = f"{m.group(1)}|{pseudo(m.group(2))}"
                if self.calls == 1 and int(m.group(1)) in self.dot:
                    out = f"{m.group(1)}|."
                if self.calls == 1 and int(m.group(1)) == self.cut:
                    lines.append(out[:len(out) // 2 + 1])  # the limit lands in the middle of this line
                    stop = "max_tokens"
                    break
                lines.append(out)
        for k, line in enumerate(lines):
            if cancel and cancel():
                raise Cancelled()
            if on_text:
                unfinished = stop == "max_tokens" and k == len(lines) - 1
                on_text(line if unfinished else line + "\n")
            if self.delay:
                time.sleep(self.delay)
        text = "\n".join(lines)
        return Completion(text, stop, inp, len(text) // 3)

    def _proofread(self, body: str, inp: int, cancel) -> Completion:
        for _ in range(20 if self.delay else 0):  # long enough to see "Proofreading…" in the demo
            if cancel and cancel():
                raise Cancelled()
            time.sleep(self.delay)
        if self.fixes is not None:
            lines = [f"{n}|{text}" for n, text in sorted(self.fixes.items())]
        else:
            lines = []
            for line in body.split("\n"):
                m = re.match(r"(\d+)\|.* => (.*)", line)
                words = m.group(2).split() if m else []
                if m and int(m.group(1)) % 8 == 3 and len(words) >= 4:
                    lines.append(f"{m.group(1)}|{' '.join(words[:-2] + words[-1:])}")
        text = "\n".join(lines) or "OK"
        return Completion(text, "end_turn", inp, len(text) // 3)


def local_settings(cfg: dict) -> dict | None:
    """The local server out of the settings, as `build` wants it (None when there is none)."""
    if not (cfg.get("local_url") or "").strip():
        return None
    return {"url": cfg["local_url"], "key": cfg.get("local_key") or "", "ctx": cfg.get("local_ctx") or 4096}


def build(api_key: str | None = None, mock: bool = False, local: dict | None = None):
    """The provider to use: the offline mock when asked for, otherwise Claude and any local server."""
    return MockProvider(delay=0.03) if mock else Router(api_key, local)
