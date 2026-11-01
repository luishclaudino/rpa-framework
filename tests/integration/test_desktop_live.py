"""Integração com a sessão de desktop do Windows (RF-034 a RF-036, seção 8.22).

Requer sessão Windows interativa e desbloqueada (risco R-07). Execute com `pytest -m gui`, sem
usar o mouse e o teclado durante a execução: os testes digitam no Bloco de Notas e usam a área
de transferência (o conteúdo anterior é restaurado no fim).

Proteção dos dados do usuário:

- o teste do Bloco de Notas é pulado se já houver um `notepad.exe` em execução;
- o Bloco de Notas abre um arquivo próprio em `tmp_path`, e toda a digitação ocorre na aba
  desse arquivo. O Bloco de Notas do Windows 11 restaura abas da sessão anterior, e a aba ativa
  poderia ser de um arquivo do usuário;
- antes de encerrar o processo, o arquivo de teste é salvo e a aba dele é fechada.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np
import pytest

from rpa_3003.bots.desktop_bot import DesktopBot

pytestmark = pytest.mark.gui

NOTEPAD = "notepad.exe"
# RF-036 cita ".*(Bloco de Notas|Notepad).*"; no Windows 11 pt-BR o título é "Bloco de notas".
NOTEPAD_TITLE_RE = "(?i).*(Bloco de Notas|Notepad).*"
ACCENTED_TEXT = "Ação, coração e pão: é, ê, à, ü, ç, ã, õ; símbolos {x}+^%~() ok"
PASTED_TEXT = "Observação colada: lançamento automático"


@pytest.fixture
def bot(tmp_path: Path) -> DesktopBot:
    return DesktopBot(
        images_dir=tmp_path / "images", screenshots_dir=tmp_path / "screenshots", timeout=5
    )


@pytest.fixture
def preserved_clipboard(bot: DesktopBot) -> Iterator[None]:
    """Guarda e restaura o texto da área de transferência do usuário."""
    previous = bot.get_clipboard()
    yield
    bot.set_clipboard(previous)


@pytest.fixture
def notepad_file(bot: DesktopBot, tmp_path: Path) -> Iterator[Path]:
    """Abre o Bloco de Notas com um arquivo próprio e encerra o processo ao final."""
    if bot.is_process_running(NOTEPAD):
        pytest.skip("feche o Bloco de Notas antes de executar o teste (evita perda de dados)")
    document = tmp_path / "rpa3003_teste_gui.txt"
    document.write_bytes(b"")
    try:
        bot.start_application(NOTEPAD, [str(document)], timeout=30)
        yield document
    finally:
        bot.kill_process(NOTEPAD)


def _title_re(document: Path) -> str:
    """Título da janela com a aba do arquivo de teste ativa."""
    return rf"(?i)\W*{re.escape(document.name)} - (Bloco de Notas|Notepad)$"


def _wait_until(condition: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.2)
    return condition()


def _document_text(bot: DesktopBot) -> str:
    bot.hotkey("ctrl", "a")
    bot.hotkey("ctrl", "c")
    return bot.get_clipboard()


def test_notepad_types_accented_text_and_kills_process(
    bot: DesktopBot, notepad_file: Path, preserved_clipboard: None
) -> None:
    """RF-036/RF-035: abre o notepad, conecta pelo título, digita com acentos e encerra."""
    title_re = _title_re(notepad_file)
    window = bot.connect_window(title_re, timeout=30)
    assert re.match(NOTEPAD_TITLE_RE, window.window_text())
    assert bot.wait_window(title_re, timeout=5) is True
    assert bot.window_exists(title_re) is True
    bot.activate_window(title_re)

    bot.type_text(ACCENTED_TEXT)
    assert _document_text(bot) == ACCENTED_TEXT

    bot.press("end")
    bot.press("enter")
    bot.set_clipboard("conteúdo do usuário")
    bot.type_text(PASTED_TEXT, method="clipboard")
    assert bot.get_clipboard() == "conteúdo do usuário"
    assert _document_text(bot).splitlines() == [ACCENTED_TEXT, PASTED_TEXT]

    bot.press("end")
    bot.hotkey("ctrl", "s")
    assert _wait_until(lambda: notepad_file.stat().st_size > 0)
    saved = notepad_file.read_text(encoding="utf-8-sig")
    assert saved.splitlines() == [ACCENTED_TEXT, PASTED_TEXT]

    # Fecha a aba do arquivo de teste (Windows 11) para ela não ser restaurada depois.
    bot.hotkey("ctrl", "w")
    assert _wait_until(lambda: not bot.window_exists(title_re))
    running = bot.is_process_running(NOTEPAD)  # False se era a única aba
    assert bot.kill_process(NOTEPAD) >= (1 if running else 0)
    assert bot.is_process_running(NOTEPAD) is False


def test_find_image_on_real_screen(bot: DesktopBot, tmp_path: Path) -> None:
    """RF-034: captura real via mss; recorte da tela é encontrado na região de origem."""
    width, height = bot.screen_size()
    region = (width // 2 - 100, height // 2 - 75, 200, 150)
    reference = bot.screenshot(tmp_path / "images" / "recorte.png", region=region)
    match = bot.find_image(reference, region=region, confidence=0.95)
    assert match is not None
    assert (match.left, match.top, match.width, match.height) == region
    frame = bot.capture_screen()
    assert frame.ndim == 3
    assert frame.shape[2] == 3
    assert frame.dtype == np.uint8


def test_mouse_moves_to_position(bot: DesktopBot) -> None:
    """RF-035: o mouse vai para a posição absoluta informada (pixels físicos, DPI awareness)."""
    original = bot.mouse_position()
    width, height = bot.screen_size()
    target = (width // 2, height // 2)
    try:
        bot.move_to(*target, duration=0)
        assert bot.mouse_position() == target
    finally:
        bot.move_to(*original, duration=0)
