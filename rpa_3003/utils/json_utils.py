"""Manipulação de JSON.

Leitura, escrita e manipulação de JSON com serialização de tipos comuns em RPA: datas,
`Decimal`, `UUID`, `Path`, `Enum`, conjuntos, dataclasses, modelos pydantic, `bytes` (Base64) e
`SecretStr` (sempre mascarado).
"""

from __future__ import annotations

import base64
import copy
import dataclasses
import json
from collections.abc import Mapping, MutableMapping, MutableSequence, Sequence
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from pathlib import Path, PurePath
from typing import Any, Final, TypeVar
from uuid import UUID

from pydantic import BaseModel, SecretBytes, SecretStr, ValidationError

from rpa_3003.core.config import PathLike
from rpa_3003.core.exceptions import DataFormatError
from rpa_3003.core.logger import MASK
from rpa_3003.utils import filesystem_utils

__all__: list[str] = [
    "ModelT",
    "RPAJSONEncoder",
    "deep_merge",
    "from_json",
    "get_value",
    "read_json",
    "set_value",
    "to_json",
    "validate",
    "write_json",
]

ModelT = TypeVar("ModelT", bound=BaseModel)

_BOM: Final[str] = "﻿"
_TEXT_TYPES: Final[tuple[type, ...]] = (str, bytes, bytearray)


class RPAJSONEncoder(json.JSONEncoder):
    """Encoder JSON com os tipos comuns em automações.

    Example:
        >>> json.dumps({"v": Decimal("1.50")}, cls=RPAJSONEncoder)
        '{"v": "1.50"}'
    """

    def default(self, o: Any) -> Any:  # noqa: ANN401 - assinatura de json.JSONEncoder
        """Converte tipos não nativos do JSON.

        `datetime`, `date` e `time` viram `isoformat()`; `Decimal`, `UUID` e `Path`, `str()`;
        `Enum`, `.value`; `set`/`frozenset`, lista ordenada; dataclass, `dataclasses.asdict`;
        `BaseModel`, `model_dump(mode="json")`; `bytes`, Base64; `SecretStr`, `"********"`.

        Args:
            o: Objeto que o `json` não serializa nativamente.

        Returns:
            Valor serializável equivalente.

        Raises:
            TypeError: Para tipos não suportados (comportamento padrão).
        """
        match o:
            case datetime() | date() | time():
                return o.isoformat()
            case Decimal() | UUID() | PurePath():
                return str(o)
            case Enum():
                return o.value
            case set() | frozenset():
                return _sorted_items(o)
            case SecretStr() | SecretBytes():
                return MASK
            case BaseModel():
                return o.model_dump(mode="json")
            case bytes() | bytearray():
                return base64.b64encode(o).decode("ascii")
            case _ if dataclasses.is_dataclass(o) and not isinstance(o, type):
                return dataclasses.asdict(o)
        return super().default(o)


def read_json(path: PathLike, *, encoding: str = "utf-8-sig") -> Any:  # noqa: ANN401
    """Lê um arquivo JSON.

    Args:
        path: Arquivo JSON.
        encoding: Codificação; o padrão aceita arquivos com e sem BOM.

    Returns:
        Conteúdo desserializado.

    Raises:
        FileOperationError: Se o arquivo não puder ser lido.
        DataFormatError: Se o conteúdo não for JSON válido (linha e coluna em `details`) ou não
            puder ser decodificado.
    """
    source = Path(path)
    text = filesystem_utils.read_text(source, encoding)
    return _loads(text, caminho=str(source.resolve()))


def write_json(
    path: PathLike,
    data: Any,  # noqa: ANN401
    *,
    indent: int | None = 4,
    ensure_ascii: bool = False,
    sort_keys: bool = False,
    atomic: bool = True,
) -> Path:
    """Grava um arquivo JSON em UTF-8, com gravação atômica por padrão.

    Args:
        path: Arquivo de destino; a pasta é criada se necessário.
        data: Conteúdo a serializar (ver `RPAJSONEncoder`).
        indent: Indentação; `None` grava em uma linha.
        ensure_ascii: Escapa caracteres não ASCII.
        sort_keys: Ordena as chaves.
        atomic: Usa gravação atômica (ver `filesystem_utils.write_text`).

    Returns:
        Caminho absoluto do arquivo gravado.

    Raises:
        DataFormatError: Se o conteúdo não for serializável.
        FileOperationError: Se a gravação falhar.
    """
    text = to_json(data, indent=indent, ensure_ascii=ensure_ascii, sort_keys=sort_keys)
    return filesystem_utils.write_text(path, f"{text}\n", encoding="utf-8", atomic=atomic)


def to_json(
    data: Any,  # noqa: ANN401
    *,
    indent: int | None = None,
    ensure_ascii: bool = False,
    sort_keys: bool = False,
) -> str:
    """Serializa para texto JSON com `RPAJSONEncoder`.

    Args:
        data: Conteúdo a serializar.
        indent: Indentação; `None` gera uma linha.
        ensure_ascii: Escapa caracteres não ASCII.
        sort_keys: Ordena as chaves.

    Returns:
        Texto JSON.

    Raises:
        DataFormatError: Se o conteúdo não for serializável (tipo não suportado, referência
            circular ou chaves não comparáveis com `sort_keys`).

    Example:
        >>> to_json({"d": date(2026, 10, 31), "v": Decimal("1.50")})
        '{"d": "2026-10-31", "v": "1.50"}'
    """
    try:
        return json.dumps(
            data, cls=RPAJSONEncoder, indent=indent, ensure_ascii=ensure_ascii, sort_keys=sort_keys
        )
    except (TypeError, ValueError) as exc:
        raise DataFormatError(
            "Conteúdo não serializável em JSON", details={"erro": str(exc)}
        ) from exc


def from_json(text: str | bytes) -> Any:  # noqa: ANN401
    """Desserializa texto JSON (texto ou bytes em UTF-8/16/32, com ou sem BOM).

    Args:
        text: Conteúdo JSON.

    Returns:
        Conteúdo desserializado.

    Raises:
        DataFormatError: Se o conteúdo não for JSON válido (linha e coluna em `details`).
    """
    if isinstance(text, bytes | bytearray):
        try:
            return _loads(bytes(text))
        except UnicodeDecodeError as exc:
            raise DataFormatError(
                "JSON com codificação inválida", details={"posicao": exc.start}
            ) from exc
    return _loads(text)


def get_value(data: Any, path: str, default: Any = None, *, sep: str = ".") -> Any:  # noqa: ANN401
    """Lê um valor por caminho pontuado; segmentos numéricos indexam listas.

    Args:
        data: Estrutura de dicionários e listas.
        path: Caminho (ex.: `"pedidos.0.valor"`).
        default: Valor retornado quando algum segmento não existe.
        sep: Separador dos segmentos.

    Returns:
        O valor encontrado ou `default`.

    Example:
        >>> get_value({"a": [{"b": 1}]}, "a.0.b")
        1
    """
    current = data
    for segment in path.split(sep):
        if isinstance(current, Mapping):
            if segment not in current:
                return default
            current = current[segment]
        elif _is_list(current):
            index = _list_index(segment, current)
            if index is None:
                return default
            current = current[index]
        else:
            return default
    return current


def set_value(
    data: dict[str, Any],
    path: str,
    value: Any,  # noqa: ANN401
    *,
    sep: str = ".",
) -> dict[str, Any]:
    """Altera um valor por caminho pontuado, criando dicionários intermediários ausentes.

    Args:
        data: Dicionário alterado no próprio objeto.
        path: Caminho (ex.: `"email.destinatarios"`); segmentos numéricos indexam listas.
        value: Valor a gravar.
        sep: Separador dos segmentos.

    Returns:
        O próprio `data`.

    Raises:
        DataFormatError: Se um índice de lista não existir ou o caminho atravessar um valor que
            não é dicionário nem lista.
    """
    *parents, last = path.split(sep)
    current: Any = data
    for segment in parents:
        if isinstance(current, MutableMapping):
            if segment not in current:
                current[segment] = {}
            current = current[segment]
        else:
            current = current[_required_index(segment, current, path)]
    if isinstance(current, MutableMapping):
        current[last] = value
    else:
        current[_required_index(last, current, path)] = value
    return data


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Mescla dicionários recursivamente sem alterar as entradas.

    Dicionários são mesclados; demais valores (inclusive listas) são substituídos pelos de
    `override`. O resultado não compartilha objetos mutáveis com as entradas.

    Args:
        base: Valores padrão.
        override: Valores que prevalecem.

    Returns:
        Novo dicionário mesclado.

    Example:
        >>> deep_merge({"a": {"x": 1}}, {"a": {"y": 2}})
        {'a': {'x': 1, 'y': 2}}
    """
    merged: dict[str, Any] = {key: copy.deepcopy(value) for key, value in base.items()}
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


# ModelT é público no contrato da seção 8.11; a sintaxe PEP 695 o tornaria local à função.
def validate(data: Any, model: type[ModelT]) -> ModelT:  # noqa: ANN401, UP047
    """Valida dados com um modelo pydantic.

    Args:
        data: Dados de entrada (ex.: resultado de `read_json`).
        model: Classe do modelo pydantic.

    Returns:
        Instância validada do modelo.

    Raises:
        DataFormatError: Se a validação falhar, com a lista de erros em `details["erros"]`.
    """
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        errors = [
            f"{_error_location(error['loc'])}: {error['msg']}"
            for error in exc.errors(include_url=False, include_input=False)
        ]
        raise DataFormatError(
            "Dados inválidos para o modelo", details={"modelo": model.__name__, "erros": errors}
        ) from exc


def _loads(text: str | bytes, **details: object) -> Any:  # noqa: ANN401
    """`json.loads` com `JSONDecodeError` convertido em `DataFormatError`."""
    if isinstance(text, str):
        text = text.removeprefix(_BOM)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise DataFormatError(
            "JSON inválido",
            details={**details, "linha": exc.lineno, "coluna": exc.colno, "erro": exc.msg},
        ) from exc


def _sorted_items(items: set[Any] | frozenset[Any]) -> list[Any]:
    """Lista ordenada; elementos de tipos não comparáveis são ordenados pela representação."""
    try:
        return sorted(items)
    except TypeError:
        return sorted(items, key=repr)


def _is_list(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, _TEXT_TYPES)


def _list_index(segment: str, items: Sequence[Any]) -> int | None:
    """Índice não negativo existente na lista, ou `None`."""
    if not (segment.isascii() and segment.isdigit()):
        return None
    index = int(segment)
    return index if index < len(items) else None


def _required_index(segment: str, current: object, path: str) -> int:
    """Índice existente de lista mutável; caso contrário, `DataFormatError`."""
    if not isinstance(current, MutableSequence):
        raise DataFormatError(
            "Caminho atravessa valor que não é dicionário nem lista",
            details={"caminho": path, "segmento": segment},
        )
    index = _list_index(segment, current)
    if index is None:
        raise DataFormatError(
            "Índice de lista inexistente",
            details={"caminho": path, "segmento": segment, "tamanho": len(current)},
        )
    return index


def _error_location(location: tuple[int | str, ...]) -> str:
    return ".".join(str(part) for part in location) or "(raiz)"
