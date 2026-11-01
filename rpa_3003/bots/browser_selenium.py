"""Automação web via Selenium.

Classe única `Browser` para Chrome, Edge e Edge em modo IE, com fábrica extensível de drivers,
esperas explícitas, downloads e evidências (seção 8.21, RF-026 a RF-033).

Os drivers vêm do Selenium Manager ou de um caminho manual configurado (P-19). A espera
implícita é sempre `0`: toda localização usa `WebDriverWait`, e o tempo limite esgotado vira
`ElementNotFoundError` com o localizador, o prazo e a URL atual (RF-030).
"""

from __future__ import annotations

import glob
import os
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Any, ClassVar, Final, Literal, Protocol, Self, TypeAlias

from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    NoSuchElementException,
    NoSuchFrameException,
    NoSuchWindowException,
    TimeoutException,
    UnexpectedTagNameException,
    WebDriverException,
)
from selenium.webdriver.chromium.options import ChromiumOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as ec
from selenium.webdriver.support.select import Select
from selenium.webdriver.support.wait import WebDriverWait

from rpa_3003.core.config import FrameworkSettings, get_settings
from rpa_3003.core.exceptions import (
    BrowserError,
    BrowserNotStartedError,
    BrowserNotSupportedError,
    DownloadTimeoutError,
    DriverInitializationError,
    ElementNotFoundError,
    FileOperationError,
)
from rpa_3003.core.logger import MASK, get_logger, register_secret
from rpa_3003.utils import filesystem_utils

Locator: TypeAlias = tuple[str, str]  # noqa: UP040 - convenção 7.7; ex.: (By.ID, "usuario")
DEFAULT_CHROMIUM_ARGS: Final[tuple[str, ...]] = (
    "--disable-notifications",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-search-engine-choice-screen",
)
__all__: list[str] = [
    "Browser",
    "BrowserFactory",
    "BrowserOptions",
    "BrowserType",
    "By",
    "DownloadResult",
    "DriverBuilder",
    "Locator",
]

_POLL_FREQUENCY: Final[float] = 0.25
_HEADLESS_WINDOW_SIZE: Final[str] = "1920,1080"
_DOWNLOAD_STABLE_SECONDS: Final[float] = 1.0
_DOWNLOAD_POLL_INTERVAL: Final[float] = 0.5
_EDGE_PROGRAM_DIR_VARS: Final[tuple[str, ...]] = ("ProgramFiles(x86)", "ProgramFiles")
_EDGE_RELATIVE_PATH: Final[tuple[str, ...]] = ("Microsoft", "Edge", "Application", "msedge.exe")
_IE_MODE_HINT: Final[str] = "verifique zoom 100%, Modo Protegido e IEDriverServer 32 bits"
_JS_CLICK: Final[str] = "arguments[0].click();"
_JS_SCROLL_INTO_VIEW: Final[str] = (
    "arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});"
)
_LABEL_INVALID_CHARS: Final[re.Pattern[str]] = re.compile(r"[^\w-]+")
_LABEL_MAX_LENGTH: Final[int] = 80
_UNKNOWN_URL: Final[str] = "desconhecida"


class BrowserType(StrEnum):
    """Navegadores registrados por padrão na `BrowserFactory`."""

    CHROME = "chrome"
    EDGE = "edge"
    EDGE_IE = "edge_ie"


@dataclass(slots=True)
class BrowserOptions:
    """Opções comuns a todos os navegadores.

    Attributes:
        headless: Executa sem janela (Chrome e Edge; ignorado no modo IE).
        maximized: Abre a janela maximizada quando não é headless.
        window_size: Tamanho da janela `(largura, altura)`; tem precedência sobre `maximized`.
        download_dir: Pasta de downloads (Chrome e Edge); usada por `Browser.expect_download`.
        page_load_timeout: Tempo limite de carregamento de página, em segundos.
        default_timeout: Tempo limite padrão das esperas explícitas, em segundos.
        driver_path: Caminho manual do driver; `None` usa o Selenium Manager.
        binary_path: Executável do navegador (no modo IE, o `msedge.exe`).
        user_data_dir: Pasta de perfil do Chrome/Edge.
        incognito: Janela anônima (`--incognito` no Chrome, `--inprivate` no Edge).
        accept_insecure_certs: Aceita certificados TLS inválidos.
        arguments: Argumentos extras de linha de comando do Chrome/Edge.
        preferences: Preferências do Chrome/Edge, mescladas por último.
        ie_ignore_zoom_level: Modo IE: ignora zoom diferente de 100%.
        ie_ignore_protected_mode_settings: Modo IE: ignora divergência de Modo Protegido entre
            zonas de segurança.
        ie_require_window_focus: Modo IE: foca a janela antes de cada interação.
        ie_ensure_clean_session: Modo IE: limpa cache e cookies ao iniciar.
        ie_initial_url: Modo IE: URL aberta ao iniciar.
    """

    headless: bool = False
    maximized: bool = True
    window_size: tuple[int, int] | None = None
    download_dir: Path | None = None
    page_load_timeout: int = 60
    default_timeout: float = 30.0
    driver_path: Path | None = None
    binary_path: Path | None = None
    user_data_dir: Path | None = None
    incognito: bool = False
    accept_insecure_certs: bool = False
    arguments: list[str] = field(default_factory=list)
    preferences: dict[str, Any] = field(default_factory=dict)
    ie_ignore_zoom_level: bool = True
    ie_ignore_protected_mode_settings: bool = False
    ie_require_window_focus: bool = False
    ie_ensure_clean_session: bool = True
    ie_initial_url: str = "about:blank"

    @classmethod
    def from_settings(cls, settings: FrameworkSettings, browser: BrowserType | str) -> Self:
        """headless, timeouts, driver_path (por tipo) e binary_path (edge_ie) a partir das settings.

        Args:
            settings: Configuração do framework (`RPA3003_*`).
            browser: Tipo de navegador; define qual caminho de driver é usado.

        Returns:
            Opções com `headless`, `page_load_timeout`, `default_timeout`, `driver_path` e, no
            modo IE, `binary_path` preenchidos; os demais campos ficam com o padrão.
        """
        key = _normalize_browser(browser)
        driver_paths: dict[str, Path | None] = {
            BrowserType.CHROME: settings.chromedriver_path,
            BrowserType.EDGE: settings.edgedriver_path,
            BrowserType.EDGE_IE: settings.iedriver_path,
        }
        return cls(
            headless=settings.browser_headless,
            page_load_timeout=settings.page_load_timeout,
            default_timeout=float(settings.selenium_timeout),
            driver_path=driver_paths.get(key),
            binary_path=settings.edge_binary_path if key == BrowserType.EDGE_IE else None,
        )


class DriverBuilder(Protocol):
    """Estratégia de criação do `WebDriver` de um navegador (RF-033)."""

    def build(self, options: BrowserOptions) -> WebDriver:
        """Cria e retorna o driver já iniciado.

        Args:
            options: Opções comuns do framework.

        Returns:
            O `WebDriver` do navegador.
        """
        ...


class ChromeDriverBuilder:
    """Cria o `webdriver.Chrome` a partir de `BrowserOptions` (RF-027)."""

    def build(self, options: BrowserOptions) -> WebDriver:
        """Cria o driver do Chrome.

        Args:
            options: Opções comuns do framework.

        Returns:
            O driver do Chrome; o chromedriver vem de `driver_path` ou do Selenium Manager.
        """
        chrome_options = webdriver.ChromeOptions()
        _apply_chromium_options(chrome_options, options, private_flag="--incognito")
        if options.driver_path is not None:
            service = webdriver.ChromeService(executable_path=str(options.driver_path))
        else:
            service = webdriver.ChromeService()
        return webdriver.Chrome(options=chrome_options, service=service)


class EdgeDriverBuilder:
    """Cria o `webdriver.Edge` a partir de `BrowserOptions` (RF-028)."""

    def build(self, options: BrowserOptions) -> WebDriver:
        """Cria o driver do Edge.

        Args:
            options: Opções comuns do framework.

        Returns:
            O driver do Edge; o msedgedriver vem de `driver_path` ou do Selenium Manager.
        """
        edge_options = webdriver.EdgeOptions()
        _apply_chromium_options(edge_options, options, private_flag="--inprivate")
        if options.driver_path is not None:
            service = webdriver.EdgeService(executable_path=str(options.driver_path))
        else:
            service = webdriver.EdgeService()
        return webdriver.Edge(options=edge_options, service=service)


class EdgeIEDriverBuilder:
    """Cria o `webdriver.Ie` que controla o Edge em modo IE (RF-029)."""

    def build(self, options: BrowserOptions) -> WebDriver:
        """Cria o driver do IEDriverServer anexado ao Edge.

        `headless` e `download_dir` não são suportados no modo IE: geram `WARNING` e são
        ignorados.

        Args:
            options: Opções comuns do framework.

        Returns:
            O driver do modo IE; o IEDriverServer vem de `driver_path` ou do Selenium Manager.

        Raises:
            DriverInitializationError: Se o `msedge.exe` não for encontrado.
        """
        log = get_logger()
        if options.headless:
            log.warning("Modo headless não suportado no Edge modo IE; opção ignorada")
        if options.download_dir is not None:
            log.warning("download_dir não suportado no Edge modo IE; opção ignorada")
        ie_options = webdriver.IeOptions()
        ie_options.attach_to_edge_chrome = True
        ie_options.edge_executable_path = str(self.find_edge_executable(options.binary_path))
        ie_options.ignore_zoom_level = options.ie_ignore_zoom_level
        # Nome atual (Selenium 4) de introduce_flakiness_by_ignoring_security_domains (D-07).
        ie_options.ignore_protected_mode_settings = options.ie_ignore_protected_mode_settings
        ie_options.require_window_focus = options.ie_require_window_focus
        ie_options.ensure_clean_session = options.ie_ensure_clean_session
        ie_options.initial_browser_url = options.ie_initial_url
        # O padrão do IEDriver (0) espera para sempre a janela do Edge em modo IE (RNF-007).
        ie_options.browser_attach_timeout = options.page_load_timeout * 1000
        if options.driver_path is not None:
            service = webdriver.IeService(executable_path=str(options.driver_path))
        else:
            service = webdriver.IeService()
        return webdriver.Ie(options=ie_options, service=service)

    @staticmethod
    def find_edge_executable(explicit: Path | None = None) -> Path:
        """explicit → %ProgramFiles(x86)%/Microsoft/Edge/Application/msedge.exe →
        %ProgramFiles%/Microsoft/Edge/Application/msedge.exe; ausente → DriverInitializationError.

        Args:
            explicit: Caminho informado em `BrowserOptions.binary_path` (ou
                `RPA3003_EDGE_BINARY_PATH`). Se não existir, gera `WARNING` e a busca continua
                nos caminhos padrão.

        Returns:
            Caminho absoluto do `msedge.exe`.

        Raises:
            DriverInitializationError: Se nenhum caminho existir; a mensagem lista os caminhos
                pesquisados.
        """
        candidates: list[Path] = []
        if explicit is not None:
            candidates.append(Path(explicit))
        for variable in _EDGE_PROGRAM_DIR_VARS:
            base = os.environ.get(variable)
            if base:
                candidates.append(Path(base).joinpath(*_EDGE_RELATIVE_PATH))
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()
            if candidate == explicit:
                get_logger().warning(
                    "msedge.exe não encontrado no caminho configurado | caminho={}", candidate
                )
        searched = "; ".join(str(candidate) for candidate in candidates) or "nenhum"
        raise DriverInitializationError(
            f"msedge.exe não encontrado. Caminhos pesquisados: {searched}",
            details={"dica": "defina RPA3003_EDGE_BINARY_PATH ou BrowserOptions.binary_path"},
        )


class BrowserFactory:
    """Registro de `DriverBuilder` por nome de navegador (Abstract Factory com registro)."""

    _registry: ClassVar[dict[str, DriverBuilder]] = {}

    @classmethod
    def register(cls, name: str, builder: DriverBuilder, *, overwrite: bool = False) -> None:
        """Registra o builder de um navegador.

        Args:
            name: Nome do navegador; normalizado para minúsculas, sem espaços nas pontas.
            builder: Implementação do protocolo `DriverBuilder`.
            overwrite: Substitui um registro existente com o mesmo nome.

        Raises:
            ValueError: Se o nome for vazio ou já estiver registrado sem `overwrite`.
        """
        key = _normalize_browser(name)
        if not key:
            raise ValueError("O nome do navegador não pode ser vazio")
        if key in cls._registry and not overwrite:
            raise ValueError(f"Navegador já registrado: {key} (use overwrite=True para substituir)")
        cls._registry[key] = builder

    @classmethod
    def available(cls) -> list[str]:
        """Nomes registrados, em ordem alfabética.

        Returns:
            Lista de nomes aceitos por `create_driver` e por `Browser`.
        """
        return sorted(cls._registry)

    @classmethod
    def create_driver(cls, browser: BrowserType | str, options: BrowserOptions) -> WebDriver:
        """Cria o driver com o builder registrado para o navegador.

        Args:
            browser: Nome ou tipo do navegador (sem diferenciar maiúsculas).
            options: Opções comuns do framework.

        Returns:
            O driver criado pelo builder.

        Raises:
            BrowserNotSupportedError: Se o navegador não estiver registrado; a mensagem lista
                os nomes disponíveis.
        """
        key = _normalize_browser(browser)
        builder = cls._registry.get(key)
        if builder is None:
            available = ", ".join(cls.available())
            raise BrowserNotSupportedError(
                f"Navegador não suportado: '{browser}'. Disponíveis: {available}",
                details={"navegador": str(browser)},
            )
        return builder.build(options)


@dataclass(slots=True)
class DownloadResult:
    """Resultado de `Browser.expect_download`, preenchido ao sair do bloco `with`.

    Attributes:
        path: Caminho absoluto do arquivo baixado.
    """

    path: Path | None = None


class Browser:
    """Fachada única para automação web em Chrome, Edge e Edge em modo IE (RF-026).

    Example:
        >>> with Browser("chrome", BrowserOptions(headless=True)) as browser:  # doctest: +SKIP
        ...     browser.open("https://example.com")
        ...     browser.click((By.ID, "entrar"))
    """

    def __init__(
        self,
        browser: BrowserType | str = BrowserType.CHROME,
        options: BrowserOptions | None = None,
        *,
        screenshots_dir: Path | None = None,
        screenshot_on_error: bool = True,
    ) -> None:
        """Configura o navegador sem iniciá-lo (o driver é criado em `start()`).

        Args:
            browser: Nome registrado na `BrowserFactory` (sem diferenciar maiúsculas).
            options: Opções do navegador; `None` usa `BrowserOptions.from_settings` com a
                configuração atual do framework (`RPA3003_*`).
            screenshots_dir: Pasta das capturas; `None` usa `<cwd>/logs/screenshots`.
            screenshot_on_error: Salva captura com label `erro` ao sair do `with` por exceção.
        """
        self._browser_type: str = _normalize_browser(browser)
        self._options: BrowserOptions = (
            options
            if options is not None
            else BrowserOptions.from_settings(get_settings(), self._browser_type)
        )
        self._screenshots_dir: Path | None = screenshots_dir
        self._screenshot_on_error: bool = screenshot_on_error
        self._driver: WebDriver | None = None

    # Ciclo de vida

    def start(self) -> Self:
        """Cria o driver e aplica tempos limite, tamanho da janela e pasta de downloads.

        Chamar `start()` com o navegador já iniciado não cria um segundo driver.

        Returns:
            A própria instância, para encadeamento.

        Raises:
            BrowserNotSupportedError: Se o navegador não estiver registrado.
            DriverInitializationError: Se o driver ou o navegador não iniciar.
        """
        if self._driver is not None:
            return self
        options = self._options
        try:
            driver = BrowserFactory.create_driver(self._browser_type, options)
        except (WebDriverException, OSError) as exc:
            raise self._initialization_error(exc) from exc
        try:
            self._configure_driver(driver)
        except WebDriverException as exc:
            _quit_quietly(driver)
            raise self._initialization_error(exc) from exc
        self._driver = driver
        if options.download_dir is not None:
            filesystem_utils.ensure_dir(options.download_dir)
        get_logger().info(
            "Navegador iniciado | tipo={} | headless={}", self._browser_type, self._is_headless()
        )
        return self

    def quit(self) -> None:
        """Encerra o navegador e o driver (idempotente; falhas viram `WARNING`)."""
        driver, self._driver = self._driver, None
        if driver is None:
            return
        try:
            driver.quit()
        except Exception as exc:  # encerramento nunca interrompe o robô
            get_logger().warning("Falha ao encerrar o navegador | erro={}", _describe(exc))
        get_logger().info("Navegador encerrado")

    def __enter__(self) -> Self:
        """Inicia o navegador (`start()`).

        Returns:
            A própria instância.
        """
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Salva captura de erro (se houve exceção) e sempre encerra o navegador.

        A exceção do bloco `with` nunca é suprimida.

        Args:
            exc_type: Classe da exceção do bloco, ou `None`.
            exc: Exceção do bloco, ou `None`.
            tb: Traceback da exceção, ou `None`.
        """
        try:
            if exc is not None and self._screenshot_on_error and self.is_running:
                self._error_screenshot()
        finally:
            self.quit()

    @property
    def driver(self) -> WebDriver:
        """`WebDriver` nativo do Selenium.

        Raises:
            BrowserNotStartedError: Se `start()` ainda não foi chamado.
        """
        if self._driver is None:
            raise BrowserNotStartedError(
                "Navegador não iniciado; chame start() ou use 'with Browser(...)'",
                details={"tipo": self._browser_type},
            )
        return self._driver

    @property
    def is_running(self) -> bool:
        """Indica se o navegador foi iniciado e ainda não foi encerrado."""
        return self._driver is not None

    @property
    def browser_type(self) -> str:
        """Nome normalizado do navegador (ex.: ``"edge_ie"``)."""
        return self._browser_type

    # Navegação

    def open(self, url: str) -> None:
        """Abre a URL na aba atual e aguarda o carregamento.

        Args:
            url: Endereço a abrir.

        Raises:
            BrowserError: Se o carregamento exceder `BrowserOptions.page_load_timeout`.
        """
        get_logger().info("Abrindo URL: {}", url)
        try:
            self.driver.get(url)
        except TimeoutException as exc:
            raise BrowserError(
                "Tempo limite de carregamento da página excedido",
                details={"url": url, "timeout": self._options.page_load_timeout},
            ) from exc

    @property
    def current_url(self) -> str:
        """URL da aba atual."""
        return self.driver.current_url

    @property
    def title(self) -> str:
        """Título da página atual."""
        return self.driver.title

    def refresh(self) -> None:
        """Recarrega a página atual."""
        self.driver.refresh()

    def back(self) -> None:
        """Volta para a página anterior do histórico."""
        self.driver.back()

    def forward(self) -> None:
        """Avança para a próxima página do histórico."""
        self.driver.forward()

    # Localização e espera

    def find(self, locator: Locator, timeout: float | None = None) -> WebElement:
        """Aguarda o elemento existir no DOM.

        Args:
            locator: Localizador `(By.<estratégia>, valor)`.
            timeout: Prazo em segundos; `None` usa `BrowserOptions.default_timeout`.

        Returns:
            O elemento encontrado.

        Raises:
            ElementNotFoundError: Se o prazo se esgotar.
        """
        return self._until(ec.presence_of_element_located(locator), locator, timeout)

    def find_all(self, locator: Locator, timeout: float | None = None) -> list[WebElement]:
        """Aguarda ao menos um elemento existir e retorna todos os que casam.

        Args:
            locator: Localizador `(By.<estratégia>, valor)`.
            timeout: Prazo em segundos; `None` usa `BrowserOptions.default_timeout`.

        Returns:
            Os elementos encontrados, ou lista vazia se o prazo se esgotar.
        """
        try:
            return self._until(ec.presence_of_all_elements_located(locator), locator, timeout)
        except ElementNotFoundError:
            return []

    def exists(self, locator: Locator, timeout: float = 0) -> bool:
        """Indica se o elemento existe no DOM, aguardando até `timeout`.

        Erros do Selenium (inclusive seletor inválido) resultam em `False`.

        Args:
            locator: Localizador `(By.<estratégia>, valor)`.
            timeout: Prazo em segundos; `0` verifica uma única vez.

        Returns:
            `True` se o elemento for encontrado no prazo.

        Raises:
            BrowserNotStartedError: Se o navegador não foi iniciado.
        """
        try:
            self.find(locator, timeout)
        except (ElementNotFoundError, WebDriverException):
            return False
        return True

    def wait_visible(self, locator: Locator, timeout: float | None = None) -> WebElement:
        """Aguarda o elemento ficar visível.

        Args:
            locator: Localizador `(By.<estratégia>, valor)`.
            timeout: Prazo em segundos; `None` usa `BrowserOptions.default_timeout`.

        Returns:
            O elemento visível.

        Raises:
            ElementNotFoundError: Se o prazo se esgotar.
        """
        return self._until(ec.visibility_of_element_located(locator), locator, timeout)

    def wait_clickable(self, locator: Locator, timeout: float | None = None) -> WebElement:
        """Aguarda o elemento ficar visível e habilitado.

        Args:
            locator: Localizador `(By.<estratégia>, valor)`.
            timeout: Prazo em segundos; `None` usa `BrowserOptions.default_timeout`.

        Returns:
            O elemento clicável.

        Raises:
            ElementNotFoundError: Se o prazo se esgotar.
        """
        return self._until(ec.element_to_be_clickable(locator), locator, timeout)

    def wait_invisible(self, locator: Locator, timeout: float | None = None) -> None:
        """Aguarda o elemento ficar invisível ou sair do DOM.

        Args:
            locator: Localizador `(By.<estratégia>, valor)`.
            timeout: Prazo em segundos; `None` usa `BrowserOptions.default_timeout`.

        Raises:
            ElementNotFoundError: Se o elemento continuar visível no prazo.
        """
        seconds = self._timeout(timeout)
        self._until(
            ec.invisibility_of_element_located(locator),
            locator,
            seconds,
            message=f"Elemento continua visível: {locator} em {seconds}s",
        )

    def wait_url_contains(self, fragment: str, timeout: float | None = None) -> None:
        """Aguarda a URL atual conter o trecho informado.

        Args:
            fragment: Trecho esperado na URL.
            timeout: Prazo em segundos; `None` usa `BrowserOptions.default_timeout`.

        Raises:
            ElementNotFoundError: Se o prazo se esgotar.
        """
        seconds = self._timeout(timeout)
        self._until(
            ec.url_contains(fragment),
            None,
            seconds,
            message=f"URL não contém '{fragment}' em {seconds}s",
        )

    # Interação

    def click(
        self, locator: Locator, timeout: float | None = None, *, js_fallback: bool = True
    ) -> None:
        """Aguarda o elemento ficar clicável e clica nele.

        Args:
            locator: Localizador `(By.<estratégia>, valor)`.
            timeout: Prazo em segundos; `None` usa `BrowserOptions.default_timeout`.
            js_fallback: Clica via JavaScript quando o clique nativo é interceptado por outro
                elemento.

        Raises:
            ElementNotFoundError: Se o elemento não ficar clicável no prazo.
            BrowserError: Se o clique for interceptado e `js_fallback` for `False`.
        """
        element = self.wait_clickable(locator, timeout)
        get_logger().debug("Clique em {}", locator)
        try:
            element.click()
        except ElementClickInterceptedException as exc:
            if not js_fallback:
                raise BrowserError(
                    f"Clique interceptado em {locator}", details={"url": self._safe_current_url()}
                ) from exc
            get_logger().debug("Clique interceptado; usando JavaScript em {}", locator)
            self.driver.execute_script(_JS_CLICK, element)

    def type_text(
        self,
        locator: Locator,
        text: str,
        timeout: float | None = None,
        *,
        clear: bool = True,
        sensitive: bool = False,
        press_enter: bool = False,
    ) -> None:
        """Aguarda o elemento ficar visível e digita o texto.

        Args:
            locator: Localizador `(By.<estratégia>, valor)`.
            text: Texto a digitar.
            timeout: Prazo em segundos; `None` usa `BrowserOptions.default_timeout`.
            clear: Limpa o campo antes de digitar.
            sensitive: Registra o texto para mascaramento e mostra `********` no log.
            press_enter: Pressiona Enter após digitar.

        Raises:
            ElementNotFoundError: Se o elemento não ficar visível no prazo.
        """
        if sensitive:
            register_secret(text)
        element = self.wait_visible(locator, timeout)
        get_logger().debug("Digitando em {}: {}", locator, MASK if sensitive else text)
        if clear:
            element.clear()
        element.send_keys(text)
        if press_enter:
            element.send_keys(Keys.ENTER)

    def get_text(self, locator: Locator, timeout: float | None = None) -> str:
        """Aguarda o elemento ficar visível e retorna o texto exibido.

        Args:
            locator: Localizador `(By.<estratégia>, valor)`.
            timeout: Prazo em segundos; `None` usa `BrowserOptions.default_timeout`.

        Returns:
            O texto visível do elemento.

        Raises:
            ElementNotFoundError: Se o elemento não ficar visível no prazo.
        """
        return self.wait_visible(locator, timeout).text

    def get_attribute(
        self, locator: Locator, name: str, timeout: float | None = None
    ) -> str | None:
        """Aguarda o elemento existir e retorna um atributo ou propriedade.

        Args:
            locator: Localizador `(By.<estratégia>, valor)`.
            name: Nome do atributo (ex.: ``"value"``, ``"href"``).
            timeout: Prazo em segundos; `None` usa `BrowserOptions.default_timeout`.

        Returns:
            O valor do atributo, ou `None` se ele não existir.

        Raises:
            ElementNotFoundError: Se o elemento não existir no prazo.
        """
        return self.find(locator, timeout).get_attribute(name)

    def select_option(
        self,
        locator: Locator,
        *,
        text: str | None = None,
        value: str | None = None,
        index: int | None = None,
        timeout: float | None = None,
    ) -> None:
        """Seleciona uma opção de um `<select>` por texto visível, valor ou índice.

        Args:
            locator: Localizador do elemento `<select>`.
            text: Texto visível da opção.
            value: Atributo `value` da opção.
            index: Índice da opção, a partir de 0.
            timeout: Prazo em segundos; `None` usa `BrowserOptions.default_timeout`.

        Raises:
            ValueError: Se não for informado exatamente um entre `text`, `value` e `index`.
            ElementNotFoundError: Se o elemento não ficar visível no prazo ou a opção não
                existir.
            BrowserError: Se o elemento não for um `<select>`.
        """
        criterion = _single_criterion(text=text, value=value, index=index)
        element = self.wait_visible(locator, timeout)
        try:
            select = Select(element)
            if text is not None:
                select.select_by_visible_text(text)
            elif value is not None:
                select.select_by_value(value)
            elif index is not None:
                select.select_by_index(index)
        except UnexpectedTagNameException as exc:
            raise BrowserError(
                f"O elemento não é um <select>: {locator}",
                details={"url": self._safe_current_url()},
            ) from exc
        except NoSuchElementException as exc:
            raise ElementNotFoundError(
                f"Opção não encontrada em {locator}",
                details={"criterio": criterion, "url": self._safe_current_url()},
            ) from exc

    def scroll_into_view(self, locator: Locator, timeout: float | None = None) -> None:
        """Rola a página até o elemento ficar no centro da área visível.

        Args:
            locator: Localizador `(By.<estratégia>, valor)`.
            timeout: Prazo em segundos; `None` usa `BrowserOptions.default_timeout`.

        Raises:
            ElementNotFoundError: Se o elemento não existir no prazo.
        """
        self.driver.execute_script(_JS_SCROLL_INTO_VIEW, self.find(locator, timeout))

    def execute_script(self, script: str, *args: Any) -> Any:  # noqa: ANN401 - retorno do JS
        """Executa JavaScript síncrono na página atual.

        Args:
            script: Código JavaScript; os argumentos ficam em `arguments[i]`.
            *args: Argumentos repassados ao script (elementos, números, textos).

        Returns:
            O valor retornado pelo script.
        """
        return self.driver.execute_script(script, *args)

    # Frames, janelas e alertas

    def switch_to_frame(self, frame: Locator | int | str, timeout: float | None = None) -> None:
        """Muda o contexto para um iframe.

        Args:
            frame: Localizador do iframe (aguardado até ficar disponível), índice ou
                nome/id.
            timeout: Prazo em segundos para o localizador; `None` usa
                `BrowserOptions.default_timeout`.

        Raises:
            ElementNotFoundError: Se o frame não estiver disponível.
        """
        if isinstance(frame, tuple):
            self._until(ec.frame_to_be_available_and_switch_to_it(frame), frame, timeout)
            return
        try:
            self.driver.switch_to.frame(frame)
        except (NoSuchFrameException, NoSuchElementException) as exc:
            raise ElementNotFoundError(
                f"Frame não encontrado: {frame!r}", details={"url": self._safe_current_url()}
            ) from exc

    def switch_to_default_content(self) -> None:
        """Volta o contexto para o documento principal da página."""
        self.driver.switch_to.default_content()

    def switch_to_window(
        self,
        *,
        index: int | None = None,
        title_contains: str | None = None,
        handle: str | None = None,
        timeout: float | None = None,
    ) -> str:
        """Muda para outra janela ou aba, aguardando até ela existir.

        Args:
            index: Posição em `window_handles` (aceita negativos, ex.: `-1` para a última).
            title_contains: Trecho do título da página.
            handle: Identificador da janela.
            timeout: Prazo em segundos; `None` usa `BrowserOptions.default_timeout`.

        Returns:
            O identificador da janela selecionada.

        Raises:
            ValueError: Se não for informado exatamente um critério.
            BrowserError: Se nenhuma janela atender ao critério no prazo.
        """
        criterion = _single_criterion(index=index, title_contains=title_contains, handle=handle)
        seconds = self._timeout(timeout)
        finder = _window_finder(index=index, title_contains=title_contains, handle=handle)
        wait = WebDriverWait(
            self.driver,
            seconds,
            poll_frequency=_POLL_FREQUENCY,
            ignored_exceptions=(NoSuchWindowException,),
        )
        try:
            selected = wait.until(finder)
        except TimeoutException as exc:
            raise BrowserError(
                "Janela não encontrada", details={"criterio": criterion, "timeout": seconds}
            ) from exc
        self.driver.switch_to.window(selected)
        return selected

    def new_tab(self, url: str | None = None) -> str:
        """Abre uma nova aba, muda para ela e, opcionalmente, abre uma URL.

        Args:
            url: Endereço a abrir na nova aba.

        Returns:
            O identificador da nova aba.
        """
        self.driver.switch_to.new_window("tab")
        handle = self.driver.current_window_handle
        if url is not None:
            self.open(url)
        return handle

    def close_tab(self) -> None:
        """Fecha a aba atual e muda para a última aba restante."""
        driver = self.driver
        driver.close()
        try:
            handles = driver.window_handles
        except WebDriverException:
            handles = []
        if handles:
            driver.switch_to.window(handles[-1])

    def handle_alert(
        self, *, accept: bool = True, text: str | None = None, timeout: float = 5.0
    ) -> str | None:
        """Aguarda um alerta JavaScript e o aceita ou rejeita.

        Args:
            accept: Aceita (`True`) ou rejeita (`False`) o alerta.
            text: Texto digitado no `prompt` antes de responder.
            timeout: Prazo em segundos para o alerta aparecer.

        Returns:
            O texto do alerta, ou `None` se nenhum alerta aparecer no prazo.
        """
        try:
            alert = WebDriverWait(self.driver, timeout, poll_frequency=_POLL_FREQUENCY).until(
                ec.alert_is_present()
            )
        except TimeoutException:
            return None
        message = alert.text
        if text is not None:
            alert.send_keys(text)
        if accept:
            alert.accept()
        else:
            alert.dismiss()
        return message

    # Evidências, downloads e cookies

    def screenshot(self, path: Path | None = None, *, label: str = "captura") -> Path:
        """Salva uma captura PNG da página.

        Args:
            path: Arquivo de destino; `None` gera `<AAAAMMDD_HHMMSS_fff>_<label>.png` em
                `screenshots_dir` (padrão `<cwd>/logs/screenshots`).
            label: Sufixo do nome gerado; caracteres fora de letras, dígitos, `_` e `-` viram
                `_`.

        Returns:
            Caminho absoluto do arquivo salvo.

        Raises:
            BrowserError: Se o driver não conseguir salvar a captura.
        """
        if path is None:
            directory = self._screenshots_dir or Path.cwd() / "logs" / "screenshots"
            path = directory / _timestamped_png_name(label)
        target = Path(path).resolve()
        filesystem_utils.ensure_dir(target.parent)
        try:
            saved = self.driver.save_screenshot(str(target))
        except WebDriverException as exc:
            raise BrowserError(
                "Falha ao salvar screenshot",
                details={"arquivo": str(target), "erro": _describe(exc)},
            ) from exc
        if not saved:
            raise BrowserError("Falha ao salvar screenshot", details={"arquivo": str(target)})
        get_logger().info("Screenshot salvo: {}", target)
        return target

    @contextmanager
    def expect_download(
        self, timeout: float = 120.0, pattern: str = "*"
    ) -> Iterator[DownloadResult]:
        """Aguarda, ao sair do bloco, um arquivo novo em `download_dir` (RF-031).

        Os arquivos existentes são registrados ao entrar no bloco. Ao sair, aguarda um arquivo
        novo que case com `pattern`, sem sufixo temporário (`.crdownload`, `.tmp`, `.partial`,
        `.part`, `.download`) e com tamanho estável por 1 s. Se o bloco lançar exceção, não há
        espera.

        Args:
            timeout: Prazo em segundos, contado a partir da saída do bloco.
            pattern: Padrão glob do arquivo esperado (ex.: ``"*.pdf"``).

        Yields:
            `DownloadResult` cujo `path` é preenchido ao sair do bloco.

        Raises:
            BrowserError: Se `BrowserOptions.download_dir` não estiver definido.
            DownloadTimeoutError: Se nenhum download terminar no prazo.
        """
        if self._options.download_dir is None:
            raise BrowserError("Defina BrowserOptions.download_dir para usar expect_download")
        directory = Path(self._options.download_dir).resolve()
        existing = _entry_names(directory)
        result = DownloadResult()
        yield result
        result.path = _wait_new_download(directory, pattern, existing, timeout)

    def get_cookies(self) -> list[dict[str, Any]]:
        """Cookies visíveis na página atual.

        Returns:
            Lista de cookies no formato do Selenium (`name`, `value`, `domain`...).
        """
        return list(self.driver.get_cookies())

    def add_cookie(self, cookie: dict[str, Any]) -> None:
        """Adiciona um cookie ao domínio da página atual.

        Args:
            cookie: Cookie com ao menos `name` e `value`.
        """
        self.driver.add_cookie(cookie)

    # Auxiliares privados

    def _timeout(self, timeout: float | None) -> float:
        return self._options.default_timeout if timeout is None else timeout

    def _is_headless(self) -> bool:
        return self._options.headless and self._browser_type != BrowserType.EDGE_IE

    def _configure_driver(self, driver: WebDriver) -> None:
        options = self._options
        driver.set_page_load_timeout(options.page_load_timeout)
        driver.implicitly_wait(0)
        if self._is_headless():
            return
        if options.window_size is not None:
            driver.set_window_size(*options.window_size)
        elif options.maximized and self._browser_type == BrowserType.EDGE_IE:
            driver.maximize_window()

    def _initialization_error(self, exc: BaseException) -> DriverInitializationError:
        details: dict[str, Any] = {"tipo": self._browser_type, "erro": _describe(exc)}
        if self._browser_type == BrowserType.EDGE_IE:
            details["dica"] = _IE_MODE_HINT
        return DriverInitializationError(
            f"Falha ao iniciar o navegador {self._browser_type}", details=details
        )

    def _until[T](
        self,
        condition: Callable[[WebDriver], Literal[False] | T],
        locator: Locator | None,
        timeout: float | None,
        *,
        message: str | None = None,
    ) -> T:
        """Executa `WebDriverWait.until`; tempo esgotado vira `ElementNotFoundError`."""
        seconds = self._timeout(timeout)
        wait = WebDriverWait(self.driver, seconds, poll_frequency=_POLL_FREQUENCY)
        try:
            return wait.until(condition)
        except TimeoutException as exc:
            raise ElementNotFoundError(
                message or f"Elemento não encontrado: {locator} em {seconds}s",
                details={"url": self._safe_current_url()},
            ) from exc

    def _safe_current_url(self) -> str:
        try:
            return self.driver.current_url
        except (WebDriverException, BrowserError):
            return _UNKNOWN_URL

    def _error_screenshot(self) -> None:
        try:
            self.screenshot(label="erro")
        except Exception as exc:  # a captura de erro não esconde a exceção original
            get_logger().warning("Falha ao salvar screenshot de erro | erro={}", _describe(exc))


def _normalize_browser(browser: BrowserType | str) -> str:
    return str(browser).strip().lower()


def _apply_chromium_options(
    target: ChromiumOptions, options: BrowserOptions, *, private_flag: str
) -> None:
    """Aplica argumentos, binário, certificados e preferências comuns ao Chrome e ao Edge."""
    for argument in _chromium_arguments(options, private_flag):
        target.add_argument(argument)
    if options.binary_path is not None:
        target.binary_location = str(options.binary_path)
    target.accept_insecure_certs = options.accept_insecure_certs
    target.add_experimental_option("prefs", _chromium_preferences(options))


def _chromium_arguments(options: BrowserOptions, private_flag: str) -> list[str]:
    arguments = [*DEFAULT_CHROMIUM_ARGS, *options.arguments]
    if options.headless:
        arguments.append("--headless=new")
        if options.window_size is None:
            arguments.append(f"--window-size={_HEADLESS_WINDOW_SIZE}")
    elif options.maximized:
        arguments.append("--start-maximized")
    if options.window_size is not None:
        width, height = options.window_size
        arguments.append(f"--window-size={width},{height}")
    if options.incognito:
        arguments.append(private_flag)
    if options.user_data_dir is not None:
        arguments.append(f"--user-data-dir={Path(options.user_data_dir).resolve()}")
    return arguments


def _chromium_preferences(options: BrowserOptions) -> dict[str, Any]:
    preferences: dict[str, Any] = {
        "credentials_enable_service": False,
        "profile.password_manager_enabled": False,
    }
    if options.download_dir is not None:
        preferences.update(
            {
                "download.default_directory": str(Path(options.download_dir).resolve()),
                "download.prompt_for_download": False,
                "download.directory_upgrade": True,
                "safebrowsing.enabled": True,
                "plugins.always_open_pdf_externally": True,
            }
        )
    preferences.update(options.preferences)
    return preferences


def _describe(exc: BaseException) -> str:
    """`Classe: primeira linha da mensagem`, sem o stacktrace que o Selenium anexa."""
    text = exc.msg if isinstance(exc, WebDriverException) and exc.msg else str(exc)
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    return f"{type(exc).__name__}: {first_line}"


def _quit_quietly(driver: WebDriver) -> None:
    try:
        driver.quit()
    except Exception as exc:  # limpeza após falha de inicialização
        get_logger().warning("Falha ao encerrar o navegador | erro={}", _describe(exc))


def _single_criterion(**criteria: object) -> str:
    """Valida que exatamente um critério foi informado e o descreve como `nome=valor`."""
    informed = {name: value for name, value in criteria.items() if value is not None}
    if len(informed) != 1:
        names = ", ".join(criteria)
        raise ValueError(f"Informe exatamente um critério entre: {names}")
    ((name, value),) = informed.items()
    return f"{name}={value!r}"


def _window_finder(
    *, index: int | None, title_contains: str | None, handle: str | None
) -> Callable[[WebDriver], str | Literal[False]]:
    """Condição de espera que retorna o handle da janela procurada, ou `False`."""

    def find(driver: WebDriver) -> str | Literal[False]:
        handles = driver.window_handles
        if handle is not None:
            return handle if handle in handles else False
        if index is not None:
            return handles[index] if -len(handles) <= index < len(handles) else False
        return _find_window_by_title(driver, handles, title_contains or "")

    return find


def _find_window_by_title(
    driver: WebDriver, handles: list[str], fragment: str
) -> str | Literal[False]:
    """Percorre as janelas procurando o título; sem sucesso, volta para a janela original."""
    try:
        original: str | None = driver.current_window_handle
    except NoSuchWindowException:
        original = None
    for candidate in handles:
        driver.switch_to.window(candidate)
        if fragment in driver.title:
            return candidate
    if original is not None and original in handles:
        driver.switch_to.window(original)
    return False


def _sanitize_label(label: str) -> str:
    cleaned = _LABEL_INVALID_CHARS.sub("_", label).strip("_")
    return cleaned[:_LABEL_MAX_LENGTH] or "captura"


def _timestamped_png_name(label: str) -> str:
    """`<AAAAMMDD_HHMMSS_fff>_<label>.png` com o horário local atual."""
    now = datetime.now()
    return f"{now:%Y%m%d_%H%M%S}_{now.microsecond // 1000:03d}_{_sanitize_label(label)}.png"


def _entry_names(directory: Path) -> frozenset[str]:
    if not directory.is_dir():
        return frozenset()
    return frozenset(entry.name for entry in directory.iterdir())


def _is_temporary(name: str) -> bool:
    return name.lower().endswith(filesystem_utils.TEMP_SUFFIXES)


def _has_partial_download(path: Path) -> bool:
    """Indica se há um arquivo temporário do mesmo download (ex.: `nome.pdf.part`)."""
    return any(
        path.with_name(path.name + suffix).exists() for suffix in filesystem_utils.TEMP_SUFFIXES
    )


def _new_download_candidate(directory: Path, pattern: str, existing: frozenset[str]) -> Path | None:
    """Arquivo novo e já renomeado para o nome final; o mais recente se houver vários."""
    if not directory.is_dir():
        return None
    candidates: list[tuple[float, Path]] = []
    for item in directory.glob(pattern):
        if item.name in existing or _is_temporary(item.name) or _has_partial_download(item):
            continue
        try:
            info = item.stat()
        except FileNotFoundError:
            continue  # renomeado entre a listagem e a consulta
        if item.is_file():
            candidates.append((info.st_mtime, item))
    return max(candidates)[1] if candidates else None


def _wait_new_download(
    directory: Path, pattern: str, existing: frozenset[str], timeout: float
) -> Path:
    """Aguarda um download novo com `filesystem_utils.wait_for_file` (tamanho estável)."""
    deadline = time.monotonic() + timeout
    minimum_wait = _DOWNLOAD_STABLE_SECONDS + _DOWNLOAD_POLL_INTERVAL
    while True:
        candidate = _new_download_candidate(directory, pattern, existing)
        if candidate is not None:
            remaining = max(deadline - time.monotonic(), minimum_wait)
            try:
                found = filesystem_utils.wait_for_file(
                    directory,
                    glob.escape(candidate.name),
                    timeout=remaining,
                    poll_interval=_DOWNLOAD_POLL_INTERVAL,
                    stable_seconds=_DOWNLOAD_STABLE_SECONDS,
                )
            except FileOperationError as exc:
                raise _download_timeout(directory, pattern, timeout) from exc
            if not _has_partial_download(found):
                return found
        now = time.monotonic()
        if now >= deadline:
            raise _download_timeout(directory, pattern, timeout)
        time.sleep(min(_DOWNLOAD_POLL_INTERVAL, deadline - now))


def _download_timeout(directory: Path, pattern: str, timeout: float) -> DownloadTimeoutError:
    return DownloadTimeoutError(
        "Download não concluído no prazo",
        details={"pasta": str(directory), "padrao": pattern, "timeout": timeout},
    )


BrowserFactory.register(BrowserType.CHROME, ChromeDriverBuilder())
BrowserFactory.register(BrowserType.EDGE, EdgeDriverBuilder())
BrowserFactory.register(BrowserType.EDGE_IE, EdgeIEDriverBuilder())
