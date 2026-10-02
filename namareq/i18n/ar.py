"""Arabic messages (derived from catalog.py)."""
from .catalog import CATALOG

MESSAGES = {key: pair[1] for key, pair in CATALOG.items()}
