"""Testes de `rpa_3003.utils.images_utils` (RF-046, seção 8.17)."""

from __future__ import annotations

import base64
import io
from pathlib import Path

import pytest
import reportlab
from PIL import Image
from pypdf import PdfReader
from pytest_mock import MockerFixture

from rpa_3003.core.exceptions import ImageProcessingError
from rpa_3003.utils import images_utils as img

pytestmark = pytest.mark.unit

TTF_FONT = Path(reportlab.__file__).parent / "fonts" / "Vera.ttf"


@pytest.fixture
def photo(tmp_path: Path) -> Path:
    """PNG RGB 400x200 com metade esquerda vermelha e metade direita azul."""
    image = Image.new("RGB", (400, 200), (255, 0, 0))
    image.paste((0, 0, 255), (200, 0, 400, 200))
    path = tmp_path / "foto ção.png"
    image.save(path)
    return path


@pytest.fixture
def transparent_png(tmp_path: Path) -> Path:
    """PNG RGBA 100x100 totalmente transparente com um quadrado preto opaco no centro."""
    image = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    image.paste((0, 0, 0, 255), (40, 40, 60, 60))
    path = tmp_path / "logo.png"
    image.save(path)
    return path


def test_open_image_loads_and_releases_file(photo: Path) -> None:
    """Seção 8.17: abre, executa load() e fecha o arquivo (sem lock no Windows)."""
    image = img.open_image(photo)
    renamed = photo.with_name("renomeada.png")
    photo.rename(renamed)
    renamed.unlink()
    assert image.size == (400, 200)
    assert image.getpixel((10, 10)) == (255, 0, 0)


def test_open_image_with_instance_returns_copy() -> None:
    """Seção 8.17: com Image.Image retorna copy(); a entrada nunca é alterada."""
    original = Image.new("RGB", (10, 10), (1, 2, 3))
    copy = img.open_image(original)
    copy.putpixel((0, 0), (9, 9, 9))
    assert copy is not original
    assert original.getpixel((0, 0)) == (1, 2, 3)


def test_open_image_errors(tmp_path: Path) -> None:
    """Seção 8.17: arquivo inexistente ou que não é imagem gera ImageProcessingError."""
    with pytest.raises(ImageProcessingError, match="Falha ao abrir imagem") as info:
        img.open_image(tmp_path / "nao_existe.png")
    assert info.value.details["arquivo"].endswith("nao_existe.png")
    text_file = tmp_path / "texto.png"
    text_file.write_text("não sou imagem", encoding="utf-8")
    with pytest.raises(ImageProcessingError) as unidentified:
        img.open_image(text_file)
    assert "UnidentifiedImageError" in unidentified.value.details["erro"]


def test_save_image_infers_format_creates_dirs_and_returns_absolute(
    photo: Path, tmp_path: Path
) -> None:
    """Seção 8.17: formato pela extensão do destino; pasta criada; caminho absoluto."""
    image = img.open_image(photo)
    result = img.save_image(image, tmp_path / "saida" / "foto.JPG", quality=85)
    assert result == (tmp_path / "saida" / "foto.JPG").resolve()
    with Image.open(result) as saved:
        assert saved.format == "JPEG"
    webp = img.save_image(image, tmp_path / "foto.webp")
    with Image.open(webp) as saved:
        assert saved.format == "WEBP"


def test_save_image_rgba_to_jpeg_uses_white_background(
    transparent_png: Path, tmp_path: Path
) -> None:
    """Seção 8.17: JPEG a partir de RGBA vira RGB sobre fundo branco; a entrada não muda."""
    image = img.open_image(transparent_png)
    result = img.save_image(image, tmp_path / "logo.jpg")
    assert image.mode == "RGBA"
    with Image.open(result) as saved:
        assert saved.mode == "RGB"
        corner = saved.getpixel((2, 2))
        center = saved.getpixel((50, 50))
    assert all(channel > 245 for channel in corner)
    assert all(channel < 10 for channel in center)


def test_save_image_palette_with_transparency_to_jpeg(tmp_path: Path) -> None:
    """Seção 8.17: JPEG a partir de P com transparência também usa fundo branco."""
    palette = Image.new("P", (20, 20), 0)
    palette.putpalette([0, 0, 0, 255, 0, 0] + [0] * 762)
    palette.info["transparency"] = 0
    with Image.open(img.save_image(palette, tmp_path / "p.jpg")) as saved:
        assert all(channel > 245 for channel in saved.getpixel((5, 5)))
    opaque = Image.new("P", (5, 5), 1)
    opaque.putpalette([0, 0, 0, 255, 0, 0] + [0] * 762)
    with Image.open(img.save_image(opaque, tmp_path / "opaco.jpg")) as saved:
        red, green, blue = saved.getpixel((2, 2))
    assert red > 200 and green < 60 and blue < 60


def test_save_image_rejects_unknown_extension_and_quality(photo: Path, tmp_path: Path) -> None:
    """Seção 8.17: extensão sem suporte gera ImageProcessingError; quality fora de 1-100."""
    image = img.open_image(photo)
    with pytest.raises(ImageProcessingError, match="Extensão de imagem não suportada"):
        img.save_image(image, tmp_path / "foto.xyz")
    with pytest.raises(ValueError, match="quality"):
        img.save_image(image, tmp_path / "foto.jpg", quality=0)
    with pytest.raises(ValueError, match="quality"):
        img.save_image(image, tmp_path / "foto.jpg", quality=101)


def test_save_image_write_failure(photo: Path, tmp_path: Path) -> None:
    """Seção 8.17: erro de gravação (OSError) vira ImageProcessingError."""
    blocker = tmp_path / "arquivo"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(ImageProcessingError, match="Falha ao salvar imagem"):
        img.save_image(img.open_image(photo), blocker / "foto.png")


def test_get_dimensions(photo: Path) -> None:
    """Seção 8.17: (largura, altura) de arquivo ou imagem."""
    assert img.get_dimensions(photo) == (400, 200)
    assert img.get_dimensions(str(photo)) == (400, 200)
    assert img.get_dimensions(Image.new("L", (3, 7))) == (3, 7)


def test_resize_with_width_keeps_aspect_ratio(photo: Path) -> None:
    """RF-046: resize(img, width=100) mantém a proporção."""
    assert img.resize(photo, width=100).size == (100, 50)
    assert img.resize(photo, height=50).size == (100, 50)
    assert img.resize(photo, scale=0.25).size == (100, 50)


def test_resize_modes_and_keep_aspect(photo: Path) -> None:
    """Seção 8.17: duas dimensões cabem na caixa com keep_aspect; sem ele, tamanho exato."""
    image = img.open_image(photo)
    assert img.resize(image, width=100, height=100).size == (100, 50)
    assert img.resize(image, width=100, height=100, keep_aspect=False).size == (100, 100)
    assert img.resize(image, width=100, keep_aspect=False).size == (100, 200)
    assert img.resize(image, height=100, keep_aspect=False).size == (400, 100)
    assert img.resize(image, scale=0.001).size == (1, 1)
    assert image.size == (400, 200)


def test_resize_uses_lanczos(photo: Path, mocker: MockerFixture) -> None:
    """Seção 8.17: filtro Image.Resampling.LANCZOS."""
    image = img.open_image(photo)
    spy = mocker.spy(image, "resize")
    img.resize(image, width=10)
    assert spy.call_args.args == ((10, 5), Image.Resampling.LANCZOS)


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"scale": 0.5, "width": 10},
        {"scale": 0.5, "height": 10},
        {"scale": 0},
        {"width": 0},
        {"height": -5},
    ],
)
def test_resize_invalid_combinations(photo: Path, kwargs: dict[str, float]) -> None:
    """Seção 8.17: exatamente um modo; combinação inválida gera ValueError."""
    with pytest.raises(ValueError):
        img.resize(photo, **kwargs)  # type: ignore[arg-type]


def test_crop_box_left_top_right_bottom(photo: Path) -> None:
    """Seção 8.17: box = (left, top, right, bottom)."""
    piece = img.crop(photo, (190, 0, 210, 20))
    assert piece.size == (20, 20)
    assert piece.getpixel((0, 0)) == (255, 0, 0)
    assert piece.getpixel((19, 0)) == (0, 0, 255)


@pytest.mark.parametrize(
    "box", [(0, 0, 0, 10), (10, 0, 5, 10), (0, 0, 401, 10), (-1, 0, 10, 10), (0, 0, 10, 201)]
)
def test_crop_invalid_box(photo: Path, box: tuple[int, int, int, int]) -> None:
    """Seção 8.17: área vazia ou fora da imagem gera ValueError."""
    with pytest.raises(ValueError, match="Área de recorte inválida"):
        img.crop(photo, box)


def test_rotate_expand_and_direction(photo: Path) -> None:
    """Seção 8.17: rotate com expand=True ajusta a tela; graus positivos são anti-horários."""
    rotated = img.rotate(photo, 90)
    assert rotated.size == (200, 400)
    assert rotated.getpixel((100, 10)) == (0, 0, 255)  # lado direito (azul) foi para cima
    assert img.rotate(photo, 90, expand=False).size == (400, 200)
    assert img.rotate(photo, 45).size[0] > 400


def test_to_grayscale_keeps_transparency(photo: Path, transparent_png: Path) -> None:
    """Seção 8.17: tons de cinza (L), preservando transparência (LA)."""
    assert img.to_grayscale(photo).mode == "L"
    gray = img.to_grayscale(transparent_png)
    assert gray.mode == "LA"
    assert gray.getpixel((0, 0))[1] == 0


def test_convert_format_transparent_png_to_jpeg_white_background(
    transparent_png: Path, tmp_path: Path
) -> None:
    """RF-046: convert_format("a.png", "a.jpg") com PNG transparente gera JPEG com fundo branco."""
    result = img.convert_format(transparent_png, tmp_path / "a.jpg")
    with Image.open(result) as converted:
        assert converted.format == "JPEG"
        assert all(channel > 245 for channel in converted.getpixel((5, 5)))


def test_compress_reduces_quality_and_width(photo: Path, tmp_path: Path) -> None:
    """Seção 8.17: compress com quality e max_width (proporção mantida)."""
    big = tmp_path / "grande.png"
    noise = Image.effect_noise((800, 400), 80).convert("RGB")
    noise.save(big)
    high = img.compress(big, tmp_path / "alta.jpg", quality=95)
    low = img.compress(big, tmp_path / "baixa.jpg", quality=20, max_width=200)
    assert low.stat().st_size < high.stat().st_size
    assert img.get_dimensions(low) == (200, 100)
    assert img.get_dimensions(img.compress(photo, tmp_path / "igual.jpg", max_width=1000)) == (
        400,
        200,
    )
    with pytest.raises(ValueError, match="max_width"):
        img.compress(photo, tmp_path / "x.jpg", max_width=0)
    with pytest.raises(ValueError, match="quality"):
        img.compress(photo, tmp_path / "x.jpg", quality=0)


def test_compare_images_identical_is_one(photo: Path) -> None:
    """RF-046: compare_images(a, a) == 1.0."""
    assert img.compare_images(photo, photo) == 1.0
    assert img.compare_images(photo, img.open_image(photo)) == 1.0


def test_compare_images_formula_and_resize(photo: Path) -> None:
    """Seção 8.17: 1 - média(|a - b|)/255 em cinza, com b redimensionada, 4 casas."""
    black = Image.new("L", (10, 10), 0)
    white = Image.new("L", (30, 30), 255)
    assert img.compare_images(black, white) == 0.0
    gray = Image.new("L", (10, 10), 51)
    assert img.compare_images(black, gray) == 0.8
    half = img.compare_images(photo, img.resize(photo, width=200))
    assert 0.95 < half <= 1.0
    value = img.compare_images(Image.new("L", (3, 1), 0), Image.new("L", (3, 1), 1))
    assert value == round(1 - 1 / 255, 4)


def test_images_to_pdf_multipage(photo: Path, transparent_png: Path, tmp_path: Path) -> None:
    """Seção 8.17: imagens convertidas para RGB e gravadas em PDF multipágina."""
    output = img.images_to_pdf(
        [photo, transparent_png, Image.new("L", (50, 50))], tmp_path / "pdf" / "digitalizado.pdf"
    )
    assert output == (tmp_path / "pdf" / "digitalizado.pdf").resolve()
    assert len(PdfReader(output).pages) == 3
    single = img.images_to_pdf(photo, tmp_path / "um.pdf")  # type: ignore[arg-type]
    assert len(PdfReader(single).pages) == 1
    with pytest.raises(ValueError, match="ao menos uma imagem"):
        img.images_to_pdf([], tmp_path / "vazio.pdf")


def test_base64_roundtrip_and_data_uri(photo: Path, transparent_png: Path) -> None:
    """Seção 8.17: to_base64/from_base64 ida e volta; aceita prefixo data: e quebras de linha."""
    encoded = img.to_base64(photo)
    assert base64.b64decode(encoded)[:8] == b"\x89PNG\r\n\x1a\n"
    decoded = img.from_base64(encoded)
    assert decoded.size == (400, 200)
    assert img.compare_images(photo, decoded) == 1.0
    wrapped = "data:image/png;base64," + "\n".join(
        encoded[i : i + 76] for i in range(0, len(encoded), 76)
    )
    assert img.from_base64(wrapped).size == (400, 200)
    jpeg = img.to_base64(transparent_png, fmt="jpg")
    with Image.open(io.BytesIO(base64.b64decode(jpeg))) as opened:
        assert opened.format == "JPEG"


def test_base64_errors() -> None:
    """Seção 8.17: Base64 inválido, conteúdo que não é imagem e formato sem suporte."""
    with pytest.raises(ImageProcessingError, match="Base64 de imagem inválido"):
        img.from_base64("não é base64!")
    with pytest.raises(ImageProcessingError, match="decodificar imagem"):
        img.from_base64(base64.b64encode(b"texto qualquer").decode())
    with pytest.raises(ValueError, match="Formato de imagem não suportado"):
        img.to_base64(Image.new("RGB", (1, 1)), fmt="XYZ")


def test_add_text_draws_on_copy(photo: Path) -> None:
    """Seção 8.17: add_text escreve sobre uma cópia; a imagem de entrada não muda."""
    original = Image.new("RGB", (200, 60), (255, 255, 255))
    result = img.add_text(original, "Ação concluída", (5, 5), font_size=30, color=(0, 0, 0))
    assert result is not original
    assert original.getcolors() == [(200 * 60, (255, 255, 255))]
    assert len(result.getcolors(200 * 60) or []) > 1  # surgiram pixels além do branco
    assert img.add_text(photo, "x", (0, 0)).mode == "RGB"


def test_add_text_with_truetype_font_and_modes(transparent_png: Path) -> None:
    """Seção 8.17: fonte via ImageFont.truetype(font_path, font_size); RGBA preservado."""
    result = img.add_text(transparent_png, "RPA", (10, 10), font_size=20, font_path=TTF_FONT)
    assert result.mode == "RGBA"
    assert result.getbbox() is not None
    gray = img.add_text(Image.new("L", (50, 20), 255), "A", (0, 0), color=(0, 0, 0))
    assert gray.mode == "RGB"


def test_add_text_errors(photo: Path, tmp_path: Path) -> None:
    """Seção 8.17: fonte inexistente gera ImageProcessingError; font_size < 1, ValueError."""
    with pytest.raises(ImageProcessingError, match="escrever texto"):
        img.add_text(photo, "x", (0, 0), font_path=tmp_path / "nao_existe.ttf")
    with pytest.raises(ValueError, match="font_size"):
        img.add_text(photo, "x", (0, 0), font_size=0)


def test_decompression_bomb_becomes_image_processing_error(
    photo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seção 8.17: erros do Pillow (inclusive DecompressionBombError) viram ImageProcessingError."""
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 10)
    with pytest.raises(ImageProcessingError):
        img.open_image(photo)
