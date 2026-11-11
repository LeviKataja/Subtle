"""Model backends. The engine only needs `complete()`; everything else stays in here."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable

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
        import anthropic

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


_PSEUDO = str.maketrans("aeiouAEIOU", "áéíöüÁÉÍÖÜ")


def pseudo(text: str) -> str:
    """Accent the vowels, leaving <tags> alone: visibly 'translated', same shape."""
    return "".join(p if p.startswith("<") else p.translate(_PSEUDO) for p in re.split(r"(<[^>]+>)", text))


class MockProvider:
    """Offline stand-in: no key, no network, no cost. Used by the tests and `--mock`.

    `drop` lists line numbers to leave out of the first reply, `refuse` makes any request
    containing that text come back as a refusal, and `cut` makes the first reply run into
    the output limit half way through that line number, to exercise the repair paths.
    """

    def __init__(self, delay: float = 0.0, drop: tuple = (), refuse: str = "", chatter: bool = False,
                 cut: int = 0):
        self.delay, self.drop, self.refuse, self.chatter = delay, set(drop), refuse, chatter
        self.cut = cut
        self.calls = 0
        self.requests: list[str] = []

    def complete(self, model, system, user, on_text=None, cancel=None, max_tokens=16000) -> Completion:
        self.calls += 1
        self.requests.append(user)
        body = user.rsplit("Translate:\n", 1)[-1]
        inp = (len(system) + len(user)) // 4
        if self.refuse and self.refuse in body:
            return Completion("", "refusal", inp, 0)
        lines = ["Here is the translation:", ""] if self.chatter else []
        stop = "end_turn"
        for line in body.split("\n"):
            m = re.match(r"(\d+)\|(.*)", line)
            if m and not (self.calls == 1 and int(m.group(1)) in self.drop):
                out = f"{m.group(1)}|{pseudo(m.group(2))}"
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


def build(api_key: str | None = None, mock: bool = False):
    """The provider to use: the offline mock when asked for, otherwise Claude."""
    return MockProvider(delay=0.03) if mock else AnthropicProvider(api_key)
