"""English messages (derived from catalog.py)."""
from .catalog import CATALOG

MESSAGES = {key: pair[0] for key, pair in CATALOG.items()}
