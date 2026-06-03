"""Easter egg tools package."""

from .loader import (
    load_easter_eggs,
    ensure_easter_eggs_loaded,
    EasterEggsLoaderHook,
)
from .easter_egg_tools import (
    download_more_ram,
    get_system_memory,
    letter_counter,
)

__all__ = [
    # Loader
    "load_easter_eggs",
    "ensure_easter_eggs_loaded",
    "EasterEggsLoaderHook",
    # Easter egg tools
    "download_more_ram",
    "get_system_memory",
    "letter_counter",
]
