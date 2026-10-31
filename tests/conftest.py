"""Fixtures e hooks compartilhados da suíte de testes do rpa_3003 (seção 12.5 do PRD)."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from typing import Final

import pytest

ISOLATED_ENV_PREFIXES: Final[tuple[str, ...]] = ("RPA3003_", "VAULT_")
# Configuração dos testes de integração (seção 12.4); não é configuração do framework.
PRESERVED_ENV_PREFIX: Final[str] = "RPA3003_TEST_"
RESET_HOOKS: Final[tuple[tuple[str, str], ...]] = (
    ("rpa_3003.core.logger", "reset_logger"),
    ("rpa_3003.core.logger", "clear_secrets"),
    ("rpa_3003.core.config", "reset_settings_cache"),
    ("rpa_3003.core.vault", "reset_vault_clients"),
)
WINDOWS_ONLY_MARKERS: Final[tuple[str, ...]] = ("windows", "gui", "browser")


def _reset_framework_state() -> None:
    """Chama as funções de reset dos módulos do framework já importados.

    Tolerante à ausência de módulos e funções enquanto o core não está implementado. Módulos
    ainda não importados não têm estado a restaurar, por isso não são importados aqui.
    """
    for module_name, function_name in RESET_HOOKS:
        reset = getattr(sys.modules.get(module_name), function_name, None)
        if callable(reset):
            reset()


@pytest.fixture(autouse=True)
def _isolate_framework_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Remove variáveis RPA3003_* e VAULT_* do ambiente; após o teste chama reset_logger(),
    clear_secrets(), reset_settings_cache() e reset_vault_clients()."""
    for name in list(os.environ):
        upper_name = name.upper()
        if upper_name.startswith(ISOLATED_ENV_PREFIXES) and not upper_name.startswith(
            PRESERVED_ENV_PREFIX
        ):
            monkeypatch.delenv(name, raising=False)
    yield
    _reset_framework_state()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Fora do Windows, adiciona skip aos itens marcados com windows, gui ou browser."""
    if sys.platform == "win32":
        return
    skip_marker = pytest.mark.skip(reason="requer Windows (P-22)")
    for item in items:
        if any(item.get_closest_marker(name) for name in WINDOWS_ONLY_MARKERS):
            item.add_marker(skip_marker)
