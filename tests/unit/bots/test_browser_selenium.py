"""Testes de `rpa_3003.bots.browser_selenium` (RF-026 a RF-033, seção 8.21).

O Selenium é simulado: `webdriver.Chrome`, `webdriver.Edge` e `webdriver.Ie` são substituídos
para capturar as opções, e a fachada `Browser` usa um driver `MagicMock` (seção 12.6).
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, PropertyMock

import pytest
from PIL import Image
from pytest_mock import MockerFixture
from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    InvalidSelectorException,
    NoAlertPresentException,
    NoSuchElementException,
    NoSuchFrameException,
    SessionNotCreatedException,
    TimeoutException,
    UnexpectedTagNameException,
    WebDriverException,
)
from selenium.webdriver.common.keys import Keys

import rpa_3003.bots
from rpa_3003.bots import browser_selenium
from rpa_3003.bots.browser_selenium import (
    DEFAULT_CHROMIUM_ARGS,
    Browser,
    BrowserFactory,
    BrowserOptions,
    BrowserType,
    By,
    ChromeDriverBuilder,
    EdgeDriverBuilder,
    EdgeIEDriverBuilder,
)
from rpa_3003.core.config import get_settings
from rpa_3003.core.exceptions import (
    BrowserError,
    BrowserNotStartedError,
    BrowserNotSupportedError,
    DownloadTimeoutError,
    DriverInitializationError,
    ElementNotFoundError,
)
from rpa_3003.core.logger import MASK

pytestmark = pytest.mark.unit

USER_FIELD = (By.ID, "usuario")
SCREENSHOT_NAME = re.compile(r"^\d{8}_\d{6}_\d{3}_(?P<label>.+)\.png$")


class FakeBuilder:
    """Builder registrado na fábrica que devolve o driver simulado (RF-033)."""

    def __init__(self, driver: MagicMock) -> None:
        self.driver = driver
        self.calls: list[BrowserOptions] = []

    def build(self, options: BrowserOptions) -> MagicMock:
        self.calls.append(options)
        return self.driver


def _write_png(path: str) -> bool:
    Image.new("RGB", (8, 8), (10, 20, 30)).save(path, "PNG")
    return True


@pytest.fixture(autouse=True)
def _restore_registry() -> Iterator[None]:
    saved = dict(BrowserFactory._registry)
    yield
    BrowserFactory._registry.clear()
    BrowserFactory._registry.update(saved)


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


@pytest.fixture
def driver(mocker: MockerFixture) -> MagicMock:
    fake: MagicMock = mocker.MagicMock(name="driver")
    fake.current_url = "http://teste.local/pagina"
    fake.title = "Página de teste"
    fake.window_handles = ["h1"]
    fake.current_window_handle = "h1"
    fake.save_screenshot.side_effect = _write_png
    return fake


@pytest.fixture
def fake_builder(driver: MagicMock) -> FakeBuilder:
    builder = FakeBuilder(driver)
    BrowserFactory.register("fake", builder)
    return builder


@pytest.fixture
def screenshots_dir(tmp_path: Path) -> Path:
    return tmp_path / "logs" / "screenshots"


@pytest.fixture
def browser(fake_builder: FakeBuilder, screenshots_dir: Path) -> Iterator[Browser]:
    instance = Browser(
        "fake", BrowserOptions(default_timeout=0.3), screenshots_dir=screenshots_dir
    ).start()
    yield instance
    instance.quit()


@pytest.fixture
def element(driver: MagicMock) -> MagicMock:
    found: MagicMock = driver.find_element.return_value
    found.is_displayed.return_value = True
    found.is_enabled.return_value = True
    found.text = "Texto exibido"
    return found


# Pacote com importação preguiçosa (8.20)


def test_package_exposes_lazy_attributes() -> None:
    """RNF-017: `rpa_3003.bots` resolve os nomes públicos sob demanda (PEP 562)."""
    assert rpa_3003.bots.Browser is Browser
    assert rpa_3003.bots.BrowserType is BrowserType
    assert set(rpa_3003.bots.__all__) == {
        "Browser",
        "BrowserFactory",
        "BrowserOptions",
        "BrowserType",
        "DesktopBot",
        "Match",
    }
    with pytest.raises(AttributeError, match="no attribute 'Firefox'"):
        _ = rpa_3003.bots.Firefox


# BrowserOptions.from_settings


@pytest.mark.parametrize(
    ("browser_type", "expected_driver", "expected_binary"),
    [
        (BrowserType.CHROME, "chromedriver.exe", None),
        ("EDGE", "msedgedriver.exe", None),
        (" edge_ie ", "IEDriverServer.exe", "msedge.exe"),
        ("fake", None, None),
    ],
)
def test_options_from_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    browser_type: str,
    expected_driver: str | None,
    expected_binary: str | None,
) -> None:
    """Seção 8.21: headless, timeouts, driver_path por tipo e binary_path (edge_ie) das settings."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RPA3003_BROWSER_HEADLESS", "true")
    monkeypatch.setenv("RPA3003_SELENIUM_TIMEOUT", "12")
    monkeypatch.setenv("RPA3003_PAGE_LOAD_TIMEOUT", "90")
    monkeypatch.setenv("RPA3003_CHROMEDRIVER_PATH", "chromedriver.exe")
    monkeypatch.setenv("RPA3003_EDGEDRIVER_PATH", "msedgedriver.exe")
    monkeypatch.setenv("RPA3003_IEDRIVER_PATH", "IEDriverServer.exe")
    monkeypatch.setenv("RPA3003_EDGE_BINARY_PATH", "msedge.exe")
    options = BrowserOptions.from_settings(get_settings(), browser_type)
    assert options.headless is True
    assert options.default_timeout == 12.0
    assert options.page_load_timeout == 90
    assert options.driver_path == (Path(expected_driver) if expected_driver else None)
    assert options.binary_path == (Path(expected_binary) if expected_binary else None)
    assert options.maximized is True
    assert options.download_dir is None


def test_browser_without_options_uses_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_builder: FakeBuilder
) -> None:
    """Seção 8.21: `Browser(options=None)` usa `BrowserOptions.from_settings`."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RPA3003_SELENIUM_TIMEOUT", "7")
    monkeypatch.setenv("RPA3003_BROWSER_HEADLESS", "1")
    with Browser("fake") as instance:
        assert instance.browser_type == "fake"
    assert fake_builder.calls[0].default_timeout == 7.0
    assert fake_builder.calls[0].headless is True


# Builders (RF-027 a RF-029)


@pytest.fixture
def chrome_class(mocker: MockerFixture) -> MagicMock:
    return mocker.patch("selenium.webdriver.Chrome")


@pytest.fixture
def edge_class(mocker: MockerFixture) -> MagicMock:
    return mocker.patch("selenium.webdriver.Edge")


def test_chrome_builder_headless_and_download_preferences(
    chrome_class: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RF-027: opções com `--headless=new` e `download.default_directory` absoluto."""
    monkeypatch.chdir(tmp_path)
    options = BrowserOptions(
        headless=True,
        download_dir=Path("saida"),
        arguments=["--lang=pt-BR"],
        preferences={"download.prompt_for_download": True, "intl.accept_languages": "pt-BR"},
        accept_insecure_certs=True,
        binary_path=Path("C:/Chrome/chrome.exe"),
        driver_path=Path("C:/drivers/chromedriver.exe"),
    )
    driver = ChromeDriverBuilder().build(options)
    assert driver is chrome_class.return_value
    chrome_options = chrome_class.call_args.kwargs["options"]
    service = chrome_class.call_args.kwargs["service"]
    assert isinstance(chrome_options, webdriver.ChromeOptions)
    assert chrome_options.arguments == [
        *DEFAULT_CHROMIUM_ARGS,
        "--lang=pt-BR",
        "--headless=new",
        "--window-size=1920,1080",
    ]
    prefs = chrome_options.experimental_options["prefs"]
    assert prefs["download.default_directory"] == str((tmp_path / "saida").resolve())
    assert prefs["download.directory_upgrade"] is True
    assert prefs["safebrowsing.enabled"] is True
    assert prefs["plugins.always_open_pdf_externally"] is True
    assert prefs["credentials_enable_service"] is False
    assert prefs["profile.password_manager_enabled"] is False
    # options.preferences são mescladas por último
    assert prefs["download.prompt_for_download"] is True
    assert prefs["intl.accept_languages"] == "pt-BR"
    assert chrome_options.accept_insecure_certs is True
    assert chrome_options.binary_location == str(Path("C:/Chrome/chrome.exe"))
    assert service.path == str(Path("C:/drivers/chromedriver.exe"))


def test_chrome_builder_window_modes_and_profile(chrome_class: MagicMock, tmp_path: Path) -> None:
    """RF-027: maximizado, tamanho de janela, modo anônimo e `user_data_dir` absoluto."""
    ChromeDriverBuilder().build(BrowserOptions())
    default_options = chrome_class.call_args.kwargs["options"]
    assert default_options.arguments == [*DEFAULT_CHROMIUM_ARGS, "--start-maximized"]
    assert "download.default_directory" not in default_options.experimental_options["prefs"]
    assert chrome_class.call_args.kwargs["service"].path == ""  # Selenium Manager

    ChromeDriverBuilder().build(
        BrowserOptions(
            maximized=False,
            window_size=(1280, 720),
            incognito=True,
            user_data_dir=tmp_path / "perfil",
        )
    )
    arguments = chrome_class.call_args.kwargs["options"].arguments
    assert "--start-maximized" not in arguments
    assert "--window-size=1280,720" in arguments
    assert "--incognito" in arguments
    assert f"--user-data-dir={(tmp_path / 'perfil').resolve()}" in arguments

    ChromeDriverBuilder().build(BrowserOptions(headless=True, window_size=(800, 600)))
    headless_arguments = chrome_class.call_args.kwargs["options"].arguments
    assert "--window-size=800,600" in headless_arguments
    assert "--window-size=1920,1080" not in headless_arguments


def test_edge_builder_uses_edge_options(edge_class: MagicMock, tmp_path: Path) -> None:
    """RF-028: as mesmas opções do RF-027 em `EdgeOptions`, com `--inprivate`."""
    options = BrowserOptions(
        headless=True,
        incognito=True,
        download_dir=tmp_path / "downloads",
        driver_path=Path("C:/drivers/msedgedriver.exe"),
    )
    driver = EdgeDriverBuilder().build(options)
    assert driver is edge_class.return_value
    edge_options = edge_class.call_args.kwargs["options"]
    assert isinstance(edge_options, webdriver.EdgeOptions)
    assert "--headless=new" in edge_options.arguments
    assert "--inprivate" in edge_options.arguments
    assert "--incognito" not in edge_options.arguments
    prefs = edge_options.experimental_options["prefs"]
    assert prefs["download.default_directory"] == str((tmp_path / "downloads").resolve())
    assert edge_class.call_args.kwargs["service"].path == str(Path("C:/drivers/msedgedriver.exe"))
    EdgeDriverBuilder().build(BrowserOptions())
    assert edge_class.call_args.kwargs["service"].path == ""


@pytest.fixture
def program_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    """`ProgramFiles(x86)` e `ProgramFiles` apontando para pastas vazias em `tmp_path`."""
    x86 = tmp_path / "Arquivos de Programas (x86)"
    x64 = tmp_path / "Arquivos de Programas"
    monkeypatch.setenv("ProgramFiles(x86)", str(x86))
    monkeypatch.setenv("ProgramFiles", str(x64))
    relative = Path("Microsoft", "Edge", "Application", "msedge.exe")
    return SimpleNamespace(x86=x86 / relative, x64=x64 / relative)


def _create_file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ")
    return path


def test_edge_ie_builder_options(
    mocker: MockerFixture, program_dirs: SimpleNamespace, log_messages: list[tuple[str, str]]
) -> None:
    """RF-029: `IeOptions` com `attach_to_edge_chrome=True` e o caminho do Edge detectado."""
    edge = _create_file(program_dirs.x86)
    ie_class = mocker.patch("selenium.webdriver.Ie")
    options = BrowserOptions(
        headless=True,
        download_dir=Path("downloads"),
        ie_ignore_zoom_level=False,
        ie_ignore_protected_mode_settings=True,
        ie_require_window_focus=True,
        ie_ensure_clean_session=False,
        ie_initial_url="http://legado.interno/",
        driver_path=Path("C:/drivers/IEDriverServer.exe"),
    )
    assert EdgeIEDriverBuilder().build(options) is ie_class.return_value
    ie_options = ie_class.call_args.kwargs["options"]
    assert isinstance(ie_options, webdriver.IeOptions)
    assert ie_options.attach_to_edge_chrome is True
    assert ie_options.edge_executable_path == str(edge.resolve())
    assert ie_options.ignore_zoom_level is False
    assert ie_options.ignore_protected_mode_settings is True
    assert ie_options.require_window_focus is True
    assert ie_options.ensure_clean_session is False
    assert ie_options.initial_browser_url == "http://legado.interno/"
    assert ie_options.browser_attach_timeout == 60_000  # RNF-007: sem espera infinita
    assert ie_class.call_args.kwargs["service"].path == str(Path("C:/drivers/IEDriverServer.exe"))
    warnings = [message for level, message in log_messages if level == "WARNING"]
    assert any("headless" in message for message in warnings)
    assert any("download_dir" in message for message in warnings)


def test_edge_ie_builder_defaults(mocker: MockerFixture, program_dirs: SimpleNamespace) -> None:
    """RF-029: padrões das opções do modo IE e IEDriverServer pelo Selenium Manager."""
    _create_file(program_dirs.x64)
    ie_class = mocker.patch("selenium.webdriver.Ie")
    EdgeIEDriverBuilder().build(BrowserOptions())
    ie_options = ie_class.call_args.kwargs["options"]
    assert ie_options.ignore_zoom_level is True
    assert ie_options.ignore_protected_mode_settings is False
    assert ie_options.require_window_focus is False
    assert ie_options.ensure_clean_session is True
    assert ie_options.initial_browser_url == "about:blank"
    assert ie_options.browser_attach_timeout == 60_000
    assert ie_class.call_args.kwargs["service"].path == ""


def test_find_edge_executable_order(program_dirs: SimpleNamespace, tmp_path: Path) -> None:
    """RF-029: explicit → ProgramFiles(x86) → ProgramFiles."""
    x64 = _create_file(program_dirs.x64)
    assert EdgeIEDriverBuilder.find_edge_executable() == x64.resolve()
    x86 = _create_file(program_dirs.x86)
    assert EdgeIEDriverBuilder.find_edge_executable() == x86.resolve()
    explicit = _create_file(tmp_path / "Edge Portátil" / "msedge.exe")
    assert EdgeIEDriverBuilder.find_edge_executable(explicit) == explicit.resolve()


def test_find_edge_executable_missing_explicit_falls_back(
    program_dirs: SimpleNamespace, tmp_path: Path, log_messages: list[tuple[str, str]]
) -> None:
    """RF-029: caminho configurado inexistente gera WARNING e a busca continua."""
    x64 = _create_file(program_dirs.x64)
    found = EdgeIEDriverBuilder.find_edge_executable(tmp_path / "nao_existe.exe")
    assert found == x64.resolve()
    assert any(level == "WARNING" and "nao_existe.exe" in m for level, m in log_messages)


def test_find_edge_executable_lists_searched_paths(
    program_dirs: SimpleNamespace, tmp_path: Path
) -> None:
    """RF-029: sem msedge.exe, a mensagem de DriverInitializationError lista os caminhos."""
    explicit = tmp_path / "config" / "msedge.exe"
    with pytest.raises(DriverInitializationError) as info:
        EdgeIEDriverBuilder.find_edge_executable(explicit)
    message = info.value.message
    assert "msedge.exe não encontrado" in message
    for path in (explicit, program_dirs.x86, program_dirs.x64):
        assert str(path) in message


def test_find_edge_executable_without_program_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """RF-029: sem variáveis de ambiente nem caminho explícito, nenhum caminho é pesquisado."""
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.delenv("ProgramFiles", raising=False)
    with pytest.raises(DriverInitializationError, match="pesquisados: nenhum"):
        EdgeIEDriverBuilder.find_edge_executable()


# Fábrica e registro (RF-033)


def test_factory_registers_default_browsers() -> None:
    """RF-033: chrome, edge e edge_ie registrados na importação do módulo."""
    assert BrowserFactory.available() == ["chrome", "edge", "edge_ie"]
    assert isinstance(BrowserFactory._registry["chrome"], ChromeDriverBuilder)
    assert isinstance(BrowserFactory._registry["edge"], EdgeDriverBuilder)
    assert isinstance(BrowserFactory._registry["edge_ie"], EdgeIEDriverBuilder)


def test_factory_register_rules(driver: MagicMock) -> None:
    """RF-033: nome normalizado; duplicado sem overwrite gera ValueError; overwrite substitui."""
    first, second = FakeBuilder(driver), FakeBuilder(driver)
    BrowserFactory.register(" Firefox ", first)
    assert "firefox" in BrowserFactory.available()
    with pytest.raises(ValueError, match="já registrado: firefox"):
        BrowserFactory.register("FIREFOX", second)
    BrowserFactory.register("firefox", second, overwrite=True)
    assert BrowserFactory.create_driver("FireFox", BrowserOptions()) is driver
    assert second.calls and not first.calls
    with pytest.raises(ValueError, match="vazio"):
        BrowserFactory.register("  ", first)


def test_registered_fake_builder_is_used_by_browser(fake_builder: FakeBuilder) -> None:
    """RF-033: dado um builder falso registrado como `fake`, `Browser("fake").start()` o usa."""
    options = BrowserOptions(headless=True)
    instance = Browser("fake", options).start()
    try:
        assert fake_builder.calls == [options]
        assert instance.driver is fake_builder.driver
    finally:
        instance.quit()


def test_unknown_browser_lists_available() -> None:
    """RF-033: `Browser("opera").start()` lança BrowserNotSupportedError com a lista."""
    with pytest.raises(BrowserNotSupportedError) as info:
        Browser("opera", BrowserOptions()).start()
    assert "chrome, edge, edge_ie" in str(info.value)
    assert info.value.exit_code == 3


# Ciclo de vida (RF-026)


def test_start_configures_driver_and_logs(
    fake_builder: FakeBuilder,
    driver: MagicMock,
    tmp_path: Path,
    log_messages: list[tuple[str, str]],
) -> None:
    """Seção 8.21: page_load_timeout, espera implícita 0, pasta de downloads e log de início."""
    download_dir = tmp_path / "data" / "output"
    instance = Browser("fake", BrowserOptions(page_load_timeout=45, download_dir=download_dir))
    assert instance.start() is instance
    driver.set_page_load_timeout.assert_called_once_with(45)
    driver.implicitly_wait.assert_called_once_with(0)
    driver.maximize_window.assert_not_called()  # Chrome/Edge usam --start-maximized
    driver.set_window_size.assert_not_called()
    assert download_dir.is_dir()
    assert instance.is_running
    assert ("INFO", "Navegador iniciado | tipo=fake | headless=False") in log_messages
    instance.start()  # segunda chamada não cria outro driver
    assert len(fake_builder.calls) == 1
    instance.quit()


def test_start_window_size_and_ie_maximize(driver: MagicMock) -> None:
    """Seção 8.21: window_size → set_window_size; edge_ie maximizado → maximize_window()."""
    BrowserFactory.register("edge_ie", FakeBuilder(driver), overwrite=True)
    BrowserFactory.register("fake", FakeBuilder(driver))
    Browser("fake", BrowserOptions(window_size=(1024, 768))).start().quit()
    driver.set_window_size.assert_called_once_with(1024, 768)
    Browser("fake", BrowserOptions(headless=True, window_size=(800, 600))).start().quit()
    driver.set_window_size.assert_called_once()  # headless: o argumento já define o tamanho
    Browser("edge_ie", BrowserOptions()).start().quit()
    driver.maximize_window.assert_called_once_with()


def test_edge_ie_ignores_headless_in_log(
    driver: MagicMock, log_messages: list[tuple[str, str]]
) -> None:
    """RF-029: headless não suportado no modo IE; a janela é maximizada normalmente."""
    BrowserFactory.register("edge_ie", FakeBuilder(driver), overwrite=True)
    Browser("edge_ie", BrowserOptions(headless=True)).start().quit()
    driver.maximize_window.assert_called_once_with()
    assert ("INFO", "Navegador iniciado | tipo=edge_ie | headless=False") in log_messages


def test_start_failure_raises_driver_initialization_error(driver: MagicMock) -> None:
    """Seção 8.21: WebDriverException na criação vira DriverInitializationError."""

    class FailingBuilder:
        def build(self, options: BrowserOptions) -> Any:
            raise SessionNotCreatedException("session not created\nStacktrace: ...")

    BrowserFactory.register("fake", FailingBuilder())
    with pytest.raises(DriverInitializationError) as info:
        Browser("fake", BrowserOptions()).start()
    assert info.value.details["tipo"] == "fake"
    assert info.value.details["erro"] == "SessionNotCreatedException: session not created"
    assert "dica" not in info.value.details

    BrowserFactory.register("edge_ie", FailingBuilder(), overwrite=True)
    with pytest.raises(DriverInitializationError) as ie_info:
        Browser("edge_ie", BrowserOptions()).start()
    assert "zoom 100%" in ie_info.value.details["dica"]
    assert "IEDriverServer 32 bits" in ie_info.value.details["dica"]


def test_start_configuration_failure_quits_driver(
    fake_builder: FakeBuilder, driver: MagicMock
) -> None:
    """RNF-006: falha ao configurar o driver encerra o processo antes de lançar o erro."""
    driver.set_page_load_timeout.side_effect = WebDriverException("sessão perdida")
    instance = Browser("fake", BrowserOptions())
    with pytest.raises(DriverInitializationError):
        instance.start()
    driver.quit.assert_called_once_with()
    assert not instance.is_running


def test_driver_before_start_raises(fake_builder: FakeBuilder) -> None:
    """Seção 8.21: `driver` antes de `start()` lança BrowserNotStartedError."""
    instance = Browser("fake", BrowserOptions())
    assert not instance.is_running
    with pytest.raises(BrowserNotStartedError):
        _ = instance.driver
    with pytest.raises(BrowserNotStartedError):
        instance.open("http://teste.local")


def test_quit_is_idempotent_and_tolerant(
    browser: Browser, driver: MagicMock, log_messages: list[tuple[str, str]]
) -> None:
    """RF-026: dois `quit()` consecutivos não lançam exceção; falha do driver vira WARNING."""
    driver.quit.side_effect = WebDriverException("driver já encerrado")
    browser.quit()
    browser.quit()
    driver.quit.assert_called_once_with()
    assert not browser.is_running
    assert any(level == "WARNING" and "driver já encerrado" in m for level, m in log_messages)
    assert ("INFO", "Navegador encerrado") in log_messages


@pytest.mark.parametrize("browser_type", [BrowserType.CHROME, BrowserType.EDGE])
def test_same_script_runs_on_chrome_and_edge(
    mocker: MockerFixture, driver: MagicMock, element: MagicMock, browser_type: BrowserType
) -> None:
    """RF-026: o mesmo roteiro executa com CHROME e EDGE alterando apenas o construtor."""
    chrome = mocker.patch("selenium.webdriver.Chrome", return_value=driver)
    edge = mocker.patch("selenium.webdriver.Edge", return_value=driver)
    with Browser(browser_type, BrowserOptions(headless=True, default_timeout=1)) as instance:
        instance.open("http://teste.local/login")
        instance.type_text(USER_FIELD, "robo01")
        instance.click((By.ID, "entrar"))
        assert instance.get_text((By.ID, "resultado")) == "Texto exibido"
        assert instance.browser_type == browser_type.value
    used, unused = (chrome, edge) if browser_type is BrowserType.CHROME else (edge, chrome)
    used.assert_called_once()
    unused.assert_not_called()
    driver.get.assert_called_once_with("http://teste.local/login")
    driver.quit.assert_called_once_with()


def test_context_manager_saves_error_screenshot_before_quit(
    fake_builder: FakeBuilder, driver: MagicMock, screenshots_dir: Path
) -> None:
    """RF-026/RF-032: com exceção no `with`, `*_erro.png` existe antes do `quit()` único."""
    seen_at_quit: list[list[Path]] = []
    driver.quit.side_effect = lambda: seen_at_quit.append(
        sorted(screenshots_dir.glob("*_erro.png"))
    )
    with (
        pytest.raises(RuntimeError, match="falha no robô"),
        Browser("fake", BrowserOptions(), screenshots_dir=screenshots_dir),
    ):
        raise RuntimeError("falha no robô")
    driver.quit.assert_called_once_with()
    assert len(seen_at_quit[0]) == 1
    with Image.open(seen_at_quit[0][0]) as image:
        assert image.format == "PNG"


def test_context_manager_without_error_or_disabled_has_no_screenshot(
    fake_builder: FakeBuilder, driver: MagicMock, screenshots_dir: Path
) -> None:
    """RF-032: sem exceção, ou com `screenshot_on_error=False`, nenhuma captura é salva."""
    with Browser("fake", BrowserOptions(), screenshots_dir=screenshots_dir):
        pass
    with (
        pytest.raises(ValueError, match="erro"),
        Browser(
            "fake", BrowserOptions(), screenshots_dir=screenshots_dir, screenshot_on_error=False
        ),
    ):
        raise ValueError("erro")
    driver.save_screenshot.assert_not_called()
    assert driver.quit.call_count == 2


def test_error_screenshot_failure_keeps_original_exception(
    fake_builder: FakeBuilder, driver: MagicMock, log_messages: list[tuple[str, str]]
) -> None:
    """Seção 8.21: falha na captura de erro vira WARNING e a exceção original continua."""
    driver.save_screenshot.side_effect = WebDriverException("janela fechada")
    with pytest.raises(KeyError), Browser("fake", BrowserOptions()):
        raise KeyError("original")
    driver.quit.assert_called_once_with()
    assert any(
        level == "WARNING" and "screenshot de erro" in message and "janela fechada" in message
        for level, message in log_messages
    )


# Navegação


def test_navigation_methods(
    browser: Browser, driver: MagicMock, log_messages: list[tuple[str, str]]
) -> None:
    """Seção 8.21: open registra `Abrindo URL`; propriedades e histórico delegam ao driver."""
    browser.open("http://teste.local/inicio")
    driver.get.assert_called_once_with("http://teste.local/inicio")
    assert ("INFO", "Abrindo URL: http://teste.local/inicio") in log_messages
    assert browser.current_url == "http://teste.local/pagina"
    assert browser.title == "Página de teste"
    browser.refresh()
    browser.back()
    browser.forward()
    driver.refresh.assert_called_once_with()
    driver.back.assert_called_once_with()
    driver.forward.assert_called_once_with()


def test_open_page_load_timeout(browser: Browser, driver: MagicMock) -> None:
    """RNF-007: carregamento acima de `page_load_timeout` vira BrowserError com a URL."""
    driver.get.side_effect = TimeoutException("timeout")
    with pytest.raises(BrowserError, match="carregamento") as info:
        browser.open("http://lento.local")
    assert info.value.details == {"url": "http://lento.local", "timeout": 60}


# Localização e espera (RF-030)


def test_find_waits_for_presence(
    browser: Browser, driver: MagicMock, element: MagicMock, no_sleep: list[float]
) -> None:
    """RF-030: elemento que aparece após algumas verificações é encontrado no prazo."""
    driver.find_element.side_effect = [NoSuchElementException(), NoSuchElementException(), element]
    assert browser.find(USER_FIELD, timeout=5) is element
    assert driver.find_element.call_count == 3
    assert no_sleep == [0.25, 0.25]


def test_find_timeout_raises_element_not_found(browser: Browser, driver: MagicMock) -> None:
    """RF-030: tempo esgotado vira ElementNotFoundError com localizador, timeout e URL."""
    driver.find_element.side_effect = NoSuchElementException()
    started = time.monotonic()
    with pytest.raises(ElementNotFoundError) as info:
        browser.find(USER_FIELD, timeout=0.2)
    assert time.monotonic() - started < 1.0
    assert info.value.message == "Elemento não encontrado: ('id', 'usuario') em 0.2s"
    assert info.value.details == {"url": "http://teste.local/pagina"}


def test_waits_use_default_timeout(browser: Browser, driver: MagicMock) -> None:
    """RF-030: sem `timeout`, a espera usa `BrowserOptions.default_timeout`."""
    driver.find_element.side_effect = NoSuchElementException()
    with pytest.raises(ElementNotFoundError, match=r"em 0\.3s"):
        browser.wait_visible(USER_FIELD)


def test_error_url_unavailable(browser: Browser, driver: MagicMock) -> None:
    """Seção 8.21: sem URL disponível (ex.: alerta aberto), details traz 'desconhecida'."""
    driver.find_element.side_effect = NoSuchElementException()
    type(driver).current_url = PropertyMock(side_effect=WebDriverException("alerta aberto"))
    with pytest.raises(ElementNotFoundError) as info:
        browser.find(USER_FIELD, timeout=0)
    assert info.value.details == {"url": "desconhecida"}


def test_find_all_and_exists(browser: Browser, driver: MagicMock) -> None:
    """Seção 8.21: find_all retorna [] no timeout; exists nunca lança."""
    first, second = MagicMock(), MagicMock()
    driver.find_elements.return_value = [first, second]
    assert browser.find_all((By.CSS_SELECTOR, "tr"), timeout=0) == [first, second]
    driver.find_elements.return_value = []
    assert browser.find_all((By.CSS_SELECTOR, "tr"), timeout=0) == []
    driver.find_element.side_effect = [MagicMock()]
    assert browser.exists(USER_FIELD) is True
    driver.find_element.side_effect = NoSuchElementException()
    assert browser.exists(USER_FIELD) is False
    driver.find_element.side_effect = InvalidSelectorException("seletor inválido")
    assert browser.exists((By.XPATH, "//[")) is False


def test_wait_visible_and_clickable(browser: Browser, element: MagicMock) -> None:
    """Seção 8.21: visibilidade e clicabilidade conforme `expected_conditions`."""
    assert browser.wait_visible(USER_FIELD) is element
    assert browser.wait_clickable(USER_FIELD) is element
    element.is_enabled.return_value = False
    with pytest.raises(ElementNotFoundError):
        browser.wait_clickable(USER_FIELD, timeout=0)
    element.is_displayed.return_value = False
    with pytest.raises(ElementNotFoundError):
        browser.wait_visible(USER_FIELD, timeout=0)


def test_wait_invisible(browser: Browser, driver: MagicMock, element: MagicMock) -> None:
    """Seção 8.21: elemento ausente conta como invisível; visível até o fim do prazo falha."""
    with pytest.raises(ElementNotFoundError, match=r"continua visível: \('id', 'usuario'\)"):
        browser.wait_invisible(USER_FIELD, timeout=0)
    driver.find_element.side_effect = NoSuchElementException()
    browser.wait_invisible(USER_FIELD, timeout=0)


def test_wait_url_contains(browser: Browser, driver: MagicMock) -> None:
    """Seção 8.21: aguarda trecho na URL; prazo esgotado vira ElementNotFoundError."""
    browser.wait_url_contains("/pagina", timeout=0)
    with pytest.raises(ElementNotFoundError, match="URL não contém '/fim'"):
        browser.wait_url_contains("/fim", timeout=0)


# Interação


def test_click_native(
    browser: Browser, driver: MagicMock, element: MagicMock, log_messages: list[tuple[str, str]]
) -> None:
    """RF-030: clique nativo após esperar o elemento clicável; log DEBUG `Clique em`."""
    browser.click((By.ID, "entrar"))
    element.click.assert_called_once_with()
    driver.execute_script.assert_not_called()
    assert ("DEBUG", "Clique em ('id', 'entrar')") in log_messages


def test_click_intercepted_uses_javascript(
    browser: Browser, driver: MagicMock, element: MagicMock
) -> None:
    """RF-030: clique interceptado com `js_fallback` usa `arguments[0].click();`."""
    element.click.side_effect = ElementClickInterceptedException("sobreposto")
    browser.click((By.ID, "entrar"))
    driver.execute_script.assert_called_once_with("arguments[0].click();", element)


def test_click_intercepted_without_fallback(browser: Browser, element: MagicMock) -> None:
    """RF-030: sem `js_fallback`, o clique interceptado vira BrowserError."""
    element.click.side_effect = ElementClickInterceptedException("sobreposto")
    with pytest.raises(BrowserError, match="Clique interceptado"):
        browser.click((By.ID, "entrar"), js_fallback=False)


def test_type_text(
    browser: Browser, element: MagicMock, log_messages: list[tuple[str, str]]
) -> None:
    """Seção 8.21: limpa, digita, Enter opcional e log DEBUG com o texto."""
    browser.type_text(USER_FIELD, "Ação 1", press_enter=True)
    element.clear.assert_called_once_with()
    assert [c.args for c in element.send_keys.call_args_list] == [("Ação 1",), (Keys.ENTER,)]
    assert ("DEBUG", "Digitando em ('id', 'usuario'): Ação 1") in log_messages
    element.reset_mock()
    browser.type_text(USER_FIELD, "x", clear=False)
    element.clear.assert_not_called()


def test_type_text_sensitive_is_masked(
    browser: Browser, element: MagicMock, log_messages: list[tuple[str, str]]
) -> None:
    """RF-018: `type_text(..., sensitive=True)` mostra `********` e registra o segredo."""
    from rpa_3003.core.logger import get_logger

    browser.type_text((By.ID, "senha"), "S3nh@Forte", sensitive=True)
    element.send_keys.assert_called_once_with("S3nh@Forte")
    assert ("DEBUG", f"Digitando em ('id', 'senha'): {MASK}") in log_messages
    get_logger().info("senha digitada: S3nh@Forte")
    assert ("INFO", f"senha digitada: {MASK}") in log_messages
    assert all("S3nh@Forte" not in message for _level, message in log_messages)


def test_get_text_and_attribute(browser: Browser, element: MagicMock) -> None:
    """Seção 8.21: texto visível e atributo do elemento."""
    element.get_attribute.return_value = "robo01"
    assert browser.get_text(USER_FIELD) == "Texto exibido"
    assert browser.get_attribute(USER_FIELD, "value") == "robo01"
    element.get_attribute.assert_called_once_with("value")


@pytest.mark.parametrize(
    "criteria",
    [{}, {"text": "A", "value": "a"}, {"text": "A", "index": 0}, {"value": "a", "index": 1}],
)
def test_select_option_requires_exactly_one_criterion(
    browser: Browser, criteria: dict[str, Any]
) -> None:
    """RF-030: `select_option(loc, text="A", value="a")` e demais combinações → ValueError."""
    with pytest.raises(ValueError, match="exatamente um critério"):
        browser.select_option((By.ID, "filial"), **criteria)


@pytest.mark.parametrize(
    ("criteria", "method", "argument"),
    [
        ({"text": "São Paulo"}, "select_by_visible_text", "São Paulo"),
        ({"value": "sp"}, "select_by_value", "sp"),
        ({"index": 0}, "select_by_index", 0),
    ],
)
def test_select_option_uses_select(
    browser: Browser,
    element: MagicMock,
    mocker: MockerFixture,
    criteria: dict[str, Any],
    method: str,
    argument: object,
) -> None:
    """Seção 8.21: `select_option` usa `Select` com o critério informado."""
    select_class = mocker.patch.object(browser_selenium, "Select")
    browser.select_option((By.ID, "filial"), **criteria)
    select_class.assert_called_once_with(element)
    getattr(select_class.return_value, method).assert_called_once_with(argument)


def test_select_option_errors(browser: Browser, element: MagicMock, mocker: MockerFixture) -> None:
    """Seção 8.21: opção inexistente → ElementNotFoundError; não `<select>` → BrowserError."""
    select_class = mocker.patch.object(browser_selenium, "Select")
    select_class.return_value.select_by_value.side_effect = NoSuchElementException()
    with pytest.raises(ElementNotFoundError, match="Opção não encontrada") as info:
        browser.select_option((By.ID, "filial"), value="mg")
    assert info.value.details["criterio"] == "value='mg'"
    select_class.side_effect = UnexpectedTagNameException("div")
    with pytest.raises(BrowserError, match="não é um <select>"):
        browser.select_option((By.ID, "filial"), index=1)


def test_scroll_and_execute_script(browser: Browser, driver: MagicMock, element: MagicMock) -> None:
    """Seção 8.21: rolagem via JavaScript e execução de script com argumentos."""
    browser.scroll_into_view(USER_FIELD)
    script, target = driver.execute_script.call_args.args
    assert "scrollIntoView" in script
    assert target is element
    driver.execute_script.return_value = 11
    assert browser.execute_script("return document.documentMode", 1, "a") == 11
    driver.execute_script.assert_called_with("return document.documentMode", 1, "a")


# Frames, janelas e alertas


def test_switch_to_frame(browser: Browser, driver: MagicMock, element: MagicMock) -> None:
    """Seção 8.21: localizador → espera o frame; índice/nome → `driver.switch_to.frame`."""
    browser.switch_to_frame((By.ID, "quadro"))
    driver.switch_to.frame.assert_called_once_with(element)
    browser.switch_to_frame(0)
    browser.switch_to_frame("quadro")
    assert driver.switch_to.frame.call_args_list[1:] == [((0,),), (("quadro",),)]
    browser.switch_to_default_content()
    driver.switch_to.default_content.assert_called_once_with()


def test_switch_to_frame_not_found(browser: Browser, driver: MagicMock) -> None:
    """Seção 8.21: frame inexistente vira ElementNotFoundError."""
    driver.switch_to.frame.side_effect = NoSuchFrameException("sem frame")
    with pytest.raises(ElementNotFoundError, match="Frame não encontrado: 'menu'"):
        browser.switch_to_frame("menu")
    driver.find_element.side_effect = NoSuchElementException()
    with pytest.raises(ElementNotFoundError, match=r"\('id', 'quadro'\)"):
        browser.switch_to_frame((By.ID, "quadro"), timeout=0)


@pytest.fixture
def windows(driver: MagicMock) -> SimpleNamespace:
    """Três janelas com títulos distintos; `switch_to.window` muda a janela atual."""
    state = SimpleNamespace(current="h1", titles={"h1": "Início", "h2": "Relatório", "h3": "Ajuda"})
    driver.window_handles = ["h1", "h2", "h3"]
    driver.switch_to.window.side_effect = lambda handle: setattr(state, "current", handle)
    type(driver).title = PropertyMock(side_effect=lambda: state.titles[state.current])
    type(driver).current_window_handle = PropertyMock(side_effect=lambda: state.current)
    return state


def test_switch_to_window_criteria(
    browser: Browser, driver: MagicMock, windows: SimpleNamespace
) -> None:
    """Seção 8.21: seleção por índice, título ou handle; retorna o handle selecionado."""
    assert browser.switch_to_window(index=-1) == "h3"
    assert windows.current == "h3"
    assert browser.switch_to_window(title_contains="Relat") == "h2"
    assert windows.current == "h2"
    assert browser.switch_to_window(handle="h1") == "h1"
    assert windows.current == "h1"


def test_switch_to_window_not_found_restores_original(
    browser: Browser, windows: SimpleNamespace
) -> None:
    """Seção 8.21: janela não encontrada → BrowserError; a janela atual é preservada."""
    windows.current = "h2"
    with pytest.raises(BrowserError, match="Janela não encontrada") as info:
        browser.switch_to_window(title_contains="Inexistente", timeout=0)
    assert info.value.details["criterio"] == "title_contains='Inexistente'"
    assert windows.current == "h2"
    with pytest.raises(BrowserError):
        browser.switch_to_window(index=5, timeout=0)
    with pytest.raises(BrowserError):
        browser.switch_to_window(handle="h9", timeout=0)


@pytest.mark.parametrize("criteria", [{}, {"index": 0, "handle": "h1"}])
def test_switch_to_window_requires_one_criterion(
    browser: Browser, criteria: dict[str, Any]
) -> None:
    """Seção 8.21: `switch_to_window` exige exatamente um critério."""
    with pytest.raises(ValueError, match="exatamente um critério"):
        browser.switch_to_window(**criteria)


def test_new_tab_and_close_tab(browser: Browser, driver: MagicMock) -> None:
    """Seção 8.21: nova aba (com URL opcional) e fechamento voltando à última aba."""
    driver.current_window_handle = "h2"
    assert browser.new_tab("http://teste.local/nova") == "h2"
    driver.switch_to.new_window.assert_called_once_with("tab")
    driver.get.assert_called_once_with("http://teste.local/nova")
    driver.window_handles = ["h1", "h3"]
    browser.close_tab()
    driver.close.assert_called_once_with()
    driver.switch_to.window.assert_called_once_with("h3")
    assert browser.new_tab() == "h2"
    assert driver.get.call_count == 1


def test_close_last_tab(browser: Browser, driver: MagicMock) -> None:
    """Seção 8.21: ao fechar a última aba não há janela para onde mudar."""
    type(driver).window_handles = PropertyMock(side_effect=WebDriverException("sem sessão"))
    browser.close_tab()
    driver.switch_to.window.assert_not_called()


def test_handle_alert(browser: Browser, driver: MagicMock) -> None:
    """Seção 8.21: retorna o texto do alerta; `text` é digitado antes de responder."""
    alert = driver.switch_to.alert
    alert.text = "Confirma o envio?"
    assert browser.handle_alert(text="sim") == "Confirma o envio?"
    alert.send_keys.assert_called_once_with("sim")
    alert.accept.assert_called_once_with()
    assert browser.handle_alert(accept=False) == "Confirma o envio?"
    alert.dismiss.assert_called_once_with()


def test_handle_alert_absent_returns_none(browser: Browser, driver: MagicMock) -> None:
    """Seção 8.21: sem alerta no prazo, retorna None."""
    type(driver.switch_to).alert = PropertyMock(side_effect=NoAlertPresentException())
    assert browser.handle_alert(timeout=0) is None


# Evidências, downloads e cookies (RF-031, RF-032)


def test_screenshot_default_name_and_log(
    browser: Browser, screenshots_dir: Path, log_messages: list[tuple[str, str]]
) -> None:
    """RF-032: PNG em `screenshots_dir` com `<AAAAMMDD_HHMMSS_fff>_<label>.png` e log INFO."""
    path = browser.screenshot()
    assert path.parent == screenshots_dir.resolve()
    match = SCREENSHOT_NAME.match(path.name)
    assert match is not None and match["label"] == "captura"
    with Image.open(path) as image:
        assert image.format == "PNG"
    assert ("INFO", f"Screenshot salvo: {path}") in log_messages
    labelled = browser.screenshot(label="relatório final/1")
    assert labelled.name.endswith("_relatório_final_1.png")
    assert browser.screenshot(label="***").name.endswith("_captura.png")


def test_screenshot_explicit_path_and_default_dir(
    fake_builder: FakeBuilder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RF-032: caminho explícito; sem `screenshots_dir`, usa `<cwd>/logs/screenshots`."""
    monkeypatch.chdir(tmp_path)
    with Browser("fake", BrowserOptions()) as instance:
        explicit = instance.screenshot(tmp_path / "evidências" / "tela.png")
        assert explicit == (tmp_path / "evidências" / "tela.png").resolve()
        assert explicit.is_file()
        default = instance.screenshot(label="padrao")
    assert default.parent == (tmp_path / "logs" / "screenshots").resolve()


def test_screenshot_failures(browser: Browser, driver: MagicMock) -> None:
    """Seção 8.21: falha do driver ao salvar a captura vira BrowserError."""
    driver.save_screenshot.side_effect = None
    driver.save_screenshot.return_value = False
    with pytest.raises(BrowserError, match="Falha ao salvar screenshot"):
        browser.screenshot()
    driver.save_screenshot.side_effect = WebDriverException("sem janela")
    with pytest.raises(BrowserError) as info:
        browser.screenshot()
    assert info.value.details["erro"] == "WebDriverException: sem janela"


@pytest.fixture
def fast_downloads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Estabilidade de tamanho imediata e verificação a cada 10 ms (testes rápidos)."""
    monkeypatch.setattr(browser_selenium, "_DOWNLOAD_STABLE_SECONDS", 0.0)
    monkeypatch.setattr(browser_selenium, "_DOWNLOAD_POLL_INTERVAL", 0.01)


@pytest.fixture
def download_browser(fake_builder: FakeBuilder, tmp_path: Path) -> Iterator[Browser]:
    with Browser("fake", BrowserOptions(download_dir=tmp_path / "downloads")) as instance:
        yield instance


def test_expect_download_returns_new_file(
    download_browser: Browser, tmp_path: Path, fast_downloads: None
) -> None:
    """RF-031: arquivos existentes são ignorados; `result.path` aponta o arquivo novo."""
    downloads = tmp_path / "downloads"
    (downloads / "antigo.pdf").write_bytes(b"antigo")
    with download_browser.expect_download(timeout=5, pattern="*.pdf") as result:
        assert result.path is None
        (downloads / "outro.txt").write_text("não casa com o padrão", encoding="utf-8")
        (downloads / "nota [1].pdf").write_bytes(b"%PDF-novo")
    assert result.path == (downloads / "nota [1].pdf").resolve()


def test_expect_download_waits_for_completion(
    download_browser: Browser, tmp_path: Path, fast_downloads: None
) -> None:
    """RF-031: arquivo temporário é ignorado até ser renomeado para o nome final."""
    downloads = tmp_path / "downloads"
    partial = downloads / "relatorio.csv.crdownload"

    def finish_download() -> None:
        partial.rename(downloads / "relatorio.csv")

    with download_browser.expect_download(timeout=5) as result:
        partial.write_text("id;nome", encoding="utf-8")
        timer = threading.Timer(0.2, finish_download)
        timer.start()
    timer.join()
    assert result.path == (downloads / "relatorio.csv").resolve()


def test_expect_download_ignores_placeholder_with_partial_file(
    download_browser: Browser, tmp_path: Path, fast_downloads: None
) -> None:
    """RF-031: arquivo final vazio com `.part` correspondente (Firefox) não conta como pronto."""
    downloads = tmp_path / "downloads"
    with pytest.raises(DownloadTimeoutError), download_browser.expect_download(timeout=0.2):
        (downloads / "boleto.pdf").write_bytes(b"")
        (downloads / "boleto.pdf.part").write_bytes(b"parcial")


def test_expect_download_timeout(
    download_browser: Browser, tmp_path: Path, fast_downloads: None
) -> None:
    """RF-031: sem download no prazo → DownloadTimeoutError com pasta, padrão e timeout."""
    started = time.monotonic()
    with (
        pytest.raises(DownloadTimeoutError) as info,
        download_browser.expect_download(timeout=0.3, pattern="*.zip"),
    ):
        pass
    assert 0.3 <= time.monotonic() - started < 2.0
    assert info.value.details == {
        "pasta": str((tmp_path / "downloads").resolve()),
        "padrao": "*.zip",
        "timeout": 0.3,
    }


def test_expect_download_stability_failure(
    download_browser: Browser, tmp_path: Path, mocker: MockerFixture
) -> None:
    """RF-031: arquivo encontrado que não estabiliza no prazo → DownloadTimeoutError."""
    from rpa_3003.core.exceptions import FileOperationError

    mocker.patch.object(
        browser_selenium.filesystem_utils,
        "wait_for_file",
        side_effect=FileOperationError("Arquivo não encontrado no prazo"),
    )
    with pytest.raises(DownloadTimeoutError), download_browser.expect_download(timeout=0.1):
        (tmp_path / "downloads" / "grande.iso").write_bytes(b"0")


def test_expect_download_requires_download_dir(browser: Browser) -> None:
    """RF-031: sem `download_dir` → BrowserError."""
    with pytest.raises(BrowserError, match="download_dir"), browser.expect_download():
        pass


def test_expect_download_does_not_wait_when_block_fails(
    download_browser: Browser, mocker: MockerFixture
) -> None:
    """RF-031: exceção no bloco é propagada sem aguardar download."""
    wait = mocker.spy(browser_selenium, "_wait_new_download")
    with (
        pytest.raises(RuntimeError, match="clique falhou"),
        download_browser.expect_download(timeout=60) as result,
    ):
        raise RuntimeError("clique falhou")
    wait.assert_not_called()
    assert result.path is None


def test_expect_download_directory_created_later(
    fake_builder: FakeBuilder, tmp_path: Path, fast_downloads: None
) -> None:
    """RF-031: a pasta de downloads pode ser removida e recriada pelo navegador."""
    downloads = tmp_path / "downloads"
    with Browser("fake", BrowserOptions(download_dir=downloads)) as instance:
        downloads.rmdir()
        with instance.expect_download(timeout=2) as result:
            downloads.mkdir()
            (downloads / "a.txt").write_text("a", encoding="utf-8")
    assert result.path == (downloads / "a.txt").resolve()


def test_cookies(browser: Browser, driver: MagicMock) -> None:
    """Seção 8.21: cookies delegados ao driver."""
    driver.get_cookies.return_value = [{"name": "sessao", "value": "1"}]
    assert browser.get_cookies() == [{"name": "sessao", "value": "1"}]
    browser.add_cookie({"name": "tema", "value": "escuro"})
    driver.add_cookie.assert_called_once_with({"name": "tema", "value": "escuro"})
