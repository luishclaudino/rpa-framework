"""Testes de `rpa_3003.bots.desktop_bot` (RF-034 a RF-036, seção 8.22).

O OpenCV é usado de verdade sobre arrays sintéticos. `mss`, `pyautogui`, `pyperclip` e
`pywinauto` são substituídos por módulos falsos em `sys.modules` (seção 12.6), então os testes
rodam fora do Windows e sem sessão de desktop. O `psutil` é simulado com `mocker.patch`.
"""

from __future__ import annotations

import abc
import ctypes
import os
import sys
import time
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import cv2
import numpy as np
import psutil
import pytest
from PIL import Image
from pytest_mock import MockerFixture

from rpa_3003.bots import desktop_bot
from rpa_3003.bots.desktop_bot import (
    SEND_KEYS_SPECIAL,
    DesktopBot,
    Match,
    enable_dpi_awareness,
    escape_send_keys,
)
from rpa_3003.core.exceptions import (
    DesktopAutomationError,
    ImageNotFoundError,
    WindowNotFoundError,
)
from rpa_3003.core.logger import MASK

pytestmark = pytest.mark.unit

TITLE_RE = ".*(Bloco de Notas|Notepad).*"


class FakeScreenShotError(Exception):
    """Equivalente a `mss.ScreenShotError`."""


class FakeWaitTimeoutError(RuntimeError):
    """Equivalente a `pywinauto.timings.TimeoutError`."""


class FakeAmbiguousError(Exception):
    """Equivalente a `pywinauto.findwindows.ElementAmbiguousError`."""


class FakeAppStartError(Exception):
    """Equivalente a `pywinauto.application.AppStartError`."""


class FakeClipboardError(RuntimeError):
    """Equivalente a `pyperclip.PyperclipException`."""


class FakeScreen:
    """Tela virtual simulada para o mss: imagem BGR com canto superior esquerdo em `origin`."""

    def __init__(self, image: np.ndarray, origin: tuple[int, int] = (0, 0)) -> None:
        self.image = image
        self.origin = origin
        self.grabs: list[dict[str, int]] = []
        self.fail = False

    @property
    def monitors(self) -> list[dict[str, int]]:
        height, width = self.image.shape[:2]
        left, top = self.origin
        return [{"left": left, "top": top, "width": width, "height": height}]

    def grab(self, monitor: dict[str, int]) -> np.ndarray:
        if self.fail:
            raise FakeScreenShotError("sessão bloqueada")
        self.grabs.append(dict(monitor))
        x, y = monitor["left"] - self.origin[0], monitor["top"] - self.origin[1]
        crop = self.image[y : y + monitor["height"], x : x + monitor["width"]]
        return cv2.cvtColor(crop, cv2.COLOR_BGR2BGRA)

    def __enter__(self) -> FakeScreen:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


@dataclass
class FakeClipboard:
    """Área de transferência simulada (pyperclip)."""

    content: str = "conteúdo anterior"
    history: list[str] = field(default_factory=list)
    fail: bool = False

    def copy(self, text: str) -> None:
        if self.fail:
            raise FakeClipboardError("OpenClipboard falhou")
        self.history.append(text)
        self.content = text

    def paste(self) -> str:
        if self.fail:
            raise FakeClipboardError("OpenClipboard falhou")
        return self.content


@dataclass
class GuiFakes:
    """Módulos falsos instalados em `sys.modules`."""

    screen: FakeScreen
    pyautogui: MagicMock
    clipboard: FakeClipboard
    desktop: MagicMock
    application: MagicMock
    send_keys: MagicMock
    dpi: MagicMock

    @property
    def window(self) -> MagicMock:
        """`WindowSpecification` retornado por `Desktop(...).window(...)`."""
        window: MagicMock = self.desktop.return_value.window.return_value
        return window


def _module(name: str, **attributes: Any) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


def _random_image(seed: int, height: int, width: int) -> np.ndarray:
    return np.random.default_rng(seed).integers(0, 256, size=(height, width, 3), dtype=np.uint8)


def _save_image(path: Path, image: np.ndarray) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded_ok, encoded = cv2.imencode(path.suffix or ".png", image)
    assert encoded_ok
    encoded.tofile(path)
    return path


@pytest.fixture
def gui(monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture) -> GuiFakes:
    """Instala mss, pyautogui, pyperclip e pywinauto falsos; tela de ruído 600x400."""
    screen = FakeScreen(_random_image(2026, 400, 600))
    monkeypatch.setitem(
        sys.modules,
        "mss",
        _module("mss", mss=lambda **_kwargs: screen, ScreenShotError=FakeScreenShotError),
    )
    pyautogui = MagicMock(name="pyautogui")
    pyautogui.position.return_value = (15, 25)
    pyautogui.size.return_value = (1920, 1080)
    monkeypatch.setitem(sys.modules, "pyautogui", pyautogui)
    clipboard = FakeClipboard()
    monkeypatch.setitem(
        sys.modules,
        "pyperclip",
        _module(
            "pyperclip",
            copy=clipboard.copy,
            paste=clipboard.paste,
            PyperclipException=FakeClipboardError,
        ),
    )
    desktop = MagicMock(name="Desktop")
    application = MagicMock(name="Application")
    send_keys = MagicMock(name="send_keys")
    fake_modules = {
        "pywinauto": _module("pywinauto", Desktop=desktop),
        "pywinauto.application": _module(
            "pywinauto.application", Application=application, AppStartError=FakeAppStartError
        ),
        "pywinauto.findwindows": _module(
            "pywinauto.findwindows", ElementAmbiguousError=FakeAmbiguousError
        ),
        "pywinauto.timings": _module("pywinauto.timings", TimeoutError=FakeWaitTimeoutError),
        "pywinauto.keyboard": _module("pywinauto.keyboard", send_keys=send_keys),
    }
    for name, module in fake_modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    dpi = mocker.patch.object(desktop_bot, "enable_dpi_awareness")
    return GuiFakes(screen, pyautogui, clipboard, desktop, application, send_keys, dpi)


@pytest.fixture
def images_dir(tmp_path: Path) -> Path:
    return tmp_path / "data" / "images"


@pytest.fixture
def screenshots_dir(tmp_path: Path) -> Path:
    return tmp_path / "logs" / "screenshots"


@pytest.fixture
def bot(gui: GuiFakes, images_dir: Path, screenshots_dir: Path) -> DesktopBot:
    return DesktopBot(images_dir=images_dir, screenshots_dir=screenshots_dir, timeout=0.2)


@pytest.fixture
def button(gui: GuiFakes, images_dir: Path) -> Path:
    """Imagem de referência recortada da tela simulada na posição (120, 80), 50x40."""
    return _save_image(images_dir / "botao.png", gui.screen.image[80:120, 120:170])


@pytest.fixture
def log_messages() -> list[tuple[str, str]]:
    """Mensagens registradas no logger (nível, mensagem já mascarada), a partir de DEBUG."""
    from loguru import logger

    from rpa_3003.core.logger import get_logger

    get_logger()
    records: list[tuple[str, str]] = []
    logger.add(
        lambda message: records.append((message.record["level"].name, message.record["message"])),
        level="DEBUG",
    )
    return records


# Construtor e DPI


def test_init_configures_pyautogui_and_defaults(
    gui: GuiFakes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seção 8.22: FAILSAFE e PAUSE do pyautogui; pastas padrão a partir do cwd."""
    monkeypatch.chdir(tmp_path)
    instance = DesktopBot(failsafe=False, pause=0.3)
    assert gui.pyautogui.FAILSAFE is False
    assert gui.pyautogui.PAUSE == 0.3
    gui.dpi.assert_called_once_with()
    assert instance._images_dir == tmp_path / "data" / "images"
    assert instance._screenshots_dir == tmp_path / "logs" / "screenshots"
    DesktopBot()
    assert gui.pyautogui.FAILSAFE is True
    assert gui.pyautogui.PAUSE == 0.1


def test_init_enables_dpi_before_importing_pyautogui(
    gui: GuiFakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RF-034: o DPI awareness é ativado antes de importar o pyautogui."""
    monkeypatch.setitem(sys.modules, "pyautogui", None)  # import antes do DPI falharia
    gui.dpi.side_effect = lambda: sys.modules.__setitem__("pyautogui", gui.pyautogui)
    DesktopBot(failsafe=False)
    assert gui.pyautogui.FAILSAFE is False


@pytest.mark.parametrize(
    "kwargs",
    [{"confidence": 0}, {"confidence": 1.01}, {"timeout": -1}, {"pause": -0.1}, {"backend": "x"}],
)
def test_init_validates_arguments(gui: GuiFakes, kwargs: dict[str, Any]) -> None:
    """Seção 8.22: confiança em (0, 1], prazos não negativos e backend uia/win32."""
    with pytest.raises(ValueError):
        DesktopBot(**kwargs)


@pytest.fixture
def fake_windll(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """`ctypes.windll` simulado em plataforma Windows, com o estado de DPI reiniciado."""
    windll = MagicMock(name="windll")
    monkeypatch.setattr(ctypes, "windll", windll, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(desktop_bot._DpiAwareness, "enabled", False)
    return windll


def test_enable_dpi_awareness_once_per_process(fake_windll: MagicMock) -> None:
    """RF-034: SetProcessDpiAwareness(2) via shcore, uma única vez por processo."""
    enable_dpi_awareness()
    enable_dpi_awareness()
    fake_windll.shcore.SetProcessDpiAwareness.assert_called_once_with(2)
    fake_windll.user32.SetProcessDPIAware.assert_not_called()


def test_enable_dpi_awareness_fallbacks(
    fake_windll: MagicMock, monkeypatch: pytest.MonkeyPatch, log_messages: list[tuple[str, str]]
) -> None:
    """RF-034: sem shcore usa user32.SetProcessDPIAware; sem os dois, registra WARNING."""
    fake_windll.shcore.SetProcessDpiAwareness.side_effect = AttributeError("sem shcore")
    enable_dpi_awareness()
    fake_windll.user32.SetProcessDPIAware.assert_called_once_with()
    monkeypatch.setattr(desktop_bot._DpiAwareness, "enabled", False)
    fake_windll.user32.SetProcessDPIAware.side_effect = OSError("sem user32")
    enable_dpi_awareness()
    assert any(level == "WARNING" and "DPI" in message for level, message in log_messages)


def test_enable_dpi_awareness_outside_windows(
    fake_windll: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P-22: fora do Windows não faz nada."""
    monkeypatch.setattr(sys, "platform", "linux")
    enable_dpi_awareness()
    fake_windll.shcore.SetProcessDpiAwareness.assert_not_called()


# Match e escape


def test_match_center_and_region() -> None:
    """Seção 8.22: centro e região do Match."""
    match = Match(left=100, top=50, width=21, height=10, confidence=0.95)
    assert match.center == (110, 55)
    assert match.region == (100, 50, 21, 10)


def test_escape_send_keys() -> None:
    """RF-035: cada caractere de `{}+^%~()` fica entre chaves."""
    assert escape_send_keys("Ação {x}+") == "Ação {{}x{}}{+}"
    assert escape_send_keys(SEND_KEYS_SPECIAL) == "{{}{}}{+}{^}{%}{~}{(}{)}"
    assert escape_send_keys("texto comum, sem especiais: 123") == "texto comum, sem especiais: 123"


# Captura e busca de imagens (RF-034)


def test_capture_screen_virtual_screen_and_region(bot: DesktopBot, gui: GuiFakes) -> None:
    """RF-034: `monitors[0]` (tela virtual) ou região; BGRA convertido para BGR."""
    frame = bot.capture_screen()
    assert frame.shape == (400, 600, 3)
    assert frame.dtype == np.uint8
    np.testing.assert_array_equal(frame, gui.screen.image)
    assert gui.screen.grabs[-1] == {"left": 0, "top": 0, "width": 600, "height": 400}
    region = bot.capture_screen((100, 50, 30, 20))
    np.testing.assert_array_equal(region, gui.screen.image[50:70, 100:130])
    assert gui.screen.grabs[-1] == {"left": 100, "top": 50, "width": 30, "height": 20}


def test_open_mss_prefers_mss_class(gui: GuiFakes, monkeypatch: pytest.MonkeyPatch) -> None:
    """Seção 8.22: usa `mss.MSS` (mss >= 10.2) e recorre a `mss.mss()` nas versões anteriores."""
    module = sys.modules["mss"]
    assert desktop_bot._open_mss() is gui.screen  # sem `MSS`: fábrica `mss.mss()`
    monkeypatch.setattr(module, "MSS", lambda: "instância MSS", raising=False)
    assert desktop_bot._open_mss() == "instância MSS"

    class AbstractMSS(abc.ABC):
        @abc.abstractmethod
        def grab(self, monitor: dict[str, int]) -> np.ndarray: ...

    monkeypatch.setattr(module, "MSS", AbstractMSS)
    assert desktop_bot._open_mss() is gui.screen


def test_capture_screen_errors(bot: DesktopBot, gui: GuiFakes) -> None:
    """Seção 8.22: região inválida → ValueError; falha do mss → DesktopAutomationError."""
    with pytest.raises(ValueError, match="Região inválida"):
        bot.capture_screen((0, 0, 0, 10))
    gui.screen.fail = True
    with pytest.raises(DesktopAutomationError, match="Falha ao capturar a tela") as info:
        bot.capture_screen()
    assert "sessão bloqueada" in info.value.details["erro"]


def test_find_image_on_synthetic_capture(
    bot: DesktopBot, button: Path, log_messages: list[tuple[str, str]]
) -> None:
    """RF-034: template recortado em (120, 80) → `Match(left=120, top=80, ...)`."""
    match = bot.find_image("botao")
    assert match is not None
    assert (match.left, match.top, match.width, match.height) == (120, 80, 50, 40)
    assert match.confidence == pytest.approx(1.0, abs=1e-4)
    assert (
        "DEBUG",
        "Imagem encontrada | imagem=botao.png | confianca=1.00 | posicao=(145, 100)",
    ) in log_messages


def test_find_image_in_color(bot: DesktopBot, button: Path) -> None:
    """RF-034: com `grayscale=False` a comparação usa os três canais."""
    match = bot.find_image(button, grayscale=False)
    assert match is not None
    assert (match.left, match.top) == (120, 80)


def test_find_image_region_adds_offset(bot: DesktopBot, gui: GuiFakes, images_dir: Path) -> None:
    """RF-034: com região (100, 100, 300, 200), as coordenadas incluem o deslocamento."""
    _save_image(images_dir / "campo.png", gui.screen.image[130:150, 150:190])
    match = bot.find_image("campo", region=(100, 100, 300, 200))
    assert match is not None
    assert (match.left, match.top) == (150, 130)
    assert gui.screen.grabs[-1] == {"left": 100, "top": 100, "width": 300, "height": 200}


def test_find_image_uses_virtual_screen_origin(
    bot: DesktopBot, gui: GuiFakes, button: Path
) -> None:
    """RF-034: monitor à esquerda/acima do principal gera coordenadas negativas absolutas."""
    gui.screen.origin = (-1920, -200)
    match = bot.find_image("botao")
    assert match is not None
    assert (match.left, match.top) == (-1920 + 120, -200 + 80)


def test_find_image_absent_returns_none(bot: DesktopBot, images_dir: Path) -> None:
    """RF-034: template ausente da tela → `find_image` retorna None."""
    _save_image(images_dir / "ausente.png", _random_image(7, 30, 30))
    assert bot.find_image("ausente") is None
    assert bot.image_exists("ausente") is False


def test_find_image_confidence(bot: DesktopBot, gui: GuiFakes, images_dir: Path) -> None:
    """RF-034: a confiança mínima decide entre encontrar ou não uma imagem parecida."""
    template = gui.screen.image[200:260, 300:380].astype(np.int16)
    noise = np.random.default_rng(3).integers(-100, 101, size=template.shape)
    _save_image(images_dir / "parecida.png", np.clip(template + noise, 0, 255).astype(np.uint8))
    assert bot.find_image("parecida") is None  # padrão 0,9
    match = bot.find_image("parecida", confidence=0.6)
    assert match is not None
    assert (match.left, match.top) == (300, 200)
    assert 0.6 <= match.confidence < 0.9
    with pytest.raises(ValueError, match="confidence"):
        bot.find_image("parecida", confidence=0)


def test_find_image_template_larger_than_capture(bot: DesktopBot, button: Path) -> None:
    """Seção 8.22: região menor que a imagem de referência não gera erro do OpenCV."""
    assert bot.find_image("botao", region=(0, 0, 20, 20)) is None
    assert bot.find_all_images("botao", region=(0, 0, 20, 20)) == []


def test_image_resolution_rules(bot: DesktopBot, gui: GuiFakes, tmp_path: Path) -> None:
    """Seção 8.22: caminho absoluto existente; senão `images_dir / nome` (+ `.png`)."""
    absolute = _save_image(tmp_path / "outra pasta" / "icone.png", gui.screen.image[0:20, 0:20])
    assert bot.find_image(absolute) is not None
    assert bot.find_image(str(absolute)) is not None
    with pytest.raises(DesktopAutomationError, match="não encontrada") as info:
        bot.find_image("inexistente")
    assert info.value.details["imagem"].endswith("inexistente.png")


def test_unicode_image_path(bot: DesktopBot, gui: GuiFakes, images_dir: Path) -> None:
    """RF-034: imagem em `data/images/ação.png` é carregada sem erro."""
    _save_image(images_dir / "ação.png", gui.screen.image[300:340, 400:460])
    match = bot.find_image("ação.png")
    assert match is not None
    assert (match.left, match.top) == (400, 300)


def test_invalid_image_file(bot: DesktopBot, images_dir: Path) -> None:
    """Seção 8.22: arquivo que não é imagem gera DesktopAutomationError."""
    images_dir.mkdir(parents=True)
    (images_dir / "texto.png").write_text("não sou imagem", encoding="utf-8")
    (images_dir / "vazio.png").write_bytes(b"")
    for name in ("texto", "vazio"):
        with pytest.raises(DesktopAutomationError, match="imagem válida"):
            bot.find_image(name)


def test_template_cache_by_path_and_grayscale(
    bot: DesktopBot, button: Path, mocker: MockerFixture
) -> None:
    """Seção 8.22: imagens de referência em cache por `(caminho, grayscale)`."""
    spy = mocker.spy(desktop_bot.np, "fromfile")
    bot.find_image("botao")
    bot.find_image("botao")
    assert spy.call_count == 1
    bot.find_image("botao", grayscale=False)
    assert spy.call_count == 2


def test_template_read_error(bot: DesktopBot, button: Path, mocker: MockerFixture) -> None:
    """Seção 8.22: falha de leitura do arquivo vira DesktopAutomationError."""
    mocker.patch.object(desktop_bot.np, "fromfile", side_effect=PermissionError("bloqueado"))
    with pytest.raises(DesktopAutomationError, match="Falha ao ler imagem"):
        bot.find_image("botao")


def test_find_all_images_sorted_without_overlap(
    bot: DesktopBot, gui: GuiFakes, images_dir: Path
) -> None:
    """RF-034: todas as ocorrências, sem sobreposição, ordenadas por top e depois left."""
    icon = _random_image(11, 20, 30)
    for left, top in ((400, 50), (100, 300), (20, 50)):
        gui.screen.image[top : top + 20, left : left + 30] = icon
    _save_image(images_dir / "icone.png", icon)
    matches = bot.find_all_images("icone")
    assert [(m.left, m.top) for m in matches] == [(20, 50), (400, 50), (100, 300)]
    assert all(m.confidence == pytest.approx(1.0, abs=1e-4) for m in matches)
    assert bot.find_all_images("icone", region=(0, 0, 300, 200)) == [matches[0]]


def test_non_maximum_suppression_rule(tmp_path: Path) -> None:
    """RF-034: descarta candidato a menos de metade da largura e da altura de um aceito."""
    scores = np.zeros((40, 60), dtype=np.float32)
    scores[10, 10] = 0.99  # aceito
    scores[12, 14] = 0.98  # dx=4 < 5 e dy=2 < 5: descartado
    scores[10, 15] = 0.97  # dx=5, não é menor que 5: aceito
    scores[30, 12] = 0.96  # dy=20: aceito
    scores[5, 50] = 0.5  # abaixo da confiança
    search = desktop_bot._Search(tmp_path / "x.png", scores, 10, 10, (100, 200))
    matches = desktop_bot._non_maximum_suppression(search, scores, 0.9)
    assert [(m.left, m.top, round(m.confidence, 2)) for m in matches] == [
        (110, 210, 0.99),
        (115, 210, 0.97),
        (112, 230, 0.96),
    ]


def test_wait_image_retries_until_found(
    bot: DesktopBot, mocker: MockerFixture, no_sleep: list[float]
) -> None:
    """RF-034: repete `find_image` a cada `poll_interval` até encontrar."""
    expected = Match(1, 2, 3, 4, 0.95)
    find = mocker.patch.object(bot, "find_image", side_effect=[None, None, expected])
    assert bot.wait_image("botao", timeout=5, poll_interval=0.25) is expected
    assert find.call_count == 3
    assert no_sleep == [0.25, 0.25]


def test_wait_image_timeout_raises_and_saves_screenshot(
    bot: DesktopBot, images_dir: Path, screenshots_dir: Path
) -> None:
    """RF-034: template ausente e `wait_image(timeout=1)` → ImageNotFoundError em até 1,5 s."""
    _save_image(images_dir / "ausente.png", _random_image(7, 30, 30))
    started = time.monotonic()
    with pytest.raises(ImageNotFoundError) as info:
        bot.wait_image("ausente", timeout=1)
    assert time.monotonic() - started <= 1.5
    assert set(info.value.details) == {"imagem", "confianca", "timeout"}
    assert info.value.details["imagem"] == str((images_dir / "ausente.png").resolve())
    assert info.value.details["confianca"] == 0.9
    assert info.value.details["timeout"] == 1
    assert len(list(screenshots_dir.glob("*_imagem_nao_encontrada.png"))) == 1


def test_wait_image_screenshot_failure_is_warning(
    bot: DesktopBot,
    images_dir: Path,
    mocker: MockerFixture,
    log_messages: list[tuple[str, str]],
) -> None:
    """Seção 8.22: falha ao salvar a captura de erro não esconde o ImageNotFoundError."""
    _save_image(images_dir / "ausente.png", _random_image(7, 30, 30))
    mocker.patch.object(bot, "screenshot", side_effect=DesktopAutomationError("disco cheio"))
    with pytest.raises(ImageNotFoundError):
        bot.wait_image("ausente", timeout=0)
    assert any(level == "WARNING" and "disco cheio" in m for level, m in log_messages)


def test_wait_validates_arguments(bot: DesktopBot, button: Path) -> None:
    """Seção 8.22: `poll_interval` positivo e `timeout` não negativo."""
    with pytest.raises(ValueError, match="poll_interval"):
        bot.wait_image("botao", poll_interval=0)
    with pytest.raises(ValueError, match="timeout"):
        bot.wait_image_vanish("botao", timeout=-1)


def test_wait_image_vanish(bot: DesktopBot, mocker: MockerFixture, no_sleep: list[float]) -> None:
    """Seção 8.22: True se a imagem sumir no prazo, senão False."""
    present = Match(0, 0, 1, 1, 1.0)
    mocker.patch.object(bot, "find_image", side_effect=[present, present, None])
    assert bot.wait_image_vanish("icone_processando", timeout=5, poll_interval=0.5) is True
    assert no_sleep == [0.5, 0.5]
    mocker.patch.object(bot, "find_image", return_value=present)
    assert bot.wait_image_vanish("icone_processando", timeout=0) is False


def test_screenshot_png_and_log(
    bot: DesktopBot, screenshots_dir: Path, log_messages: list[tuple[str, str]]
) -> None:
    """Seção 8.22: captura PNG com `<AAAAMMDD_HHMMSS_fff>_<label>.png` e log INFO."""
    path = bot.screenshot(label="tela inicial")
    assert path.parent == screenshots_dir.resolve()
    assert path.name.endswith("_tela_inicial.png")
    with Image.open(path) as image:
        assert image.format == "PNG"
        assert image.size == (600, 400)
    assert ("INFO", f"Screenshot salvo: {path}") in log_messages


def test_screenshot_explicit_path_region_and_format(bot: DesktopBot, tmp_path: Path) -> None:
    """Seção 8.22: caminho explícito com acentos, região e formato pela extensão."""
    path = bot.screenshot(tmp_path / "evidências" / "ação.bmp", region=(10, 20, 40, 30))
    assert path == (tmp_path / "evidências" / "ação.bmp").resolve()
    with Image.open(path) as image:
        assert image.format == "BMP"
        assert image.size == (40, 30)


def test_screenshot_encoding_failures(
    bot: DesktopBot, tmp_path: Path, mocker: MockerFixture
) -> None:
    """Seção 8.22: extensão sem suporte ou falha na codificação → DesktopAutomationError."""
    with pytest.raises(DesktopAutomationError, match="Falha ao salvar screenshot"):
        bot.screenshot(tmp_path / "captura.formato_inexistente")
    cv2_imencode = mocker.patch("cv2.imencode", return_value=(False, None))
    with pytest.raises(DesktopAutomationError, match="codificar"):
        bot.screenshot(tmp_path / "captura.png")
    cv2_imencode.assert_called_once()


# Mouse (RF-035)


def test_mouse_actions(bot: DesktopBot, gui: GuiFakes) -> None:
    """RF-035: click, double_click, right_click, move_to, drag_to e scroll via pyautogui."""
    bot.click(10, 20, clicks=2, interval=0.3)
    gui.pyautogui.click.assert_called_with(x=10, y=20, clicks=2, interval=0.3, button="left")
    bot.right_click(30, 40)
    gui.pyautogui.click.assert_called_with(x=30, y=40, clicks=1, interval=0.1, button="right")
    bot.double_click(5, 6)
    gui.pyautogui.doubleClick.assert_called_once_with(x=5, y=6)
    bot.move_to(7, 8, duration=0.5)
    gui.pyautogui.moveTo.assert_called_once_with(7, 8, duration=0.5)
    bot.drag_to(9, 10, button="middle")
    gui.pyautogui.dragTo.assert_called_once_with(9, 10, duration=0.5, button="middle")
    bot.scroll(-3, x=100, y=200)
    gui.pyautogui.scroll.assert_called_once_with(-3, x=100, y=200)
    assert bot.mouse_position() == (15, 25)
    assert bot.screen_size() == (1920, 1080)


def test_click_image_clicks_center_plus_offset(
    bot: DesktopBot, gui: GuiFakes, mocker: MockerFixture
) -> None:
    """RF-035: `click_image` chama `pyautogui.click` no centro do Match somado ao offset."""
    found = Match(left=100, top=50, width=20, height=10, confidence=0.95)
    wait = mocker.patch.object(bot, "wait_image", return_value=found)
    result = bot.click_image(
        "botao_salvar", offset=(5, -3), clicks=2, button="right", timeout=4, confidence=0.8
    )
    assert result is found
    wait.assert_called_once_with("botao_salvar", timeout=4, region=None, confidence=0.8)
    gui.pyautogui.click.assert_called_once_with(x=115, y=52, clicks=2, interval=0.1, button="right")


def test_click_image_on_synthetic_screen(bot: DesktopBot, gui: GuiFakes, button: Path) -> None:
    """RF-035: busca real na tela simulada e clique no centro da imagem (145, 100)."""
    bot.click_image("botao")
    gui.pyautogui.click.assert_called_once_with(x=145, y=100, clicks=1, interval=0.1, button="left")


# Teclado e área de transferência (RF-035)


def test_type_text_keys_escapes_special_characters(
    bot: DesktopBot, gui: GuiFakes, log_messages: list[tuple[str, str]]
) -> None:
    """RF-035: `type_text("Ação {x}+")` chama `send_keys` com `"Ação {{}x{}}{+}"`."""
    bot.type_text("Ação {x}+")
    gui.send_keys.assert_called_once_with(
        "Ação {{}x{}}{+}", with_spaces=True, with_tabs=True, with_newlines=True, pause=0.02
    )
    assert ("DEBUG", "Digitando texto | metodo=keys | texto=Ação {x}+") in log_messages


def test_type_text_sensitive_is_masked(
    bot: DesktopBot, gui: GuiFakes, log_messages: list[tuple[str, str]]
) -> None:
    """RF-018/RF-035: `type_text(..., sensitive=True)` registra `********`."""
    from rpa_3003.core.logger import get_logger

    bot.type_text("S3nh@Forte", sensitive=True, interval=0.05)
    gui.send_keys.assert_called_once_with(
        "S3nh@Forte", with_spaces=True, with_tabs=True, with_newlines=True, pause=0.05
    )
    assert ("DEBUG", f"Digitando texto | metodo=keys | texto={MASK}") in log_messages
    get_logger().info("valor: S3nh@Forte")
    assert all("S3nh@Forte" not in message for _level, message in log_messages)


def test_type_text_clipboard_restores_previous_content(
    bot: DesktopBot, gui: GuiFakes, no_sleep: list[float]
) -> None:
    """RF-035: `type_text("abc", method="clipboard")` restaura o conteúdo anterior."""
    pasted: list[str] = []
    gui.pyautogui.hotkey.side_effect = lambda *keys: pasted.append(gui.clipboard.content)
    bot.type_text("abc", method="clipboard")
    gui.pyautogui.hotkey.assert_called_once_with("ctrl", "v")
    assert pasted == ["abc"]
    assert gui.clipboard.content == "conteúdo anterior"
    assert no_sleep == [0.1]
    gui.send_keys.assert_not_called()


def test_type_text_clipboard_restores_after_failure(
    bot: DesktopBot, gui: GuiFakes, no_sleep: list[float]
) -> None:
    """RF-035: a área de transferência é restaurada mesmo se a colagem falhar."""
    gui.pyautogui.hotkey.side_effect = RuntimeError("FailSafe")
    with pytest.raises(RuntimeError):
        bot.type_text("abc", method="clipboard")
    assert gui.clipboard.content == "conteúdo anterior"


def test_type_text_invalid_method(bot: DesktopBot) -> None:
    """Seção 8.22: método diferente de keys/clipboard → ValueError."""
    with pytest.raises(ValueError, match="method"):
        bot.type_text("x", method="voz")  # type: ignore[arg-type]


def test_press_hotkey_and_clipboard(bot: DesktopBot, gui: GuiFakes) -> None:
    """RF-035: press, hotkey, get_clipboard e set_clipboard."""
    bot.press("tab", presses=3, interval=0.2)
    gui.pyautogui.press.assert_called_once_with("tab", presses=3, interval=0.2)
    bot.hotkey("alt", "e")
    gui.pyautogui.hotkey.assert_called_once_with("alt", "e")
    bot.set_clipboard("Observação")
    assert bot.get_clipboard() == "Observação"


def test_clipboard_errors(bot: DesktopBot, gui: GuiFakes) -> None:
    """Seção 8.22: área de transferência inacessível vira DesktopAutomationError."""
    gui.clipboard.fail = True
    with pytest.raises(DesktopAutomationError, match="ler a área"):
        bot.get_clipboard()
    with pytest.raises(DesktopAutomationError, match="gravar na área"):
        bot.set_clipboard("x")


# Janelas e processos (RF-036)


def test_start_application(bot: DesktopBot, gui: GuiFakes, tmp_path: Path) -> None:
    """RF-036: `Application(backend).start(list2cmdline([...]), timeout=...)` e espera da janela."""
    executable = tmp_path / "ERP Financeiro" / "erp.exe"
    app = bot.start_application(
        executable, ["--empresa", "Filial SP"], wait_title_re=".*ERP.*", timeout=60
    )
    gui.application.assert_called_once_with(backend="uia")
    started = gui.application.return_value.start
    assert app is started.return_value
    started.assert_called_once_with(f'"{executable}" --empresa "Filial SP"', timeout=60)
    gui.desktop.return_value.window.assert_called_once_with(title_re=".*ERP.*")
    gui.window.wait.assert_called_once_with("exists visible", timeout=60)


def test_start_application_failure(bot: DesktopBot, gui: GuiFakes) -> None:
    """RF-036: falha ao criar o processo vira DesktopAutomationError."""
    gui.application.return_value.start.side_effect = FakeAppStartError("arquivo não encontrado")
    with pytest.raises(DesktopAutomationError, match="Falha ao iniciar aplicação") as info:
        bot.start_application("nao_existe.exe")
    assert info.value.details["comando"] == "nao_existe.exe"
    gui.desktop.assert_not_called()


def test_connect_and_wait_window(bot: DesktopBot, gui: GuiFakes) -> None:
    """RF-036: `Desktop(backend).window(title_re)` + `wait("exists visible")`."""
    assert bot.connect_window(TITLE_RE, timeout=3) is gui.window
    gui.desktop.assert_called_with(backend="uia")
    gui.window.wait.assert_called_with("exists visible", timeout=3)
    assert bot.wait_window(TITLE_RE, timeout=1) is True
    gui.window.wait.side_effect = FakeWaitTimeoutError("timeout")
    with pytest.raises(WindowNotFoundError, match="Janela não encontrada") as info:
        bot.connect_window(TITLE_RE, timeout=1)
    assert info.value.details == {"titulo": TITLE_RE, "timeout": 1}
    assert bot.wait_window(TITLE_RE, timeout=1) is False


def test_connect_window_ambiguous(bot: DesktopBot, gui: GuiFakes) -> None:
    """Seção 8.22: mais de uma janela com o título gera DesktopAutomationError."""
    gui.window.wait.side_effect = FakeAmbiguousError("2 elementos")
    with pytest.raises(DesktopAutomationError, match="Mais de uma janela") as info:
        bot.connect_window(TITLE_RE)
    assert not isinstance(info.value, WindowNotFoundError)


def test_win32_backend(gui: GuiFakes, tmp_path: Path) -> None:
    """Seção 8.22: o backend escolhido é repassado ao pywinauto."""
    instance = DesktopBot(backend="win32", images_dir=tmp_path)
    instance.connect_window(TITLE_RE)
    gui.desktop.assert_called_once_with(backend="win32")


def test_activate_window(bot: DesktopBot, gui: GuiFakes) -> None:
    """RF-036: restaura se minimizada e chama `set_focus()`."""
    gui.window.is_minimized.return_value = True
    bot.activate_window(TITLE_RE)
    gui.window.restore.assert_called_once_with()
    gui.window.set_focus.assert_called_once_with()
    gui.window.is_minimized.return_value = False
    bot.activate_window(TITLE_RE)
    gui.window.restore.assert_called_once_with()
    assert gui.window.set_focus.call_count == 2


def test_window_exists(bot: DesktopBot, gui: GuiFakes) -> None:
    """RF-036: `window_exists` retorna False quando o pywinauto não encontra a janela."""
    gui.window.exists.return_value = False
    assert bot.window_exists(TITLE_RE) is False
    gui.window.exists.assert_called_once_with(timeout=0)
    gui.window.exists.return_value = True
    assert bot.window_exists(TITLE_RE) is True
    gui.window.exists.side_effect = FakeAmbiguousError("2 elementos")
    assert bot.window_exists(TITLE_RE) is True


def test_close_window(bot: DesktopBot, gui: GuiFakes) -> None:
    """RF-036: `close()` e espera `wait_not("exists")`; janela aberta → DesktopAutomationError."""
    bot.close_window(TITLE_RE, timeout=4)
    gui.window.close.assert_called_once_with()
    gui.window.wait_not.assert_called_once_with("exists", timeout=4)
    gui.window.wait_not.side_effect = FakeWaitTimeoutError("ainda aberta")
    with pytest.raises(DesktopAutomationError, match="continua aberta"):
        bot.close_window(TITLE_RE)


def _process(name: str | None, pid: int = 1000) -> MagicMock:
    process = MagicMock(name=f"processo_{name}")
    process.pid = pid
    process.info = {"name": name}
    return process


def test_is_process_running(bot: DesktopBot, mocker: MockerFixture) -> None:
    """RF-036: compara o nome sem diferenciar maiúsculas; ignora o processo atual."""
    processes = [
        _process("Notepad.exe", 10),
        _process(None, 11),
        _process("python.exe", os.getpid()),
    ]
    mocker.patch("psutil.process_iter", return_value=processes)
    assert bot.is_process_running("notepad.EXE") is True
    assert bot.is_process_running("calc.exe") is False
    assert bot.is_process_running("python.exe") is False


def test_kill_process(
    bot: DesktopBot, mocker: MockerFixture, log_messages: list[tuple[str, str]]
) -> None:
    """RF-036: terminate, espera de 5 s, kill nos remanescentes; erros de acesso viram WARNING."""
    graceful = _process("ERP.exe", 1)
    stubborn = _process("erp.exe", 2)
    protected = _process("Erp.EXE", 3)
    protected.terminate.side_effect = psutil.AccessDenied(pid=3)
    vanished = _process("erp.exe", 4)
    vanished.terminate.side_effect = psutil.NoSuchProcess(pid=4)
    exited = _process("erp.exe", 5)
    exited.kill.side_effect = psutil.NoSuchProcess(pid=5)
    locked = _process("erp.exe", 6)
    locked.kill.side_effect = psutil.AccessDenied(pid=6)
    myself = _process("erp.exe", os.getpid())
    processes = [graceful, stubborn, protected, vanished, exited, locked, myself, _process("x.exe")]
    mocker.patch("psutil.process_iter", return_value=processes)
    wait = mocker.patch("psutil.wait_procs", return_value=([graceful], [stubborn, exited, locked]))
    assert bot.kill_process("ERP.EXE") == 3
    assert wait.call_args.args[0] == [graceful, stubborn, exited, locked]
    assert wait.call_args.kwargs == {"timeout": 5.0}
    stubborn.kill.assert_called_once_with()
    graceful.kill.assert_not_called()
    myself.terminate.assert_not_called()
    warnings = [message for level, message in log_messages if level == "WARNING"]
    assert any("pid=3" in m and "AccessDenied" in m for m in warnings)
    assert any("pid=4" in m and "NoSuchProcess" in m for m in warnings)
    assert any("pid=6" in m and "AccessDenied" in m for m in warnings)
    assert ("INFO", "Processos encerrados | nome=ERP.EXE | quantidade=3") in log_messages


def test_kill_missing_process_returns_zero(bot: DesktopBot) -> None:
    """RF-036: `kill_process("inexistente.exe")` retorna 0 (psutil real)."""
    assert bot.kill_process("inexistente_rpa3003.exe") == 0
    assert bot.is_process_running("inexistente_rpa3003.exe") is False
