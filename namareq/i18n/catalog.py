"""Single source of truth for translations: every key carries an
(English, Arabic) pair, so the two languages can never drift apart."""
from .catalog_advice import ADVICE, ERRORS
from .catalog_enums import ENUMS
from .catalog_ui import UI

CATALOG: dict[str, tuple[str, str]] = {}
for _section in (UI, ENUMS, ADVICE, ERRORS):
    for _key, _pair in _section.items():
        if _key in CATALOG:
            raise ValueError(f"duplicate translation key: {_key}")
        CATALOG[_key] = _pair
