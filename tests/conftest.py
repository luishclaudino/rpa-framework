"""Fixtures e hooks compartilhados da suíte de testes do rpa_3003 (seção 12.5 do PRD)."""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Final
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

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


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Substitui time.sleep por função que apenas registra as durações solicitadas."""
    calls: list[float] = []

    def fake_sleep(seconds: float) -> None:
        calls.append(seconds)

    monkeypatch.setattr(time, "sleep", fake_sleep)
    return calls


@pytest.fixture
def projects_dir(tmp_path: Path) -> Path:
    """Pasta `projects/` vazia dentro de `tmp_path`."""
    path = tmp_path / "projects"
    path.mkdir()
    return path


@pytest.fixture
def sample_csv(tmp_path: Path) -> Path:
    """CSV ';' em utf-8-sig com cabeçalho 'id;nome;valor' e 3 linhas acentuadas."""
    path = tmp_path / "amostra.csv"
    content = "id;nome;valor\r\n1;José;10,50\r\n2;Conceição;1.500,00\r\n3;Açaí Ltda;0,99\r\n"
    path.write_bytes(content.encode("utf-8-sig"))
    return path


@pytest.fixture
def sample_pdf(tmp_path: Path) -> Path:
    """PDF de 3 páginas gerado com reportlab, contendo 'Página 1/2/3' e 'Ação R$ 1.500,00'."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    path = tmp_path / "amostra.pdf"
    document = canvas.Canvas(str(path), pagesize=A4)
    for number in range(1, 4):
        document.setFont("Helvetica", 14)
        document.drawString(72, 760, f"Página {number}")
        document.drawString(72, 730, "Ação R$ 1.500,00")
        document.showPage()
    document.save()
    return path


@pytest.fixture
def read_log_lines() -> Callable[[Path], list[str]]:
    """Função que chama logger.complete() e retorna as linhas de todos os *.log da pasta."""

    def read(logs_dir: Path) -> list[str]:
        from loguru import logger

        logger.complete()
        lines: list[str] = []
        for log_file in sorted(logs_dir.glob("*.log")):
            lines.extend(log_file.read_text(encoding="utf-8").splitlines())
        return lines

    return read


@pytest.fixture
def fake_hvac(mocker: MockerFixture) -> MagicMock:
    """Substitui hvac.Client por MagicMock configurável (autenticação e leituras KV v2).

    Retorna a classe simulada; o cliente criado pelo framework é ``fake_hvac.return_value``.
    Por padrão o token é válido e toda leitura KV v2 retorna ``username``/``password``.
    """
    client_class: MagicMock = mocker.patch("hvac.Client")
    client = client_class.return_value
    client.is_authenticated.return_value = True
    client.secrets.kv.v2.read_secret_version.return_value = {
        "data": {"data": {"username": "robo.teste", "password": "S3nh@Forte"}}
    }
    client.secrets.kv.v1.read_secret.return_value = {
        "data": {"username": "robo.teste", "password": "S3nh@Forte"}
    }
    return client_class


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Fora do Windows, adiciona skip aos itens marcados com windows, gui ou browser."""
    if sys.platform == "win32":
        return
    skip_marker = pytest.mark.skip(reason="requer Windows (P-22)")
    for item in items:
        if any(item.get_closest_marker(name) for name in WINDOWS_ONLY_MARKERS):
            item.add_marker(skip_marker)
