"""Integração com PostgreSQL real (RF-043, RNF-010, P-24, seção 8.15).

Requer `RPA3003_TEST_PG_HOST`, `RPA3003_TEST_PG_DB`, `RPA3003_TEST_PG_USER` e
`RPA3003_TEST_PG_PASSWORD` (`RPA3003_TEST_PG_PORT` é opcional, padrão 5432); sem elas, os testes
são pulados (seção 12.4). Execute com `pytest -m integration`. O usuário deve ter somente
privilégio de consulta (P-24).
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from pydantic import SecretStr
from pytest_mock import MockerFixture

from rpa_3003.core.exceptions import DatabaseError
from rpa_3003.utils import database_utils
from rpa_3003.utils.database_utils import Database, DatabaseConfig, PostgreSQLDatabase, connect

pytestmark = pytest.mark.integration

PREFIX = "RPA3003_TEST_PG_"
REQUIRED = ("HOST", "DB", "USER", "PASSWORD")


@pytest.fixture
def pg_config() -> DatabaseConfig:
    """Configuração lida das variáveis RPA3003_TEST_PG_*; pula o teste se faltar alguma."""
    values = {name: os.environ.get(PREFIX + name, "").strip() for name in (*REQUIRED, "PORT")}
    missing = [PREFIX + name for name in REQUIRED if not values[name]]
    if missing:
        pytest.skip(f"variáveis ausentes: {', '.join(missing)}")
    return DatabaseConfig(
        engine="postgresql",
        host=values["HOST"],
        port=int(values["PORT"]) if values["PORT"] else None,
        database=values["DB"],
        user=values["USER"],
        password=SecretStr(values["PASSWORD"]),
    )


@pytest.fixture
def pg(pg_config: DatabaseConfig) -> Iterator[Database]:
    with connect(pg_config) as db:
        yield db


def test_select_1_returns_dict(pg: Database) -> None:
    """RF-043: fetch_all("SELECT 1 AS n") retorna [{"n": 1}]."""
    assert isinstance(pg, PostgreSQLDatabase)
    assert pg.fetch_all("SELECT 1 AS n") == [{"n": 1}]


def test_transactions_are_read_only(pg: Database) -> None:
    """Seção 8.15: cada transação começa com BEGIN READ ONLY (set_session readonly)."""
    assert pg.fetch_scalar("SELECT current_setting('transaction_read_only')") == "on"


def test_create_temp_table_fails_with_sqlstate_25006(pg: Database, mocker: MockerFixture) -> None:
    """RF-043: com a validação desativada, CREATE TEMP TABLE falha com SQLSTATE 25006."""
    mocker.patch.object(database_utils, "_read_only_violation", return_value=None)
    with pytest.raises(DatabaseError) as info:
        pg.fetch_all("CREATE TEMP TABLE rpa3003_teste_escrita (id integer)")
    assert info.value.details["sqlstate"] == "25006"
    mocker.stopall()
    assert pg.fetch_scalar("SELECT 1") == 1  # a conexão continua utilizável após o rollback


def test_write_is_blocked_before_reaching_server(pg: Database) -> None:
    """RF-043: instrução de escrita é bloqueada pela validação antes do envio."""
    with pytest.raises(DatabaseError, match="somente leitura"):
        pg.fetch_all("DELETE FROM pg_catalog.pg_class")


def test_params_iter_rows_and_table_exists(pg: Database) -> None:
    """RF-043: parâmetros pyformat, iter_rows com cursor nomeado em lotes e table_exists."""
    assert pg.fetch_scalar("SELECT %s::int + 1", (1,)) == 2
    named = pg.fetch_one("SELECT %(a)s::int AS a, %(b)s::text AS b", {"a": 2, "b": "ação"})
    assert named == {"a": 2, "b": "ação"}
    total = sum(
        row["n"] for row in pg.iter_rows("SELECT generate_series(1, 2500) AS n", batch_size=1000)
    )
    assert total == 2500 * 2501 // 2
    assert pg.table_exists("tables", schema="information_schema") is True
    assert pg.table_exists("rpa3003_tabela_inexistente") is False
