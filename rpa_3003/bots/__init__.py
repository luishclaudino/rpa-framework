"""Bots de automação web e desktop (importação preguiçosa).

Os nomes públicos são resolvidos no primeiro acesso (PEP 562). Importar o pacote não carrega
selenium, OpenCV, numpy, mss, pyautogui nem pywinauto (RNF-017):

    from rpa_3003.bots import Browser, DesktopBot  # selenium e cv2 carregados somente aqui
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from rpa_3003.bots.browser_selenium import Browser, BrowserFactory, BrowserOptions, BrowserType
    from rpa_3003.bots.desktop_bot import DesktopBot, Match

_LAZY_ATTRS: Final[dict[str, str]] = {
    "Browser": "rpa_3003.bots.browser_selenium",
    "BrowserFactory": "rpa_3003.bots.browser_selenium",
    "BrowserOptions": "rpa_3003.bots.browser_selenium",
    "BrowserType": "rpa_3003.bots.browser_selenium",
    "DesktopBot": "rpa_3003.bots.desktop_bot",
    "Match": "rpa_3003.bots.desktop_bot",
}
__all__: list[str] = [
    "Browser",
    "BrowserFactory",
    "BrowserOptions",
    "BrowserType",
    "DesktopBot",
    "Match",
]


def __getattr__(name: str) -> Any:  # noqa: ANN401 - o tipo depende do nome solicitado
    """Importa o módulo que define `name` no primeiro acesso (PEP 562).

    Args:
        name: Nome público solicitado (ex.: ``"Browser"``).

    Returns:
        O objeto exportado pelo módulo correspondente.

    Raises:
        AttributeError: Se `name` não for um nome público do pacote.
    """
    module_name = _LAZY_ATTRS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(module_name), name)
