"""Imagens.

Manipulação de imagens com Pillow: abertura sem manter o arquivo bloqueado, gravação com formato
pela extensão, redimensionamento, recorte, rotação, tons de cinza, conversão, compressão,
comparação, PDF a partir de imagens, Base64 e texto sobre imagens.

As funções nunca alteram a imagem recebida: toda operação retorna uma nova imagem.
"""

from __future__ import annotations

import base64
import io
import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Final, TypeAlias

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageStat

from rpa_3003.core.config import PathLike
from rpa_3003.core.exceptions import ImageProcessingError
from rpa_3003.core.logger import get_logger

__all__: list[str] = [
    "ImageSource",
    "add_text",
    "compare_images",
    "compress",
    "convert_format",
    "crop",
    "from_base64",
    "get_dimensions",
    "images_to_pdf",
    "open_image",
    "resize",
    "rotate",
    "save_image",
    "to_base64",
    "to_grayscale",
]

ImageSource: TypeAlias = str | os.PathLike[str] | Image.Image  # noqa: UP040 - convenção 7.7

_WHITE: Final[tuple[int, int, int]] = (255, 255, 255)
_JPEG: Final[str] = "JPEG"
_PDF: Final[str] = "PDF"
# Modos que o JPEG grava sem conversão; os demais viram RGB sobre fundo branco.
_JPEG_MODES: Final[frozenset[str]] = frozenset({"RGB", "L", "CMYK"})
_FORMAT_ALIASES: Final[dict[str, str]] = {"JPG": _JPEG, "TIF": "TIFF"}
_DATA_URI_MARKER: Final[str] = ";base64,"
_MAX_QUALITY: Final[int] = 100
# Erros do Pillow: OSError inclui UnidentifiedImageError e arquivo inexistente.
_PILLOW_ERRORS: Final[tuple[type[Exception], ...]] = (
    OSError,
    ValueError,
    Image.DecompressionBombError,
)


def open_image(source: ImageSource) -> Image.Image:
    """Abre uma imagem totalmente carregada em memória, sem manter o arquivo aberto.

    Args:
        source: Caminho do arquivo ou imagem já aberta (retorna uma cópia).

    Returns:
        Nova imagem, independente da origem.

    Raises:
        ImageProcessingError: Se o arquivo não existir ou não for uma imagem válida.
    """
    if isinstance(source, Image.Image):
        return source.copy()
    path = Path(source)
    with _pillow_errors("abrir imagem", path), Image.open(path) as image:
        image.load()
    # Após load(), os pixels estão em memória; o `with` fechou o arquivo (sem lock no Windows).
    return image


def save_image(
    image: Image.Image, path: PathLike, *, quality: int = 90, optimize: bool = True
) -> Path:
    """Grava a imagem no formato indicado pela extensão do destino.

    JPEG a partir de imagens com transparência ou paleta é convertido para RGB sobre fundo
    branco. A pasta de destino é criada se necessário.

    Args:
        image: Imagem a gravar (não é alterada).
        path: Arquivo de destino (ex.: `"saida.jpg"`).
        quality: Qualidade de 1 a 100 (JPEG e WebP).
        optimize: Otimiza a codificação (JPEG, PNG e GIF).

    Returns:
        Caminho absoluto do arquivo gravado.

    Raises:
        ValueError: Se `quality` estiver fora de 1 a 100.
        ImageProcessingError: Se a extensão não for suportada ou a gravação falhar.
    """
    _check_quality(quality)
    target = Path(path)
    image_format = _format_for_path(target)
    with _pillow_errors("salvar imagem", target):
        prepared = _prepare_for_format(image, image_format)
        target.parent.mkdir(parents=True, exist_ok=True)
        prepared.save(target, format=image_format, quality=quality, optimize=optimize)
    resolved = target.resolve()
    get_logger().debug("Imagem salva | arquivo={} | formato={}", resolved, image_format)
    return resolved


def get_dimensions(source: ImageSource) -> tuple[int, int]:
    """Retorna `(largura, altura)` em pixels, sem decodificar os pixels do arquivo.

    Args:
        source: Caminho do arquivo ou imagem.

    Returns:
        Largura e altura.

    Raises:
        ImageProcessingError: Se o arquivo não for uma imagem válida.
    """
    if isinstance(source, Image.Image):
        return source.size
    path = Path(source)
    with _pillow_errors("ler dimensões da imagem", path), Image.open(path) as image:
        return image.size


def resize(
    source: ImageSource,
    *,
    width: int | None = None,
    height: int | None = None,
    scale: float | None = None,
    keep_aspect: bool = True,
) -> Image.Image:
    """Redimensiona com filtro `LANCZOS` por fator (`scale`) ou por dimensões.

    Com `keep_aspect=True`, uma única dimensão calcula a outra e as duas juntas definem a caixa
    em que a imagem cabe sem distorção. Com `keep_aspect=False`, a dimensão omitida é mantida.

    Args:
        source: Caminho ou imagem.
        width: Largura desejada em pixels.
        height: Altura desejada em pixels.
        scale: Fator de escala (ex.: `0.5`); exclusivo com `width`/`height`.
        keep_aspect: Mantém a proporção original.

    Returns:
        Nova imagem redimensionada.

    Raises:
        ValueError: Se nenhum ou ambos os modos forem informados, ou algum valor não for
            positivo.
        ImageProcessingError: Se a imagem não puder ser processada.
    """
    _check_resize_arguments(width, height, scale)
    image = _as_image(source)
    size = _target_size(image.size, width, height, scale, keep_aspect=keep_aspect)
    with _pillow_errors("redimensionar imagem"):
        return image.resize(size, Image.Resampling.LANCZOS)


def crop(source: ImageSource, box: tuple[int, int, int, int]) -> Image.Image:
    """Recorta a área `box = (left, top, right, bottom)`, em pixels.

    Args:
        source: Caminho ou imagem.
        box: Área de recorte, contida na imagem, com `left < right` e `top < bottom`.

    Returns:
        Nova imagem recortada.

    Raises:
        ValueError: Se a área for vazia ou sair dos limites da imagem.
        ImageProcessingError: Se a imagem não puder ser processada.
    """
    image = _as_image(source)
    left, top, right, bottom = box
    width, height = image.size
    if not (0 <= left < right <= width and 0 <= top < bottom <= height):
        raise ValueError(f"Área de recorte inválida para imagem {width}x{height}: {box}")
    with _pillow_errors("recortar imagem"):
        return image.crop((left, top, right, bottom))


def rotate(source: ImageSource, degrees: float, *, expand: bool = True) -> Image.Image:
    """Gira a imagem no sentido anti-horário (graus positivos), como o Pillow.

    Args:
        source: Caminho ou imagem.
        degrees: Ângulo em graus; negativo gira no sentido horário.
        expand: Aumenta a tela para caber a imagem inteira.

    Returns:
        Nova imagem girada.

    Raises:
        ImageProcessingError: Se a imagem não puder ser processada.
    """
    image = _as_image(source)
    with _pillow_errors("girar imagem"):
        return image.rotate(degrees, resample=Image.Resampling.BICUBIC, expand=expand)


def to_grayscale(source: ImageSource) -> Image.Image:
    """Converte para tons de cinza (`L`, ou `LA` quando há transparência).

    Args:
        source: Caminho ou imagem.

    Returns:
        Nova imagem em tons de cinza.

    Raises:
        ImageProcessingError: Se a imagem não puder ser processada.
    """
    image = _as_image(source)
    with _pillow_errors("converter imagem para tons de cinza"):
        if image.has_transparency_data:
            return image.convert("RGBA").convert("LA")
        return image.convert("L")


def convert_format(src: PathLike, dst: PathLike) -> Path:
    """Converte o arquivo para o formato da extensão de `dst` (ex.: PNG → JPEG).

    Args:
        src: Imagem de origem.
        dst: Arquivo de destino.

    Returns:
        Caminho absoluto do arquivo gravado.

    Raises:
        ImageProcessingError: Se a leitura, a conversão ou a gravação falharem.
    """
    return save_image(open_image(src), dst)


def compress(
    src: PathLike, dst: PathLike, *, quality: int = 75, max_width: int | None = None
) -> Path:
    """Grava uma versão menor da imagem, reduzindo a qualidade e, opcionalmente, a largura.

    Args:
        src: Imagem de origem.
        dst: Arquivo de destino (o formato vem da extensão).
        quality: Qualidade de 1 a 100.
        max_width: Largura máxima; imagens mais largas são reduzidas mantendo a proporção.

    Returns:
        Caminho absoluto do arquivo gravado.

    Raises:
        ValueError: Se `quality` ou `max_width` forem inválidos.
        ImageProcessingError: Se a leitura ou a gravação falharem.
    """
    _check_quality(quality)
    if max_width is not None and max_width < 1:
        raise ValueError(f"max_width deve ser maior ou igual a 1 (recebido: {max_width})")
    image = open_image(src)
    if max_width is not None and image.width > max_width:
        image = resize(image, width=max_width)
    result = save_image(image, dst, quality=quality, optimize=True)
    get_logger().info(
        "Imagem comprimida | origem={} | destino={} | bytes={}",
        Path(src).resolve(),
        result,
        result.stat().st_size,
    )
    return result


def compare_images(a: ImageSource, b: ImageSource) -> float:
    """Similaridade entre 0 e 1: `1 - média(|a - b|) / 255` em tons de cinza, com 4 casas.

    A imagem `b` é redimensionada para o tamanho de `a` antes da comparação.

    Args:
        a: Imagem de referência.
        b: Imagem comparada.

    Returns:
        `1.0` para imagens idênticas.

    Raises:
        ImageProcessingError: Se alguma imagem não puder ser processada.
    """
    first = _as_image(a)
    second = _as_image(b)
    with _pillow_errors("comparar imagens"):
        gray_a = first.convert("L")
        gray_b = second.convert("L")
        if gray_b.size != gray_a.size:
            gray_b = gray_b.resize(gray_a.size, Image.Resampling.LANCZOS)
        mean = ImageStat.Stat(ImageChops.difference(gray_a, gray_b)).mean[0]
    return round(1 - mean / 255, 4)


def images_to_pdf(sources: Sequence[ImageSource], output: PathLike) -> Path:
    """Gera um PDF com uma página por imagem, na ordem recebida.

    Args:
        sources: Caminhos ou imagens; transparência vira fundo branco.
        output: Arquivo PDF de destino.

    Returns:
        Caminho absoluto do PDF gravado.

    Raises:
        ValueError: Se `sources` estiver vazio.
        ImageProcessingError: Se alguma imagem não puder ser lida ou o PDF não puder ser
            gravado.
    """
    items: list[ImageSource] = (
        [sources] if isinstance(sources, str | os.PathLike | Image.Image) else list(sources)
    )
    if not items:
        raise ValueError("Informe ao menos uma imagem para gerar o PDF")
    target = Path(output)
    with _pillow_errors("gerar PDF a partir de imagens", target):
        pages = [_flatten(_as_image(item)) for item in items]
        target.parent.mkdir(parents=True, exist_ok=True)
        pages[0].save(target, format=_PDF, save_all=True, append_images=pages[1:])
    resolved = target.resolve()
    get_logger().info(
        "PDF gerado a partir de imagens | arquivo={} | paginas={}", resolved, len(pages)
    )
    return resolved


def to_base64(source: ImageSource, *, fmt: str = "PNG") -> str:
    """Codifica a imagem em Base64 no formato indicado.

    Args:
        source: Caminho ou imagem.
        fmt: Formato do Pillow (ex.: `"PNG"`, `"JPEG"`).

    Returns:
        Texto Base64 (sem prefixo `data:`).

    Raises:
        ValueError: Se o formato não for suportado para gravação.
        ImageProcessingError: Se a imagem não puder ser codificada.
    """
    image_format = _normalize_format(fmt)
    image = _as_image(source)
    buffer = io.BytesIO()
    with _pillow_errors("codificar imagem em Base64"):
        _prepare_for_format(image, image_format).save(buffer, format=image_format)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def from_base64(data: str) -> Image.Image:
    """Decodifica uma imagem em Base64 (aceita prefixo `data:image/...;base64,`).

    Args:
        data: Texto Base64; espaços e quebras de linha são ignorados.

    Returns:
        Imagem carregada em memória.

    Raises:
        ImageProcessingError: Se o Base64 for inválido ou o conteúdo não for uma imagem.
    """
    payload = data.split(_DATA_URI_MARKER, 1)[1] if _DATA_URI_MARKER in data else data
    try:
        raw = base64.b64decode("".join(payload.split()), validate=True)
    except ValueError as exc:  # binascii.Error e texto não ASCII
        raise ImageProcessingError("Base64 de imagem inválido") from exc
    with _pillow_errors("decodificar imagem"), Image.open(io.BytesIO(raw)) as image:
        image.load()
    return image


def add_text(
    source: ImageSource,
    text: str,
    position: tuple[int, int],
    *,
    font_size: int = 24,
    color: tuple[int, int, int] = (255, 0, 0),
    font_path: PathLike | None = None,
) -> Image.Image:
    """Escreve texto sobre uma cópia da imagem (RGB, ou RGBA quando há transparência).

    Args:
        source: Caminho ou imagem.
        text: Texto a escrever (aceita acentos).
        position: Canto superior esquerdo do texto `(x, y)`.
        font_size: Tamanho da fonte em pixels.
        color: Cor RGB.
        font_path: Fonte TrueType; `None` usa a fonte padrão do Pillow.

    Returns:
        Nova imagem com o texto.

    Raises:
        ValueError: Se `font_size` for menor que 1.
        ImageProcessingError: Se a fonte não puder ser carregada ou o desenho falhar.
    """
    if font_size < 1:
        raise ValueError(f"font_size deve ser maior ou igual a 1 (recebido: {font_size})")
    image = _as_image(source)
    with _pillow_errors("escrever texto na imagem"):
        canvas = image.convert("RGBA" if image.has_transparency_data else "RGB")
        font = _load_font(font_path, font_size)
        ImageDraw.Draw(canvas).text(position, text, fill=color, font=font)
    return canvas


# --- auxiliares -------------------------------------------------------------------------------


@contextmanager
def _pillow_errors(action: str, path: Path | None = None) -> Iterator[None]:
    """Converte erros do Pillow em `ImageProcessingError`."""
    try:
        yield
    except _PILLOW_ERRORS as exc:
        details = {"arquivo": str(path.resolve())} if path is not None else {}
        details["erro"] = f"{type(exc).__name__}: {exc}"
        raise ImageProcessingError(f"Falha ao {action}", details=details) from exc


def _as_image(source: ImageSource) -> Image.Image:
    """Imagem para leitura: a própria instância (não é alterada) ou o arquivo aberto."""
    return source if isinstance(source, Image.Image) else open_image(source)


def _format_for_path(path: Path) -> str:
    extension = path.suffix.lower()
    image_format = Image.registered_extensions().get(extension)
    if image_format is None or image_format not in Image.SAVE:
        raise ImageProcessingError(
            "Extensão de imagem não suportada para gravação",
            details={"arquivo": str(path.resolve()), "extensao": extension},
        )
    return image_format


def _normalize_format(fmt: str) -> str:
    Image.init()
    name = fmt.strip().upper()
    name = _FORMAT_ALIASES.get(name, name)
    if name not in Image.SAVE:
        raise ValueError(f"Formato de imagem não suportado para gravação: {fmt}")
    return name


def _prepare_for_format(image: Image.Image, image_format: str) -> Image.Image:
    if image_format == _JPEG and image.mode not in _JPEG_MODES:
        return _flatten(image)
    return image


def _flatten(image: Image.Image) -> Image.Image:
    """Converte para RGB; a transparência vira fundo branco."""
    if image.mode == "RGB":
        return image
    if not image.has_transparency_data:
        return image.convert("RGB")
    rgba = image.convert("RGBA")
    background = Image.new("RGB", rgba.size, _WHITE)
    background.paste(rgba, mask=rgba.getchannel("A"))
    return background


def _check_quality(quality: int) -> None:
    if not 1 <= quality <= _MAX_QUALITY:
        raise ValueError(f"quality deve estar entre 1 e 100 (recebido: {quality})")


def _check_resize_arguments(width: int | None, height: int | None, scale: float | None) -> None:
    if scale is not None and (width is not None or height is not None):
        raise ValueError("Informe scale ou width/height, não ambos")
    if scale is None and width is None and height is None:
        raise ValueError("Informe scale, width ou height")
    if scale is not None and scale <= 0:
        raise ValueError(f"scale deve ser maior que zero (recebido: {scale})")
    for name, value in (("width", width), ("height", height)):
        if value is not None and value < 1:
            raise ValueError(f"{name} deve ser maior ou igual a 1 (recebido: {value})")


def _target_size(
    size: tuple[int, int],
    width: int | None,
    height: int | None,
    scale: float | None,
    *,
    keep_aspect: bool,
) -> tuple[int, int]:
    current_width, current_height = size
    if scale is not None:
        return _scaled(size, scale)
    if width is not None and height is not None:
        if not keep_aspect:
            return width, height
        return _scaled(size, min(width / current_width, height / current_height))
    if width is not None:
        return (width, _proportional(current_height, width, current_width, keep_aspect))
    target_height = height if height is not None else current_height
    return (_proportional(current_width, target_height, current_height, keep_aspect), target_height)


def _scaled(size: tuple[int, int], factor: float) -> tuple[int, int]:
    return max(1, round(size[0] * factor)), max(1, round(size[1] * factor))


def _proportional(other: int, target: int, current: int, keep_aspect: bool) -> int:
    """Dimensão complementar: proporcional a `target / current` ou mantida."""
    return max(1, round(other * target / current)) if keep_aspect else other


def _load_font(
    font_path: PathLike | None, font_size: int
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if font_path is None:
        return ImageFont.load_default(size=font_size)
    return ImageFont.truetype(str(Path(font_path)), font_size)
