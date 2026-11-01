"""rpa_3003 — framework RPA corporativo para Windows."""

from __future__ import annotations

from rpa_3003._version import __version__
from rpa_3003.core.config import Environment
from rpa_3003.core.logger import get_logger, setup_logger

__all__: list[str] = ["Environment", "__version__", "get_logger", "setup_logger"]
