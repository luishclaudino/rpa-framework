"""CSV.

Leitura, escrita, detecção de delimitador e conversão de CSV compatível com o Excel brasileiro:
delimitador `;` e codificação `utf-8-sig` por padrão (P-09).
"""

from __future__ import annotations

import codecs
import csv
import itertools
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import IO, Any, Final, Literal, Protocol, TypeAlias, cast

from rpa_3003.core.config import PathLike
from rpa_3003.core.exceptions import DataFormatError, FileOperationError
from rpa_3003.core.logger import get_logger
from rpa_3003.utils import json_utils

__all__: list[str] = [
    "DEFAULT_DELIMITER",
    "DEFAULT_ENCODING",
    "DELIMITER_CANDIDATES",
    "csv_to_json",
    "detect_delimiter",
    "iter_csv",
    "read_csv",
    "read_csv_rows",
    "write_csv",
]

DEFAULT_DELIMITER: Final[str] = ";"
DEFAULT_ENCODING: Final[str] = "utf-8-sig"
DELIMITER_CANDIDATES: Final[tuple[str, ...]] = (";", ",", "\t", "|")

_IMPLICIT_KEY_PREFIX: Final[str] = "col_"
_APPEND_ENCODING: Final[str] = "utf-8"
# Valores de `csv.QUOTE_*` aceitos por `csv.writer` (o contrato recebe `int`).
_Quoting: TypeAlias = Literal[0, 1, 2, 3, 4, 5]  # noqa: UP040 - convenção 7.7 usa TypeAlias
_BOM_CODEC: Final[str] = codecs.lookup("utf-8-sig").name


def read_csv(
    path: PathLike,
    *,
    delimiter: str = DEFAULT_DELIMITER,
    encoding: str = DEFAULT_ENCODING,
    has_header: bool = True,
    fieldnames: Sequence[str] | None = None,
    skip_empty_rows: bool = True,
) -> list[dict[str, str]]:
    """Lê um CSV inteiro como lista de dicionários.

    Args:
        path: Arquivo CSV.
        delimiter: Delimitador de campos.
        encoding: Codificação; use `"cp1252"` para arquivos de sistemas legados.
        has_header: A primeira linha não vazia é o cabeçalho.
        fieldnames: Nomes das colunas; com `has_header=True`, substituem o cabeçalho do arquivo.
        skip_empty_rows: Ignora linhas cujos campos são todos vazios.

    Returns:
        Uma linha por dicionário, com os valores como texto.

    Raises:
        FileOperationError: Se o arquivo não puder ser lido.
        DataFormatError: Em cabeçalho duplicado, linha com colunas a mais ou falha de
            decodificação.
    """
    return list(
        iter_csv(
            path,
            delimiter=delimiter,
            encoding=encoding,
            has_header=has_header,
            fieldnames=fieldnames,
            skip_empty_rows=skip_empty_rows,
        )
    )


def iter_csv(
    path: PathLike,
    *,
    delimiter: str = DEFAULT_DELIMITER,
    encoding: str = DEFAULT_ENCODING,
    has_header: bool = True,
    fieldnames: Sequence[str] | None = None,
    skip_empty_rows: bool = True,
) -> Iterator[dict[str, str]]:
    """Lê um CSV linha a linha (streaming), sem carregar o arquivo em memória.

    Sem cabeçalho e sem `fieldnames`, as chaves são `col_1`, `col_2`... conforme a primeira
    linha de dados. Linhas mais curtas recebem `""` nas colunas ausentes; linhas mais longas
    geram `DataFormatError` com o número da linha. O arquivo só é aberto na primeira iteração.

    Args:
        path: Arquivo CSV.
        delimiter: Delimitador de campos.
        encoding: Codificação.
        has_header: A primeira linha não vazia é o cabeçalho.
        fieldnames: Nomes das colunas; com `has_header=True`, substituem o cabeçalho do arquivo.
        skip_empty_rows: Ignora linhas cujos campos são todos vazios.

    Yields:
        Um dicionário por linha de dados.

    Raises:
        FileOperationError: Se o arquivo não puder ser lido.
        DataFormatError: Em cabeçalho duplicado, linha com colunas a mais ou falha de
            decodificação.
    """
    source = Path(path)
    keys = None if fieldnames is None else _header_keys(fieldnames, source)
    with _open_csv(source, encoding) as handle:
        reader = csv.reader(handle, delimiter=delimiter)
        with _reading_errors(source, encoding, reader):
            yield from _records(reader, source, keys, has_header, skip_empty_rows)


def read_csv_rows(
    path: PathLike, *, delimiter: str = DEFAULT_DELIMITER, encoding: str = DEFAULT_ENCODING
) -> list[list[str]]:
    """Lê um CSV como lista de linhas brutas, incluindo o cabeçalho.

    Args:
        path: Arquivo CSV.
        delimiter: Delimitador de campos.
        encoding: Codificação.

    Returns:
        Lista de linhas; cada linha é a lista de campos (linha em branco vira `[]`).

    Raises:
        FileOperationError: Se o arquivo não puder ser lido.
        DataFormatError: Se o conteúdo não puder ser decodificado ou interpretado.
    """
    source = Path(path)
    with _open_csv(source, encoding) as handle:
        reader = csv.reader(handle, delimiter=delimiter)
        with _reading_errors(source, encoding, reader):
            return list(reader)


def write_csv(
    path: PathLike,
    rows: Iterable[Mapping[str, Any]],
    *,
    fieldnames: Sequence[str] | None = None,
    delimiter: str = DEFAULT_DELIMITER,
    encoding: str = DEFAULT_ENCODING,
    append: bool = False,
    quoting: int = csv.QUOTE_MINIMAL,
) -> Path:
    """Grava dicionários em CSV, criando a pasta de destino.

    Sem `fieldnames`, as colunas são as chaves da primeira linha, na ordem. Chaves ausentes
    viram `""`; `None` vira `""`; `date`/`datetime` viram `isoformat()`; os demais valores,
    `str()`. Com `append=True` e arquivo existente não vazio, o cabeçalho não é repetido e
    `utf-8-sig` é trocado por `utf-8`, para não gravar BOM no meio do arquivo.

    Args:
        path: Arquivo de destino.
        rows: Linhas a gravar.
        fieldnames: Colunas e sua ordem.
        delimiter: Delimitador de campos.
        encoding: Codificação.
        append: Acrescenta ao final do arquivo existente.
        quoting: Política de aspas do módulo `csv` (ex.: `csv.QUOTE_ALL`).

    Returns:
        Caminho absoluto do arquivo.

    Raises:
        DataFormatError: Se uma linha tiver chave fora das colunas ou valor não representável
            na codificação.
        FileOperationError: Se a gravação falhar.
    """
    target = Path(path)
    iterator = iter(rows)
    first = next(iterator, None)
    columns = list(fieldnames) if fieldnames is not None else list(first or {})
    continuing = append and _has_content(target)
    file_encoding = _APPEND_ENCODING if continuing and _writes_bom(encoding) else encoding
    pending: Iterator[Mapping[str, Any]] = (
        itertools.chain([first], iterator) if first is not None else iter(())
    )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a" if append else "w", encoding=file_encoding, newline="") as handle:
            writer = csv.writer(handle, delimiter=delimiter, quoting=cast("_Quoting", quoting))
            if columns and not continuing:
                writer.writerow(columns)
            written = _write_rows(writer, pending, columns)
    except UnicodeEncodeError as exc:
        raise DataFormatError(
            "Valor não representável na codificação do CSV",
            details={"caminho": str(target.resolve()), "codificacao": file_encoding},
        ) from exc
    except OSError as exc:
        raise _file_error("Falha ao gravar CSV", target, exc) from exc
    get_logger().debug("CSV gravado | arquivo={} | linhas={}", target.resolve(), written)
    return target.resolve()


def detect_delimiter(
    path: PathLike,
    *,
    encoding: str = DEFAULT_ENCODING,
    candidates: Sequence[str] = DELIMITER_CANDIDATES,
    sample_size: int = 65_536,
) -> str:
    """Detecta o delimitador do CSV com `csv.Sniffer` sobre uma amostra do início do arquivo.

    Se o Sniffer falhar, retorna a candidata com mais ocorrências na primeira linha (empate:
    a primeira na ordem de `candidates`). Arquivo vazio retorna `DEFAULT_DELIMITER`.

    Args:
        path: Arquivo CSV.
        encoding: Codificação.
        candidates: Delimitadores aceitos.
        sample_size: Quantidade máxima de caracteres lidos.

    Returns:
        O delimitador detectado.

    Raises:
        ValueError: Se `candidates` for vazio ou `sample_size` não for positivo.
        FileOperationError: Se o arquivo não puder ser lido.
        DataFormatError: Se o conteúdo não puder ser decodificado.

    Example:
        >>> detect_delimiter("data/input/extrato.csv")  # doctest: +SKIP
        ','
    """
    if not candidates or sample_size < 1:
        raise ValueError("candidates não pode ser vazio e sample_size deve ser positivo")
    source = Path(path)
    with _open_csv(source, encoding) as handle, _reading_errors(source, encoding, None):
        sample = handle.read(sample_size)
        truncated = bool(handle.read(1))
    if truncated and "\n" in sample:
        sample = sample[: sample.rindex("\n") + 1]  # descarta a última linha incompleta
    if not sample.strip():
        return DEFAULT_DELIMITER
    try:
        return csv.Sniffer().sniff(sample, delimiters="".join(candidates)).delimiter
    except csv.Error:
        first_line = next(line for line in sample.splitlines() if line.strip())
        return max(candidates, key=first_line.count)


def csv_to_json(
    csv_path: PathLike,
    json_path: PathLike,
    **read_kwargs: Any,  # noqa: ANN401
) -> Path:
    """Converte um CSV em arquivo JSON (lista de objetos).

    Args:
        csv_path: Arquivo CSV de origem.
        json_path: Arquivo JSON de destino.
        **read_kwargs: Argumentos repassados a `read_csv` (ex.: `delimiter`, `encoding`).

    Returns:
        Caminho absoluto do arquivo JSON.

    Raises:
        FileOperationError: Se a leitura ou a gravação falhar.
        DataFormatError: Se o CSV for inválido.
    """
    return json_utils.write_json(json_path, read_csv(csv_path, **read_kwargs))


class _CsvReader(Protocol):
    """Leitor do módulo `csv` (`csv.reader`)."""

    @property
    def line_num(self) -> int: ...

    def __iter__(self) -> Iterator[list[str]]: ...

    def __next__(self) -> list[str]: ...


class _CsvWriter(Protocol):
    """Escritor do módulo `csv` (`csv.writer`)."""

    def writerow(self, row: Iterable[str], /) -> object: ...


@contextmanager
def _reading_errors(source: Path, encoding: str, reader: _CsvReader | None) -> Iterator[None]:
    """Converte erros de leitura de CSV em exceções do framework."""
    try:
        yield
    except UnicodeDecodeError as exc:
        raise DataFormatError(
            'Falha ao decodificar CSV; tente encoding="cp1252"',
            details={
                "caminho": str(source.resolve()),
                "codificacao": encoding,
                "posicao": exc.start,
            },
        ) from exc
    except csv.Error as exc:
        line = reader.line_num if reader is not None else 0
        raise DataFormatError(
            "CSV inválido",
            details={"caminho": str(source.resolve()), "linha": line, "erro": str(exc)},
        ) from exc
    except OSError as exc:
        raise _file_error("Falha ao ler CSV", source, exc) from exc


def _open_csv(source: Path, encoding: str) -> IO[str]:
    try:
        return source.open("r", encoding=encoding, newline="")
    except OSError as exc:
        raise _file_error("Falha ao abrir CSV", source, exc) from exc


def _records(
    reader: _CsvReader,
    source: Path,
    keys: list[str] | None,
    expect_header: bool,
    skip_empty_rows: bool,
) -> Iterator[dict[str, str]]:
    for row in reader:
        if _is_empty_row(row):
            if skip_empty_rows or expect_header or keys is None:
                continue
        elif expect_header:
            expect_header = False
            if keys is None:
                keys = _header_keys(row, source)
            continue
        if keys is None:
            keys = [f"{_IMPLICIT_KEY_PREFIX}{index}" for index in range(1, len(row) + 1)]
        yield _record(keys, row, source, reader.line_num)


def _record(keys: list[str], row: list[str], source: Path, line: int) -> dict[str, str]:
    if len(row) > len(keys):
        raise DataFormatError(
            "Linha com mais colunas que o cabeçalho",
            details={
                "caminho": str(source.resolve()),
                "linha": line,
                "colunas": len(row),
                "esperado": len(keys),
            },
        )
    padded = [*row, *([""] * (len(keys) - len(row)))]
    return dict(zip(keys, padded, strict=True))


def _header_keys(names: Sequence[str], source: Path) -> list[str]:
    keys = [name.strip() for name in names]
    duplicates = sorted(name for name, count in Counter(keys).items() if count > 1)
    if duplicates:
        raise DataFormatError(
            "Cabeçalhos duplicados no CSV",
            details={"caminho": str(source.resolve()), "duplicados": ", ".join(duplicates)},
        )
    return keys


def _is_empty_row(row: Sequence[str]) -> bool:
    return not any(field.strip() for field in row)


def _write_rows(writer: _CsvWriter, rows: Iterable[Mapping[str, Any]], columns: list[str]) -> int:
    allowed = set(columns)
    written = 0
    for index, row in enumerate(rows, start=1):
        extra = [key for key in row if key not in allowed]
        if extra:
            raise DataFormatError(
                "Linha com chave fora das colunas do CSV",
                details={"linha": index, "chaves": ", ".join(map(str, extra))},
            )
        writer.writerow([_cell(row.get(column)) for column in columns])
        written += 1
    return written


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, date | datetime):
        return value.isoformat()
    return str(value)


def _has_content(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError as exc:
        raise _file_error("Falha ao consultar CSV", path, exc) from exc


def _writes_bom(encoding: str) -> bool:
    try:
        return codecs.lookup(encoding).name == _BOM_CODEC
    except LookupError:
        return False


def _file_error(message: str, path: Path, exc: OSError) -> FileOperationError:
    reason = f"{type(exc).__name__}: {exc.strerror}" if exc.strerror else type(exc).__name__
    return FileOperationError(message, details={"caminho": str(path.absolute()), "erro": reason})
