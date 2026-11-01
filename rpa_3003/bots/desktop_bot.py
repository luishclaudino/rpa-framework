"""Automação GUI: visão computacional, teclado e mouse.

Automação de interfaces gráficas Windows por visão computacional (OpenCV + mss), mouse e
teclado (pyautogui + pywinauto), área de transferência (pyperclip), janelas (pywinauto) e
processos (psutil) (seção 8.22, RF-034 a RF-036).

No nível do módulo só o numpy é importado. As demais bibliotecas são carregadas em
`DesktopBot.__init__` ou nos métodos que as usam (RNF-004, RNF-017).

As coordenadas são absolutas na tela virtual (todos os monitores), em pixels físicos: o DPI
awareness por monitor é ativado antes de importar o pyautogui.
"""

from __future__ import annotations

import inspect
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, TypeAlias

import numpy as np
import numpy.typing as npt

from rpa_3003.core.config import PathLike
from rpa_3003.core.exceptions import (
    DesktopAutomationError,
    ImageNotFoundError,
    WindowNotFoundError,
)
from rpa_3003.core.logger import MASK, get_logger, register_secret
from rpa_3003.utils import filesystem_utils

if TYPE_CHECKING:
    from pywinauto.application import Application, WindowSpecification

Region: TypeAlias = tuple[int, int, int, int]  # noqa: UP040 - (left, top, width, height) absolutos
ImageRef: TypeAlias = str | os.PathLike[str]  # noqa: UP040 - convenção 7.7
MouseButton: TypeAlias = Literal["left", "right", "middle"]  # noqa: UP040 - convenção 7.7
TypeMethod: TypeAlias = Literal["keys", "clipboard"]  # noqa: UP040 - convenção 7.7
SEND_KEYS_SPECIAL: Final[str] = "{}+^%~()"

__all__: list[str] = [
    "SEND_KEYS_SPECIAL",
    "DesktopBot",
    "ImageRef",
    "Match",
    "MouseButton",
    "Region",
    "TypeMethod",
    "enable_dpi_awareness",
    "escape_send_keys",
]

_Image: TypeAlias = npt.NDArray[np.uint8]  # noqa: UP040 - convenção 7.7
_Scores: TypeAlias = npt.NDArray[np.float32]  # noqa: UP040 - convenção 7.7

_PER_MONITOR_DPI_AWARE: Final[int] = 2
_BACKENDS: Final[tuple[str, ...]] = ("uia", "win32")
_TYPE_METHODS: Final[tuple[str, ...]] = ("keys", "clipboard")
_PASTE_DELAY_SECONDS: Final[float] = 0.1
_KILL_GRACE_SECONDS: Final[float] = 5.0
_LABEL_INVALID_CHARS: Final[re.Pattern[str]] = re.compile(r"[^\w-]+")
_LABEL_MAX_LENGTH: Final[int] = 80
_DEFAULT_IMAGE_SUFFIX: Final[str] = ".png"


@dataclass(frozen=True, slots=True)
class Match:
    """Posição de uma imagem encontrada na tela, em coordenadas absolutas.

    Attributes:
        left: Coordenada X do canto superior esquerdo.
        top: Coordenada Y do canto superior esquerdo.
        width: Largura da imagem de referência.
        height: Altura da imagem de referência.
        confidence: Pontuação `TM_CCOEFF_NORMED` (0 a 1).
    """

    left: int
    top: int
    width: int
    height: int
    confidence: float

    @property
    def center(self) -> tuple[int, int]:
        """Centro da área encontrada `(x, y)`."""
        return self.left + self.width // 2, self.top + self.height // 2

    @property
    def region(self) -> Region:
        """Área encontrada `(left, top, width, height)`, utilizável como `region`."""
        return self.left, self.top, self.width, self.height


@dataclass(frozen=True, slots=True)
class _Search:
    """Resultado de `matchTemplate` sobre a captura, com o deslocamento da área capturada."""

    path: Path
    scores: _Scores | None
    width: int
    height: int
    origin: tuple[int, int]

    def match_at(self, x: int, y: int, confidence: float) -> Match:
        return Match(
            left=self.origin[0] + x,
            top=self.origin[1] + y,
            width=self.width,
            height=self.height,
            confidence=confidence,
        )


class _DpiAwareness:
    """Estado de `enable_dpi_awareness` (uma vez por processo)."""

    enabled: bool = False
    lock: Final[threading.Lock] = threading.Lock()


class DesktopBot:
    """Fachada para automação desktop por imagem, mouse, teclado, janelas e processos.

    Example:
        >>> bot = DesktopBot(confidence=0.92, timeout=15)  # doctest: +SKIP
        >>> bot.click_image("botao_salvar", offset=(0, 5))  # doctest: +SKIP
    """

    def __init__(
        self,
        *,
        images_dir: PathLike | None = None,
        screenshots_dir: PathLike | None = None,
        confidence: float = 0.9,
        timeout: float = 10.0,
        grayscale: bool = True,
        failsafe: bool = True,
        pause: float = 0.1,
        backend: Literal["uia", "win32"] = "uia",
    ) -> None:
        """Ativa o DPI awareness e configura o pyautogui.

        Args:
            images_dir: Pasta das imagens de referência; padrão `<cwd>/data/images` (P-12).
            screenshots_dir: Pasta das capturas; padrão `<cwd>/logs/screenshots`.
            confidence: Confiança mínima padrão da busca de imagens (0 a 1).
            timeout: Prazo padrão, em segundos, de `wait_image` e `wait_image_vanish`.
            grayscale: Compara as imagens em escala de cinza por padrão.
            failsafe: `pyautogui.FAILSAFE`: mover o mouse para um canto da tela aborta.
            pause: `pyautogui.PAUSE`: pausa, em segundos, após cada ação do pyautogui.
            backend: Backend do pywinauto (`"uia"` ou `"win32"`).

        Raises:
            ValueError: Se `confidence`, `timeout`, `pause` ou `backend` forem inválidos.
        """
        _validate_confidence(confidence)
        if timeout < 0 or pause < 0:
            raise ValueError("timeout e pause não podem ser negativos")
        if backend not in _BACKENDS:
            raise ValueError(f"backend inválido: {backend!r} (aceitos: uia, win32)")
        cwd = Path.cwd()
        self._images_dir: Path = _absolute(images_dir, cwd / "data" / "images")
        self._screenshots_dir: Path = _absolute(screenshots_dir, cwd / "logs" / "screenshots")
        self._confidence: float = confidence
        self._timeout: float = timeout
        self._grayscale: bool = grayscale
        self._backend: str = backend
        self._templates: dict[tuple[Path, bool], _Image] = {}
        enable_dpi_awareness()
        import pyautogui

        pyautogui.FAILSAFE = failsafe
        pyautogui.PAUSE = pause

    # Visão computacional

    def capture_screen(self, region: Region | None = None) -> npt.NDArray[np.uint8]:
        """Captura a tela virtual (todos os monitores) ou uma região dela.

        Args:
            region: Área `(left, top, width, height)` em coordenadas absolutas; `None` captura
                todos os monitores.

        Returns:
            Imagem BGR (`numpy.ndarray` com forma `(altura, largura, 3)`).

        Raises:
            ValueError: Se a região tiver largura ou altura não positivas.
            DesktopAutomationError: Se a captura falhar (ex.: sessão bloqueada).
        """
        import cv2
        import mss

        if region is not None:
            _validate_region(region)
        try:
            with _open_mss() as screen:
                monitor = screen.monitors[0] if region is None else _monitor_dict(region)
                shot = screen.grab(monitor)
        except mss.ScreenShotError as exc:
            raise DesktopAutomationError(
                "Falha ao capturar a tela", details={"regiao": region, "erro": _describe(exc)}
            ) from exc
        frame = np.asarray(shot, dtype=np.uint8)
        return np.asarray(cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR), dtype=np.uint8)

    def find_image(
        self,
        image: ImageRef,
        *,
        region: Region | None = None,
        confidence: float | None = None,
        grayscale: bool | None = None,
    ) -> Match | None:
        """Procura a imagem uma vez e retorna a posição de maior confiança.

        Args:
            image: Nome em `images_dir` (sem extensão vira `.png`) ou caminho absoluto.
            region: Área de busca `(left, top, width, height)`; `None` busca em todos os
                monitores.
            confidence: Confiança mínima; `None` usa a do construtor.
            grayscale: Compara em escala de cinza; `None` usa o padrão do construtor.

        Returns:
            O `Match` em coordenadas absolutas, ou `None` se a confiança não for atingida.

        Raises:
            DesktopAutomationError: Se a imagem de referência não existir ou for inválida.
        """
        import cv2

        threshold = self._threshold(confidence)
        search = self._search(image, region, grayscale)
        if search.scores is None:
            return None
        _min_value, max_value, _min_loc, max_loc = cv2.minMaxLoc(search.scores)
        if max_value < threshold:
            return None
        match = search.match_at(int(max_loc[0]), int(max_loc[1]), float(max_value))
        get_logger().debug(
            "Imagem encontrada | imagem={} | confianca={:.2f} | posicao=({}, {})",
            search.path.name,
            match.confidence,
            *match.center,
        )
        return match

    def find_all_images(
        self,
        image: ImageRef,
        *,
        region: Region | None = None,
        confidence: float | None = None,
        grayscale: bool | None = None,
    ) -> list[Match]:
        """Procura todas as ocorrências da imagem, sem sobreposição.

        Candidatos são avaliados por confiança decrescente; um candidato cujo centro fica a
        menos de metade da largura e da altura de um match já aceito é descartado.

        Args:
            image: Nome em `images_dir` (sem extensão vira `.png`) ou caminho absoluto.
            region: Área de busca `(left, top, width, height)`; `None` busca em todos os
                monitores.
            confidence: Confiança mínima; `None` usa a do construtor.
            grayscale: Compara em escala de cinza; `None` usa o padrão do construtor.

        Returns:
            As ocorrências ordenadas por `top` e depois por `left`.

        Raises:
            DesktopAutomationError: Se a imagem de referência não existir ou for inválida.
        """
        threshold = self._threshold(confidence)
        search = self._search(image, region, grayscale)
        if search.scores is None:
            return []
        matches = _non_maximum_suppression(search, search.scores, threshold)
        get_logger().debug(
            "Imagens encontradas | imagem={} | quantidade={}", search.path.name, len(matches)
        )
        return sorted(matches, key=lambda match: (match.top, match.left))

    def wait_image(
        self,
        image: ImageRef,
        *,
        timeout: float | None = None,
        region: Region | None = None,
        confidence: float | None = None,
        grayscale: bool | None = None,
        poll_interval: float = 0.5,
    ) -> Match:
        """Repete `find_image` até encontrar a imagem ou esgotar o prazo.

        Args:
            image: Nome em `images_dir` (sem extensão vira `.png`) ou caminho absoluto.
            timeout: Prazo em segundos; `None` usa o do construtor.
            region: Área de busca; `None` busca em todos os monitores.
            confidence: Confiança mínima; `None` usa a do construtor.
            grayscale: Compara em escala de cinza; `None` usa o padrão do construtor.
            poll_interval: Intervalo entre buscas, em segundos.

        Returns:
            O `Match` encontrado.

        Raises:
            ImageNotFoundError: Se o prazo se esgotar; antes, salva uma captura com label
                `imagem_nao_encontrada`.
            DesktopAutomationError: Se a imagem de referência não existir ou for inválida.
        """
        limit = self._wait_limit(timeout, poll_interval)
        deadline = time.monotonic() + limit
        while True:
            match = self.find_image(
                image, region=region, confidence=confidence, grayscale=grayscale
            )
            if match is not None:
                return match
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(poll_interval, remaining))
        self._failure_screenshot("imagem_nao_encontrada")
        path = self._resolve_image(image)
        raise ImageNotFoundError(
            f"Imagem não encontrada na tela: {path.name}",
            details={
                "imagem": str(path),
                "confianca": self._threshold(confidence),
                "timeout": limit,
            },
        )

    def wait_image_vanish(
        self,
        image: ImageRef,
        *,
        timeout: float | None = None,
        region: Region | None = None,
        confidence: float | None = None,
        poll_interval: float = 0.5,
    ) -> bool:
        """Aguarda a imagem deixar de ser encontrada.

        Args:
            image: Nome em `images_dir` (sem extensão vira `.png`) ou caminho absoluto.
            timeout: Prazo em segundos; `None` usa o do construtor.
            region: Área de busca; `None` busca em todos os monitores.
            confidence: Confiança mínima; `None` usa a do construtor.
            poll_interval: Intervalo entre buscas, em segundos.

        Returns:
            `True` se a imagem sumir no prazo; `False` se continuar na tela.

        Raises:
            DesktopAutomationError: Se a imagem de referência não existir ou for inválida.
        """
        limit = self._wait_limit(timeout, poll_interval)
        deadline = time.monotonic() + limit
        while True:
            if self.find_image(image, region=region, confidence=confidence) is None:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(poll_interval, remaining))

    def image_exists(
        self, image: ImageRef, *, region: Region | None = None, confidence: float | None = None
    ) -> bool:
        """Indica se a imagem está na tela agora (uma única busca).

        Args:
            image: Nome em `images_dir` (sem extensão vira `.png`) ou caminho absoluto.
            region: Área de busca; `None` busca em todos os monitores.
            confidence: Confiança mínima; `None` usa a do construtor.

        Returns:
            `True` se a imagem for encontrada.
        """
        return self.find_image(image, region=region, confidence=confidence) is not None

    def screenshot(
        self, path: PathLike | None = None, *, region: Region | None = None, label: str = "tela"
    ) -> Path:
        """Salva uma captura da tela (ou de uma região) em PNG.

        Args:
            path: Arquivo de destino; `None` gera `<AAAAMMDD_HHMMSS_fff>_<label>.png` em
                `screenshots_dir`.
            region: Área capturada; `None` captura todos os monitores.
            label: Sufixo do nome gerado; caracteres fora de letras, dígitos, `_` e `-` viram
                `_`.

        Returns:
            Caminho absoluto do arquivo salvo (aceita caminhos com acentos).

        Raises:
            DesktopAutomationError: Se a captura ou a gravação falhar.
        """
        import cv2

        frame = self.capture_screen(region)
        if path is None:
            path = self._screenshots_dir / _timestamped_png_name(label)
        target = Path(path).resolve()
        filesystem_utils.ensure_dir(target.parent)
        try:
            encoded_ok, encoded = cv2.imencode(target.suffix or _DEFAULT_IMAGE_SUFFIX, frame)
            if not encoded_ok:
                raise DesktopAutomationError(
                    "Falha ao codificar a captura", details={"arquivo": str(target)}
                )
            encoded.tofile(target)
        except (cv2.error, OSError) as exc:
            raise DesktopAutomationError(
                "Falha ao salvar screenshot",
                details={"arquivo": str(target), "erro": _describe(exc)},
            ) from exc
        get_logger().info("Screenshot salvo: {}", target)
        return target

    # Mouse

    def click(
        self,
        x: int,
        y: int,
        *,
        button: MouseButton = "left",
        clicks: int = 1,
        interval: float = 0.1,
    ) -> None:
        """Clica na posição absoluta `(x, y)`.

        Args:
            x: Coordenada X.
            y: Coordenada Y.
            button: Botão do mouse.
            clicks: Quantidade de cliques.
            interval: Intervalo entre cliques, em segundos.
        """
        import pyautogui

        get_logger().debug(
            "Clique na tela | x={} | y={} | botao={} | cliques={}", x, y, button, clicks
        )
        pyautogui.click(x=x, y=y, clicks=clicks, interval=interval, button=button)

    def double_click(self, x: int, y: int) -> None:
        """Clique duplo com o botão esquerdo em `(x, y)`.

        Args:
            x: Coordenada X.
            y: Coordenada Y.
        """
        import pyautogui

        get_logger().debug("Clique duplo na tela | x={} | y={}", x, y)
        pyautogui.doubleClick(x=x, y=y)

    def right_click(self, x: int, y: int) -> None:
        """Clique com o botão direito em `(x, y)`.

        Args:
            x: Coordenada X.
            y: Coordenada Y.
        """
        self.click(x, y, button="right")

    def move_to(self, x: int, y: int, *, duration: float = 0.2) -> None:
        """Move o mouse até `(x, y)`.

        Args:
            x: Coordenada X.
            y: Coordenada Y.
            duration: Duração do movimento, em segundos.
        """
        import pyautogui

        pyautogui.moveTo(x, y, duration=duration)

    def drag_to(
        self, x: int, y: int, *, duration: float = 0.5, button: MouseButton = "left"
    ) -> None:
        """Arrasta da posição atual do mouse até `(x, y)` com o botão pressionado.

        Args:
            x: Coordenada X de destino.
            y: Coordenada Y de destino.
            duration: Duração do arraste, em segundos.
            button: Botão mantido pressionado.
        """
        import pyautogui

        get_logger().debug("Arrastando mouse | x={} | y={} | botao={}", x, y, button)
        pyautogui.dragTo(x, y, duration=duration, button=button)

    def scroll(self, amount: int, *, x: int | None = None, y: int | None = None) -> None:
        """Rola a roda do mouse.

        Args:
            amount: Quantidade de "cliques" da roda; positivo rola para cima.
            x: Coordenada X onde rolar; `None` usa a posição atual.
            y: Coordenada Y onde rolar; `None` usa a posição atual.
        """
        import pyautogui

        pyautogui.scroll(amount, x=x, y=y)

    def click_image(
        self,
        image: ImageRef,
        *,
        button: MouseButton = "left",
        clicks: int = 1,
        offset: tuple[int, int] = (0, 0),
        timeout: float | None = None,
        region: Region | None = None,
        confidence: float | None = None,
    ) -> Match:
        """Aguarda a imagem e clica no centro dela somado ao deslocamento.

        Args:
            image: Nome em `images_dir` (sem extensão vira `.png`) ou caminho absoluto.
            button: Botão do mouse.
            clicks: Quantidade de cliques.
            offset: Deslocamento `(dx, dy)` a partir do centro da imagem.
            timeout: Prazo em segundos; `None` usa o do construtor.
            region: Área de busca; `None` busca em todos os monitores.
            confidence: Confiança mínima; `None` usa a do construtor.

        Returns:
            O `Match` usado no clique.

        Raises:
            ImageNotFoundError: Se a imagem não aparecer no prazo.
        """
        match = self.wait_image(image, timeout=timeout, region=region, confidence=confidence)
        center_x, center_y = match.center
        self.click(center_x + offset[0], center_y + offset[1], button=button, clicks=clicks)
        return match

    def mouse_position(self) -> tuple[int, int]:
        """Posição atual do mouse.

        Returns:
            Coordenadas `(x, y)`.
        """
        import pyautogui

        position = pyautogui.position()
        return int(position[0]), int(position[1])

    def screen_size(self) -> tuple[int, int]:
        """Resolução do monitor principal.

        Returns:
            `(largura, altura)` em pixels.
        """
        import pyautogui

        size = pyautogui.size()
        return int(size[0]), int(size[1])

    # Teclado e área de transferência

    def type_text(
        self,
        text: str,
        *,
        method: TypeMethod = "keys",
        interval: float = 0.02,
        sensitive: bool = False,
    ) -> None:
        """Digita o texto na janela em foco, com suporte a acentos (RF-035).

        Args:
            text: Texto a digitar.
            method: `"keys"` digita tecla a tecla com `pywinauto.keyboard.send_keys` (os
                caracteres `{}+^%~()` são escapados); `"clipboard"` cola o texto com Ctrl+V e
                restaura o conteúdo anterior da área de transferência.
            interval: Pausa entre teclas, em segundos (método `"keys"`).
            sensitive: Registra o texto para mascaramento e mostra `********` no log.

        Raises:
            ValueError: Se `method` for inválido.
        """
        if method not in _TYPE_METHODS:
            raise ValueError(f"method inválido: {method!r} (aceitos: keys, clipboard)")
        if sensitive:
            register_secret(text)
        get_logger().debug(
            "Digitando texto | metodo={} | texto={}", method, MASK if sensitive else text
        )
        if method == "clipboard":
            self._paste(text)
            return
        from pywinauto.keyboard import send_keys

        send_keys(
            escape_send_keys(text),
            with_spaces=True,
            with_tabs=True,
            with_newlines=True,
            pause=interval,
        )

    def press(self, key: str, *, presses: int = 1, interval: float = 0.05) -> None:
        """Pressiona uma tecla (nomes do pyautogui: ``"enter"``, ``"tab"``, ``"f5"``...).

        Args:
            key: Nome da tecla.
            presses: Quantidade de pressionamentos.
            interval: Intervalo entre pressionamentos, em segundos.
        """
        import pyautogui

        get_logger().debug("Pressionando tecla | tecla={} | vezes={}", key, presses)
        pyautogui.press(key, presses=presses, interval=interval)

    def hotkey(self, *keys: str) -> None:
        """Pressiona uma combinação de teclas (ex.: ``hotkey("ctrl", "s")``).

        Args:
            *keys: Teclas pressionadas em ordem e soltas em ordem inversa.
        """
        import pyautogui

        get_logger().debug("Atalho de teclado | teclas={}", "+".join(keys))
        pyautogui.hotkey(*keys)

    def get_clipboard(self) -> str:
        """Texto atual da área de transferência.

        Returns:
            O texto; vazio se a área de transferência não tiver texto.

        Raises:
            DesktopAutomationError: Se a área de transferência estiver inacessível.
        """
        import pyperclip

        try:
            return str(pyperclip.paste() or "")
        except pyperclip.PyperclipException as exc:
            raise DesktopAutomationError(
                "Falha ao ler a área de transferência", details={"erro": _describe(exc)}
            ) from exc

    def set_clipboard(self, text: str) -> None:
        """Substitui o conteúdo da área de transferência pelo texto.

        Args:
            text: Texto a copiar.

        Raises:
            DesktopAutomationError: Se a área de transferência estiver inacessível.
        """
        import pyperclip

        try:
            pyperclip.copy(text)
        except pyperclip.PyperclipException as exc:
            raise DesktopAutomationError(
                "Falha ao gravar na área de transferência", details={"erro": _describe(exc)}
            ) from exc

    # Janelas e processos

    def start_application(
        self,
        path: PathLike,
        args: Sequence[str] = (),
        *,
        wait_title_re: str | None = None,
        timeout: float = 30.0,
    ) -> Application:
        """Inicia uma aplicação com o pywinauto.

        Args:
            path: Executável (caminho ou nome encontrado no `PATH`).
            args: Argumentos de linha de comando.
            wait_title_re: Expressão regular do título da janela a aguardar.
            timeout: Prazo, em segundos, para a aplicação ficar pronta e a janela aparecer.

        Returns:
            O `pywinauto.application.Application` iniciado.

        Raises:
            DesktopAutomationError: Se o processo não puder ser criado.
            WindowNotFoundError: Se a janela de `wait_title_re` não aparecer no prazo.
        """
        from pywinauto.application import Application as PywinautoApplication
        from pywinauto.application import AppStartError

        command = subprocess.list2cmdline([str(path), *args])
        get_logger().info("Iniciando aplicação | comando={}", command)
        try:
            application = PywinautoApplication(backend=self._backend).start(
                command, timeout=timeout
            )
        except AppStartError as exc:
            raise DesktopAutomationError(
                "Falha ao iniciar aplicação", details={"comando": command, "erro": _describe(exc)}
            ) from exc
        if wait_title_re is not None:
            self.connect_window(wait_title_re, timeout=timeout)
        return application

    def connect_window(self, title_re: str, *, timeout: float = 30.0) -> WindowSpecification:
        """Aguarda a janela existir e ficar visível.

        Args:
            title_re: Expressão regular do título da janela.
            timeout: Prazo em segundos.

        Returns:
            O `WindowSpecification` da janela.

        Raises:
            WindowNotFoundError: Se a janela não aparecer no prazo.
            DesktopAutomationError: Se mais de uma janela corresponder ao título.
        """
        from pywinauto import Desktop
        from pywinauto.findwindows import ElementAmbiguousError
        from pywinauto.timings import TimeoutError as WaitTimeoutError

        window = Desktop(backend=self._backend).window(title_re=title_re)
        try:
            window.wait("exists visible", timeout=timeout)
        except WaitTimeoutError as exc:
            raise WindowNotFoundError(
                f"Janela não encontrada: {title_re}",
                details={"titulo": title_re, "timeout": timeout},
            ) from exc
        except ElementAmbiguousError as exc:
            raise DesktopAutomationError(
                f"Mais de uma janela corresponde ao título: {title_re}",
                details={"titulo": title_re},
            ) from exc
        return window

    def wait_window(self, title_re: str, *, timeout: float = 30.0) -> bool:
        """Como `connect_window`, mas retorna `False` se a janela não aparecer.

        Args:
            title_re: Expressão regular do título da janela.
            timeout: Prazo em segundos.

        Returns:
            `True` se a janela existir e estiver visível no prazo.
        """
        try:
            self.connect_window(title_re, timeout=timeout)
        except WindowNotFoundError:
            return False
        return True

    def activate_window(self, title_re: str, *, timeout: float = 10.0) -> None:
        """Restaura a janela, se minimizada, e a coloca em primeiro plano.

        Args:
            title_re: Expressão regular do título da janela.
            timeout: Prazo em segundos para a janela aparecer.

        Raises:
            WindowNotFoundError: Se a janela não aparecer no prazo.
        """
        window = self.connect_window(title_re, timeout=timeout)
        if window.is_minimized():
            window.restore()
        window.set_focus()

    def window_exists(self, title_re: str) -> bool:
        """Indica se existe janela com o título agora (sem espera).

        Args:
            title_re: Expressão regular do título da janela.

        Returns:
            `True` se ao menos uma janela corresponder ao título.
        """
        from pywinauto import Desktop
        from pywinauto.findwindows import ElementAmbiguousError

        try:
            return bool(Desktop(backend=self._backend).window(title_re=title_re).exists(timeout=0))
        except ElementAmbiguousError:
            return True

    def close_window(self, title_re: str, *, timeout: float = 10.0) -> None:
        """Fecha a janela e aguarda ela deixar de existir.

        Args:
            title_re: Expressão regular do título da janela.
            timeout: Prazo em segundos para encontrar e para fechar a janela.

        Raises:
            WindowNotFoundError: Se a janela não for encontrada.
            DesktopAutomationError: Se a janela continuar aberta (ex.: diálogo "Salvar?").
        """
        from pywinauto.timings import TimeoutError as WaitTimeoutError

        window = self.connect_window(title_re, timeout=timeout)
        window.close()
        try:
            window.wait_not("exists", timeout=timeout)
        except WaitTimeoutError as exc:
            raise DesktopAutomationError(
                f"A janela continua aberta: {title_re}",
                details={"titulo": title_re, "timeout": timeout},
            ) from exc

    def is_process_running(self, name: str) -> bool:
        """Indica se há processo com o nome informado (sem diferenciar maiúsculas).

        Args:
            name: Nome do executável (ex.: ``"erp.exe"``).

        Returns:
            `True` se ao menos um processo tiver esse nome.
        """
        return bool(_processes_named(name))

    def kill_process(self, name: str) -> int:
        """Encerra todos os processos com o nome informado (exceto o processo atual).

        Envia `terminate()`, aguarda até 5 s e chama `kill()` nos remanescentes. Processos que
        já terminaram ou sem permissão de acesso geram `WARNING` e são ignorados.

        Args:
            name: Nome do executável, sem diferenciar maiúsculas (ex.: ``"erp.exe"``).

        Returns:
            Quantidade de processos encerrados.
        """
        import psutil

        terminated: list[Any] = []
        for process in _processes_named(name):
            try:
                process.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
                _warn_process(name, process, exc)
            else:
                terminated.append(process)
        gone, alive = psutil.wait_procs(terminated, timeout=_KILL_GRACE_SECONDS)
        ended = len(gone)
        for process in alive:
            try:
                process.kill()
            except psutil.NoSuchProcess:
                ended += 1  # terminou entre a espera e o kill()
            except psutil.AccessDenied as exc:
                _warn_process(name, process, exc)
            else:
                ended += 1
        get_logger().info("Processos encerrados | nome={} | quantidade={}", name, ended)
        return ended

    # Auxiliares privados

    def _threshold(self, confidence: float | None) -> float:
        if confidence is None:
            return self._confidence
        _validate_confidence(confidence)
        return confidence

    def _wait_limit(self, timeout: float | None, poll_interval: float) -> float:
        if poll_interval <= 0:
            raise ValueError("poll_interval deve ser positivo")
        limit = self._timeout if timeout is None else timeout
        if limit < 0:
            raise ValueError("timeout não pode ser negativo")
        return limit

    def _resolve_image(self, image: ImageRef) -> Path:
        """Caminho absoluto existente → usa; senão `images_dir / image` (+ `.png`)."""
        path = Path(image)
        if path.is_absolute() and path.is_file():
            return path
        candidate = self._images_dir / path
        if not candidate.suffix:
            candidate = candidate.with_name(candidate.name + _DEFAULT_IMAGE_SUFFIX)
        if not candidate.is_file():
            raise DesktopAutomationError(
                "Imagem de referência não encontrada", details={"imagem": str(candidate)}
            )
        return candidate.resolve()

    def _template(self, path: Path, grayscale: bool) -> _Image:
        """Lê a imagem de referência (caminho Unicode) com cache por `(caminho, grayscale)`."""
        key = (path, grayscale)
        cached = self._templates.get(key)
        if cached is not None:
            return cached
        import cv2

        try:
            data = np.fromfile(path, dtype=np.uint8)
        except OSError as exc:
            raise DesktopAutomationError(
                "Falha ao ler imagem de referência",
                details={"imagem": str(path), "erro": _describe(exc)},
            ) from exc
        decoded = cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None
        if decoded is None:
            raise DesktopAutomationError(
                "Arquivo não é uma imagem válida", details={"imagem": str(path)}
            )
        template = _convert_channels(np.asarray(decoded, dtype=np.uint8), grayscale)
        self._templates[key] = template
        return template

    def _search(self, image: ImageRef, region: Region | None, grayscale: bool | None) -> _Search:
        """Captura a tela e calcula `TM_CCOEFF_NORMED` para cada posição."""
        import cv2

        use_gray = self._grayscale if grayscale is None else grayscale
        path = self._resolve_image(image)
        template = self._template(path, use_gray)
        screen = _convert_channels(self.capture_screen(region), use_gray)
        origin = (region[0], region[1]) if region is not None else self._virtual_screen_origin()
        height, width = template.shape[:2]
        if screen.shape[0] < height or screen.shape[1] < width:
            return _Search(path, None, width, height, origin)
        scores = np.asarray(cv2.matchTemplate(screen, template, cv2.TM_CCOEFF_NORMED))
        return _Search(path, scores.astype(np.float32), width, height, origin)

    def _virtual_screen_origin(self) -> tuple[int, int]:
        """Canto superior esquerdo da tela virtual (negativo com monitor à esquerda/acima)."""
        with _open_mss() as screen:
            monitor = screen.monitors[0]
        return int(monitor["left"]), int(monitor["top"])

    def _failure_screenshot(self, label: str) -> None:
        try:
            self.screenshot(label=label)
        except Exception as exc:  # a captura de evidência não esconde o erro original
            get_logger().warning("Falha ao salvar screenshot de erro | erro={}", _describe(exc))

    def _paste(self, text: str) -> None:
        previous = self.get_clipboard()
        try:
            self.set_clipboard(text)
            self.hotkey("ctrl", "v")
            time.sleep(_PASTE_DELAY_SECONDS)
        finally:
            self.set_clipboard(previous)


def escape_send_keys(text: str) -> str:
    """Envolve cada caractere de SEND_KEYS_SPECIAL em chaves: '+' → '{+}', '{' → '{{}'.

    Args:
        text: Texto literal a digitar.

    Returns:
        Texto seguro para `pywinauto.keyboard.send_keys`.

    Example:
        >>> escape_send_keys("Ação {x}+")
        'Ação {{}x{}}{+}'
    """
    return "".join(f"{{{char}}}" if char in SEND_KEYS_SPECIAL else char for char in text)


def enable_dpi_awareness() -> None:
    """Ativa o DPI awareness por monitor, uma vez por processo.

    SetProcessDpiAwareness(2) via shcore; fallback user32.SetProcessDPIAware. Fora do Windows
    não faz nada. Sem DPI awareness, o Windows escala as coordenadas e as
    capturas em monitores com zoom diferente de 100% (risco R-06).
    """
    with _DpiAwareness.lock:
        if _DpiAwareness.enabled:
            return
        _DpiAwareness.enabled = True
    if sys.platform != "win32":
        return
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(_PER_MONITOR_DPI_AWARE)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError) as exc:
            get_logger().warning("DPI awareness não ativado | erro={}", _describe(exc))


def _open_mss() -> Any:  # noqa: ANN401 - classe do mss varia conforme a versão
    """Instância de captura: `mss.MSS` (mss >= 10.2) ou `mss.mss()` (versões anteriores)."""
    import mss

    factory = getattr(mss, "MSS", None)
    if factory is None or inspect.isabstract(factory):
        factory = mss.mss
    return factory()


def _absolute(path: PathLike | None, default: Path) -> Path:
    return default if path is None else Path(path).resolve()


def _validate_confidence(confidence: float) -> None:
    if not 0 < confidence <= 1:
        raise ValueError(f"confidence deve estar entre 0 (exclusivo) e 1 (recebido: {confidence})")


def _validate_region(region: Region) -> None:
    if len(region) != 4 or region[2] <= 0 or region[3] <= 0:
        raise ValueError(f"Região inválida: {region!r} (esperado: left, top, width, height > 0)")


def _monitor_dict(region: Region) -> dict[str, int]:
    left, top, width, height = region
    return {"left": int(left), "top": int(top), "width": int(width), "height": int(height)}


def _convert_channels(image: _Image, grayscale: bool) -> _Image:
    """Converte BGR, BGRA ou cinza para BGR (ou cinza, quando `grayscale`)."""
    import cv2

    if image.ndim == 2:
        converted = image if grayscale else cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    elif image.shape[2] == 4:
        converted = cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY if grayscale else cv2.COLOR_BGRA2BGR)
    else:
        converted = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if grayscale else image
    return np.asarray(converted, dtype=np.uint8)


def _non_maximum_suppression(search: _Search, scores: _Scores, threshold: float) -> list[Match]:
    """Aceita candidatos por confiança decrescente, descartando os sobrepostos (RF-034)."""
    rows, columns = np.nonzero(scores >= threshold)
    values = scores[rows, columns]
    suppressed = np.zeros(scores.shape, dtype=bool)
    half_width, half_height = (search.width - 1) // 2, (search.height - 1) // 2
    matches: list[Match] = []
    for position in np.argsort(-values, kind="stable"):
        x, y = int(columns[position]), int(rows[position])
        if suppressed[y, x]:
            continue
        matches.append(search.match_at(x, y, float(values[position])))
        top, left = max(y - half_height, 0), max(x - half_width, 0)
        suppressed[top : y + half_height + 1, left : x + half_width + 1] = True
    return matches


def _processes_named(name: str) -> list[Any]:
    """Processos com o nome informado (sem diferenciar maiúsculas), exceto o atual."""
    import psutil

    target = name.strip().lower()
    own_pid = os.getpid()
    return [
        process
        for process in psutil.process_iter(["name"])
        if process.pid != own_pid and (process.info.get("name") or "").lower() == target
    ]


def _warn_process(name: str, process: Any, exc: BaseException) -> None:  # noqa: ANN401
    get_logger().warning(
        "Processo não encerrado | nome={} | pid={} | erro={}",
        name,
        getattr(process, "pid", "?"),
        type(exc).__name__,
    )


def _describe(exc: BaseException) -> str:
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text.splitlines()[0]}" if text else type(exc).__name__


def _sanitize_label(label: str) -> str:
    cleaned = _LABEL_INVALID_CHARS.sub("_", label).strip("_")
    return cleaned[:_LABEL_MAX_LENGTH] or "tela"


def _timestamped_png_name(label: str) -> str:
    """`<AAAAMMDD_HHMMSS_fff>_<label>.png` com o horário local atual."""
    now = datetime.now()
    return f"{now:%Y%m%d_%H%M%S}_{now.microsecond // 1000:03d}_{_sanitize_label(label)}.png"
