"""Testes de `rpa_3003.utils.json_utils` (RF-038, RNF-008, RNF-011, seção 8.11)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from enum import Enum
from pathlib import Path, PureWindowsPath
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel, SecretStr

from rpa_3003.core.exceptions import DataFormatError, FileOperationError
from rpa_3003.utils import filesystem_utils, json_utils

pytestmark = pytest.mark.unit


class Status(Enum):
    APROVADO = "aprovado"
    PENDENTE = 2


@dataclass
class Boleto:
    numero: str
    vencimento: date


class Parametros(BaseModel):
    data_ref: date
    filiais: list[int]


class Acesso(BaseModel):
    usuario: str
    senha: SecretStr


def _encode(value: object) -> Any:
    return json.loads(json_utils.to_json(value))


def test_to_json_acceptance_example() -> None:
    """RF-038: to_json com date e Decimal gera o texto exato do critério de aceitação."""
    result = json_utils.to_json({"d": date(2026, 10, 31), "v": Decimal("1.50")})
    assert result == '{"d": "2026-10-31", "v": "1.50"}'


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (datetime(2026, 10, 31, 8, 30, tzinfo=UTC), "2026-10-31T08:30:00+00:00"),
        (date(2026, 10, 31), "2026-10-31"),
        (time(23, 59, 1), "23:59:01"),
        (Decimal("1500.00"), "1500.00"),
        (UUID("12345678-1234-5678-1234-567812345678"), "12345678-1234-5678-1234-567812345678"),
        (PureWindowsPath("C:/rpa/saida.csv"), str(PureWindowsPath("C:/rpa/saida.csv"))),
        (Status.APROVADO, "aprovado"),
        (Status.PENDENTE, 2),
        ({"b", "a", "c"}, ["a", "b", "c"]),
        (frozenset({3, 1, 2}), [1, 2, 3]),
        ({1, "a"}, ["a", 1]),  # tipos não comparáveis: ordem de repr ("'a'" < "1")
        (b"\x00\xffrpa", "AP9ycGE="),
        (bytearray(b"rpa"), "cnBh"),
        (SecretStr("S3nh@Forte"), "********"),
        (Boleto("123", date(2026, 11, 3)), {"numero": "123", "vencimento": "2026-11-03"}),
        (
            Parametros(data_ref=date(2026, 10, 1), filiais=[1, 2]),
            {"data_ref": "2026-10-01", "filiais": [1, 2]},
        ),
    ],
    ids=lambda value: type(value).__name__,
)
def test_encoder_supported_types(value: object, expected: object) -> None:
    """RF-038: RPAJSONEncoder serializa cada tipo suportado."""
    assert _encode(value) == expected


def test_encoder_masks_secret_and_never_leaks_value() -> None:
    """RNF-008: SecretStr nunca aparece em claro, nem dentro de modelos pydantic."""
    text = json_utils.to_json(
        {
            "senha": SecretStr("S3nh@Forte"),
            "acesso": Acesso(usuario="robo", senha=SecretStr("S3nh@Forte")),
        }
    )
    assert "S3nh@Forte" not in text
    assert json.loads(text)["senha"] == "********"


def test_encoder_unknown_type_raises_type_error() -> None:
    """RF-038: tipos não suportados mantêm o TypeError padrão do encoder."""
    with pytest.raises(TypeError, match="not JSON serializable"):
        json.dumps(object(), cls=json_utils.RPAJSONEncoder)
    with pytest.raises(TypeError):
        json.dumps(Boleto, cls=json_utils.RPAJSONEncoder)  # classe de dataclass, não instância


def test_to_json_options_and_unserializable() -> None:
    """RF-038: indent, sort_keys e ensure_ascii; conteúdo inválido vira DataFormatError."""
    assert json_utils.to_json({"b": 1, "a": "ç"}, sort_keys=True) == '{"a": "ç", "b": 1}'
    assert json_utils.to_json({"a": "ç"}, ensure_ascii=True) == '{"a": "\\u00e7"}'
    assert json_utils.to_json([1], indent=2) == "[\n  1\n]"
    with pytest.raises(DataFormatError, match="não serializável"):
        json_utils.to_json({"x": object()})
    circular: list[object] = []
    circular.append(circular)
    with pytest.raises(DataFormatError):
        json_utils.to_json(circular)


def test_write_and_read_json_roundtrip_with_accents(tmp_path: Path) -> None:
    """RF-038 e RNF-011: gravação atômica em UTF-8 sem escapar acentos; leitura de volta."""
    data = {"cliente": "Conceição", "valor": Decimal("1.50"), "itens": [1, 2]}
    path = json_utils.write_json(tmp_path / "saida" / "execucao.json", data)
    assert path == (tmp_path / "saida" / "execucao.json").resolve()
    assert "Conceição" in path.read_text(encoding="utf-8")
    assert json_utils.read_json(path) == {"cliente": "Conceição", "valor": "1.50", "itens": [1, 2]}
    assert [item.name for item in path.parent.iterdir()] == ["execucao.json"]


def test_write_json_compact_and_non_atomic(tmp_path: Path) -> None:
    """RF-038: indent=None grava em uma linha; atomic=False grava direto."""
    path = json_utils.write_json(tmp_path / "a.json", {"a": 1}, indent=None, atomic=False)
    assert path.read_text(encoding="utf-8").strip() == '{"a": 1}'


def test_read_json_accepts_bom(tmp_path: Path) -> None:
    """RNF-011: read_json aceita UTF-8 com BOM (padrão utf-8-sig)."""
    path = tmp_path / "bom.json"
    path.write_bytes('{"nome": "José"}'.encode("utf-8-sig"))
    assert json_utils.read_json(path) == {"nome": "José"}


def test_read_json_invalid_reports_line_and_column(tmp_path: Path) -> None:
    """RF-038: JSONDecodeError vira DataFormatError com linha e coluna."""
    path = tmp_path / "ruim.json"
    path.write_text('{\n  "a": 1,\n  "b": \n}', encoding="utf-8")
    with pytest.raises(DataFormatError, match="JSON inválido") as info:
        json_utils.read_json(path)
    assert info.value.details["linha"] == 4
    assert info.value.details["coluna"] == 1
    assert info.value.details["caminho"] == str(path.resolve())
    assert isinstance(info.value.__cause__, json.JSONDecodeError)


def test_read_json_missing_file() -> None:
    """RF-038: arquivo inexistente gera FileOperationError."""
    with pytest.raises(FileOperationError):
        json_utils.read_json("nao_existe.json")


def test_from_json_text_and_bytes() -> None:
    """RF-038: from_json aceita texto e bytes (com BOM) e converte erros."""
    assert json_utils.from_json('{"a": [1, 2]}') == {"a": [1, 2]}
    assert json_utils.from_json("\ufeff[1]") == [1]
    assert json_utils.from_json('{"a": "ç"}'.encode("utf-8-sig")) == {"a": "ç"}
    with pytest.raises(DataFormatError) as info:
        json_utils.from_json("[1,")
    assert (info.value.details["linha"], info.value.details["coluna"]) == (1, 4)
    with pytest.raises(DataFormatError, match="codificação"):
        json_utils.from_json(b'"\xff\xfe\xfa"' + b"\x80" * 3)


def test_get_value_acceptance_examples() -> None:
    """RF-038: get_value com índice de lista; caminho inexistente retorna o padrão."""
    data = {"a": [{"b": 1}]}
    assert json_utils.get_value(data, "a.0.b") == 1
    assert json_utils.get_value(data, "a.1.b") is None
    assert json_utils.get_value(data, "x.y", default="padrão") == "padrão"


@pytest.mark.parametrize(
    "path",
    ["a.b", "a.-1", "a.x", "a.0.b.c", "n.0", "s.0", "a.²"],
)
def test_get_value_missing_segments_return_default(path: str) -> None:
    """RF-038: qualquer segmento inexistente retorna default."""
    data = {"a": [{"b": 1}], "n": 5, "s": "texto"}
    assert json_utils.get_value(data, path, default="d") == "d"


def test_get_value_custom_separator_and_numeric_keys() -> None:
    """RF-038: separador customizado e chaves numéricas em dicionários."""
    data = {"a.b": {"0": "chave"}, "lista": ("x", "y")}
    assert json_utils.get_value(data, "a.b/0", sep="/") == "chave"
    assert json_utils.get_value(data, "lista.1") == "y"


def test_set_value_creates_intermediate_dicts_and_returns_same_object() -> None:
    """RF-038: set_value cria dicionários intermediários e altera o próprio data."""
    data: dict[str, Any] = {"email": {"ativo": True}}
    result = json_utils.set_value(data, "email.destinatarios", ["fin@empresa.com"])
    assert result is data
    json_utils.set_value(data, "novo.nivel.valor", 1)
    assert data == {
        "email": {"ativo": True, "destinatarios": ["fin@empresa.com"]},
        "novo": {"nivel": {"valor": 1}},
    }


def test_set_value_list_indexes() -> None:
    """RF-038: segmentos numéricos alteram listas existentes."""
    data: dict[str, Any] = {"itens": [{"v": 1}, {"v": 2}]}
    json_utils.set_value(data, "itens.1.v", 20)
    json_utils.set_value(data, "itens.0", {"v": 10})
    assert data == {"itens": [{"v": 10}, {"v": 20}]}


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("itens.5", "Índice de lista inexistente"),
        ("itens.x.v", "Índice de lista inexistente"),
        ("itens.9.v", "Índice de lista inexistente"),
        ("total.valor", "não é dicionário nem lista"),
        ("tupla.0", "não é dicionário nem lista"),
    ],
)
def test_set_value_invalid_paths(path: str, message: str) -> None:
    """RF-038: índice de lista inexistente ou valor escalar no caminho geram DataFormatError."""
    data: dict[str, Any] = {"itens": [1], "total": 10, "tupla": (1, 2)}
    with pytest.raises(DataFormatError, match=message):
        json_utils.set_value(data, path, 0)


def test_deep_merge_acceptance_example() -> None:
    """RF-038: deep_merge mescla dicionários aninhados."""
    assert json_utils.deep_merge({"a": {"x": 1}}, {"a": {"y": 2}}) == {"a": {"x": 1, "y": 2}}


def test_deep_merge_does_not_mutate_or_share_inputs() -> None:
    """RF-038: deep_merge não altera as entradas; listas são substituídas, não mescladas."""
    base: dict[str, Any] = {"timeout": 30, "email": {"ativo": False, "para": ["a@x"]}, "l": [1, 2]}
    override: dict[str, Any] = {"email": {"ativo": True}, "l": [3], "novo": {"k": [1]}}
    merged = json_utils.deep_merge(base, override)
    assert merged == {
        "timeout": 30,
        "email": {"ativo": True, "para": ["a@x"]},
        "l": [3],
        "novo": {"k": [1]},
    }
    merged["email"]["para"].append("b@x")
    merged["novo"]["k"].append(2)
    assert base == {"timeout": 30, "email": {"ativo": False, "para": ["a@x"]}, "l": [1, 2]}
    assert override == {"email": {"ativo": True}, "l": [3], "novo": {"k": [1]}}


def test_deep_merge_replaces_dict_with_scalar_and_vice_versa() -> None:
    """RF-038: valores que não são dicionários nos dois lados são substituídos."""
    assert json_utils.deep_merge({"a": {"x": 1}}, {"a": None}) == {"a": None}
    assert json_utils.deep_merge({"a": 1}, {"a": {"x": 1}}) == {"a": {"x": 1}}


def test_validate_success_and_errors() -> None:
    """RF-038: validate retorna o modelo; ValidationError vira DataFormatError com os erros."""
    params = json_utils.validate({"data_ref": "2026-10-31", "filiais": [1, "2"]}, Parametros)
    assert params == Parametros(data_ref=date(2026, 10, 31), filiais=[1, 2])
    with pytest.raises(DataFormatError, match="Dados inválidos") as info:
        json_utils.validate({"data_ref": "31/13/2026", "filiais": ["x"]}, Parametros)
    assert info.value.details["modelo"] == "Parametros"
    errors = info.value.details["erros"]
    assert len(errors) == 2
    assert errors[0].startswith("data_ref: ")
    assert errors[1].startswith("filiais.0: ")
    with pytest.raises(DataFormatError) as root_info:
        json_utils.validate("texto", Parametros)
    assert root_info.value.details["erros"][0].startswith("(raiz): ")


def test_validate_does_not_expose_input_values() -> None:
    """RNF-008: os erros de validação não incluem os valores recebidos."""
    with pytest.raises(DataFormatError) as info:
        json_utils.validate({"usuario": "robo", "senha": 12345678}, Acesso)
    assert "12345678" not in str(info.value)


def test_usage_example(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Seção 8.11: exemplo de uso do módulo (e da seção 8.9)."""
    monkeypatch.chdir(tmp_path)
    filesystem_utils.write_text(
        "data/input/parametros.json", '{"data_ref": "2026-10-31", "filiais": [1, 2]}'
    )
    bruto = json_utils.read_json("data/input/parametros.json")
    params = json_utils.validate(bruto, Parametros)
    cfg = json_utils.deep_merge(
        {"timeout": 30, "email": {"ativo": False}}, {"email": {"ativo": True}}
    )
    json_utils.set_value(cfg, "email.destinatarios", ["fin@empresa.com"])
    json_utils.write_json("data/output/execucao.json", {"params": params, "cfg": cfg})
    json_utils.write_json(filesystem_utils.ensure_dir("data/output") / "resumo.json", {"total": 10})
    assert json_utils.read_json("data/output/execucao.json") == {
        "params": {"data_ref": "2026-10-31", "filiais": [1, 2]},
        "cfg": {"timeout": 30, "email": {"ativo": True, "destinatarios": ["fin@empresa.com"]}},
    }
    assert json_utils.read_json("data/output/resumo.json") == {"total": 10}
