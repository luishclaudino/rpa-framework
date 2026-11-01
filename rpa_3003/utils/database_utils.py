"""Bancos de dados: PostgreSQL, SQLite, DuckDB e Denodo, somente leitura.

Acesso por interface única, com linhas como dicionários, leitura em lotes e extensão por
registro. Não há inclusão, alteração, exclusão, DDL nem controle de transações (P-24).

O acesso somente leitura tem quatro camadas (ADR-18):

1. privilégio de consulta do usuário no banco, concedido pela equipe responsável;
2. conexão aberta em modo somente leitura, sempre que o driver permite;
3. API sem escrita: `Database` não define `execute`, `executemany` nem `transaction`, e o
   framework nunca chama `commit`;
4. validação de cada instrução antes do envio ao driver.

A camada 4 protege contra erros de programação; a barreira de segurança são as camadas 1 e 2.
Os drivers `psycopg2` e `duckdb` são importados somente ao conectar (RNF-017).
"""

from __future__ import annotations

import contextlib
import re
import sqlite3
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Any, ClassVar, Final, Self, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from rpa_3003.core.exceptions import ConfigurationError, DatabaseError, RPAError
from rpa_3003.core.logger import get_logger, register_secret
from rpa_3003.core.vault import Credentials

__all__: list[str] = [
    "READ_ONLY_STATEMENTS",
    "RESERVED_OPTIONS",
    "WRITE_KEYWORDS",
    "Database",
    "DatabaseConfig",
    "DatabaseEngine",
    "DenodoDatabase",
    "DuckDBDatabase",
    "Params",
    "PostgreSQLDatabase",
    "Row",
    "SQLiteDatabase",
    "available_engines",
    "connect",
    "create_database",
    "register_engine",
]

Params: TypeAlias = Sequence[Any] | Mapping[str, Any] | None  # noqa: UP040 - convenção 7.7
Row: TypeAlias = dict[str, Any]  # noqa: UP040 - convenção 7.7

READ_ONLY_STATEMENTS: Final[frozenset[str]] = frozenset(
    {"SELECT", "WITH", "VALUES", "TABLE", "SHOW", "DESCRIBE", "EXPLAIN"}
)
WRITE_KEYWORDS: Final[frozenset[str]] = frozenset(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
        "MERGE",
        "INTO",
        "CREATE",
        "ALTER",
        "DROP",
        "TRUNCATE",
        "EXECUTE",
    }
)
RESERVED_OPTIONS: Final[frozenset[str]] = frozenset(
    {"autocommit", "readonly", "read_only", "access_mode", "mode", "uri", "dsn", "isolation_level"}
)

_SQL_PREVIEW_LENGTH: Final[int] = 200
_READ_ONLY_MESSAGE: Final[str] = "Instrução SQL não permitida: acesso somente leitura"
_IDENTIFIER: Final[re.Pattern[str]] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Comentários, literais e identificadores entre aspas, desconsiderados na análise. O grupo
# `unclosed` só casa quando a forma completa não casa na mesma posição.
_SQL_TOKEN: Final[re.Pattern[str]] = re.compile(
    r"""
      (?P<line_comment>--[^\n]*)
    | (?P<block_comment>/\*.*?\*/)
    | (?P<literal>'(?:[^']|'')*')
    | (?P<identifier>"(?:[^"]|"")*")
    | (?P<unclosed>/\*|'|")
    """,
    re.VERBOSE | re.DOTALL,
)
_UNCLOSED_REASONS: Final[dict[str, str]] = {
    "/*": "comentário não fechado",
    "'": "literal não fechado",
    '"': "identificador entre aspas não fechado",
}
# `$$` ou `$tag$` (dollar quoting do PostgreSQL); `$1` e `$nome` são parâmetros.
_DOLLAR_QUOTE: Final[re.Pattern[str]] = re.compile(r"\$(?:(?!\d)\w+)?\$")
# Palavra que não segue letra, dígito, `_` ou `$` (ignora `$nome`, parâmetro do DuckDB).
_WORD: Final[re.Pattern[str]] = re.compile(r"(?<![\w$])(?!\d)\w[\w$]*")
_FIRST_WORD: Final[re.Pattern[str]] = re.compile(r"[\s(]*((?!\d)\w[\w$]*)")


class DatabaseEngine(StrEnum):
    """Engines nativas de banco de dados."""

    POSTGRESQL = "postgresql"
    SQLITE = "sqlite"
    DUCKDB = "duckdb"
    DENODO = "denodo"


class DatabaseConfig(BaseModel):
    """Configuração de conexão de uma engine.

    Não existe opção de escrita: o modo de acesso é definido apenas pelo framework. A senha é
    `SecretStr` e não aparece em `repr()` (RNF-008).

    Attributes:
        engine: Nome da engine registrada (ex.: `"postgresql"`); maiúsculas são aceitas.
        host: Servidor (PostgreSQL e Denodo).
        port: Porta; `None` usa a porta padrão da engine.
        database: Nome do banco (PostgreSQL e Denodo).
        user: Usuário com privilégio somente de consulta (P-24).
        password: Senha, registrada para mascaramento ao conectar.
        path: Arquivo do banco (SQLite; DuckDB, opcional).
        connect_timeout: Tempo limite de conexão em segundos (RNF-007).
        sslmode: Modo SSL repassado ao `psycopg2`.
        options: Parâmetros extras do driver; chaves de `RESERVED_OPTIONS` são proibidas.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    engine: str
    host: str | None = None
    port: int | None = None
    database: str | None = None
    user: str | None = None
    password: SecretStr | None = None
    path: Path | None = None
    connect_timeout: int = Field(default=10, ge=1)
    sslmode: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_credentials(cls, engine: str, credentials: Credentials, **kwargs: Any) -> Self:  # noqa: ANN401
        """Cria a configuração com usuário e senha lidos do Vault.

        Args:
            engine: Nome da engine.
            credentials: Credenciais obtidas com `VaultClient.get_credentials`.
            **kwargs: Demais campos de `DatabaseConfig` (ex.: `host`, `database`).

        Returns:
            Configuração com `user` e `password` preenchidos pelas credenciais.

        Raises:
            pydantic.ValidationError: Se algum campo for inválido ou não existir no contrato.
        """
        return cls(
            engine=engine, user=credentials.username, password=credentials.password, **kwargs
        )


class Database(ABC):
    """Interface única de acesso somente leitura (Strategy + Template Method).

    A classe base valida as instruções, conecta automaticamente na primeira consulta, converte
    as linhas em `dict`, encerra a leitura após cada consulta, mede a duração e converte erros
    do driver em `DatabaseError`. Cada engine implementa `_connect` (conexão em modo somente
    leitura sempre que o driver permitir) e `table_exists`.

    Attributes:
        engine_name: Nome da engine no registro.
        paramstyle: Estilo de parâmetros nativo do driver (PEP 249).
        default_port: Porta padrão, quando a engine usa rede.
        config: Configuração recebida no construtor.

    Example:
        >>> with connect(DatabaseConfig(engine="duckdb")) as db:
        ...     db.fetch_scalar("SELECT 42")
        42
    """

    engine_name: ClassVar[str]
    paramstyle: ClassVar[str]
    default_port: ClassVar[int | None] = None
    # Campos de DatabaseConfig exigidos pela engine (verificados por validate_config).
    _required_fields: ClassVar[tuple[str, ...]] = ()

    def __init__(self, config: DatabaseConfig) -> None:
        """Armazena a configuração e chama validate_config().

        Args:
            config: Configuração da conexão.

        Raises:
            ConfigurationError: Se a configuração for inválida para a engine.
        """
        self.config: DatabaseConfig = config
        self._connection: Any = None
        self.validate_config()

    def validate_config(self) -> None:
        """Campos obrigatórios da engine ausentes ou chave de RESERVED_OPTIONS em options →
        ConfigurationError.

        Raises:
            ConfigurationError: Se faltar campo obrigatório ou `options` tiver chave reservada.
        """
        missing = [name for name in self._required_fields if _is_blank(getattr(self.config, name))]
        if missing:
            raise ConfigurationError(
                f"Campos obrigatórios ausentes na configuração do banco: {', '.join(missing)}",
                details={"engine": self.engine_name, "campos": missing},
            )
        reserved = sorted(
            key for key in self.config.options if key.strip().lower() in RESERVED_OPTIONS
        )
        if reserved:
            raise ConfigurationError(
                "Opções reservadas ao framework não podem ser informadas em options: "
                f"{', '.join(reserved)}",
                details={"engine": self.engine_name, "opcoes": reserved},
            )

    @abstractmethod
    def _connect(self) -> Any:  # noqa: ANN401 - conexão nativa do driver
        """Abre a conexão do driver em modo somente leitura, sempre que o driver permitir."""
        ...

    @abstractmethod
    def table_exists(self, table: str, schema: str | None = None) -> bool:
        """Indica se a tabela (ou view) existe, sem diferenciar maiúsculas.

        Args:
            table: Nome da tabela (`^[A-Za-z_][A-Za-z0-9_]*$`).
            schema: Schema; `None` usa o schema padrão da conexão.

        Returns:
            `True` se a tabela existir.

        Raises:
            ValueError: Se `table` ou `schema` tiverem caracteres inválidos.
            DatabaseError: Se a consulta falhar.
        """
        ...

    def connect(self) -> Self:
        """Abre a conexão; sem efeito se já estiver conectado.

        Returns:
            A própria instância.

        Raises:
            DatabaseError: Se o driver não conseguir conectar.
        """
        if self._connection is not None:
            return self
        if self.config.password is not None:
            register_secret(self.config.password.get_secret_value())
        try:
            connection = self._connect()
        except RPAError:
            raise
        except Exception as exc:
            raise DatabaseError(
                "Falha ao conectar ao banco de dados",
                details={"engine": self.engine_name, **_error_details(exc)},
            ) from exc
        self._connection = connection
        get_logger().info(
            "Conexão com banco de dados aberta | engine={} | destino={}",
            self.engine_name,
            self._target(),
        )
        return self

    def close(self) -> None:
        """Fecha a conexão; sem efeito se já estiver fechada."""
        connection, self._connection = self._connection, None
        if connection is None:
            return
        try:
            connection.close()
        except Exception as exc:
            get_logger().warning(
                "Falha ao fechar conexão com banco de dados | engine={} | erro={}",
                self.engine_name,
                _describe(exc),
            )
        else:
            get_logger().debug("Conexão com banco de dados fechada | engine={}", self.engine_name)

    @property
    def is_connected(self) -> bool:
        """Indica se há conexão aberta."""
        return self._connection is not None

    def __enter__(self) -> Self:
        """Conecta e retorna a instância."""
        return self.connect()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Fecha a conexão, inclusive após exceção."""
        self.close()

    def fetch_all(self, sql: str, params: Params = None) -> list[Row]:
        """Executa a consulta e retorna todas as linhas.

        Args:
            sql: Instrução somente leitura, com placeholders no `paramstyle` da engine.
            params: Parâmetros posicionais ou nomeados.

        Returns:
            Linhas como dicionários (coluna → valor).

        Raises:
            DatabaseError: Se a instrução não for somente leitura ou a consulta falhar.
        """
        return self._query(
            sql, params, lambda cursor, convert: [convert(raw) for raw in cursor.fetchall()]
        )

    def fetch_one(self, sql: str, params: Params = None) -> Row | None:
        """Executa a consulta e retorna a primeira linha, ou `None` sem resultados.

        Args:
            sql: Instrução somente leitura.
            params: Parâmetros posicionais ou nomeados.

        Returns:
            Primeira linha como dicionário, ou `None`.

        Raises:
            DatabaseError: Se a instrução não for somente leitura ou a consulta falhar.
        """
        rows = self._query(sql, params, _first_row)
        return rows[0] if rows else None

    def fetch_scalar(self, sql: str, params: Params = None) -> Any:  # noqa: ANN401
        """Executa a consulta e retorna a primeira coluna da primeira linha, ou `None`.

        Args:
            sql: Instrução somente leitura.
            params: Parâmetros posicionais ou nomeados.

        Returns:
            Valor da primeira coluna, ou `None` sem resultados.

        Raises:
            DatabaseError: Se a instrução não for somente leitura ou a consulta falhar.
        """
        row = self.fetch_one(sql, params)
        if row is None:
            return None
        return next(iter(row.values()), None)

    def iter_rows(
        self, sql: str, params: Params = None, *, batch_size: int = 1000
    ) -> Iterator[Row]:
        """Itera as linhas em lotes, sem carregar todo o resultado em memória.

        A instrução é validada na chamada; a conexão e a consulta ocorrem na primeira
        iteração. A leitura é encerrada ao fim da iteração ou ao fechar o iterador.

        Args:
            sql: Instrução somente leitura.
            params: Parâmetros posicionais ou nomeados.
            batch_size: Linhas obtidas do driver por lote.

        Returns:
            Iterador de linhas como dicionários.

        Raises:
            ValueError: Se `batch_size` for menor que 1.
            DatabaseError: Se a instrução não for somente leitura ou a consulta falhar.
        """
        if batch_size < 1:
            raise ValueError(f"batch_size deve ser maior ou igual a 1 (recebido: {batch_size})")
        self._ensure_read_only(sql)
        _check_params(params)
        return self._stream(sql, params, batch_size)

    # --- pontos de extensão do Template Method ---------------------------------------------

    def _open_cursor(self, connection: Any) -> Any:  # noqa: ANN401 - objetos do driver
        """Cursor usado por `fetch_all`, `fetch_one` e `fetch_scalar`."""
        return connection.cursor()

    def _open_stream_cursor(self, connection: Any, batch_size: int) -> Any:  # noqa: ANN401
        """Cursor usado por `iter_rows`; o padrão é o mesmo de `_open_cursor`."""
        return self._open_cursor(connection)

    def _end_read(self, connection: Any) -> None:  # noqa: ANN401, B027 - gancho opcional
        """Encerra a leitura após cada consulta (no PostgreSQL, `rollback`)."""

    def _target(self) -> str:
        """Descrição do destino da conexão para o log, sem segredos."""
        config = self.config
        if config.path is not None:
            return str(config.path.resolve())
        if config.host:
            return f"{config.host}:{config.port or self.default_port}/{config.database or ''}"
        return ":memory:"

    # --- execução ----------------------------------------------------------------------------

    def _ensure_read_only(self, sql: str) -> None:
        reason = _read_only_violation(sql)
        if reason is not None:
            raise DatabaseError(
                _READ_ONLY_MESSAGE,
                details={"engine": self.engine_name, "motivo": reason, "sql": _sql_preview(sql)},
            )

    def _active_connection(self) -> Any:  # noqa: ANN401 - conexão do driver
        if self._connection is None:
            self.connect()
        return self._connection

    def _query(
        self,
        sql: str,
        params: Params,
        consume: Callable[[Any, Callable[[Any], Row]], list[Row]],
    ) -> list[Row]:
        self._ensure_read_only(sql)
        _check_params(params)
        connection = self._active_connection()
        started = time.perf_counter()
        cursor: Any = None
        try:
            cursor = self._open_cursor(connection)
            _execute(cursor, sql, params)
            rows = consume(cursor, _row_converter(cursor))
        except Exception as exc:
            raise self._query_error(exc, sql) from exc
        finally:
            self._release(connection, cursor)
        self._log_query(len(rows), started)
        return rows

    def _stream(self, sql: str, params: Params, batch_size: int) -> Iterator[Row]:
        connection = self._active_connection()
        started = time.perf_counter()
        cursor: Any = None
        total = 0
        try:
            try:
                cursor = self._open_stream_cursor(connection, batch_size)
                _execute(cursor, sql, params)
                convert = _row_converter(cursor)
            except Exception as exc:
                raise self._query_error(exc, sql) from exc
            while batch := self._fetch_batch(cursor, sql, batch_size):
                for raw in batch:
                    total += 1
                    yield convert(raw)
        finally:
            self._release(connection, cursor)
        self._log_query(total, started)

    def _fetch_batch(self, cursor: Any, sql: str, batch_size: int) -> list[Any]:  # noqa: ANN401
        try:
            return list(cursor.fetchmany(batch_size))
        except Exception as exc:
            raise self._query_error(exc, sql) from exc

    def _release(self, connection: Any, cursor: Any) -> None:  # noqa: ANN401
        if cursor is not None:
            with contextlib.suppress(Exception):
                cursor.close()
        try:
            self._end_read(connection)
        except Exception as exc:
            get_logger().warning(
                "Falha ao encerrar a leitura | engine={} | erro={}",
                self.engine_name,
                _describe(exc),
            )

    def _query_error(self, exc: Exception, sql: str) -> DatabaseError:
        details: dict[str, Any] = {"engine": self.engine_name, **_error_details(exc)}
        details["sql"] = _sql_preview(sql)
        return DatabaseError("Falha na consulta ao banco de dados", details=details)

    def _log_query(self, rows: int, started: float) -> None:
        get_logger().debug(
            "Consulta executada | engine={} | linhas={} | duracao={:.3f}s",
            self.engine_name,
            rows,
            time.perf_counter() - started,
        )


class PostgreSQLDatabase(Database):
    """PostgreSQL via `psycopg2`, com cada transação iniciada por `BEGIN READ ONLY`.

    A sessão usa `set_session(readonly=True, autocommit=False)`; cada consulta termina com
    `rollback()` e `iter_rows` usa cursor nomeado (server-side). Parâmetros no estilo
    `pyformat` (`%s`, `%(nome)s`).
    """

    engine_name: ClassVar[str] = DatabaseEngine.POSTGRESQL.value
    paramstyle: ClassVar[str] = "pyformat"
    default_port: ClassVar[int | None] = 5432
    _required_fields: ClassVar[tuple[str, ...]] = ("host", "database", "user")

    def _connect(self) -> Any:  # noqa: ANN401 - conexão do psycopg2
        connection = self._open_psycopg2()
        try:
            connection.set_session(readonly=True, autocommit=False)
        except Exception:
            with contextlib.suppress(Exception):
                connection.close()
            raise
        return connection

    def table_exists(self, table: str, schema: str | None = None) -> bool:
        """Consulta `information_schema.tables` (schema padrão: `current_schema()`).

        Args:
            table: Nome da tabela ou view.
            schema: Schema; `None` usa `current_schema()`.

        Returns:
            `True` se a tabela existir.

        Raises:
            ValueError: Se `table` ou `schema` tiverem caracteres inválidos.
            DatabaseError: Se a consulta falhar.
        """
        _check_identifier(table, "tabela")
        sql = (
            "SELECT count(*) FROM information_schema.tables "
            "WHERE lower(table_name) = lower(%s) AND "
        )
        if schema is None:
            return bool(self.fetch_scalar(sql + "table_schema = current_schema()", (table,)))
        _check_identifier(schema, "schema")
        return bool(self.fetch_scalar(sql + "lower(table_schema) = lower(%s)", (table, schema)))

    def _open_psycopg2(self) -> Any:  # noqa: ANN401 - conexão do psycopg2
        import psycopg2
        from psycopg2.extras import RealDictCursor

        config = self.config
        kwargs: dict[str, Any] = {
            "host": config.host,
            "port": config.port or self.default_port,
            "dbname": config.database,
            "user": config.user,
            "connect_timeout": config.connect_timeout,
            "cursor_factory": RealDictCursor,
        }
        if config.password is not None:
            kwargs["password"] = config.password.get_secret_value()
        if config.sslmode is not None:
            kwargs["sslmode"] = config.sslmode
        return psycopg2.connect(**kwargs, **config.options)

    def _open_stream_cursor(self, connection: Any, batch_size: int) -> Any:  # noqa: ANN401
        cursor = connection.cursor(name=f"rpa3003_{uuid.uuid4().hex}")
        cursor.itersize = batch_size
        return cursor

    def _end_read(self, connection: Any) -> None:  # noqa: ANN401
        connection.rollback()


class DenodoDatabase(PostgreSQLDatabase):
    """Denodo Virtual DataPort pela interface compatível com PostgreSQL (P-08).

    O Virtual DataPort não garante `BEGIN READ ONLY`: a conexão usa `autocommit=True`, e a
    proteção vem da validação de instruções e do usuário Denodo com privilégio só de consulta
    (P-24, R-11). `iter_rows` usa `fetchmany`, sem cursor nomeado.
    """

    engine_name: ClassVar[str] = DatabaseEngine.DENODO.value
    default_port: ClassVar[int | None] = 9996

    def _connect(self) -> Any:  # noqa: ANN401 - conexão do psycopg2
        connection = self._open_psycopg2()
        try:
            connection.autocommit = True
        except Exception:
            with contextlib.suppress(Exception):
                connection.close()
            raise
        return connection

    def table_exists(self, table: str, schema: str | None = None) -> bool:
        """Executa `SELECT * FROM <nome> WHERE 1 = 0`; erro na consulta significa ausência.

        Args:
            table: Nome da view.
            schema: Banco virtual que qualifica o nome; `None` usa o banco da conexão.

        Returns:
            `True` se a consulta for aceita.

        Raises:
            ValueError: Se `table` ou `schema` tiverem caracteres inválidos.
            DatabaseError: Se não for possível conectar.
        """
        _check_identifier(table, "tabela")
        name = table
        if schema is not None:
            _check_identifier(schema, "schema")
            name = f"{schema}.{table}"
        self._active_connection()
        try:
            # Nomes validados por _IDENTIFIER; não há como parametrizar identificadores.
            self.fetch_all(f"SELECT * FROM {name} WHERE 1 = 0")  # noqa: S608
        except DatabaseError:
            return False
        return True

    def _open_stream_cursor(self, connection: Any, batch_size: int) -> Any:  # noqa: ANN401
        return connection.cursor()

    def _end_read(self, connection: Any) -> None:  # noqa: ANN401
        """Sem transação a encerrar (`autocommit=True`)."""


class SQLiteDatabase(Database):
    """SQLite em arquivo existente, aberto por URI com `mode=ro` e `PRAGMA query_only = ON`.

    Parâmetros no estilo `qmark` (`?`) ou nomeados (`:nome`).
    """

    engine_name: ClassVar[str] = DatabaseEngine.SQLITE.value
    paramstyle: ClassVar[str] = "qmark"
    _required_fields: ClassVar[tuple[str, ...]] = ("path",)

    def _connect(self) -> Any:  # noqa: ANN401 - conexão do sqlite3
        path = _existing_file(self.config.path, self.engine_name)
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode=ro",
            uri=True,
            timeout=self.config.connect_timeout,
            **self.config.options,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
        except Exception:
            connection.close()
            raise
        return connection

    def table_exists(self, table: str, schema: str | None = None) -> bool:
        """Consulta `sqlite_master` (tabelas e views).

        Args:
            table: Nome da tabela ou view.
            schema: Banco (`main` ou `temp`); `None` usa `main`.

        Returns:
            `True` se a tabela existir.

        Raises:
            ValueError: Se `table` ou `schema` tiverem caracteres inválidos.
            DatabaseError: Se a consulta falhar.
        """
        _check_identifier(table, "tabela")
        master = "sqlite_master"
        if schema is not None:
            _check_identifier(schema, "schema")
            master = f'"{schema}".sqlite_master'
        # Nome do schema validado por _IDENTIFIER; o nome da tabela vai como parâmetro.
        sql = (
            f"SELECT count(*) FROM {master} "  # noqa: S608
            "WHERE type IN ('table', 'view') AND lower(name) = lower(?)"
        )
        return bool(self.fetch_scalar(sql, (table,)))


class DuckDBDatabase(Database):
    """DuckDB em arquivo existente (`read_only=True`) ou em memória (`path=None`).

    Em memória, o banco não tem tabelas próprias e serve para consultar arquivos com
    `read_csv` e `read_parquet`. Parâmetros no estilo `qmark` (`?`) ou nomeados (`$nome`);
    `options` é repassado como `config` do `duckdb.connect`.
    """

    engine_name: ClassVar[str] = DatabaseEngine.DUCKDB.value
    paramstyle: ClassVar[str] = "qmark"

    def _connect(self) -> Any:  # noqa: ANN401 - conexão do duckdb
        import duckdb

        options = dict(self.config.options)
        if self.config.path is None:
            return duckdb.connect(":memory:", config=options)
        path = _existing_file(self.config.path, self.engine_name)
        return duckdb.connect(str(path), read_only=True, config=options)

    def table_exists(self, table: str, schema: str | None = None) -> bool:
        """Consulta `information_schema.tables` (schema padrão: `current_schema()`).

        Args:
            table: Nome da tabela ou view.
            schema: Schema; `None` usa `current_schema()`.

        Returns:
            `True` se a tabela existir.

        Raises:
            ValueError: Se `table` ou `schema` tiverem caracteres inválidos.
            DatabaseError: Se a consulta falhar.
        """
        _check_identifier(table, "tabela")
        sql = (
            "SELECT count(*) FROM information_schema.tables WHERE lower(table_name) = lower(?) AND "
        )
        if schema is None:
            return bool(self.fetch_scalar(sql + "table_schema = current_schema()", (table,)))
        _check_identifier(schema, "schema")
        return bool(self.fetch_scalar(sql + "lower(table_schema) = lower(?)", (table, schema)))


_ENGINES: Final[dict[str, type[Database]]] = {}


def register_engine(name: str, cls: type[Database], *, overwrite: bool = False) -> None:
    """Registra uma engine para `create_database` (extensão sem alterar o framework).

    A nova classe DEVE abrir a conexão em modo somente leitura em `_connect` sempre que o
    driver permitir; a validação de instruções é herdada de `Database` (P-24).

    Args:
        name: Nome da engine (normalizado para minúsculas).
        cls: Subclasse de `Database`.
        overwrite: Substitui uma engine já registrada.

    Raises:
        ValueError: Se o nome for vazio ou já estiver registrado sem `overwrite`.
        TypeError: Se `cls` não for subclasse de `Database`.
    """
    key = _normalize_engine(name)
    if not key:
        raise ValueError("O nome da engine não pode ser vazio")
    if not (isinstance(cls, type) and issubclass(cls, Database)):
        raise TypeError(f"A engine {key} deve ser subclasse de Database")
    if key in _ENGINES and not overwrite:
        raise ValueError(f"Engine de banco de dados já registrada: {key}")
    _ENGINES[key] = cls


def available_engines() -> list[str]:
    """Nomes das engines registradas, em ordem alfabética."""
    return sorted(_ENGINES)


def create_database(config: DatabaseConfig) -> Database:
    """Cria a instância da engine indicada em `config.engine`, ainda não conectada.

    Args:
        config: Configuração da conexão.

    Returns:
        Instância não conectada.

    Raises:
        ConfigurationError: Se a engine não estiver registrada ou a configuração for inválida.
    """
    key = _normalize_engine(config.engine)
    cls = _ENGINES.get(key)
    if cls is None:
        engines = available_engines()
        raise ConfigurationError(
            f"Engine de banco de dados não registrada: {key}",
            details={"engine": key, "disponiveis": ", ".join(engines)},
        )
    return cls(config)


def connect(config: DatabaseConfig) -> Database:
    """Cria a instância da engine e conecta.

    Args:
        config: Configuração da conexão.

    Returns:
        Instância conectada (use com `with` para fechar ao final).

    Raises:
        ConfigurationError: Se a engine não estiver registrada ou a configuração for inválida.
        DatabaseError: Se não for possível conectar.
    """
    return create_database(config).connect()


def _register_builtin_engines() -> None:
    for engine, cls in (
        (DatabaseEngine.POSTGRESQL, PostgreSQLDatabase),
        (DatabaseEngine.DENODO, DenodoDatabase),
        (DatabaseEngine.SQLITE, SQLiteDatabase),
        (DatabaseEngine.DUCKDB, DuckDBDatabase),
    ):
        register_engine(engine.value, cls)


# --- validação de instruções (camada 4) -----------------------------------------------------


def _read_only_violation(sql: str) -> str | None:
    """Retorna o motivo pelo qual a instrução não é somente leitura, ou `None` se for.

    Comentários, literais e identificadores entre aspas são desconsiderados; a primeira palavra
    deve estar em `READ_ONLY_STATEMENTS`, nenhuma palavra de `WRITE_KEYWORDS` pode aparecer e
    `;` só é aceito no final. Construções de interpretação ambígua entre dialetos (literal com
    `\\` antes de aspas, dollar quoting, comentário aninhado) são rejeitadas.
    """
    code, reason = _strip_sql(sql)
    if reason is not None:
        return reason
    if _DOLLAR_QUOTE.search(code):
        return "dollar quoting não permitido"
    body = code.rstrip()
    body = body.removesuffix(";")
    if ";" in body:
        return "apenas uma instrução é permitida"
    if not body.strip():
        return "instrução vazia"
    first = _FIRST_WORD.match(body)
    if first is None or first.group(1).upper() not in READ_ONLY_STATEMENTS:
        start = first.group(1).upper() if first is not None else body.split()[0]
        return f"instrução não permitida: {start}"
    for match in _WORD.finditer(body):
        word = match.group(0).upper()
        if word in WRITE_KEYWORDS:
            return f"palavra-chave de escrita: {word}"
    return None


def _strip_sql(sql: str) -> tuple[str, str | None]:
    """Substitui comentários, literais e identificadores entre aspas por espaço."""
    problems: list[str] = []

    def replace(match: re.Match[str]) -> str:
        kind = match.lastgroup
        text = match.group(0)
        if kind == "unclosed":
            problems.append(_UNCLOSED_REASONS[text])
        elif kind == "block_comment" and "/*" in text[2:-2]:
            problems.append("comentário aninhado não permitido")
        elif kind == "literal" and "\\'" in text[1:]:
            problems.append("literal com barra invertida antes de aspas não permitido")
        return " "

    code = _SQL_TOKEN.sub(replace, sql)
    return code, (problems[0] if problems else None)


# --- auxiliares -------------------------------------------------------------------------------


def _first_row(cursor: Any, convert: Callable[[Any], Row]) -> list[Row]:  # noqa: ANN401
    raw = cursor.fetchone()
    return [] if raw is None else [convert(raw)]


def _row_converter(cursor: Any) -> Callable[[Any], Row]:  # noqa: ANN401 - cursor do driver
    """Converte linhas do driver em `dict` (nomes de coluna de `cursor.description`)."""
    names = [column[0] for column in (cursor.description or ())]

    def convert(raw: Any) -> Row:  # noqa: ANN401 - linha nativa do driver
        if isinstance(raw, Mapping | sqlite3.Row):
            return dict(raw)
        return dict(zip(names, raw, strict=False))

    return convert


def _execute(cursor: Any, sql: str, params: Params) -> None:  # noqa: ANN401
    # Sem parâmetros, o SQL vai ao driver sem interpolação (um `%` literal continua válido).
    if params is None:
        cursor.execute(sql)
    else:
        cursor.execute(sql, params)


def _check_params(params: Params) -> None:
    if isinstance(params, str | bytes):
        raise TypeError("params deve ser sequência ou mapeamento de valores, não texto")


def _check_identifier(name: str, kind: str) -> None:
    if not isinstance(name, str) or _IDENTIFIER.fullmatch(name) is None:
        raise ValueError(f"Nome de {kind} inválido: {name!r}")


def _existing_file(path: Path | None, engine: str) -> Path:
    if path is None or not path.is_file():
        shown = "" if path is None else str(path.resolve())
        raise DatabaseError(
            "Arquivo de banco de dados não encontrado", details={"engine": engine, "arquivo": shown}
        )
    return path.resolve()


def _is_blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _normalize_engine(name: str) -> str:
    return name.strip().lower()


def _sql_preview(sql: str) -> str:
    """Primeiros 200 caracteres do SQL, em uma linha (espaços consecutivos viram um)."""
    return " ".join(sql[:_SQL_PREVIEW_LENGTH].split())


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {' '.join(str(exc).split())}"


def _error_details(exc: BaseException) -> dict[str, Any]:
    """SQLSTATE (quando o driver informa) e descrição do erro, nunca valores de parâmetros."""
    details: dict[str, Any] = {}
    sqlstate = getattr(exc, "pgcode", None) or getattr(exc, "sqlstate", None)
    if sqlstate:
        details["sqlstate"] = sqlstate
    details["erro"] = _describe(exc)
    return details


# As quatro engines nativas são registradas na importação do módulo.
_register_builtin_engines()
