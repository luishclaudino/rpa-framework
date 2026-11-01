"""Leitura, criação e edição de PDFs.

Leitura de texto e tabelas com `pdfplumber`; páginas, metadados, edição, cifra e formulários com
`pypdf`; criação e marca d'água com `reportlab`. Páginas são numeradas a partir de 1 (P-11), e
`pages=None` significa todas. As bibliotecas são importadas somente quando usadas (RNF-004).

Os PDFs gerados são gravados em arquivo temporário na pasta de destino e renomeados ao final,
para nunca deixar um arquivo parcial.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import re
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, TypeAlias

from rpa_3003.core.config import PathLike
from rpa_3003.core.exceptions import PDFError, RPAError
from rpa_3003.core.logger import get_logger
from rpa_3003.utils.filesystem_utils import safe_filename

if TYPE_CHECKING:
    from pdfplumber.pdf import PDF
    from pypdf import PageObject, PdfReader, PdfWriter

__all__: list[str] = [
    "PageSelection",
    "PageSize",
    "add_watermark",
    "create_pdf_from_text",
    "create_pdf_table",
    "decrypt_pdf",
    "encrypt_pdf",
    "extract_pages",
    "extract_tables",
    "extract_text",
    "extract_text_by_page",
    "fill_form",
    "get_form_fields",
    "get_metadata",
    "get_page_count",
    "is_encrypted",
    "merge_pdfs",
    "rotate_pages",
    "search_text",
    "split_pdf",
]

PageSelection: TypeAlias = Sequence[int] | None  # noqa: UP040 - convenção 7.7
PageSize: TypeAlias = Literal["A4", "LETTER"]  # noqa: UP040 - convenção 7.7

_ROTATIONS: Final[frozenset[int]] = frozenset({90, 180, 270})
_ENCRYPTION_ALGORITHM: Final[str] = "AES-256"
_MARGIN_CM: Final[float] = 2.0
_BASE_FONT: Final[str] = "Helvetica"
_BOLD_FONT: Final[str] = "Helvetica-Bold"
_WATERMARK_GRAY: Final[float] = 0.5
_TITLE_SCALE: Final[float] = 1.6
_LEADING_SCALE: Final[float] = 1.25
_TABLE_FONT_SIZE: Final[int] = 9
_ANNOTATIONS_KEY: Final[str] = "/Annots"


def is_encrypted(path: PathLike) -> bool:
    """Indica se o PDF é cifrado.

    Args:
        path: Arquivo PDF.

    Returns:
        `True` se o PDF tiver dicionário de criptografia.

    Raises:
        PDFError: Se o arquivo não existir ou não for um PDF válido.
    """
    from pypdf import PdfReader

    source = _existing_pdf(path)
    with _pdf_errors("abrir PDF", source):
        return bool(PdfReader(source).is_encrypted)


def get_page_count(path: PathLike, *, password: str | None = None) -> int:
    """Retorna a quantidade de páginas.

    Args:
        path: Arquivo PDF.
        password: Senha, para PDF cifrado.

    Returns:
        Quantidade de páginas.

    Raises:
        PDFError: Se o arquivo for inválido, cifrado sem senha ou com senha errada.
    """
    source = _existing_pdf(path)
    reader = _reader(source, password)
    with _pdf_errors("ler páginas do PDF", source):
        return len(reader.pages)


def get_metadata(path: PathLike, *, password: str | None = None) -> dict[str, str]:
    """Retorna os metadados do documento, sem a barra inicial das chaves.

    Args:
        path: Arquivo PDF.
        password: Senha, para PDF cifrado.

    Returns:
        Metadados (ex.: `{"Title": "Relatório", "Author": "rpa_3003"}`).

    Raises:
        PDFError: Se o arquivo for inválido, cifrado sem senha ou com senha errada.
    """
    source = _existing_pdf(path)
    reader = _reader(source, password)
    with _pdf_errors("ler metadados do PDF", source):
        metadata = reader.metadata
        if metadata is None:
            return {}
        values = {str(key).lstrip("/"): metadata[key] for key in metadata}
        return {key: str(value) for key, value in values.items() if value is not None}


def extract_text(
    path: PathLike,
    *,
    pages: PageSelection = None,
    password: str | None = None,
    layout: bool = False,
) -> str:
    """Extrai o texto das páginas selecionadas, unidas por `"\\n"`.

    Args:
        path: Arquivo PDF.
        pages: Números de página (a partir de 1); `None` para todas.
        password: Senha, para PDF cifrado.
        layout: Preserva o posicionamento aproximado do texto.

    Returns:
        Texto extraído; páginas sem texto contribuem com `""`.

    Raises:
        ValueError: Se alguma página estiver fora do intervalo.
        PDFError: Se o arquivo for inválido, cifrado sem senha ou com senha errada.
    """
    return "\n".join(extract_text_by_page(path, pages=pages, password=password, layout=layout))


def extract_text_by_page(
    path: PathLike,
    *,
    pages: PageSelection = None,
    password: str | None = None,
    layout: bool = False,
) -> list[str]:
    """Extrai o texto de cada página selecionada, na ordem informada.

    Args:
        path: Arquivo PDF.
        pages: Números de página (a partir de 1); `None` para todas.
        password: Senha, para PDF cifrado.
        layout: Preserva o posicionamento aproximado do texto.

    Returns:
        Um texto por página; página sem texto resulta em `""`.

    Raises:
        ValueError: Se alguma página estiver fora do intervalo.
        PDFError: Se o arquivo for inválido, cifrado sem senha ou com senha errada.
    """
    return [text for _number, text in _page_texts(path, pages, password, layout=layout)]


def extract_tables(
    path: PathLike, *, pages: PageSelection = None, password: str | None = None
) -> list[list[list[str | None]]]:
    """Extrai as tabelas das páginas selecionadas, na ordem das páginas.

    Args:
        path: Arquivo PDF.
        pages: Números de página (a partir de 1); `None` para todas.
        password: Senha, para PDF cifrado.

    Returns:
        Lista de tabelas; cada tabela é uma lista de linhas, e cada célula é texto ou `None`.

    Raises:
        ValueError: Se alguma página estiver fora do intervalo.
        PDFError: Se o arquivo for inválido, cifrado sem senha ou com senha errada.
    """
    source = _existing_pdf(path)
    with _open_plumber(source, password) as pdf:
        indexes = _page_indexes(pages, len(pdf.pages))
        tables: list[list[list[str | None]]] = []
        with _pdf_errors("extrair tabelas do PDF", source):
            for index in indexes:
                tables.extend(pdf.pages[index].extract_tables())
        return tables


def search_text(
    path: PathLike, pattern: str, *, flags: int = 0, pages: PageSelection = None
) -> list[tuple[int, str]]:
    """Busca uma expressão regular no texto das páginas.

    Args:
        path: Arquivo PDF.
        pattern: Expressão regular (ex.: `r"R\\$\\s?[\\d\\.]+,\\d{2}"`).
        flags: Flags do módulo `re`.
        pages: Números de página (a partir de 1); `None` para todas.

    Returns:
        `(página, trecho)` para cada ocorrência de `re.finditer`, na ordem das páginas.

    Raises:
        ValueError: Se alguma página estiver fora do intervalo.
        re.error: Se a expressão regular for inválida.
        PDFError: Se o arquivo for inválido ou cifrado com senha.
    """
    regex = re.compile(pattern, flags)
    return [
        (number, match.group(0))
        for number, text in _page_texts(path, pages, None, layout=False)
        for match in regex.finditer(text)
    ]


def merge_pdfs(paths: Sequence[PathLike], output: PathLike) -> Path:
    """Mescla os PDFs na ordem recebida.

    Args:
        paths: Arquivos PDF de origem.
        output: Arquivo de destino (pode ser um dos arquivos de origem).

    Returns:
        Caminho absoluto do PDF gravado.

    Raises:
        ValueError: Se `paths` estiver vazio.
        PDFError: Se alguma origem for inválida ou cifrada com senha, ou a gravação falhar.
    """
    from pypdf import PdfWriter

    sources = [_existing_pdf(item) for item in _path_list(paths)]
    if not sources:
        raise ValueError("Informe ao menos um PDF para mesclar")
    writer = PdfWriter()
    for source in sources:
        reader = _reader(source, None)
        with _pdf_errors("mesclar PDF", source):
            writer.append(reader)
    result = _write_pdf(writer, output)
    get_logger().info("PDFs mesclados | arquivos={} | destino={}", len(sources), result)
    return result


def split_pdf(
    path: PathLike,
    output_dir: PathLike,
    *,
    ranges: Sequence[tuple[int, int]] | None = None,
    prefix: str | None = None,
) -> list[Path]:
    """Divide o PDF em arquivos `<prefixo>_p<inicio>-<fim>.pdf`.

    Args:
        path: Arquivo PDF.
        output_dir: Pasta de destino (criada se necessário).
        ranges: Intervalos `(inicio, fim)` inclusivos; `None` gera um arquivo por página.
        prefix: Prefixo dos nomes; padrão: nome do arquivo original sem extensão.

    Returns:
        Caminhos absolutos gravados, na ordem dos intervalos.

    Raises:
        ValueError: Se `ranges` estiver vazio ou algum intervalo for inválido.
        PDFError: Se o arquivo for inválido ou cifrado com senha, ou a gravação falhar.
    """
    from pypdf import PdfWriter

    source = _existing_pdf(path)
    reader = _reader(source, None)
    total = len(reader.pages)
    spans = _page_ranges(ranges, total)
    name = source.stem if prefix is None else prefix
    outputs: list[Path] = []
    for start, end in spans:
        writer = PdfWriter()
        with _pdf_errors("dividir PDF", source):
            for index in range(start - 1, end):
                writer.add_page(reader.pages[index])
        target = Path(output_dir) / safe_filename(f"{name}_p{start}-{end}.pdf")
        outputs.append(_write_pdf(writer, target))
    get_logger().info("PDF dividido | origem={} | arquivos={}", source, len(outputs))
    return outputs


def extract_pages(path: PathLike, pages: Sequence[int], output: PathLike) -> Path:
    """Grava um novo PDF somente com as páginas informadas, na ordem recebida.

    Args:
        path: Arquivo PDF.
        pages: Números de página (a partir de 1).
        output: Arquivo de destino.

    Returns:
        Caminho absoluto do PDF gravado.

    Raises:
        ValueError: Se `pages` estiver vazio ou alguma página estiver fora do intervalo.
        PDFError: Se o arquivo for inválido ou cifrado com senha, ou a gravação falhar.
    """
    from pypdf import PdfWriter

    if not pages:
        raise ValueError("Informe ao menos uma página para extrair")
    source = _existing_pdf(path)
    reader = _reader(source, None)
    indexes = _page_indexes(pages, len(reader.pages))
    writer = PdfWriter()
    with _pdf_errors("extrair páginas do PDF", source):
        for index in indexes:
            writer.add_page(reader.pages[index])
    return _write_pdf(writer, output)


def rotate_pages(
    path: PathLike,
    output: PathLike,
    *,
    degrees: Literal[90, 180, 270],
    pages: PageSelection = None,
) -> Path:
    """Gira as páginas selecionadas no sentido horário.

    Args:
        path: Arquivo PDF.
        output: Arquivo de destino.
        degrees: 90, 180 ou 270.
        pages: Números de página (a partir de 1); `None` para todas.

    Returns:
        Caminho absoluto do PDF gravado.

    Raises:
        ValueError: Se `degrees` for inválido ou alguma página estiver fora do intervalo.
        PDFError: Se o arquivo for inválido ou cifrado com senha, ou a gravação falhar.
    """
    from pypdf import PdfWriter

    if degrees not in _ROTATIONS:
        raise ValueError(f"degrees deve ser 90, 180 ou 270 (recebido: {degrees})")
    source = _existing_pdf(path)
    reader = _reader(source, None)
    selected = set(_page_indexes(pages, len(reader.pages)))
    with _pdf_errors("girar páginas do PDF", source):
        writer = PdfWriter(clone_from=reader)
        for index, page in enumerate(writer.pages):
            if index in selected:
                page.rotate(degrees)
    return _write_pdf(writer, output)


def encrypt_pdf(
    path: PathLike, output: PathLike, *, user_password: str, owner_password: str | None = None
) -> Path:
    """Cifra o PDF com AES-256.

    Args:
        path: Arquivo PDF.
        output: Arquivo de destino.
        user_password: Senha para abrir o documento.
        owner_password: Senha de proprietário; padrão: `user_password`.

    Returns:
        Caminho absoluto do PDF cifrado.

    Raises:
        PDFError: Se o arquivo for inválido ou cifrado com senha, ou a gravação falhar.
    """
    from pypdf import PdfWriter

    source = _existing_pdf(path)
    reader = _reader(source, None)
    with _pdf_errors("cifrar PDF", source):
        writer = PdfWriter(clone_from=reader)
        writer.encrypt(
            user_password, owner_password or user_password, algorithm=_ENCRYPTION_ALGORITHM
        )
    result = _write_pdf(writer, output)
    get_logger().info("PDF cifrado | arquivo={} | algoritmo={}", result, _ENCRYPTION_ALGORITHM)
    return result


def decrypt_pdf(path: PathLike, output: PathLike, *, password: str) -> Path:
    """Grava uma cópia sem criptografia do PDF.

    Args:
        path: Arquivo PDF cifrado.
        output: Arquivo de destino.
        password: Senha de usuário ou de proprietário.

    Returns:
        Caminho absoluto do PDF decifrado.

    Raises:
        PDFError: Se a senha estiver errada, o arquivo for inválido ou a gravação falhar.
    """
    from pypdf import PdfWriter

    source = _existing_pdf(path)
    reader = _reader(source, password)
    with _pdf_errors("decifrar PDF", source):
        writer = PdfWriter(clone_from=reader)
    result = _write_pdf(writer, output)
    get_logger().info("PDF decifrado | arquivo={}", result)
    return result


def add_watermark(
    path: PathLike,
    output: PathLike,
    *,
    text: str | None = None,
    watermark_pdf: PathLike | None = None,
    font_size: int = 48,
    opacity: float = 0.15,
    angle: float = 45.0,
) -> Path:
    """Aplica marca d'água de texto (Helvetica, cinza) ou a primeira página de outro PDF.

    Args:
        path: Arquivo PDF.
        output: Arquivo de destino.
        text: Texto da marca d'água, centralizado em cada página.
        watermark_pdf: PDF cuja primeira página é mesclada sobre cada página.
        font_size: Tamanho da fonte do texto.
        opacity: Opacidade do texto, de 0 a 1.
        angle: Rotação do texto em graus (anti-horário).

    Returns:
        Caminho absoluto do PDF gravado.

    Raises:
        ValueError: Se não for informado exatamente um entre `text` e `watermark_pdf`, ou se
            `font_size`/`opacity` forem inválidos.
        PDFError: Se algum arquivo for inválido ou a gravação falhar.
    """
    from pypdf import PdfWriter

    stamp = _watermark_factory(text, watermark_pdf, font_size, opacity, angle)
    source = _existing_pdf(path)
    reader = _reader(source, None)
    with _pdf_errors("aplicar marca d'água", source):
        writer = PdfWriter(clone_from=reader)
        for page in writer.pages:
            page.merge_page(stamp(page))
    result = _write_pdf(writer, output)
    get_logger().info("Marca d'água aplicada | arquivo={}", result)
    return result


def get_form_fields(path: PathLike) -> dict[str, str | None]:
    """Retorna os campos do formulário e seus valores atuais.

    Args:
        path: Arquivo PDF com formulário (AcroForm).

    Returns:
        Nome qualificado do campo → valor (`None` quando vazio); `{}` sem formulário.

    Raises:
        PDFError: Se o arquivo for inválido ou cifrado com senha.
    """
    source = _existing_pdf(path)
    reader = _reader(source, None)
    with _pdf_errors("ler campos do formulário", source):
        fields = reader.get_fields() or {}
        return {name: _field_value(field.get("/V")) for name, field in fields.items()}


def fill_form(path: PathLike, output: PathLike, fields: Mapping[str, str]) -> Path:
    """Preenche os campos do formulário em todas as páginas.

    Args:
        path: Arquivo PDF com formulário.
        output: Arquivo de destino.
        fields: Nome do campo → valor.

    Returns:
        Caminho absoluto do PDF gravado.

    Raises:
        PDFError: Se algum campo não existir (a mensagem lista os disponíveis), o arquivo for
            inválido ou a gravação falhar.
    """
    from pypdf import PdfWriter

    source = _existing_pdf(path)
    reader = _reader(source, None)
    with _pdf_errors("ler campos do formulário", source):
        available = sorted(reader.get_fields() or {})
    unknown = sorted(set(fields) - set(available))
    if unknown:
        raise PDFError(
            f"Campos inexistentes no formulário: {', '.join(unknown)}",
            details={"campos": unknown, "disponiveis": ", ".join(available)},
        )
    with _pdf_errors("preencher formulário", source):
        writer = PdfWriter(clone_from=reader)
        for page in writer.pages:
            if _ANNOTATIONS_KEY in page:
                writer.update_page_form_field_values(page, dict(fields))
    result = _write_pdf(writer, output)
    get_logger().info("Formulário preenchido | arquivo={} | campos={}", result, len(fields))
    return result


def create_pdf_from_text(
    output: PathLike,
    text: str | Sequence[str],
    *,
    title: str | None = None,
    font_size: int = 11,
    page_size: PageSize = "A4",
    font_path: PathLike | None = None,
) -> Path:
    """Cria um PDF de texto com margens de 2 cm e quebra de página automática.

    Args:
        output: Arquivo de destino.
        text: Texto (quebras de linha preservadas) ou lista de parágrafos.
        title: Título no topo da primeira página e nos metadados.
        font_size: Tamanho da fonte do corpo.
        page_size: `"A4"` ou `"LETTER"`.
        font_path: Fonte TrueType para Unicode completo; padrão: Helvetica.

    Returns:
        Caminho absoluto do PDF gravado.

    Raises:
        ValueError: Se `font_size` ou `page_size` forem inválidos.
        PDFError: Se a fonte for inválida ou a geração falhar.
    """
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph

    if font_size < 1:
        raise ValueError(f"font_size deve ser maior ou igual a 1 (recebido: {font_size})")
    size = _page_size(page_size, landscape=False)
    target = Path(output)
    font, bold = _fonts(font_path)
    body = ParagraphStyle(
        "rpa3003_body", fontName=font, fontSize=font_size, leading=font_size * _LEADING_SCALE
    )
    paragraphs = [text] if isinstance(text, str) else list(text)
    story: list[Any] = _title_flowables(title, bold, font_size)
    story.extend(Paragraph(_markup(paragraph), body) for paragraph in paragraphs)
    data = _build_document(target, size, title, lambda _width: story)
    result = _write_bytes(target, data)
    get_logger().info("PDF de texto criado | arquivo={} | paragrafos={}", result, len(paragraphs))
    return result


def create_pdf_table(
    output: PathLike,
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
    *,
    title: str | None = None,
    landscape: bool = False,
    page_size: PageSize = "A4",
    font_path: PathLike | None = None,
) -> Path:
    """Cria um PDF com tabela em grade, cabeçalho cinza repetido a cada página.

    Args:
        output: Arquivo de destino.
        headers: Títulos das colunas.
        rows: Linhas, com a mesma quantidade de colunas de `headers`; `None` vira vazio.
        title: Título acima da tabela e nos metadados.
        landscape: Página em paisagem.
        page_size: `"A4"` ou `"LETTER"`.
        font_path: Fonte TrueType para Unicode completo; padrão: Helvetica.

    Returns:
        Caminho absoluto do PDF gravado.

    Raises:
        ValueError: Se `headers` estiver vazio, alguma linha tiver outra quantidade de colunas
            ou `page_size` for inválido.
        PDFError: Se a fonte for inválida ou a geração falhar.
    """
    if not headers:
        raise ValueError("Informe ao menos uma coluna em headers")
    for number, row in enumerate(rows, start=1):
        if len(row) != len(headers):
            raise ValueError(f"Linha {number} tem {len(row)} colunas; esperado {len(headers)}")
    size = _page_size(page_size, landscape=landscape)
    target = Path(output)
    font, bold = _fonts(font_path)

    def story(width: float) -> list[Any]:
        return [
            *_title_flowables(title, bold, _TABLE_FONT_SIZE),
            _table(headers, rows, width, font, bold),
        ]

    data = _build_document(target, size, title, story)
    result = _write_bytes(target, data)
    get_logger().info("PDF de tabela criado | arquivo={} | linhas={}", result, len(rows))
    return result


# --- leitura ----------------------------------------------------------------------------------


@contextlib.contextmanager
def _pdf_errors(action: str, path: Path | None = None) -> Iterator[None]:
    """Converte exceções de pypdf, pdfplumber/pdfminer e reportlab em `PDFError`."""
    try:
        yield
    except RPAError:
        raise
    except Exception as exc:
        details = {"arquivo": str(path)} if path is not None else {}
        details["erro"] = f"{type(exc).__name__}: {' '.join(str(exc).split())}"
        raise PDFError(f"Falha ao {action}", details=details) from exc


def _existing_pdf(path: PathLike) -> Path:
    source = Path(path).resolve()
    if not source.is_file():
        raise PDFError("Arquivo PDF não encontrado", details={"arquivo": str(source)})
    return source


def _reader(source: Path, password: str | None) -> PdfReader:
    """Abre o PDF com pypdf e decifra quando necessário.

    PDF cifrado com senha de usuário vazia (somente restrições de permissão) abre sem senha.
    """
    from pypdf import PdfReader

    with _pdf_errors("abrir PDF", source):
        reader = PdfReader(source)
        if reader.is_encrypted and reader.decrypt(password or "") == 0:
            message = "Senha do PDF incorreta" if password else "PDF cifrado: informe a senha"
            raise PDFError(message, details={"arquivo": str(source)})
    return reader


@contextlib.contextmanager
def _open_plumber(source: Path, password: str | None) -> Iterator[PDF]:
    """Abre o PDF com pdfplumber depois de validar a senha com pypdf (mensagens uniformes)."""
    import pdfplumber

    _reader(source, password)
    with _pdf_errors("abrir PDF", source):
        pdf = pdfplumber.open(source, password=password or "")
    try:
        yield pdf
    finally:
        with contextlib.suppress(Exception):
            pdf.close()


def _page_texts(
    path: PathLike, pages: PageSelection, password: str | None, *, layout: bool
) -> list[tuple[int, str]]:
    source = _existing_pdf(path)
    with _open_plumber(source, password) as pdf:
        indexes = _page_indexes(pages, len(pdf.pages))
        with _pdf_errors("extrair texto do PDF", source):
            return [
                (index + 1, pdf.pages[index].extract_text(layout=layout) or "") for index in indexes
            ]


def _page_indexes(pages: PageSelection, total: int) -> list[int]:
    """Converte números de página (a partir de 1) em índices; `None` seleciona todas."""
    if pages is None:
        return list(range(total))
    indexes: list[int] = []
    for number in pages:
        if isinstance(number, bool) or not isinstance(number, int):
            raise ValueError(f"Número de página inválido: {number!r}")
        if not 1 <= number <= total:
            raise ValueError(f"Página {number} fora do intervalo 1 a {total} (P-11)")
        indexes.append(number - 1)
    return indexes


def _page_ranges(ranges: Sequence[tuple[int, int]] | None, total: int) -> list[tuple[int, int]]:
    if ranges is None:
        return [(number, number) for number in range(1, total + 1)]
    if not ranges:
        raise ValueError("ranges não pode ser vazio; use None para um arquivo por página")
    spans: list[tuple[int, int]] = []
    for start, end in ranges:
        _page_indexes([start, end], total)
        if start > end:
            raise ValueError(f"Intervalo inválido: início {start} maior que fim {end}")
        spans.append((start, end))
    return spans


def _field_value(value: object) -> str | None:
    if value is None:
        return None
    get_object = getattr(value, "get_object", None)
    resolved = get_object() if callable(get_object) else value
    return None if resolved is None else str(resolved)


def _path_list(paths: Sequence[PathLike] | PathLike) -> list[PathLike]:
    return [paths] if isinstance(paths, str | os.PathLike) else list(paths)


# --- gravação ---------------------------------------------------------------------------------


def _write_pdf(writer: PdfWriter, output: PathLike) -> Path:
    target = Path(output)
    buffer = io.BytesIO()
    with _pdf_errors("gerar PDF", target):
        writer.write(buffer)
    return _write_bytes(target, buffer.getvalue())


def _write_bytes(target: Path, data: bytes) -> Path:
    """Grava em `<destino>.<uuid>.tmp` na mesma pasta e renomeia com `os.replace`."""
    temp = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp.write_bytes(data)
        os.replace(temp, target)
    except OSError as exc:
        raise PDFError(
            "Falha ao gravar PDF",
            details={"arquivo": str(target.resolve()), "erro": f"{type(exc).__name__}: {exc}"},
        ) from exc
    finally:
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)
    return target.resolve()


# --- marca d'água -----------------------------------------------------------------------------


def _watermark_factory(
    text: str | None,
    watermark_pdf: PathLike | None,
    font_size: int,
    opacity: float,
    angle: float,
) -> Callable[[PageObject], PageObject]:
    """Valida os parâmetros e retorna a função que produz o carimbo de cada página."""
    if (text is None) == (watermark_pdf is None):
        raise ValueError("Informe exatamente um entre text e watermark_pdf")
    if watermark_pdf is not None:
        stamp_source = _existing_pdf(watermark_pdf)
        stamp_reader = _reader(stamp_source, None)
        with _pdf_errors("ler PDF de marca d'água", stamp_source):
            stamp_page = stamp_reader.pages[0]
        return lambda _page: stamp_page
    if not str(text).strip():
        raise ValueError("text da marca d'água não pode ser vazio")
    if font_size < 1:
        raise ValueError(f"font_size deve ser maior ou igual a 1 (recebido: {font_size})")
    if not 0 <= opacity <= 1:
        raise ValueError(f"opacity deve estar entre 0 e 1 (recebido: {opacity})")
    cache: dict[tuple[float, float, float, float], PageObject] = {}

    def stamp(page: PageObject) -> PageObject:
        box = page.mediabox
        key = (float(box.left), float(box.bottom), float(box.right), float(box.top))
        if key not in cache:
            cache[key] = _text_stamp(str(text), key, font_size, opacity, angle)
        return cache[key]

    return stamp


def _text_stamp(
    text: str,
    box: tuple[float, float, float, float],
    font_size: int,
    opacity: float,
    angle: float,
) -> PageObject:
    """Página com o texto centralizado na área `box`, gerada em memória com reportlab."""
    from pypdf import PdfReader
    from reportlab.pdfgen import canvas

    left, bottom, right, top = box
    buffer = io.BytesIO()
    drawing = canvas.Canvas(buffer, pagesize=(right, top))
    drawing.setFillColorRGB(_WATERMARK_GRAY, _WATERMARK_GRAY, _WATERMARK_GRAY)
    drawing.setFillAlpha(opacity)
    drawing.setFont(_BASE_FONT, font_size)
    drawing.translate((left + right) / 2, (bottom + top) / 2)
    drawing.rotate(angle)
    drawing.drawCentredString(0, -font_size / 3, text)
    drawing.showPage()
    drawing.save()
    return PdfReader(io.BytesIO(buffer.getvalue())).pages[0]


# --- criação ----------------------------------------------------------------------------------


def _page_size(name: str, *, landscape: bool) -> tuple[float, float]:
    from reportlab.lib import pagesizes

    sizes: dict[str, tuple[float, float]] = {"A4": pagesizes.A4, "LETTER": pagesizes.LETTER}
    size = sizes.get(str(name).upper())
    if size is None:
        raise ValueError(f"page_size deve ser 'A4' ou 'LETTER' (recebido: {name!r})")
    return pagesizes.landscape(size) if landscape else size


def _fonts(font_path: PathLike | None) -> tuple[str, str]:
    """Fontes `(normal, negrito)`: Helvetica ou a fonte TrueType registrada no reportlab."""
    if font_path is None:
        return _BASE_FONT, _BOLD_FONT
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    path = Path(font_path).resolve()
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:12]
    name = f"rpa3003_{digest}"
    if name not in pdfmetrics.getRegisteredFontNames():
        if not path.is_file():
            raise PDFError("Fonte TrueType não encontrada", details={"arquivo": str(path)})
        with _pdf_errors("registrar fonte TrueType", path):
            pdfmetrics.registerFont(TTFont(name, str(path)))
    return name, name


def _title_flowables(title: str | None, font: str, font_size: int) -> list[Any]:
    if not title:
        return []
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, Spacer

    size = font_size * _TITLE_SCALE
    style = ParagraphStyle("rpa3003_title", fontName=font, fontSize=size, leading=size * 1.2)
    return [Paragraph(_markup(title), style), Spacer(1, 0.4 * cm)]


def _table(
    headers: Sequence[str], rows: Sequence[Sequence[Any]], width: float, font: str, bold: str
) -> Any:  # noqa: ANN401 - reportlab.platypus.Table
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph, Table, TableStyle

    leading = _TABLE_FONT_SIZE * _LEADING_SCALE
    header_style = ParagraphStyle(
        "rpa3003_header", fontName=bold, fontSize=_TABLE_FONT_SIZE, leading=leading
    )
    cell_style = ParagraphStyle(
        "rpa3003_cell", fontName=font, fontSize=_TABLE_FONT_SIZE, leading=leading
    )
    data = [[Paragraph(_markup(str(header)), header_style) for header in headers]]
    data.extend(
        [Paragraph(_markup("" if value is None else str(value)), cell_style) for value in row]
        for row in rows
    )
    table = Table(data, colWidths=[width / len(headers)] * len(headers), repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    return table


def _build_document(
    target: Path,
    size: tuple[float, float],
    title: str | None,
    story: Callable[[float], list[Any]],
) -> bytes:
    """Monta o documento com margens de 2 cm; `story` recebe a largura útil da página."""
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate

    buffer = io.BytesIO()
    margin = _MARGIN_CM * cm
    with _pdf_errors("criar PDF", target):
        document = SimpleDocTemplate(
            buffer,
            pagesize=size,
            leftMargin=margin,
            rightMargin=margin,
            topMargin=margin,
            bottomMargin=margin,
            title=title or "",
            author="rpa_3003",
        )
        document.build(story(document.width))
    return buffer.getvalue()


def _markup(text: str) -> str:
    """Escapa o texto para Paragraph do reportlab, preservando quebras de linha."""
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return escaped.replace("\r\n", "\n").replace("\n", "<br/>")
