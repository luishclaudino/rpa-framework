"""Testes de `rpa_3003.utils.pdf_utils` (RF-047, P-11, seção 8.18)."""

# Credenciais e senhas deste arquivo são fictícias (valores de teste, não segredos).
# ruff: noqa: S106

from __future__ import annotations

import re
from pathlib import Path

import pytest
import reportlab
from pypdf import PdfReader, PdfWriter
from pytest_mock import MockerFixture

from rpa_3003.core.exceptions import PDFError
from rpa_3003.utils import pdf_utils

pytestmark = pytest.mark.unit

TTF_FONT = Path(reportlab.__file__).parent / "fonts" / "Vera.ttf"
ACCENTED = "Ação de cobrança: R$ 1.500,00 — conferência concluída às 14h (ç, ã, é, ô, ü)."


@pytest.fixture
def form_pdf(tmp_path: Path) -> Path:
    """PDF de 2 páginas com formulário (campos `nome` e `cnpj`) somente na primeira."""
    from reportlab.pdfgen import canvas

    path = tmp_path / "formulario.pdf"
    document = canvas.Canvas(str(path))
    document.drawString(72, 760, "Cadastro de fornecedor")
    document.acroForm.textfield(name="nome", x=72, y=700, width=300, height=20)
    document.acroForm.textfield(name="cnpj", x=72, y=660, width=300, height=20, value="000")
    document.showPage()
    document.drawString(72, 760, "Segunda página sem campos")
    document.showPage()
    document.save()
    return path


def _texts(path: Path) -> list[str]:
    return [page.extract_text() or "" for page in PdfReader(path).pages]


# --- criação e extração -----------------------------------------------------------------------


def test_create_pdf_from_text_roundtrip_keeps_accents(tmp_path: Path) -> None:
    """RF-047: create_pdf_from_text → extract_text contém o texto original com acentos."""
    output = pdf_utils.create_pdf_from_text(tmp_path / "saida" / "texto.pdf", ACCENTED)
    assert output == (tmp_path / "saida" / "texto.pdf").resolve()
    assert ACCENTED in pdf_utils.extract_text(output)
    assert not list(output.parent.glob("*.tmp"))


def test_create_pdf_from_text_paragraphs_title_and_metadata(tmp_path: Path) -> None:
    """Seção 8.18: lista de parágrafos, título no topo e nos metadados, quebras de linha."""
    output = pdf_utils.create_pdf_from_text(
        tmp_path / "relatorio.pdf",
        ["Primeiro parágrafo <com> & símbolos", "linha 1\nlinha 2"],
        title="Relatório de Conciliação",
        page_size="LETTER",
    )
    text = pdf_utils.extract_text(output)
    assert text.splitlines()[0] == "Relatório de Conciliação"
    assert "Primeiro parágrafo <com> & símbolos" in text
    assert "linha 1\nlinha 2" in text
    metadata = pdf_utils.get_metadata(output)
    assert metadata["Title"] == "Relatório de Conciliação"
    assert metadata["Author"] == "rpa_3003"
    box = PdfReader(output).pages[0].mediabox
    assert (round(float(box.width)), round(float(box.height))) == (612, 792)


def test_create_pdf_from_text_breaks_pages_and_uses_ttf_font(tmp_path: Path) -> None:
    """Seção 8.18: texto longo quebra páginas; font_path registra fonte TTF."""
    lines = [f"Linha {number} — informação" for number in range(1, 151)]
    output = pdf_utils.create_pdf_from_text(
        tmp_path / "longo.pdf", "\n".join(lines), font_path=TTF_FONT
    )
    assert pdf_utils.get_page_count(output) > 1
    fonts = PdfReader(output).pages[0]["/Resources"]["/Font"].get_object()
    base_fonts = [str(font.get_object()["/BaseFont"]) for font in fonts.values()]
    assert any("Vera" in name for name in base_fonts)
    assert "Linha 150" in pdf_utils.extract_text(output)
    again = pdf_utils.create_pdf_from_text(tmp_path / "outro.pdf", "x", font_path=TTF_FONT)
    assert again.is_file()  # fonte já registrada é reutilizada


def test_create_pdf_from_text_validation(tmp_path: Path) -> None:
    """Seção 8.18: font_size e page_size inválidos geram ValueError; fonte ausente, PDFError."""
    with pytest.raises(ValueError, match="font_size"):
        pdf_utils.create_pdf_from_text(tmp_path / "a.pdf", "x", font_size=0)
    with pytest.raises(ValueError, match="page_size"):
        pdf_utils.create_pdf_from_text(tmp_path / "a.pdf", "x", page_size="A3")  # type: ignore[arg-type]
    with pytest.raises(PDFError, match="Fonte TrueType não encontrada"):
        pdf_utils.create_pdf_from_text(tmp_path / "a.pdf", "x", font_path=tmp_path / "x.ttf")
    invalid_font = tmp_path / "invalida.ttf"
    invalid_font.write_bytes(b"nao e fonte")
    with pytest.raises(PDFError, match="registrar fonte"):
        pdf_utils.create_pdf_from_text(tmp_path / "a.pdf", "x", font_path=invalid_font)


def test_extract_text_by_page_and_page_selection(sample_pdf: Path) -> None:
    """Seção 8.18: páginas a partir de 1, na ordem informada; extract_text une com \\n."""
    pages = pdf_utils.extract_text_by_page(sample_pdf)
    assert len(pages) == 3
    assert [page.splitlines()[0] for page in pages] == ["Página 1", "Página 2", "Página 3"]
    assert pdf_utils.extract_text_by_page(sample_pdf, pages=[3, 1])[0].startswith("Página 3")
    assert pdf_utils.extract_text(sample_pdf, pages=[2]) == pages[1]
    assert pdf_utils.extract_text(sample_pdf) == "\n".join(pages)
    assert pdf_utils.extract_text(sample_pdf, pages=[]) == ""
    layout = pdf_utils.extract_text(sample_pdf, pages=[1], layout=True)
    assert "Página 1" in layout


@pytest.mark.parametrize("pages", [[0], [4], [1, -1], [True], ["1"]])
def test_page_numbers_out_of_range_raise_value_error(sample_pdf: Path, pages: list[int]) -> None:
    """RF-047: pages=[0] (e qualquer número fora de 1..total) lança ValueError."""
    with pytest.raises(ValueError, match=r"[Pp]ágina"):
        pdf_utils.extract_text(sample_pdf, pages=pages)


def test_blank_page_returns_empty_text(tmp_path: Path) -> None:
    """Seção 8.18: páginas sem texto retornam ""."""
    writer = PdfWriter()
    writer.add_blank_page(200, 200)
    path = tmp_path / "branco.pdf"
    writer.write(path)
    assert pdf_utils.extract_text_by_page(path) == [""]


def test_search_text_returns_page_and_match(sample_pdf: Path) -> None:
    """Seção 8.18: search_text retorna (página, trecho) para cada ocorrência de re.finditer."""
    found = pdf_utils.search_text(sample_pdf, r"R\$\s?[\d\.]+,\d{2}")
    assert found == [(1, "R$ 1.500,00"), (2, "R$ 1.500,00"), (3, "R$ 1.500,00")]
    assert pdf_utils.search_text(sample_pdf, r"página \d", flags=re.IGNORECASE, pages=[2]) == [
        (2, "Página 2")
    ]
    assert pdf_utils.search_text(sample_pdf, "inexistente") == []


def test_extract_tables_from_generated_table(tmp_path: Path) -> None:
    """Seção 8.18: extract_tables lista as tabelas das páginas selecionadas."""
    output = pdf_utils.create_pdf_table(
        tmp_path / "tabela.pdf",
        ["Título", "Valor"],
        [["NF 123", "1.500,00"], ["NF 124", "320,10"], ["NF 125", None]],
        title="Processados",
    )
    tables = pdf_utils.extract_tables(output)
    assert len(tables) == 1
    assert tables[0][0] == ["Título", "Valor"]
    assert tables[0][1] == ["NF 123", "1.500,00"]
    assert tables[0][3][0] == "NF 125"
    assert tables[0][3][1] in ("", None)
    assert pdf_utils.extract_tables(output, pages=[1]) == tables
    assert "Processados" in pdf_utils.extract_text(output)


def test_create_pdf_table_repeats_header_and_landscape(tmp_path: Path) -> None:
    """Seção 8.18: cabeçalho repetido a cada página (repeatRows=1); paisagem."""
    rows = [[f"NF {number}", f"{number},00"] for number in range(1, 121)]
    output = pdf_utils.create_pdf_table(
        tmp_path / "longa.pdf", ["Título", "Valor"], rows, landscape=True
    )
    texts = pdf_utils.extract_text_by_page(output)
    assert len(texts) > 1
    assert all(text.startswith("Título Valor") for text in texts)
    box = PdfReader(output).pages[0].mediabox
    assert float(box.width) > float(box.height)
    with_font = pdf_utils.create_pdf_table(
        tmp_path / "fonte.pdf", ["A"], [["ç"]], font_path=TTF_FONT
    )
    assert "ç" in pdf_utils.extract_text(with_font)


def test_create_pdf_table_validation(tmp_path: Path) -> None:
    """Seção 8.18: sem colunas ou linha com outra quantidade de colunas gera ValueError."""
    with pytest.raises(ValueError, match="headers"):
        pdf_utils.create_pdf_table(tmp_path / "a.pdf", [], [])
    with pytest.raises(ValueError, match="Linha 2 tem 1 colunas; esperado 2"):
        pdf_utils.create_pdf_table(tmp_path / "a.pdf", ["A", "B"], [["1", "2"], ["3"]])


def test_reportlab_build_failure_becomes_pdf_error(tmp_path: Path, mocker: MockerFixture) -> None:
    """Seção 8.18: exceções do reportlab viram PDFError e nenhum arquivo é deixado."""
    mocker.patch("reportlab.platypus.SimpleDocTemplate.build", side_effect=RuntimeError("layout"))
    with pytest.raises(PDFError, match="Falha ao criar PDF"):
        pdf_utils.create_pdf_from_text(tmp_path / "falha.pdf", "x")
    assert list(tmp_path.iterdir()) == []


# --- leitura, metadados e erros ---------------------------------------------------------------


def test_get_page_count_and_metadata(sample_pdf: Path, tmp_path: Path) -> None:
    """Seção 8.18: quantidade de páginas e metadados sem a barra inicial das chaves."""
    assert pdf_utils.get_page_count(sample_pdf) == 3
    assert pdf_utils.is_encrypted(sample_pdf) is False
    assert "Producer" in pdf_utils.get_metadata(sample_pdf)
    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    bare = tmp_path / "sem_metadados.pdf"
    writer.write(bare)
    reader_metadata = PdfReader(bare).metadata
    assert pdf_utils.get_metadata(bare) == {
        str(key).lstrip("/"): str(value) for key, value in (reader_metadata or {}).items()
    }


def test_missing_and_invalid_files_raise_pdf_error(tmp_path: Path) -> None:
    """Seção 8.18: arquivo inexistente ou que não é PDF gera PDFError."""
    with pytest.raises(PDFError, match="não encontrado") as missing:
        pdf_utils.get_page_count(tmp_path / "nao_existe.pdf")
    assert missing.value.details["arquivo"].endswith("nao_existe.pdf")
    invalid = tmp_path / "falso.pdf"
    invalid.write_text("não sou PDF", encoding="utf-8")
    for function in (pdf_utils.is_encrypted, pdf_utils.get_page_count, pdf_utils.extract_text):
        with pytest.raises(PDFError, match="Falha ao abrir PDF"):
            function(invalid)


# --- edição -----------------------------------------------------------------------------------


def test_split_pdf_one_file_per_page(sample_pdf: Path, tmp_path: Path) -> None:
    """RF-047: split_pdf de PDF com 3 páginas gera 3 arquivos de 1 página."""
    outputs = pdf_utils.split_pdf(sample_pdf, tmp_path / "partes")
    assert [path.name for path in outputs] == [
        "amostra_p1-1.pdf",
        "amostra_p2-2.pdf",
        "amostra_p3-3.pdf",
    ]
    assert [pdf_utils.get_page_count(path) for path in outputs] == [1, 1, 1]
    assert pdf_utils.extract_text(outputs[2]).startswith("Página 3")


def test_split_pdf_ranges_and_prefix(sample_pdf: Path, tmp_path: Path) -> None:
    """Seção 8.18: ranges inclusivos e nomes <prefixo>_p<inicio>-<fim>.pdf (saneados)."""
    outputs = pdf_utils.split_pdf(sample_pdf, tmp_path, ranges=[(1, 2), (3, 3)], prefix="lote:10")
    assert [path.name for path in outputs] == ["lote_10_p1-2.pdf", "lote_10_p3-3.pdf"]
    assert [pdf_utils.get_page_count(path) for path in outputs] == [2, 1]


@pytest.mark.parametrize("ranges", [[], [(0, 1)], [(2, 4)], [(3, 2)]])
def test_split_pdf_invalid_ranges(
    sample_pdf: Path, tmp_path: Path, ranges: list[tuple[int, int]]
) -> None:
    """Seção 8.18: intervalo vazio, fora do total ou invertido gera ValueError."""
    with pytest.raises(ValueError):
        pdf_utils.split_pdf(sample_pdf, tmp_path, ranges=ranges)


def test_merge_pdfs_in_order_and_over_input(sample_pdf: Path, tmp_path: Path) -> None:
    """Seção 8.18: mescla na ordem; o destino pode ser uma das origens."""
    single = pdf_utils.extract_pages(sample_pdf, [2], tmp_path / "p2.pdf")
    merged = pdf_utils.merge_pdfs([single, sample_pdf], tmp_path / "consolidado.pdf")
    texts = [text.splitlines()[0] for text in pdf_utils.extract_text_by_page(merged)]
    assert texts == ["Página 2", "Página 1", "Página 2", "Página 3"]
    pdf_utils.merge_pdfs([sample_pdf, single], sample_pdf)
    assert pdf_utils.get_page_count(sample_pdf) == 4
    assert pdf_utils.get_page_count(pdf_utils.merge_pdfs(single, tmp_path / "um.pdf")) == 1  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="ao menos um PDF"):
        pdf_utils.merge_pdfs([], tmp_path / "vazio.pdf")


def test_extract_pages_in_given_order(sample_pdf: Path, tmp_path: Path) -> None:
    """Seção 8.18: extract_pages grava as páginas na ordem recebida."""
    output = pdf_utils.extract_pages(sample_pdf, [3, 1], tmp_path / "selecao.pdf")
    assert [text.splitlines()[0] for text in pdf_utils.extract_text_by_page(output)] == [
        "Página 3",
        "Página 1",
    ]
    with pytest.raises(ValueError, match="ao menos uma página"):
        pdf_utils.extract_pages(sample_pdf, [], tmp_path / "x.pdf")
    with pytest.raises(ValueError):
        pdf_utils.extract_pages(sample_pdf, [0], tmp_path / "x.pdf")


def test_rotate_pages_selected_only(sample_pdf: Path, tmp_path: Path) -> None:
    """Seção 8.18: gira só as páginas selecionadas (sentido horário)."""
    output = pdf_utils.rotate_pages(sample_pdf, tmp_path / "girado.pdf", degrees=90, pages=[2])
    assert [page.rotation for page in PdfReader(output).pages] == [0, 90, 0]
    every = pdf_utils.rotate_pages(sample_pdf, tmp_path / "todas.pdf", degrees=180)
    assert [page.rotation for page in PdfReader(every).pages] == [180, 180, 180]
    with pytest.raises(ValueError, match="degrees"):
        pdf_utils.rotate_pages(sample_pdf, tmp_path / "x.pdf", degrees=45)  # type: ignore[arg-type]


def test_encrypt_pdf_uses_aes_256(sample_pdf: Path, tmp_path: Path) -> None:
    """Seção 8.18: encrypt_pdf com AES-256; leitura exige a senha."""
    output = pdf_utils.encrypt_pdf(sample_pdf, tmp_path / "protegido.pdf", user_password="leitura")
    assert pdf_utils.is_encrypted(output) is True
    encrypt = PdfReader(output).trailer["/Encrypt"].get_object()
    assert (encrypt["/V"], encrypt["/R"]) == (5, 6)  # AES-256
    with pytest.raises(PDFError, match="PDF cifrado: informe a senha"):
        pdf_utils.extract_text(output)
    with pytest.raises(PDFError, match="Senha do PDF incorreta"):
        pdf_utils.get_page_count(output, password="errada")
    assert pdf_utils.get_page_count(output, password="leitura") == 3
    assert "Página 1" in pdf_utils.extract_text(output, pages=[1], password="leitura")
    assert pdf_utils.extract_tables(output, password="leitura") == []
    assert "Producer" in pdf_utils.get_metadata(output, password="leitura")


def test_decrypt_pdf_wrong_password_raises(sample_pdf: Path, tmp_path: Path) -> None:
    """RF-047: decrypt_pdf com senha errada lança PDFError."""
    protected = pdf_utils.encrypt_pdf(
        sample_pdf, tmp_path / "protegido.pdf", user_password="leitura", owner_password="dono"
    )
    with pytest.raises(PDFError, match="Senha do PDF incorreta"):
        pdf_utils.decrypt_pdf(protected, tmp_path / "aberto.pdf", password="errada")
    assert not (tmp_path / "aberto.pdf").exists()


def test_decrypt_pdf_with_user_or_owner_password(sample_pdf: Path, tmp_path: Path) -> None:
    """Seção 8.18: decrypt_pdf grava cópia sem criptografia com senha de usuário ou dono."""
    protected = pdf_utils.encrypt_pdf(
        sample_pdf, tmp_path / "protegido.pdf", user_password="leitura", owner_password="dono"
    )
    for password in ("leitura", "dono"):
        output = pdf_utils.decrypt_pdf(
            protected, tmp_path / f"aberto_{password}.pdf", password=password
        )
        assert pdf_utils.is_encrypted(output) is False
        assert pdf_utils.extract_text(output, pages=[3]).startswith("Página 3")
    plain = pdf_utils.decrypt_pdf(sample_pdf, tmp_path / "copia.pdf", password="qualquer")
    assert pdf_utils.get_page_count(plain) == 3


def test_pdf_with_empty_user_password_opens_without_password(
    sample_pdf: Path, tmp_path: Path
) -> None:
    """Seção 8.18: PDF com senha de usuário vazia (só permissões) abre sem senha."""
    writer = PdfWriter(clone_from=sample_pdf)
    writer.encrypt("", "dono", algorithm="AES-256")
    restricted = tmp_path / "restrito.pdf"
    writer.write(restricted)
    assert pdf_utils.is_encrypted(restricted) is True
    assert pdf_utils.get_page_count(restricted) == 3
    assert "Página 2" in pdf_utils.extract_text(restricted, pages=[2])


def test_add_watermark_with_text(sample_pdf: Path, tmp_path: Path) -> None:
    """Seção 8.18: marca d'água de texto gerada com reportlab e mesclada em cada página."""
    output = pdf_utils.add_watermark(
        sample_pdf, tmp_path / "marcado.pdf", text="CONFIDENCIAL", opacity=0.3
    )
    texts = _texts(output)
    assert len(texts) == 3
    assert all("CONFIDENCIAL" in text and "Página" in text for text in texts)
    states = PdfReader(output).pages[0]["/Resources"]["/ExtGState"].get_object()
    alphas = [
        float(state.get_object()["/ca"]) for state in states.values() if "/ca" in state.get_object()
    ]
    assert alphas == [pytest.approx(0.3)]


def test_add_watermark_with_pdf(sample_pdf: Path, tmp_path: Path) -> None:
    """Seção 8.18: watermark_pdf mescla a primeira página do outro PDF em cada página."""
    stamp = pdf_utils.create_pdf_from_text(tmp_path / "carimbo.pdf", "CARIMBO OFICIAL")
    output = pdf_utils.add_watermark(sample_pdf, tmp_path / "carimbado.pdf", watermark_pdf=stamp)
    assert all("CARIMBO OFICIAL" in text for text in _texts(output))


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"text": "x", "watermark_pdf": "a.pdf"},
        {"text": "  "},
        {"text": "x", "opacity": 1.5},
        {"text": "x", "opacity": -0.1},
        {"text": "x", "font_size": 0},
    ],
)
def test_add_watermark_validation(
    sample_pdf: Path, tmp_path: Path, kwargs: dict[str, object]
) -> None:
    """Seção 8.18: exatamente um entre text e watermark_pdf; opacidade e fonte válidas."""
    with pytest.raises(ValueError):
        pdf_utils.add_watermark(sample_pdf, tmp_path / "x.pdf", **kwargs)  # type: ignore[arg-type]


def test_form_fields_and_fill_form(form_pdf: Path, tmp_path: Path) -> None:
    """Seção 8.18: get_form_fields e fill_form em todas as páginas."""
    fields = pdf_utils.get_form_fields(form_pdf)
    assert set(fields) == {"nome", "cnpj"}
    assert fields["cnpj"] == "000"
    output = pdf_utils.fill_form(
        form_pdf, tmp_path / "preenchido.pdf", {"nome": "Açaí Ltda", "cnpj": "11222333000181"}
    )
    assert pdf_utils.get_form_fields(output) == {"nome": "Açaí Ltda", "cnpj": "11222333000181"}
    assert pdf_utils.get_page_count(output) == 2


def test_fill_form_unknown_field_lists_available(form_pdf: Path, tmp_path: Path) -> None:
    """Seção 8.18: campo inexistente gera PDFError listando os campos disponíveis."""
    with pytest.raises(PDFError, match="Campos inexistentes no formulário: cpf") as info:
        pdf_utils.fill_form(form_pdf, tmp_path / "x.pdf", {"cpf": "1", "nome": "x"})
    assert info.value.details["disponiveis"] == "cnpj, nome"
    assert not (tmp_path / "x.pdf").exists()


def test_form_functions_on_pdf_without_form(sample_pdf: Path, tmp_path: Path) -> None:
    """Seção 8.18: PDF sem formulário retorna {} e recusa preenchimento."""
    assert pdf_utils.get_form_fields(sample_pdf) == {}
    with pytest.raises(PDFError, match="Campos inexistentes"):
        pdf_utils.fill_form(sample_pdf, tmp_path / "x.pdf", {"nome": "x"})


def test_write_failure_leaves_no_partial_file(
    sample_pdf: Path, tmp_path: Path, mocker: MockerFixture
) -> None:
    """Seção 8.18: falha ao gravar gera PDFError sem arquivo parcial ou temporário."""
    target = tmp_path / "saida" / "parcial.pdf"
    mocker.patch("os.replace", side_effect=PermissionError("bloqueado"))
    with pytest.raises(PDFError, match="Falha ao gravar PDF"):
        pdf_utils.extract_pages(sample_pdf, [1], target)
    assert list(target.parent.iterdir()) == []


def test_pypdf_errors_become_pdf_error(
    sample_pdf: Path, tmp_path: Path, mocker: MockerFixture
) -> None:
    """Seção 8.18: exceções do pypdf durante a edição viram PDFError."""
    mocker.patch.object(PdfWriter, "write", side_effect=ValueError("corrompido"))
    with pytest.raises(PDFError, match="Falha ao gerar PDF") as info:
        pdf_utils.merge_pdfs([sample_pdf], tmp_path / "x.pdf")
    assert "ValueError: corrompido" in info.value.details["erro"]
