"""Normalise the many ways servers spell a language into ISO 639-1 codes."""

from __future__ import annotations

_ALIASES = {
    "ja": ("ja", "jp", "jpn", "japanese", "日本語", "nihongo"),
    "en": ("en", "eng", "english", "英語"),
    "zh": ("zh", "chi", "zho", "chinese", "中文", "中国語", "mandarin", "cantonese", "yue", "cmn"),
    "ko": ("ko", "kor", "korean", "한국어", "韓国語"),
    "es": ("es", "spa", "spanish", "español"),
    "fr": ("fr", "fre", "fra", "french", "français"),
    "de": ("de", "ger", "deu", "german", "deutsch"),
    "it": ("it", "ita", "italian"),
    "pt": ("pt", "por", "portuguese"),
    "ru": ("ru", "rus", "russian"),
}
_LOOKUP = {alias: code for code, aliases in _ALIASES.items() for alias in aliases}
_UNKNOWN = {"", "und", "unknown", "mul", "zxx", "none", "null"}

# kechimochi stores a human-readable language name on media entries.
DISPLAY_NAMES = {
    "ja": "Japanese", "en": "English", "zh": "Chinese", "ko": "Korean", "es": "Spanish",
    "fr": "French", "de": "German", "it": "Italian", "pt": "Portuguese", "ru": "Russian",
}


def normalize(value: str | None) -> str | None:
    """Return an ISO 639-1 code (or a best-effort lowercase tag), or None if unknown."""
    if value is None:
        return None
    v = str(value).strip().lower()
    if v in _UNKNOWN:
        return None
    if v in _LOOKUP:
        return _LOOKUP[v]
    # Region / script subtags: "ja-JP", "zh_Hant", "en-US".
    base = v.replace("_", "-").split("-", 1)[0]
    if base in _LOOKUP:
        return _LOOKUP[base]
    return base or None


def display_name(code: str | None) -> str:
    if code is None:
        return "Japanese"
    return DISPLAY_NAMES.get(code, code)
