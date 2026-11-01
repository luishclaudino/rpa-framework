"""Testes de `rpa_3003.utils.database_utils` (RF-043, RNF-010, P-24, seção 8.15)."""

# Credenciais e senhas deste arquivo são fictícias (valores de teste, não segredos).
# ruff: noqa: S106

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import MagicMock

import duckdb
import psycopg2
import pytest
from psycopg2.extras import RealDictCursor
from pydantic import SecretStr, ValidationError
from pytest_mock import MockerFixture

from rpa_3003.core.exceptions import ConfigurationError, DatabaseError
from rpa_3003.core.logger import MASK, get_logger
from rpa_3003.core.vault import Credentials
from rpa_3003.utils import database_utils
from rpa_3003.utils.database_utils import (
    Database,
    DatabaseConfig,
    DenodoDatabase,
    DuckDBDatabase,
    PostgreSQLDatabase,
    SQLiteDatabase,
    available_engines,
    connect,
    create_database,
    register_engine,
)

pytestmark = pytest.mark.unit

FORNECEDORES: list[tuple[int, str, str, int]] = [
    (1, "11222333000181", "Açaí Ltda", 1),
    (2, "22333444000192", "Conceição & Filhos", 1),
    (3, "33444555000103", "José Peças", 0),
    (4, "44555666000114", "Ótica Visão", 1),
    (5, "55666777000125", "Útil Comércio", 1),
]
BLOCKED_STATEMENTS: list[str] = [
    "INSERT INTO fornecedores (id) VALUES (9)",
    "UPDATE fornecedores SET nome = 'x'",
    "DELETE FROM fornecedores",
    "CREATE TABLE t (id INTEGER)",
    "WITH apagados AS (DELETE FROM fornecedores RETURNING *) SELECT * FROM apagados",
    "SELECT 1; DROP TABLE t",
    "MERGE INTO fornecedores USING x ON 1 = 1",
    "TRUNCATE fornecedores",
    "ALTER TABLE fornecedores ADD COLUMN x INTEGER",
    "DROP TABLE fornecedores",
    "SELECT * INTO copia FROM fornecedores",
    "SELECT * FROM fornecedores FOR UPDATE",
    "EXPLAIN ANALYZE DELETE FROM fornecedores",
    "PRAGMA query_only = OFF",
    "ATTACH DATABASE 'outro.db' AS outro",
    "COPY fornecedores TO 'saida.csv'",
    "EXECUTE plano",
    "select 1 ; delete from fornecedores ;",
    "(SELECT 1) UNION (SELECT 2); INSERT INTO t VALUES (1)",
]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def sqlite_path(tmp_path: Path) -> Path:
    """Banco SQLite criado e populado pelo teste (o framework só lê), em pasta com acento."""
    path = tmp_path / "dados ção" / "cadastro.db"
    path.parent.mkdir()
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE fornecedores (id INTEGER PRIMARY KEY, cnpj TEXT, nome TEXT, ativo INT)"
        )
        conn.executemany("INSERT INTO fornecedores VALUES (?, ?, ?, ?)", FORNECEDORES)
        conn.execute("CREATE VIEW ativos AS SELECT * FROM fornecedores WHERE ativo = 1")
    conn.close()
    return path


@pytest.fixture
def duckdb_path(tmp_path: Path) -> Path:
    """Banco DuckDB criado e populado pelo teste com o próprio driver."""
    path = tmp_path / "analitico.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE Titulos (id INTEGER, status VARCHAR, valor DOUBLE)")
    conn.executemany(
        "INSERT INTO Titulos VALUES (?, ?, ?)",
        [(i, "PENDENTE" if i % 2 else "PAGO", i * 10.5) for i in range(1, 8)],
    )
    conn.close()
    return path


@pytest.fixture
def sqlite_db(sqlite_path: Path) -> Iterator[Database]:
    with connect(DatabaseConfig(engine="sqlite", path=sqlite_path)) as db:
        yield db


@pytest.fixture
def duckdb_db(duckdb_path: Path) -> Iterator[Database]:
    with connect(DatabaseConfig(engine="duckdb", path=duckdb_path)) as db:
        yield db


@pytest.fixture
def log_messages() -> list[tuple[str, str]]:
    """Mensagens registradas no logger (nível, mensagem já mascarada), a partir de DEBUG."""
    from loguru import logger

    get_logger()
    records: list[tuple[str, str]] = []
    logger.add(
        lambda message: records.append((message.record["level"].name, message.record["message"])),
        level="DEBUG",
    )
    return records


@pytest.fixture
def no_validation(mocker: MockerFixture) -> MagicMock:
    """Desativa a validação de instruções (camada 4) para testar a conexão somente leitura."""
    return mocker.patch.object(database_utils, "_read_only_violation", return_value=None)


@pytest.fixture
def pg_connect(mocker: MockerFixture) -> MagicMock:
    """`psycopg2.connect` simulado; a conexão é `pg_connect.return_value`."""
    connect_mock: MagicMock = mocker.patch("psycopg2.connect")
    cursor = connect_mock.return_value.cursor.return_value
    cursor.fetchall.return_value = [{"n": 1}]
    cursor.fetchone.return_value = {"n": 1}
    return connect_mock


def pg_config(**overrides: Any) -> DatabaseConfig:
    values: dict[str, Any] = {
        "engine": "postgresql",
        "host": "pg.interno",
        "database": "financeiro",
        "user": "robo_leitura",
        "password": SecretStr("S3nh@Banco"),
    }
    values.update(overrides)
    return DatabaseConfig(**values)


# --- SQLite e DuckDB reais --------------------------------------------------------------------


def test_sqlite_fetch_all_returns_dicts_with_qmark_and_named_params(sqlite_db: Database) -> None:
    """RF-043: SQLite real, linhas como dict e paramstyle qmark (`?` e `:nome`)."""
    rows = sqlite_db.fetch_all(
        "SELECT id, nome FROM fornecedores WHERE ativo = ? ORDER BY id", (1,)
    )
    assert rows == [
        {"id": 1, "nome": "Açaí Ltda"},
        {"id": 2, "nome": "Conceição & Filhos"},
        {"id": 4, "nome": "Ótica Visão"},
        {"id": 5, "nome": "Útil Comércio"},
    ]
    named = sqlite_db.fetch_all("SELECT nome FROM fornecedores WHERE id = :id", {"id": 3})
    assert named == [{"nome": "José Peças"}]
    assert SQLiteDatabase.paramstyle == "qmark"


def test_sqlite_fetch_one_and_fetch_scalar(sqlite_db: Database) -> None:
    """RF-043: fetch_one retorna a primeira linha ou None; fetch_scalar a primeira coluna."""
    row = sqlite_db.fetch_one("SELECT nome FROM fornecedores WHERE cnpj = ?", ("11222333000181",))
    assert row == {"nome": "Açaí Ltda"}
    assert sqlite_db.fetch_one("SELECT * FROM fornecedores WHERE id = ?", (99,)) is None
    assert sqlite_db.fetch_scalar("SELECT count(*) FROM fornecedores") == 5
    assert sqlite_db.fetch_scalar("SELECT nome FROM fornecedores WHERE id = 99") is None


def test_sqlite_iter_rows_reads_in_batches(sqlite_db: Database, mocker: MockerFixture) -> None:
    """RF-043: iter_rows obtém as linhas em lotes de batch_size, na ordem."""
    spy = mocker.spy(SQLiteDatabase, "_fetch_batch")
    rows = list(sqlite_db.iter_rows("SELECT id FROM fornecedores ORDER BY id", batch_size=2))
    assert [row["id"] for row in rows] == [1, 2, 3, 4, 5]
    assert [call.args[3] for call in spy.call_args_list] == [2, 2, 2, 2]
    assert spy.call_count == 4  # 2 + 2 + 1 + lote vazio


def test_sqlite_table_exists(sqlite_db: Database) -> None:
    """RF-043: table_exists consulta sqlite_master (tabelas e views), sem diferenciar caixa."""
    assert sqlite_db.table_exists("fornecedores") is True
    assert sqlite_db.table_exists("FORNECEDORES") is True
    assert sqlite_db.table_exists("ativos") is True
    assert sqlite_db.table_exists("fornecedores", schema="main") is True
    assert sqlite_db.table_exists("inexistente") is False


@pytest.mark.parametrize("name", ["fornecedores; DROP TABLE x", "1tabela", "a-b", "", "x y"])
def test_table_exists_rejects_invalid_names(sqlite_db: Database, name: str) -> None:
    """Seção 8.15: nomes fora de ^[A-Za-z_][A-Za-z0-9_]*$ geram ValueError."""
    with pytest.raises(ValueError, match="inválido"):
        sqlite_db.table_exists(name)
    with pytest.raises(ValueError, match="schema"):
        sqlite_db.table_exists("fornecedores", schema=name)


def test_duckdb_file_fetch_methods_and_named_params(duckdb_db: Database) -> None:
    """RF-043: DuckDB real em arquivo: fetch_all, fetch_one, fetch_scalar com `?` e `$nome`."""
    pendentes = duckdb_db.fetch_all(
        "SELECT id, valor FROM titulos WHERE status = ? ORDER BY id", ["PENDENTE"]
    )
    assert pendentes == [
        {"id": 1, "valor": 10.5},
        {"id": 3, "valor": 31.5},
        {"id": 5, "valor": 52.5},
        {"id": 7, "valor": 73.5},
    ]
    assert duckdb_db.fetch_one("SELECT status FROM titulos WHERE id = $id", {"id": 2}) == {
        "status": "PAGO"
    }
    assert duckdb_db.fetch_one("SELECT * FROM titulos WHERE id = 99") is None
    assert duckdb_db.fetch_scalar("SELECT count(*) FROM titulos") == 7
    assert DuckDBDatabase.paramstyle == "qmark"


def test_duckdb_iter_rows_and_table_exists(duckdb_db: Database, mocker: MockerFixture) -> None:
    """RF-043: DuckDB real: iter_rows em lotes e table_exists via information_schema."""
    spy = mocker.spy(DuckDBDatabase, "_fetch_batch")
    ids = [
        row["id"] for row in duckdb_db.iter_rows("SELECT id FROM titulos ORDER BY id", batch_size=3)
    ]
    assert ids == [1, 2, 3, 4, 5, 6, 7]
    assert spy.call_count == 4
    assert duckdb_db.table_exists("titulos") is True
    assert duckdb_db.table_exists("TITULOS", schema="MAIN") is True
    assert duckdb_db.table_exists("titulos", schema="outro") is False
    assert duckdb_db.table_exists("inexistente") is False


def test_duckdb_in_memory_reads_csv_files(tmp_path: Path) -> None:
    """Seção 8.15: DuckDB sem path é banco em memória para consultar arquivos (read_csv)."""
    csv_file = tmp_path / "extrato.csv"
    csv_file.write_text("status,valor\nPAGO,1\nPENDENTE,2\nPAGO,3\n", encoding="utf-8")
    with connect(DatabaseConfig(engine="duckdb")) as db:
        rows = db.fetch_all(
            "SELECT status, count(*) AS n FROM read_csv(?) GROUP BY status ORDER BY status",
            [str(csv_file)],
        )
        assert db.table_exists("qualquer") is False
    assert rows == [{"status": "PAGO", "n": 2}, {"status": "PENDENTE", "n": 1}]


def test_duckdb_options_are_passed_as_config(duckdb_path: Path) -> None:
    """Seção 8.15: chaves não reservadas de options vão ao driver (config do DuckDB)."""
    with connect(DatabaseConfig(engine="duckdb", path=duckdb_path, options={"threads": 1})) as db:
        assert db.fetch_scalar("SELECT current_setting('threads')") == 1


def test_auto_connect_on_first_query_and_context_manager(sqlite_path: Path) -> None:
    """Seção 8.15: sem connect() a primeira consulta conecta; o with fecha a conexão."""
    db = create_database(DatabaseConfig(engine="sqlite", path=sqlite_path))
    assert db.is_connected is False
    assert db.fetch_scalar("SELECT 1") == 1
    assert db.is_connected is True
    db.close()
    db.close()  # idempotente
    assert db.is_connected is False
    with db as same:
        assert same is db
        assert db.connect() is db  # já conectado
        assert db.is_connected is True
    assert db.is_connected is False


def test_iter_rows_is_lazy_and_closes_cursor_on_early_exit(sqlite_db: Database) -> None:
    """Seção 8.15: iter_rows só consulta ao iterar e encerra a leitura ao fechar o iterador."""
    iterator = sqlite_db.iter_rows("SELECT id FROM fornecedores ORDER BY id", batch_size=1)
    assert next(iterator) == {"id": 1}
    iterator.close()  # type: ignore[attr-defined]
    assert sqlite_db.fetch_scalar("SELECT count(*) FROM fornecedores") == 5


def test_missing_database_file_raises_database_error(tmp_path: Path) -> None:
    """Seção 8.15: arquivo inexistente gera DatabaseError ao conectar (SQLite e DuckDB)."""
    for engine in ("sqlite", "duckdb"):
        db = create_database(DatabaseConfig(engine=engine, path=tmp_path / "nao_existe.db"))
        with pytest.raises(DatabaseError, match="não encontrado") as info:
            db.connect()
        assert info.value.details["engine"] == engine
        assert info.value.details["arquivo"].endswith("nao_existe.db")
        assert db.is_connected is False


def test_sqlite_connection_uses_read_only_uri_and_query_only(
    sqlite_path: Path, mocker: MockerFixture
) -> None:
    """Seção 8.15: URI `<path>.as_uri()?mode=ro`, uri=True, timeout e PRAGMA query_only."""
    spy = mocker.spy(sqlite3, "connect")
    config = DatabaseConfig(engine="sqlite", path=sqlite_path, connect_timeout=7)
    with connect(config) as db:
        assert db.fetch_scalar("SELECT 1") == 1
        # PRAGMA não é instrução permitida pela API; a verificação usa a conexão nativa.
        assert db._connection.execute("PRAGMA query_only").fetchone()[0] == 1
    args, kwargs = spy.call_args
    assert args[0] == f"{sqlite_path.resolve().as_uri()}?mode=ro"
    assert kwargs["uri"] is True
    assert kwargs["timeout"] == 7


# --- bloqueio de escrita (camada 4) -----------------------------------------------------------


@pytest.mark.parametrize("sql", BLOCKED_STATEMENTS)
@pytest.mark.parametrize("method", ["fetch_all", "fetch_one", "fetch_scalar", "iter_rows"])
def test_write_statements_blocked_before_sqlite_driver(
    sqlite_path: Path, mocker: MockerFixture, sql: str, method: str
) -> None:
    """RF-043/RNF-010: escrita bloqueada sem conectar nem chamar o driver; arquivo idêntico."""
    before = _sha256(sqlite_path)
    driver = mocker.spy(sqlite3, "connect")
    db = create_database(DatabaseConfig(engine="sqlite", path=sqlite_path))
    with pytest.raises(DatabaseError, match="Instrução SQL não permitida: acesso somente leitura"):
        getattr(db, method)(sql)
    driver.assert_not_called()
    assert db.is_connected is False
    assert _sha256(sqlite_path) == before


@pytest.mark.parametrize("sql", BLOCKED_STATEMENTS[:6])
def test_write_statements_blocked_before_duckdb_driver(
    duckdb_path: Path, mocker: MockerFixture, sql: str
) -> None:
    """RF-043: no DuckDB a instrução de escrita também é bloqueada antes do driver."""
    before = _sha256(duckdb_path)
    driver = mocker.patch("duckdb.connect")
    db = create_database(DatabaseConfig(engine="duckdb", path=duckdb_path))
    with pytest.raises(DatabaseError, match="somente leitura"):
        db.fetch_all(sql)
    driver.assert_not_called()
    assert _sha256(duckdb_path) == before


def test_write_statements_blocked_before_connected_postgres(pg_connect: MagicMock) -> None:
    """RF-043: com conexão aberta, a instrução bloqueada não chega ao cursor do driver."""
    db = connect(pg_config())
    cursor = pg_connect.return_value.cursor.return_value
    with pytest.raises(DatabaseError):
        db.fetch_all("DELETE FROM titulos")
    cursor.execute.assert_not_called()


def test_rejection_details_engine_reason_and_sql_preview(sqlite_path: Path) -> None:
    """Seção 8.15: details com engine, motivo e os primeiros 200 caracteres do SQL."""
    db = create_database(DatabaseConfig(engine="sqlite", path=sqlite_path))
    sql = "DELETE FROM fornecedores WHERE nome IN (" + ", ".join(["'x'"] * 100) + ")"  # noqa: S608 - SQL longo de teste
    with pytest.raises(DatabaseError) as info:
        db.fetch_all(sql)
    error = info.value
    assert error.message == "Instrução SQL não permitida: acesso somente leitura"
    assert error.details["engine"] == "sqlite"
    assert error.details["motivo"] == "instrução não permitida: DELETE"
    assert error.details["sql"] == sql[:200].rstrip()  # espaços finais não são preservados


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("", "instrução vazia"),
        ("   -- só comentário\n /* e bloco */ ", "instrução vazia"),
        (";", "instrução vazia"),
        ("SELECT 1;;", "apenas uma instrução é permitida"),
        ("SELECT 1; SELECT 2", "apenas uma instrução é permitida"),
        ("SELECT 'aberto", "literal não fechado"),
        ('SELECT "coluna', "identificador entre aspas não fechado"),
        ("SELECT 1 /* sem fim", "comentário não fechado"),
        ("SELECT 'C:\\pasta\\'", "literal com barra invertida antes de aspas não permitido"),
        ("SELECT 'a\\''b'", "literal com barra invertida antes de aspas não permitido"),
        ("SELECT $$texto$$", "dollar quoting não permitido"),
        ("SELECT $tag$texto$tag$", "dollar quoting não permitido"),
        ("SELECT 1 /* externo /* interno */ DELETE */", "comentário aninhado não permitido"),
        ("123", "instrução não permitida: 123"),
        ("PRAGMA table_info(t)", "instrução não permitida: PRAGMA"),
        ("WITH x AS (UPDATE t SET a = 1 RETURNING *) SELECT 1", "palavra-chave de escrita: UPDATE"),
        ("select * from t for update", "palavra-chave de escrita: UPDATE"),
        ("SELECT t.delete FROM t", "palavra-chave de escrita: DELETE"),
    ],
)
def test_read_only_violation_reasons(sql: str, reason: str) -> None:
    """Seção 8.15: regras de validação de instruções e motivo informado."""
    assert database_utils._read_only_violation(sql) == reason


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select 1;",
        "  SELECT 1 ;  -- comentário final",
        "((SELECT 1)) UNION (SELECT 2)",
        "WITH x AS (SELECT 1 AS n) SELECT n FROM x",
        "VALUES (1), (2)",
        "TABLE fornecedores",
        "SHOW search_path",
        "DESCRIBE fornecedores",
        "EXPLAIN SELECT * FROM fornecedores",
        "SELECT 'DELETE FROM t; DROP TABLE t' AS texto",
        "SELECT 'it''s' AS texto",
        'SELECT "update", "into" FROM "insert"',
        'SELECT "a""b" FROM t',
        "SELECT 1 -- DELETE FROM t\n",
        "SELECT /* INSERT INTO t */ 1",
        "SELECT deleted_at, updated_by, insert_count FROM log",
        "SELECT * FROM t WHERE a = $update AND b = $1",
        "SELECT 'C:\\pasta\\arquivo.txt'",
        "SELECT 'x' || chr(36) || 'y'",
        "SeLeCt count(*) FROM fornecedores",
        "SELECT ação FROM t",
    ],
)
def test_read_only_statements_are_accepted(sql: str) -> None:
    """Seção 8.15: comentários, literais e identificadores entre aspas não são analisados."""
    assert database_utils._read_only_violation(sql) is None


def test_quoted_identifier_with_write_keyword_runs_on_sqlite(tmp_path: Path) -> None:
    """Seção 8.15: identificador igual a palavra de escrita é aceito entre aspas duplas."""
    path = tmp_path / "palavras.db"
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE "insert" ("update" TEXT, "into" TEXT)')
        conn.execute("INSERT INTO \"insert\" VALUES ('a', 'DELETE FROM x')")
    conn.close()
    with connect(DatabaseConfig(engine="sqlite", path=path)) as db:
        rows = db.fetch_all('SELECT "update", "into" FROM "insert" WHERE "into" LIKE \'DELETE%\'')
    assert rows == [{"update": "a", "into": "DELETE FROM x"}]


# --- conexão somente leitura (camada 2) -------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO fornecedores (id, nome) VALUES (9, 'novo')",
        "UPDATE fornecedores SET nome = 'x'",
        "DELETE FROM fornecedores",
        "CREATE TABLE t (id INTEGER)",
        "DROP TABLE fornecedores",
    ],
)
def test_sqlite_read_only_connection_refuses_writes(
    sqlite_db: Database, sqlite_path: Path, no_validation: MagicMock, sql: str
) -> None:
    """RF-043: com a validação desativada, a conexão SQLite somente leitura recusa a escrita."""
    before = _sha256(sqlite_path)
    with pytest.raises(DatabaseError, match="Falha na consulta") as info:
        sqlite_db.fetch_all(sql)
    assert "readonly" in info.value.details["erro"]
    assert no_validation.called
    assert _sha256(sqlite_path) == before
    assert sqlite_db.fetch_scalar("SELECT count(*) FROM fornecedores") == 5


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO titulos VALUES (99, 'NOVO', 1.0)",
        "UPDATE titulos SET status = 'X'",
        "DELETE FROM titulos",
        "CREATE TABLE t (id INTEGER)",
    ],
)
def test_duckdb_read_only_connection_refuses_writes(
    duckdb_db: Database, duckdb_path: Path, no_validation: MagicMock, sql: str
) -> None:
    """RF-043: com a validação desativada, a conexão DuckDB read_only recusa a escrita."""
    with pytest.raises(DatabaseError, match="Falha na consulta") as info:
        duckdb_db.fetch_all(sql)
    assert "read-only" in info.value.details["erro"]
    duckdb_db.close()
    conn = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        assert conn.execute("SELECT count(*) FROM titulos").fetchone() == (7,)
    finally:
        conn.close()


# --- PostgreSQL e Denodo simulados ------------------------------------------------------------


def test_postgres_connect_maps_config_and_sets_read_only_session(pg_connect: MagicMock) -> None:
    """RF-043: psycopg2.connect com a configuração; set_session(readonly=True, autocommit=False)."""
    db = connect(pg_config(sslmode="verify-full", options={"application_name": "robo"}))
    pg_connect.assert_called_once_with(
        host="pg.interno",
        port=5432,
        dbname="financeiro",
        user="robo_leitura",
        password="S3nh@Banco",
        connect_timeout=10,
        cursor_factory=RealDictCursor,
        sslmode="verify-full",
        application_name="robo",
    )
    connection = pg_connect.return_value
    connection.set_session.assert_called_once_with(readonly=True, autocommit=False)
    assert db.is_connected is True
    assert PostgreSQLDatabase.paramstyle == "pyformat"


def test_postgres_queries_end_with_rollback_and_never_commit(pg_connect: MagicMock) -> None:
    """RF-043: cada consulta termina com rollback() e o framework nunca chama commit()."""
    connection = pg_connect.return_value
    cursor = connection.cursor.return_value
    with connect(pg_config(port=6543)) as db:
        assert db.fetch_all("SELECT 1 AS n") == [{"n": 1}]
        assert db.fetch_one("SELECT %s AS n", (1,)) == {"n": 1}
        assert db.fetch_scalar("SELECT %(n)s AS n", {"n": 1}) == 1
    assert pg_connect.call_args.kwargs["port"] == 6543
    assert cursor.execute.call_args_list[0].args == ("SELECT 1 AS n",)
    assert cursor.execute.call_args_list[1].args == ("SELECT %s AS n", (1,))
    assert connection.rollback.call_count == 3
    assert cursor.close.call_count == 3
    connection.commit.assert_not_called()
    connection.close.assert_called_once()


def test_postgres_iter_rows_uses_named_cursor_and_rolls_back(pg_connect: MagicMock) -> None:
    """Seção 8.15: iter_rows com cursor nomeado (server-side), rollback ao fim e sem commit."""
    connection = pg_connect.return_value
    named = MagicMock(name="cursor_nomeado")
    named.fetchmany.side_effect = [[{"id": 1}, {"id": 2}], [{"id": 3}], []]
    connection.cursor.return_value = named
    db = connect(pg_config())
    rows = list(
        db.iter_rows("SELECT id FROM lancamentos WHERE ano = %(ano)s", {"ano": 2026}, batch_size=2)
    )
    assert rows == [{"id": 1}, {"id": 2}, {"id": 3}]
    name = connection.cursor.call_args.kwargs["name"]
    assert name.startswith("rpa3003_")
    assert named.itersize == 2
    named.execute.assert_called_once_with(
        "SELECT id FROM lancamentos WHERE ano = %(ano)s", {"ano": 2026}
    )
    named.fetchmany.assert_called_with(2)
    named.close.assert_called_once()
    connection.rollback.assert_called_once()
    connection.commit.assert_not_called()


def test_postgres_iter_rows_rolls_back_when_iterator_is_closed(pg_connect: MagicMock) -> None:
    """Seção 8.15: fechar o iterador antes do fim também encerra a leitura com rollback."""
    connection = pg_connect.return_value
    connection.cursor.return_value.fetchmany.return_value = [{"id": 1}, {"id": 2}]
    iterator = connect(pg_config()).iter_rows("SELECT id FROM t")
    assert next(iterator) == {"id": 1}
    connection.rollback.assert_not_called()
    iterator.close()  # type: ignore[attr-defined]
    connection.rollback.assert_called_once()
    connection.commit.assert_not_called()


def test_postgres_read_only_error_maps_sqlstate(pg_connect: MagicMock) -> None:
    """Seção 8.15: erro do driver vira DatabaseError com engine, SQLSTATE e SQL (25006)."""

    class ReadOnlySqlTransaction(psycopg2.Error):
        pgcode = "25006"

    cursor = pg_connect.return_value.cursor.return_value
    cursor.execute.side_effect = ReadOnlySqlTransaction(
        "cannot execute CREATE TABLE in a read-only transaction"
    )
    db = connect(pg_config())
    with pytest.raises(DatabaseError) as info:
        db.fetch_all("SELECT 1")
    assert info.value.details["engine"] == "postgresql"
    assert info.value.details["sqlstate"] == "25006"
    assert info.value.details["sql"] == "SELECT 1"
    assert "read-only transaction" in info.value.details["erro"]
    assert isinstance(info.value.__cause__, ReadOnlySqlTransaction)
    pg_connect.return_value.rollback.assert_called_once()


def test_postgres_connection_failure_raises_database_error(mocker: MockerFixture) -> None:
    """Seção 8.15: falha de conexão do driver vira DatabaseError sem expor a senha."""
    mocker.patch("psycopg2.connect", side_effect=psycopg2.OperationalError("timeout expired"))
    db = create_database(pg_config())
    with pytest.raises(DatabaseError, match="Falha ao conectar") as info:
        db.connect()
    assert info.value.details["engine"] == "postgresql"
    assert "timeout expired" in info.value.details["erro"]
    assert "S3nh@Banco" not in str(info.value)
    assert db.is_connected is False


def test_postgres_set_session_failure_closes_connection(pg_connect: MagicMock) -> None:
    """Seção 8.15: se o modo somente leitura não puder ser aplicado, a conexão é fechada."""
    connection = pg_connect.return_value
    connection.set_session.side_effect = psycopg2.OperationalError("falhou")
    with pytest.raises(DatabaseError, match="Falha ao conectar"):
        connect(pg_config())
    connection.close.assert_called_once()


def test_postgres_password_registered_for_masking(
    pg_connect: MagicMock, log_messages: list[tuple[str, str]]
) -> None:
    """Seção 8.15: a senha é registrada no mascaramento ao conectar."""
    connect(pg_config())
    get_logger().info("senha=S3nh@Banco")
    assert ("INFO", f"senha={MASK}") in log_messages
    assert all("S3nh@Banco" not in message for _level, message in log_messages)


def test_postgres_table_exists_queries_information_schema(pg_connect: MagicMock) -> None:
    """Seção 8.15: PostgreSQL consulta information_schema.tables."""
    cursor = pg_connect.return_value.cursor.return_value
    cursor.fetchone.side_effect = [{"count": 1}, {"count": 0}]
    db = connect(pg_config())
    assert db.table_exists("titulos") is True
    assert db.table_exists("titulos", schema="financeiro") is False
    first, second = (call.args for call in cursor.execute.call_args_list)
    assert "information_schema.tables" in first[0]
    assert "current_schema()" in first[0]
    assert first[1] == ("titulos",)
    assert second[1] == ("titulos", "financeiro")


def test_denodo_uses_autocommit_without_read_only_session(pg_connect: MagicMock) -> None:
    """Seção 8.15: Denodo usa porta 9996 e autocommit=True, sem set_session nem rollback."""
    connection = pg_connect.return_value
    db = connect(pg_config(engine="denodo", database="vdp"))
    assert isinstance(db, DenodoDatabase)
    assert pg_connect.call_args.kwargs["port"] == 9996
    assert connection.autocommit is True
    connection.set_session.assert_not_called()
    assert db.fetch_all("SELECT 1 AS n") == [{"n": 1}]
    connection.rollback.assert_not_called()
    connection.commit.assert_not_called()


def test_denodo_iter_rows_uses_fetchmany_without_named_cursor(pg_connect: MagicMock) -> None:
    """Seção 8.15: no Denodo iter_rows usa fetchmany em cursor comum."""
    connection = pg_connect.return_value
    cursor = connection.cursor.return_value
    cursor.fetchmany.side_effect = [[{"id": 1}], []]
    db = connect(pg_config(engine="denodo"))
    assert list(db.iter_rows("SELECT id FROM vw", batch_size=5)) == [{"id": 1}]
    assert connection.cursor.call_args.kwargs == {}
    cursor.fetchmany.assert_called_with(5)


def test_denodo_write_blocked_by_statement_validation(pg_connect: MagicMock) -> None:
    """Seção 8.15/R-11: sem modo somente leitura no driver, a validação bloqueia a escrita."""
    db = create_database(pg_config(engine="denodo"))
    with pytest.raises(DatabaseError, match="somente leitura"):
        db.fetch_all("INSERT INTO vw VALUES (1)")
    pg_connect.assert_not_called()


def test_denodo_table_exists_runs_where_1_equals_0(pg_connect: MagicMock) -> None:
    """Seção 8.15: Denodo executa SELECT * FROM <nome> WHERE 1 = 0; erro significa ausência."""
    cursor = pg_connect.return_value.cursor.return_value
    cursor.fetchall.return_value = []
    db = connect(pg_config(engine="denodo"))
    assert db.table_exists("vw_titulos") is True
    cursor.execute.assert_called_with("SELECT * FROM vw_titulos WHERE 1 = 0")
    assert db.table_exists("vw_titulos", schema="financeiro") is True
    cursor.execute.assert_called_with("SELECT * FROM financeiro.vw_titulos WHERE 1 = 0")
    cursor.execute.side_effect = psycopg2.ProgrammingError("view not found")
    assert db.table_exists("vw_inexistente") is False


def test_denodo_table_exists_propagates_connection_failure(mocker: MockerFixture) -> None:
    """Seção 8.15: falha de conexão não é confundida com tabela inexistente."""
    mocker.patch("psycopg2.connect", side_effect=psycopg2.OperationalError("recusada"))
    db = create_database(pg_config(engine="denodo"))
    with pytest.raises(DatabaseError, match="Falha ao conectar"):
        db.table_exists("vw")


def test_end_read_failure_is_logged_and_rows_are_returned(
    pg_connect: MagicMock, log_messages: list[tuple[str, str]]
) -> None:
    """Seção 8.15: falha ao encerrar a leitura gera WARNING sem perder o resultado."""
    pg_connect.return_value.rollback.side_effect = psycopg2.InterfaceError("connection closed")
    assert connect(pg_config()).fetch_all("SELECT 1 AS n") == [{"n": 1}]
    assert any(
        level == "WARNING" and message.startswith("Falha ao encerrar a leitura | engine=postgresql")
        for level, message in log_messages
    )


def test_close_failure_is_logged(
    pg_connect: MagicMock, log_messages: list[tuple[str, str]]
) -> None:
    """Seção 8.15: erro ao fechar a conexão gera WARNING e a instância fica desconectada."""
    pg_connect.return_value.close.side_effect = psycopg2.InterfaceError("já fechada")
    db = connect(pg_config())
    db.close()
    assert db.is_connected is False
    assert any(
        level == "WARNING" and "Falha ao fechar" in message for level, message in log_messages
    )


# --- API, configuração e registro -------------------------------------------------------------


@pytest.mark.parametrize(
    "cls", [Database, PostgreSQLDatabase, DenodoDatabase, SQLiteDatabase, DuckDBDatabase]
)
def test_database_api_has_no_write_methods(cls: type[Database]) -> None:
    """RF-043/ADR-18: Database não expõe execute, executemany, transaction nem commit."""
    for name in ("execute", "executemany", "transaction", "commit", "executescript"):
        assert not hasattr(cls, name)


def test_database_config_forbids_extra_fields_and_is_frozen() -> None:
    """RF-043: DatabaseConfig não aceita campos fora do contrato e não tem opção de escrita."""
    with pytest.raises(ValidationError):
        DatabaseConfig(engine="sqlite", read_only=False)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        DatabaseConfig(engine="sqlite", autocommit=True)  # type: ignore[call-arg]
    config = DatabaseConfig(engine="sqlite", path="a.db")  # type: ignore[arg-type]
    assert config.path == Path("a.db")
    with pytest.raises(ValidationError):
        config.engine = "duckdb"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        DatabaseConfig(engine="postgresql", connect_timeout=0)


def test_database_config_repr_hides_password() -> None:
    """RNF-008: repr() de DatabaseConfig não exibe a senha."""
    config = pg_config()
    assert "S3nh@Banco" not in repr(config)
    assert "S3nh@Banco" not in str(config)
    assert config.connect_timeout == 10


def test_from_credentials_fills_user_and_password() -> None:
    """Seção 8.15: from_credentials usa usuário e senha do Vault."""
    credentials = Credentials(username="robo_leitura", password=SecretStr("S3nh@Banco"))
    config = DatabaseConfig.from_credentials(
        "postgresql", credentials, host="pg.interno", database="financeiro"
    )
    assert config.user == "robo_leitura"
    assert config.password is not None
    assert config.password.get_secret_value() == "S3nh@Banco"
    assert (config.engine, config.host, config.database) == (
        "postgresql",
        "pg.interno",
        "financeiro",
    )


@pytest.mark.parametrize(
    "key",
    [
        "readonly",
        "READ_ONLY",
        "Access_Mode",
        "mode",
        "uri",
        "dsn",
        "autocommit",
        " isolation_level ",
    ],
)
def test_reserved_options_raise_configuration_error(key: str, sqlite_path: Path) -> None:
    """Seção 8.15: chave de RESERVED_OPTIONS em options (sem diferenciar caixa) é proibida."""
    with pytest.raises(ConfigurationError, match="reservadas") as info:
        create_database(DatabaseConfig(engine="sqlite", path=sqlite_path, options={key: False}))
    assert info.value.details["opcoes"] == [key]


def test_create_database_requires_engine_fields() -> None:
    """RF-043: postgresql sem host e sqlite sem path geram ConfigurationError."""
    with pytest.raises(ConfigurationError) as pg_error:
        create_database(DatabaseConfig(engine="postgresql"))
    assert pg_error.value.details["campos"] == ["host", "database", "user"]
    with pytest.raises(ConfigurationError) as blank:
        create_database(pg_config(host="  "))
    assert blank.value.details["campos"] == ["host"]
    with pytest.raises(ConfigurationError) as sqlite_error:
        create_database(DatabaseConfig(engine="sqlite"))
    assert sqlite_error.value.details["campos"] == ["path"]
    with pytest.raises(ConfigurationError):
        create_database(DatabaseConfig(engine="denodo", host="vdp"))


def test_create_database_unknown_engine_lists_registered() -> None:
    """RF-043: engine não registrada gera ConfigurationError listando available_engines()."""
    with pytest.raises(ConfigurationError) as info:
        create_database(DatabaseConfig(engine="oracle"))
    message = str(info.value)
    assert "oracle" in message
    for engine in ("duckdb", "postgresql", "sqlite", "denodo"):
        assert engine in message


def test_create_database_normalizes_engine_and_returns_unconnected(sqlite_path: Path) -> None:
    """Seção 8.15: engine normalizada para minúsculas; instância não conectada."""
    db = create_database(DatabaseConfig(engine=" SQLite ", path=sqlite_path))
    assert isinstance(db, SQLiteDatabase)
    assert db.is_connected is False
    assert available_engines() == ["denodo", "duckdb", "postgresql", "sqlite"]


class _MemoryEngine(Database):
    """Engine de extensão usada nos testes de register_engine (SQLite em memória)."""

    engine_name: ClassVar[str] = "memoria"
    paramstyle: ClassVar[str] = "qmark"
    connections: ClassVar[int] = 0

    def _connect(self) -> sqlite3.Connection:
        type(self).connections += 1
        return sqlite3.connect(":memory:")

    def table_exists(self, table: str, schema: str | None = None) -> bool:
        return False


@pytest.fixture
def memory_engine() -> Iterator[type[_MemoryEngine]]:
    _MemoryEngine.connections = 0
    register_engine("Memoria", _MemoryEngine)
    yield _MemoryEngine
    database_utils._ENGINES.pop("memoria", None)


def test_register_engine_inherits_read_only_validation(memory_engine: type[_MemoryEngine]) -> None:
    """Seção 8.15/UC-05: engine registrada herda a validação de instruções da base."""
    db = create_database(DatabaseConfig(engine="memoria"))
    assert isinstance(db, memory_engine)
    with pytest.raises(DatabaseError, match="somente leitura"):
        db.fetch_all("CREATE TABLE x (id INTEGER)")
    assert memory_engine.connections == 0
    assert db.fetch_all("SELECT 7 AS n, 'a' AS letra") == [{"n": 7, "letra": "a"}]
    assert memory_engine.connections == 1


def test_register_engine_duplicate_overwrite_and_type_checks(
    memory_engine: type[_MemoryEngine],
) -> None:
    """Seção 8.15: nome duplicado sem overwrite gera ValueError; classe inválida, TypeError."""
    with pytest.raises(ValueError, match="já registrada"):
        register_engine("memoria", memory_engine)
    with pytest.raises(ValueError, match="já registrada"):
        register_engine("sqlite", SQLiteDatabase)
    register_engine("memoria", memory_engine, overwrite=True)
    with pytest.raises(ValueError, match="vazio"):
        register_engine("  ", memory_engine)
    with pytest.raises(TypeError):
        register_engine("lista", list)  # type: ignore[arg-type]


# --- logs, erros e parâmetros -----------------------------------------------------------------


def test_debug_log_has_engine_rows_and_duration(
    log_messages: list[tuple[str, str]], sqlite_db: Database
) -> None:
    """Seção 8.15: DEBUG `Consulta executada | engine | linhas | duracao`, sem parâmetros."""
    sqlite_db.fetch_all("SELECT * FROM fornecedores WHERE cnpj <> ?", ("valor-param-sigiloso",))
    list(sqlite_db.iter_rows("SELECT id FROM fornecedores", batch_size=2))
    queries = [
        message for level, message in log_messages if level == "DEBUG" and "Consulta" in message
    ]
    assert len(queries) == 2
    for message in queries:
        assert message.startswith("Consulta executada | engine=sqlite | linhas=5 | duracao=")
        assert message.endswith("s")
    assert all("valor-param-sigiloso" not in message for _level, message in log_messages)
    assert any("Conexão com banco de dados aberta | engine=sqlite" in m for _l, m in log_messages)


def test_driver_error_details_without_parameter_values(sqlite_db: Database) -> None:
    """Seção 8.15: erro do driver com engine e SQL; valores de parâmetros nunca aparecem."""
    sql = "SELECT *\n  FROM inexistente\n WHERE x = ?"
    with pytest.raises(DatabaseError, match="Falha na consulta") as info:
        sqlite_db.fetch_all(sql, ("valor-param-sigiloso",))
    error = info.value
    assert error.details["engine"] == "sqlite"
    assert error.details["sql"] == "SELECT * FROM inexistente WHERE x = ?"
    assert "no such table" in error.details["erro"]
    assert "sqlstate" not in error.details
    assert "valor-param-sigiloso" not in str(error)
    assert isinstance(error.__cause__, sqlite3.OperationalError)


def test_iter_rows_driver_errors_are_wrapped(sqlite_db: Database) -> None:
    """Seção 8.15: erros em iter_rows (execução) também viram DatabaseError."""
    with pytest.raises(DatabaseError, match="Falha na consulta"):
        list(sqlite_db.iter_rows("SELECT * FROM inexistente"))


def test_iter_rows_fetch_errors_are_wrapped(pg_connect: MagicMock) -> None:
    """Seção 8.15: erro ao buscar um lote vira DatabaseError e a leitura é encerrada."""
    connection = pg_connect.return_value
    connection.cursor.return_value.fetchmany.side_effect = psycopg2.OperationalError("caiu")
    with pytest.raises(DatabaseError, match="Falha na consulta"):
        list(connect(pg_config()).iter_rows("SELECT 1"))
    connection.rollback.assert_called_once()


def test_iter_rows_and_params_validation(sqlite_db: Database) -> None:
    """Seção 8.15: batch_size < 1 gera ValueError; params como texto gera TypeError."""
    with pytest.raises(ValueError, match="batch_size"):
        sqlite_db.iter_rows("SELECT 1", batch_size=0)
    with pytest.raises(TypeError, match="params"):
        sqlite_db.fetch_all("SELECT ?", "abc")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="params"):
        sqlite_db.iter_rows("SELECT ?", b"abc")  # type: ignore[arg-type]


def test_percent_literal_without_params_is_not_interpolated(pg_connect: MagicMock) -> None:
    """Seção 8.15: sem parâmetros, o SQL vai ao driver sem interpolação (`%` literal)."""
    cursor = pg_connect.return_value.cursor.return_value
    connect(pg_config()).fetch_all("SELECT * FROM t WHERE nome LIKE 'A%'")
    cursor.execute.assert_called_once_with("SELECT * FROM t WHERE nome LIKE 'A%'")


def test_target_description_in_connection_log(
    pg_connect: MagicMock, log_messages: list[tuple[str, str]]
) -> None:
    """Seção 8.15: o log de conexão descreve o destino sem segredos."""
    connect(pg_config())
    connect(DatabaseConfig(engine="duckdb"))
    messages = [message for _level, message in log_messages]
    assert (
        "Conexão com banco de dados aberta | engine=postgresql | destino=pg.interno:5432/financeiro"
        in messages
    )
    assert "Conexão com banco de dados aberta | engine=duckdb | destino=:memory:" in messages


def test_row_converter_uses_description_for_tuples() -> None:
    """Seção 8.15: linhas em tupla são montadas a partir de cursor.description."""
    cursor = MagicMock(description=[("id", None), ("nome", None)])
    convert: Callable[[Any], dict[str, Any]] = database_utils._row_converter(cursor)
    assert convert((1, "Ana")) == {"id": 1, "nome": "Ana"}
    assert convert({"x": 1}) == {"x": 1}
