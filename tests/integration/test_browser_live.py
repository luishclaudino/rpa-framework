"""Integração com Chrome, Edge e Edge em modo IE reais (RF-026 a RF-032, RNF-006, seção 8.21).

Requer Windows com Chrome e Edge instalados e acesso do Selenium Manager aos drivers (ou os
caminhos `RPA3003_CHROMEDRIVER_PATH`/`RPA3003_EDGEDRIVER_PATH`). Execute com
`pytest -m browser`. As páginas vêm de `tests/fixtures/html`, abertas por `file://` ou pela
fixture `http_server`.

O teste do modo IE só roda com `RPA3003_TEST_IE_MODE=1`, em máquina com o modo IE configurado
(zoom 100%, mesmo Modo Protegido em todas as zonas e IEDriverServer 32 bits; risco R-01).

Após cada teste, nenhum `chromedriver.exe`, `msedgedriver.exe` ou `IEDriverServer.exe` pode
continuar como filho do processo de teste (RNF-006).
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path

import psutil
import pytest
from PIL import Image

from rpa_3003.bots.browser_selenium import Browser, BrowserOptions, BrowserType, By
from rpa_3003.core.exceptions import DownloadTimeoutError, ElementNotFoundError

pytestmark = pytest.mark.browser

HTML_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "html"
DRIVER_PROCESSES = frozenset({"chromedriver.exe", "msedgedriver.exe", "iedriverserver.exe"})
ORPHAN_WAIT_SECONDS = 10.0


def _driver_children() -> list[str]:
    """Drivers de navegador que ainda são filhos (diretos ou não) do processo de teste."""
    found: list[str] = []
    for child in psutil.Process().children(recursive=True):
        try:
            name = child.name()
        except psutil.Error:
            continue
        if name.lower() in DRIVER_PROCESSES:
            found.append(f"{name} (pid={child.pid})")
    return found


@pytest.fixture(autouse=True)
def _no_orphan_drivers() -> Iterator[None]:
    """RNF-006: ao fim do teste, nenhum processo de driver remanescente."""
    yield
    deadline = time.monotonic() + ORPHAN_WAIT_SECONDS
    orphans = _driver_children()
    while orphans and time.monotonic() < deadline:
        time.sleep(0.25)
        orphans = _driver_children()
    assert orphans == []


@pytest.fixture(params=[BrowserType.CHROME, BrowserType.EDGE], ids=str)
def browser_type(request: pytest.FixtureRequest) -> BrowserType:
    browser: BrowserType = request.param
    return browser


@pytest.fixture
def downloads(tmp_path: Path) -> Path:
    return tmp_path / "downloads"


@pytest.fixture
def browser(browser_type: BrowserType, downloads: Path, tmp_path: Path) -> Iterator[Browser]:
    """Navegador headless com pasta de downloads e capturas em `tmp_path`."""
    options = BrowserOptions(headless=True, download_dir=downloads, default_timeout=10)
    with Browser(browser_type, options, screenshots_dir=tmp_path / "screenshots") as instance:
        yield instance


def test_headless_opens_local_file_and_reads_title(browser: Browser) -> None:
    """RF-027/RF-028: inicia headless, abre página local `file://` e lê o título."""
    browser.open((HTML_DIR / "login.html").as_uri())
    assert browser.title == "Login - rpa_3003"
    assert browser.current_url.startswith("file:")


def test_same_login_script_on_chrome_and_edge(browser: Browser, http_server: str) -> None:
    """RF-026/RF-030: o mesmo roteiro (digitação, select e clique) nos dois navegadores."""
    browser.open(f"{http_server}login.html")
    browser.type_text((By.ID, "usuario"), "robo01")
    browser.type_text((By.ID, "senha"), "S3nh@Forte", sensitive=True)
    browser.select_option((By.ID, "filial"), text="São Paulo")
    browser.click((By.ID, "entrar"))
    assert browser.get_text((By.ID, "resultado")) == (
        "Bem-vindo, robo01 (São Paulo) | senha=10 caracteres"
    )
    assert browser.get_attribute((By.ID, "usuario"), "value") == "robo01"
    assert browser.exists((By.ID, "filial")) is True
    assert browser.exists((By.ID, "inexistente")) is False


def test_element_created_after_two_seconds_is_found(browser: Browser, http_server: str) -> None:
    """RF-030: elemento que aparece após 2 s é encontrado com `timeout=5`."""
    browser.open(f"{http_server}delayed.html")
    started = time.monotonic()
    element = browser.wait_visible((By.ID, "atrasado"), timeout=5)
    assert time.monotonic() - started >= 1.0
    assert element.text == "Conteúdo carregado"
    browser.wait_invisible((By.ID, "aguarde"), timeout=1)


def test_missing_element_raises_within_timeout(browser: Browser, http_server: str) -> None:
    """RF-030: elemento inexistente e `timeout=1` → ElementNotFoundError em até 1,5 s."""
    browser.open(f"{http_server}login.html")
    started = time.monotonic()
    with pytest.raises(ElementNotFoundError) as info:
        browser.find((By.ID, "inexistente"), timeout=1)
    assert time.monotonic() - started <= 1.5
    assert "('id', 'inexistente')" in str(info.value)
    assert info.value.details["url"] == f"{http_server}login.html"


def test_iframe_alert_and_tabs(browser: Browser, http_server: str) -> None:
    """Seção 8.21: iframe, alerta JavaScript e troca de abas."""
    browser.open(f"{http_server}iframe.html")
    browser.switch_to_frame((By.ID, "quadro"))
    browser.type_text((By.ID, "usuario"), "dentro do frame")
    browser.switch_to_default_content()
    assert browser.get_text((By.ID, "titulo-principal")) == "Documento principal"

    browser.open(f"{http_server}login.html")
    browser.click((By.ID, "alerta"))
    assert browser.handle_alert() == "Confirma o envio?"

    original = browser.switch_to_window(index=0)
    new_tab = browser.new_tab(f"{http_server}download.html")
    assert new_tab != original
    assert browser.title == "Download - rpa_3003"
    assert browser.switch_to_window(title_contains="Login") == original
    assert browser.switch_to_window(handle=new_tab) == new_tab
    browser.close_tab()
    assert browser.title == "Login - rpa_3003"


def test_download_completes(browser: Browser, http_server: str, downloads: Path) -> None:
    """RF-031: após clicar no link dentro do bloco, `result.path` tem o conteúdo esperado."""
    browser.open(f"{http_server}download.html")
    with browser.expect_download(timeout=60, pattern="*.csv") as result:
        browser.click((By.ID, "baixar"))
    assert result.path is not None
    assert result.path.parent == downloads.resolve()
    assert result.path.read_bytes() == (HTML_DIR / "relatorio.csv").read_bytes()


def test_download_timeout(browser: Browser, http_server: str) -> None:
    """RF-031: nenhum download em `timeout=2` → DownloadTimeoutError."""
    browser.open(f"{http_server}download.html")
    started = time.monotonic()
    with pytest.raises(DownloadTimeoutError), browser.expect_download(timeout=2):
        pass
    assert time.monotonic() - started >= 2.0


def test_screenshots_are_png(browser_type: BrowserType, http_server: str, tmp_path: Path) -> None:
    """RF-032: a captura abre com PIL em PNG; exceção no `with` gera `*_erro.png`."""
    screenshots = tmp_path / "screenshots"
    options = BrowserOptions(headless=True, window_size=(1280, 720), default_timeout=10)
    with (
        pytest.raises(ElementNotFoundError),
        Browser(browser_type, options, screenshots_dir=screenshots) as browser,
    ):
        browser.open(f"{http_server}login.html")
        path = browser.screenshot(label="login")
        with Image.open(path) as image:
            assert image.format == "PNG"
            assert 0 < image.width <= 1280
            assert 0 < image.height <= 720
        browser.find((By.ID, "inexistente"), timeout=0)
    errors = list(screenshots.glob("*_erro.png"))
    assert len(errors) == 1
    with Image.open(errors[0]) as image:
        assert image.format == "PNG"


@pytest.mark.skipif(
    os.environ.get("RPA3003_TEST_IE_MODE") != "1",
    reason="defina RPA3003_TEST_IE_MODE=1 em máquina com o modo IE configurado (R-01)",
)
def test_edge_ie_mode_document_mode(http_server: str, tmp_path: Path) -> None:
    """RF-029: no modo IE, `document.documentMode` é um número ≤ 11."""
    options = BrowserOptions(default_timeout=20)
    with Browser("edge_ie", options, screenshots_dir=tmp_path / "screenshots") as browser:
        browser.open(f"{http_server}ie_mode.html")
        browser.wait_visible((By.ID, "modo"))
        document_mode = browser.execute_script("return document.documentMode")
        assert isinstance(document_mode, int | float)
        assert document_mode <= 11
        assert browser.get_text((By.ID, "modo")) == str(int(document_mode))
