"""Núcleo do framework: logging, configuração e exceções.

Não importa `vault` nem `workflow`, para não carregar `hvac` e `filelock` em todo import.
"""

from __future__ import annotations

from rpa_3003.core.config import (
    Environment,
    FrameworkSettings,
    ProjectPaths,
    get_settings,
    load_settings,
)
from rpa_3003.core.exceptions import BusinessRuleError, ConfigurationError, ExitCode, RPAError
from rpa_3003.core.logger import get_logger, register_secret, setup_logger

__all__: list[str] = [
    "BusinessRuleError",
    "ConfigurationError",
    "Environment",
    "ExitCode",
    "FrameworkSettings",
    "ProjectPaths",
    "RPAError",
    "get_logger",
    "get_settings",
    "load_settings",
    "register_secret",
    "setup_logger",
]
