"""Target languages offered in the picker. Any other language works too: type its name."""

LANGUAGES = [
    ("ar", "Arabic"), ("bg", "Bulgarian"), ("ca", "Catalan"), ("zh", "Chinese (Simplified)"),
    ("zh-TW", "Chinese (Traditional)"), ("hr", "Croatian"), ("cs", "Czech"), ("da", "Danish"),
    ("nl", "Dutch"), ("en", "English"), ("et", "Estonian"), ("fi", "Finnish"), ("fr", "French"),
    ("de", "German"), ("el", "Greek"), ("he", "Hebrew"), ("hi", "Hindi"), ("hu", "Hungarian"),
    ("is", "Icelandic"), ("id", "Indonesian"), ("it", "Italian"), ("ja", "Japanese"),
    ("ko", "Korean"), ("lv", "Latvian"), ("lt", "Lithuanian"), ("nb", "Norwegian"),
    ("fa", "Persian"), ("pl", "Polish"), ("pt", "Portuguese"), ("pt-BR", "Portuguese (Brazil)"),
    ("ro", "Romanian"), ("ru", "Russian"), ("sr", "Serbian"), ("sk", "Slovak"), ("sl", "Slovenian"),
    ("es", "Spanish"), ("es-419", "Spanish (Latin America)"), ("sv", "Swedish"), ("th", "Thai"),
    ("tr", "Turkish"), ("uk", "Ukrainian"), ("vi", "Vietnamese"),
]

_BY_CODE = {code.lower(): name for code, name in LANGUAGES}
_BY_NAME = {name.lower(): code for code, name in LANGUAGES}


def resolve(value: str) -> tuple[str, str]:
    """'fi' or 'Finnish' -> ('fi', 'Finnish'). Unknown names get a made-up three-letter code."""
    v = (value or "").strip()
    low = v.lower()
    if low in _BY_CODE:
        return next(c for c, _ in LANGUAGES if c.lower() == low), _BY_CODE[low]
    if low in _BY_NAME:
        return _BY_NAME[low], next(n for _, n in LANGUAGES if n.lower() == low)
    if not v:
        return "", ""
    code = "".join(ch for ch in low if ch.isalpha())[:3] or "xx"
    return code, v[:1].upper() + v[1:]


# ISO 639-2 codes (both the bibliographic and terminology forms) as they appear on tracks inside
# video files and in subtitle file names.
_ISO3 = {
    "ar": "ara", "bg": "bul", "ca": "cat", "zh": "chi zho", "hr": "hrv", "cs": "cze ces", "da": "dan",
    "nl": "dut nld", "en": "eng", "et": "est", "fi": "fin", "fr": "fre fra", "de": "ger deu", "el": "gre ell",
    "he": "heb", "hi": "hin", "hu": "hun", "is": "ice isl", "id": "ind", "it": "ita", "ja": "jpn", "ko": "kor",
    "lv": "lav", "lt": "lit", "nb": "nob nor no", "fa": "per fas", "pl": "pol", "pt": "por", "ro": "rum ron",
    "ru": "rus", "sr": "srp", "sk": "slo slk", "sl": "slv", "es": "spa", "sv": "swe", "th": "tha", "tr": "tur",
    "uk": "ukr", "vi": "vie",
}


def variants(code: str) -> set[str]:
    """Every tag that means this language: 'fi' -> {'fi', 'fin', 'finnish'}."""
    low = (code or "").lower()
    if not low:
        return set()
    base = low.split("-")[0]
    out = {low, base, *_ISO3.get(base, "").split()}
    out |= {name.lower().split(" (")[0] for c, name in LANGUAGES if c.lower() in (low, base)}
    if low == "pt-br":
        out |= {"pob", "pb"}
    return out - {""}
