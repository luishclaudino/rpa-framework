"""Integração com Denodo Virtual DataPort real (RF-043, P-08, P-24, riscos R-11 e R-20).

Requer `RPA3003_TEST_DENODO_HOST`, `RPA3003_TEST_DENODO_DB`, `RPA3003_TEST_DENODO_USER` e
`RPA3003_TEST_DENODO_PASSWORD` (`RPA3003_TEST_DENODO_PORT` é opcional, padrão 9996); sem elas,
os testes são pulados (seção 12.4). Execute com `pytest -m integration`.

O DoD da Sprint 3 exige executar este arquivo ao menos uma vez contra o Denodo corporativo e
registrar o resultado em `docs/DESVIOS.md`, com a confirmação da equipe de dados de que o
usuário do robô só tem privilégio de consulta: o Virtual DataPort não garante `BEGIN READ ONLY`,
e a proteção de escrita vem da validação de instruções e desse privilégio.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from pydantic import SecretStr

from rpa_3003.core.exceptions import DatabaseError
from rpa_3003.utils.database_utils import Database, DatabaseConfig, DenodoDatabase, connect

pytestmark = pytest.mark.integration

PREFIX = "RPA3003_TEST_DENODO_"
REQUIRED = ("HOST", "DB", "USER", "PASSWORD")


@pytest.fixture
def denodo_config() -> DatabaseConfig:
    """Configuração lida das variáveis RPA3003_TEST_DENODO_*; pula o teste se faltar alguma."""
    values = {name: os.environ.get(PREFIX + name, "").strip() for name in (*REQUIRED, "PORT")}
    missing = [PREFIX + name for name in REQUIRED if not values[name]]
    if missing:
        pytest.skip(f"variáveis ausentes: {', '.join(missing)}")
    return DatabaseConfig(
        engine="denodo",
        host=values["HOST"],
        port=int(values["PORT"]) if values["PORT"] else None,
        database=values["DB"],
        user=values["USER"],
        password=SecretStr(values["PASSWORD"]),
    )


@pytest.fixture
def denodo(denodo_config: DatabaseConfig) -> Iterator[Database]:
    with connect(denodo_config) as db:
        yield db


def test_select_1_returns_dict(denodo: Database) -> None:
    """RF-043: fetch_all("SELECT 1 AS n") retorna [{"n": 1}] no Denodo."""
    assert isinstance(denodo, DenodoDatabase)
    assert denodo.fetch_all("SELECT 1 AS n") == [{"n": 1}]


def test_iter_rows_with_fetchmany(denodo: Database) -> None:
    """Seção 8.15: iter_rows com fetchmany (sem cursor nomeado) funciona no Denodo."""
    assert list(denodo.iter_rows("SELECT 1 AS n", batch_size=1)) == [{"n": 1}]


def test_write_is_blocked_before_reaching_server(denodo: Database) -> None:
    """RF-043/R-11: sem modo somente leitura no driver, a validação bloqueia a escrita."""
    with pytest.raises(DatabaseError, match="somente leitura"):
        denodo.fetch_all("INSERT INTO rpa3003_inexistente VALUES (1)")
    with pytest.raises(DatabaseError, match="somente leitura"):
        denodo.fetch_all("SELECT 1; DROP VIEW rpa3003_inexistente")


def test_table_exists_for_missing_view(denodo: Database) -> None:
    """Seção 8.15: table_exists executa SELECT * FROM <nome> WHERE 1 = 0; ausência é False."""
    assert denodo.table_exists("rpa3003_view_inexistente") is False
    assert denodo.fetch_scalar("SELECT 1 AS n") == 1  # a conexão continua utilizável
