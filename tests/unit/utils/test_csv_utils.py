"""Testes de `rpa_3003.utils.csv_utils` (RF-039, RNF-011, P-09, seção 8.19)."""

from __future__ import annotations

import csv
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from rpa_3003.core.exceptions import DataFormatError, FileOperationError
from rpa_3003.utils import csv_utils, json_utils

pytestmark = pytest.mark.unit

BOM = b"\xef\xbb\xbf"


def _write_raw(path: Path, text: str, encoding: str = "utf-8-sig") -> Path:
    path.write_bytes(text.encode(encoding))
    return path


def test_read_sample_csv(sample_csv: Path) -> None:
    """RF-039 e P-09: leitura padrão com ';' e utf-8-sig."""
    rows = csv_utils.read_csv(sample_csv)
    assert rows == [
        {"id": "1", "nome": "José", "valor": "10,50"},
        {"id": "2", "nome": "Conceição", "valor": "1.500,00"},
        {"id": "3", "nome": "Açaí Ltda", "valor": "0,99"},
    ]


def test_roundtrip_preserves_accents_and_excel_layout(tmp_path: Path) -> None:
    """RF-039: ida e volta preserva acentos; BOM e ';' permitem abrir no Excel pt-BR em colunas."""
    rows = [
        {"cliente": "Conceição", "cidade": "São Paulo"},
        {"cliente": "Açaí; Cia", "cidade": "Belém"},
    ]
    path = csv_utils.write_csv(tmp_path / "saida" / "clientes.csv", rows)
    assert path == (tmp_path / "saida" / "clientes.csv").resolve()
    raw = path.read_bytes()
    assert raw.startswith(BOM + b"cliente;cidade\r\n")
    assert '"Açaí; Cia";Belém'.encode() in raw
    assert csv_utils.read_csv(path) == rows


def test_append_twice_keeps_single_bom_and_header(tmp_path: Path) -> None:
    """RF-039: anexar duas vezes resulta em um único BOM e um único cabeçalho."""
    path = tmp_path / "log.csv"
    csv_utils.write_csv(path, [{"id": 1, "status": "OK"}], append=True)
    csv_utils.write_csv(path, [{"id": 2, "status": "ERRO"}], append=True)
    csv_utils.write_csv(path, [{"id": 3, "status": "Exceção"}], append=True)
    raw = path.read_bytes()
    assert raw.count(BOM) == 1
    assert raw.startswith(BOM)
    assert raw.count(b"id;status") == 1
    assert [row["id"] for row in csv_utils.read_csv(path)] == ["1", "2", "3"]


def test_append_to_empty_file_writes_header(tmp_path: Path) -> None:
    """RF-039: arquivo existente vazio recebe BOM e cabeçalho no append."""
    path = tmp_path / "vazio.csv"
    path.write_bytes(b"")
    csv_utils.write_csv(path, [{"a": 1}], append=True)
    assert path.read_bytes() == BOM + b"a\r\n1\r\n"


def test_append_keeps_other_encodings(tmp_path: Path) -> None:
    """RF-039: no append, apenas utf-8-sig é trocado por utf-8."""
    path = tmp_path / "legado.csv"
    csv_utils.write_csv(path, [{"nome": "ação"}], encoding="cp1252")
    csv_utils.write_csv(path, [{"nome": "razão"}], encoding="cp1252", append=True)
    assert path.read_bytes() == "nome\r\nação\r\nrazão\r\n".encode("cp1252")


def test_write_csv_value_conversion_and_missing_keys(tmp_path: Path) -> None:
    """RF-039: None vira "", datas viram isoformat, demais via str(); chave ausente vira ""."""
    rows = [
        {
            "data": date(2026, 10, 31),
            "hora": datetime(2026, 10, 31, 8, 0),
            "valor": Decimal("1.5"),
            "obs": None,
        },
        {"data": "", "valor": 2},
    ]
    path = csv_utils.write_csv(tmp_path / "a.csv", rows)
    assert csv_utils.read_csv_rows(path) == [
        ["data", "hora", "valor", "obs"],
        ["2026-10-31", "2026-10-31T08:00:00", "1.5", ""],
        ["", "", "2", ""],
    ]


def test_write_csv_extra_key_raises(tmp_path: Path) -> None:
    """RF-039: chave fora das colunas em linha posterior gera DataFormatError."""
    rows = [{"a": 1}, {"a": 2, "b": 3}]
    with pytest.raises(DataFormatError, match="fora das colunas") as info:
        csv_utils.write_csv(tmp_path / "a.csv", rows)
    assert info.value.details == {"linha": 2, "chaves": "b"}


def test_write_csv_explicit_fieldnames_quoting_and_generator(tmp_path: Path) -> None:
    """RF-039: fieldnames definem as colunas; quoting é repassado; aceita gerador."""

    def generate() -> Iterator[dict[str, object]]:
        yield {"b": "x", "a": 1}
        yield {"a": 2}

    path = csv_utils.write_csv(
        tmp_path / "a.csv", generate(), fieldnames=["a", "b"], delimiter=",", quoting=csv.QUOTE_ALL
    )
    assert path.read_bytes() == BOM + b'"a","b"\r\n"1","x"\r\n"2",""\r\n'


def test_write_csv_without_rows(tmp_path: Path) -> None:
    """RF-039: sem linhas, grava só o cabeçalho (com fieldnames) ou arquivo vazio."""
    with_header = csv_utils.write_csv(tmp_path / "a.csv", [], fieldnames=["id", "nome"])
    assert with_header.read_bytes() == BOM + b"id;nome\r\n"
    empty = csv_utils.write_csv(tmp_path / "b.csv", [])
    assert empty.read_bytes() == b""


def test_write_csv_errors(tmp_path: Path) -> None:
    """RF-039: valor não representável gera DataFormatError; OSError, FileOperationError."""
    with pytest.raises(DataFormatError, match="não representável"):
        csv_utils.write_csv(tmp_path / "a.csv", [{"v": "€"}], encoding="latin-1")
    (tmp_path / "pasta.csv").mkdir()
    with pytest.raises(FileOperationError, match="Falha ao gravar CSV"):
        csv_utils.write_csv(tmp_path / "pasta.csv", [{"a": 1}])


def test_duplicate_headers_raise_with_names(tmp_path: Path) -> None:
    """RF-039: cabeçalhos duplicados (após strip) geram DataFormatError com os nomes."""
    path = _write_raw(tmp_path / "dup.csv", "id; nome ;valor;nome;id\r\n1;a;2;b;3\r\n")
    with pytest.raises(DataFormatError, match="duplicados") as info:
        csv_utils.read_csv(path)
    assert info.value.details["duplicados"] == "id, nome"


def test_headers_are_stripped(tmp_path: Path) -> None:
    """RF-039: cabeçalhos têm espaços removidos nas pontas; valores não."""
    path = _write_raw(tmp_path / "a.csv", " id ;  nome\r\n1; José \r\n")
    assert csv_utils.read_csv(path) == [{"id": "1", "nome": " José "}]


def test_no_header_uses_implicit_keys_and_pads_short_rows(tmp_path: Path) -> None:
    """RF-039: sem cabeçalho e sem fieldnames, chaves col_N; linhas curtas recebem ""."""
    path = _write_raw(tmp_path / "a.csv", "1;José;10\r\n2;Ana\r\n")
    assert csv_utils.read_csv(path, has_header=False) == [
        {"col_1": "1", "col_2": "José", "col_3": "10"},
        {"col_1": "2", "col_2": "Ana", "col_3": ""},
    ]


def test_longer_row_raises_with_line_number(tmp_path: Path) -> None:
    """RF-039: linha mais longa que o cabeçalho gera DataFormatError com o número da linha."""
    path = _write_raw(tmp_path / "a.csv", "id;nome\r\n1;a\r\n\r\n2;b;extra\r\n")
    with pytest.raises(DataFormatError, match="mais colunas") as info:
        csv_utils.read_csv(path)
    assert info.value.details["linha"] == 4
    assert (info.value.details["colunas"], info.value.details["esperado"]) == (3, 2)
    no_header = _write_raw(tmp_path / "b.csv", "1;a\r\n2;b;c\r\n")
    with pytest.raises(DataFormatError, match="mais colunas"):
        csv_utils.read_csv(no_header, has_header=False)


def test_fieldnames_replace_or_define_header(tmp_path: Path) -> None:
    """RF-039: fieldnames substituem o cabeçalho (has_header=True) ou o definem (False)."""
    path = _write_raw(tmp_path / "a.csv", "ID;NOME\r\n1;José\r\n")
    assert csv_utils.read_csv(path, fieldnames=["id", "nome"]) == [{"id": "1", "nome": "José"}]
    assert csv_utils.read_csv(path, fieldnames=["id", "nome"], has_header=False) == [
        {"id": "ID", "nome": "NOME"},
        {"id": "1", "nome": "José"},
    ]
    with pytest.raises(DataFormatError, match="duplicados"):
        csv_utils.read_csv(path, fieldnames=["id", "id"])


def test_skip_empty_rows(tmp_path: Path) -> None:
    """RF-039: skip_empty_rows ignora linhas com todos os campos vazios."""
    path = _write_raw(tmp_path / "a.csv", "\r\nid;nome\r\n1;a\r\n;\r\n \r\n2;b\r\n")
    assert [row["id"] for row in csv_utils.read_csv(path)] == ["1", "2"]
    kept = csv_utils.read_csv(path, skip_empty_rows=False)
    assert kept == [
        {"id": "1", "nome": "a"},
        {"id": "", "nome": ""},
        {"id": " ", "nome": ""},
        {"id": "2", "nome": "b"},
    ]


def test_iter_csv_streams_rows(sample_csv: Path) -> None:
    """RF-039: iter_csv retorna um iterador preguiçoso de dicionários."""
    iterator = csv_utils.iter_csv(sample_csv)
    assert isinstance(iterator, Iterator)
    assert next(iterator)["nome"] == "José"
    assert [row["id"] for row in iterator] == ["2", "3"]


def test_read_csv_rows_returns_raw_rows(sample_csv: Path) -> None:
    """RF-039: read_csv_rows retorna as linhas brutas, incluindo o cabeçalho."""
    rows = csv_utils.read_csv_rows(sample_csv)
    assert rows[0] == ["id", "nome", "valor"]
    assert len(rows) == 4


def test_decode_error_suggests_cp1252(tmp_path: Path) -> None:
    """RF-039 e RNF-011: UnicodeDecodeError vira DataFormatError sugerindo encoding="cp1252"."""
    path = _write_raw(tmp_path / "legado.csv", "id;descrição\r\n1;ação\r\n", encoding="cp1252")
    with pytest.raises(DataFormatError, match='encoding="cp1252"') as info:
        csv_utils.read_csv(path)
    assert info.value.details["codificacao"] == "utf-8-sig"
    with pytest.raises(DataFormatError, match='encoding="cp1252"'):
        csv_utils.read_csv_rows(path)
    assert csv_utils.read_csv(path, encoding="cp1252") == [{"id": "1", "descrição": "ação"}]


def test_invalid_csv_content_raises_with_line(tmp_path: Path) -> None:
    """RF-039: erro do módulo csv vira DataFormatError com a linha."""
    path = _write_raw(tmp_path / "campo_longo.csv", "a;b\r\n1;2\r\n3;xx\r\n")
    original = csv.field_size_limit(1)
    try:
        with pytest.raises(DataFormatError, match="CSV inválido") as info:
            csv_utils.read_csv(path)
    finally:
        csv.field_size_limit(original)
    assert info.value.details["linha"] == 3
    assert isinstance(info.value.__cause__, csv.Error)


def test_missing_file_raises_file_operation_error(tmp_path: Path) -> None:
    """RF-039: arquivo inexistente gera FileOperationError (na primeira iteração em iter_csv)."""
    with pytest.raises(FileOperationError, match="Falha ao abrir CSV"):
        csv_utils.read_csv(tmp_path / "nada.csv")
    lazy = csv_utils.iter_csv(tmp_path / "nada.csv")
    with pytest.raises(FileOperationError):
        next(lazy)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("id,nome,valor\r\n1,José,10\r\n2,Ana,20\r\n", ","),
        ("id;nome;valor\r\n1;José;10,5\r\n2;Ana;20,1\r\n", ";"),
        ("id\tnome\r\n1\tJosé\r\n2\tAna\r\n", "\t"),
        ("id|nome\r\n1|José\r\n2|Ana\r\n", "|"),
    ],
)
def test_detect_delimiter(tmp_path: Path, content: str, expected: str) -> None:
    """RF-039: detect_delimiter retorna "," para arquivo separado por vírgulas (e demais)."""
    path = _write_raw(tmp_path / "a.csv", content)
    assert csv_utils.detect_delimiter(path) == expected


def test_detect_delimiter_empty_file_and_sniffer_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RF-039: arquivo vazio retorna ';'; falha do Sniffer usa a contagem na primeira linha."""
    empty = _write_raw(tmp_path / "vazio.csv", "\r\n \r\n")
    assert csv_utils.detect_delimiter(empty) == ";"

    def failing_sniff(*_args: object, **_kwargs: object) -> None:
        raise csv.Error("Could not determine delimiter")

    monkeypatch.setattr(csv.Sniffer, "sniff", failing_sniff)
    path = _write_raw(tmp_path / "a.csv", "\r\na|b|c,d\r\n1|2|3,4\r\n")
    assert csv_utils.detect_delimiter(path) == "|"
    tie = _write_raw(tmp_path / "b.csv", "abc\r\n")
    assert csv_utils.detect_delimiter(tie, candidates=(",", "|")) == ","


def test_detect_delimiter_discards_incomplete_last_line(tmp_path: Path) -> None:
    """RF-039: amostra truncada descarta a última linha incompleta antes do Sniffer."""
    lines = "".join(f"{index},nome {index},{index * 10}\r\n" for index in range(200))
    path = _write_raw(tmp_path / "grande.csv", "id,nome,valor\r\n" + lines)
    assert csv_utils.detect_delimiter(path, sample_size=100) == ","


def test_detect_delimiter_validates_arguments(sample_csv: Path) -> None:
    """RF-039: candidatas vazias ou amostra não positiva geram ValueError."""
    with pytest.raises(ValueError, match="candidates"):
        csv_utils.detect_delimiter(sample_csv, candidates=())
    with pytest.raises(ValueError, match="sample_size"):
        csv_utils.detect_delimiter(sample_csv, sample_size=0)


def test_csv_to_json(sample_csv: Path, tmp_path: Path) -> None:
    """RF-039: csv_to_json grava a lista de objetos e repassa os argumentos de leitura."""
    path = csv_utils.csv_to_json(sample_csv, tmp_path / "saida" / "amostra.json")
    assert json_utils.read_json(path)[1] == {"id": "2", "nome": "Conceição", "valor": "1.500,00"}
    raw = _write_raw(tmp_path / "virgula.csv", "a,b\r\n1,2\r\n")
    converted = csv_utils.csv_to_json(raw, tmp_path / "virgula.json", delimiter=",")
    assert json_utils.read_json(converted) == [{"a": "1", "b": "2"}]


def test_usage_example(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Seção 8.19: exemplo de uso do módulo."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "input").mkdir(parents=True)
    _write_raw(
        tmp_path / "data" / "input" / "extrato.csv",
        "id,status\r\n1,APROVADO\r\n2,RECUSADO\r\n3,APROVADO\r\n",
    )
    delim = csv_utils.detect_delimiter("data/input/extrato.csv")
    linhas = csv_utils.read_csv("data/input/extrato.csv", delimiter=delim)
    aprovados = [linha for linha in linhas if linha["status"] == "APROVADO"]
    csv_utils.write_csv("data/output/aprovados.csv", aprovados)
    assert [row["id"] for row in csv_utils.iter_csv("data/output/aprovados.csv")] == ["1", "3"]
