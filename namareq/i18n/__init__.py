"""Arabic / English message catalogues.

Both catalogues must define exactly the same keys (enforced by tests).
Placeholders use ``{name}``; money placeholders are formatted by the caller.
"""
from __future__ import annotations

import string

from .ar import MESSAGES as AR
from .en import MESSAGES as EN

CATALOGUES = {"ar": AR, "en": EN}
LANGS = ("ar", "en")
DIRECTION = {"ar": "rtl", "en": "ltr"}

# Message parameters that hold minor-unit money amounts.
MONEY_PARAMS = {"amount", "saved", "extra", "required", "outstanding", "available", "debits", "credits",
                "plan", "original", "excluded"}


class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


# Keys requested but missing from the catalogue (checked by the test suite).
MISSING: set[str] = set()


def translate(lang: str, key: str, **params) -> str:
    catalogue = CATALOGUES.get(lang, AR)
    text = catalogue.get(key)
    if text is None:
        MISSING.add(key)
        text = key
    if params:
        return string.Formatter().vformat(text, (), _SafeDict(params))
    return text


def has(key: str) -> bool:
    return key in EN
