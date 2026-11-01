"""Regras de arquitetura do pacote rpa_3003 verificadas por análise estática de imports.

Cobre as regras de dependência entre camadas (seção 7.2), a exclusividade de ``import logging``
em ``core/logger.py`` (RF-014), o isolamento de dependências pesadas (RNF-017) e a portabilidade
de caminhos (RNF-016).
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

pytestmark = pytest.mark.unit

ROOT: Final[Path] = Path(__file__).resolve().parents[2]
PACKAGE: Final[str] = "rpa_3003"
PACKAGE_DIR: Final[Path] = ROOT / PACKAGE
EXCLUDED_DIRS: Final[tuple[Path, ...]] = (PACKAGE_DIR / "templates" / "example",)

ROOT_PACKAGE: Final[str] = PACKAGE
MAIN: Final[str] = "rpa_3003.__main__"
VERSION: Final[str] = "rpa_3003._version"
CLI: Final[str] = "rpa_3003.cli"
CORE: Final[str] = "rpa_3003.core"
EXCEPTIONS: Final[str] = "rpa_3003.core.exceptions"
LOGGER: Final[str] = "rpa_3003.core.logger"
CONFIG: Final[str] = "rpa_3003.core.config"
VAULT: Final[str] = "rpa_3003.core.vault"
WORKFLOW: Final[str] = "rpa_3003.core.workflow"
UTILS: Final[str] = "rpa_3003.utils"
BOTS: Final[str] = "rpa_3003.bots"
TEMPLATES: Final[str] = "rpa_3003.templates"

# Barra invertida entre caracteres de nome de caminho (ex.: "C:\\rpa\\saida"); não casa com
# conjuntos de caracteres como '<>:"/\\|?*' nem com classes de regex como "[/\\]".
BACKSLASH_SEPARATOR: Final[re.Pattern[str]] = re.compile(r"[\w.$%~-]\\[\w.$%~-]")
PYWIN32_MODULES: Final[frozenset[str]] = frozenset(
    {"pywintypes", "win32api", "win32con", "win32crypt"}
)
HEAVY_MODULES: Final[tuple[str, ...]] = (
    "selenium",
    "cv2",
    "numpy",
    "mss",
    "pyautogui",
    "pywinauto",
    "win32com",
    "duckdb",
    "psycopg2",
    "reportlab",
    "pypdf",
    "pdfplumber",
)


@dataclass(frozen=True, slots=True)
class LayerRule:
    """Regra de dependência de um módulo ou grupo de módulos do framework.

    Attributes:
        allowed: Prefixos de módulos do framework que podem ser importados.
        denied: Prefixos proibidos mesmo quando cobertos por ``allowed``.
        third_party: Bibliotecas externas permitidas; ``None`` libera qualquer uma. A biblioteca
            padrão é sempre permitida.
    """

    allowed: tuple[str, ...]
    denied: tuple[str, ...] = ()
    third_party: frozenset[str] | None = None


@dataclass(frozen=True, slots=True)
class SourceModule:
    """Módulo Python do pacote, identificado pelo nome pontuado e pelo arquivo-fonte."""

    name: str
    path: Path


# Tabela da seção 7.2, complementada pelas dependências das seções 8.1, 8.3, 8.7 e 8.8:
# `vault.py` usa `pydantic.SecretStr` em `Credentials` e `workflow.py` importa `loguru.Logger`
# sob TYPE_CHECKING (RNF-003). Ver docs/DESVIOS.md, D-05.
MODULE_RULES: Final[dict[str, LayerRule]] = {
    ROOT_PACKAGE: LayerRule(allowed=(VERSION, CONFIG, LOGGER), third_party=frozenset()),
    MAIN: LayerRule(allowed=(CLI,), third_party=frozenset()),
    VERSION: LayerRule(allowed=(), third_party=frozenset()),
    CORE: LayerRule(allowed=(EXCEPTIONS, LOGGER, CONFIG), third_party=frozenset()),
    EXCEPTIONS: LayerRule(allowed=(), third_party=frozenset()),
    LOGGER: LayerRule(allowed=(), third_party=frozenset({"loguru"})),
    CONFIG: LayerRule(
        allowed=(EXCEPTIONS, LOGGER), third_party=frozenset({"pydantic", "pydantic_settings"})
    ),
    VAULT: LayerRule(
        allowed=(EXCEPTIONS, LOGGER, CONFIG),
        third_party=frozenset({"hvac", "requests", "pydantic"}) | PYWIN32_MODULES,
    ),
    WORKFLOW: LayerRule(
        allowed=(EXCEPTIONS, LOGGER, CONFIG, VERSION),
        third_party=frozenset({"filelock", "loguru"}),
    ),
    TEMPLATES: LayerRule(
        allowed=(CORE, VERSION), denied=(WORKFLOW, VAULT), third_party=frozenset()
    ),
    CLI: LayerRule(allowed=(CORE, TEMPLATES, VERSION), third_party=frozenset({"psutil"})),
}
# Regras aplicadas a todos os módulos sob o pacote indicado.
GROUP_RULES: Final[dict[str, LayerRule]] = {
    UTILS: LayerRule(allowed=(CORE, UTILS), denied=(WORKFLOW,)),
    BOTS: LayerRule(allowed=(CORE, UTILS, BOTS), denied=(WORKFLOW,)),
}


def _is_within(name: str, prefix: str) -> bool:
    """Indica se ``name`` é ``prefix`` ou está contido nele (fronteira de ponto)."""
    return name == prefix or name.startswith(f"{prefix}.")


def _top_level(name: str) -> str:
    return name.partition(".")[0]


def _iter_package_modules() -> Iterator[SourceModule]:
    for path in sorted(PACKAGE_DIR.rglob("*.py")):
        if any(path.is_relative_to(excluded) for excluded in EXCLUDED_DIRS):
            continue
        parts = path.relative_to(ROOT).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        yield SourceModule(name=".".join(parts), path=path)


MODULES: Final[tuple[SourceModule, ...]] = tuple(_iter_package_modules())
MODULES_BY_NAME: Final[dict[str, SourceModule]] = {module.name: module for module in MODULES}


def _rule_for(module_name: str) -> LayerRule | None:
    if module_name in MODULE_RULES:
        return MODULE_RULES[module_name]
    for group, rule in GROUP_RULES.items():
        if _is_within(module_name, group):
            return rule
    return None


def _is_framework_module(dotted_name: str) -> bool:
    """Indica se o nome pontuado corresponde a um módulo ou subpacote existente do framework."""
    base = ROOT.joinpath(*dotted_name.split("."))
    return base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()


def _resolve_relative(module: SourceModule, level: int, target: str | None) -> str:
    package_parts = module.name.split(".")
    if module.path.name != "__init__.py":
        package_parts = package_parts[:-1]
    if level > 1:
        package_parts = package_parts[: -(level - 1)]
    return ".".join([*package_parts, target] if target else package_parts)


def _imported_names(module: SourceModule, source: str | None = None) -> set[str]:
    """Retorna os nomes pontuados importados pelo módulo.

    Considera todos os imports da árvore sintática, inclusive locais e sob ``TYPE_CHECKING``.
    Em ``from pacote import nome``, registra ``pacote.nome`` quando ``nome`` é submódulo do
    framework e ``pacote`` nos demais casos.
    """
    text = module.path.read_text(encoding="utf-8") if source is None else source
    names: set[str] = set()
    for node in ast.walk(ast.parse(text, filename=str(module.path))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = _resolve_relative(module, node.level, node.module)
            else:
                base = node.module or ""
            for alias in node.names:
                candidate = f"{base}.{alias.name}"
                is_submodule = _is_within(base, PACKAGE) and _is_framework_module(candidate)
                names.add(candidate if is_submodule else base)
    return names


def _violations(module: SourceModule, source: str | None = None) -> list[str]:
    """Retorna os imports do módulo que violam a regra de camada aplicável."""
    rule = _rule_for(module.name)
    assert rule is not None, f"Módulo sem regra de dependência: {module.name}"
    offending: set[str] = set()
    for name in _imported_names(module, source):
        top_level = _top_level(name)
        if top_level == PACKAGE:
            allowed = any(_is_within(name, prefix) for prefix in rule.allowed)
            denied = any(_is_within(name, prefix) for prefix in rule.denied)
            if not allowed or denied:
                offending.add(name)
        elif top_level not in sys.stdlib_module_names and rule.third_party is not None:
            if top_level not in rule.third_party:
                offending.add(top_level)
    return sorted(offending)


def _modules_loaded_by(statement: str) -> set[str]:
    """Executa ``statement`` em um interpretador novo e retorna as chaves de ``sys.modules``."""
    code = f"import json, sys\n{statement}\nprint(json.dumps(sorted(sys.modules)))"
    completed = subprocess.run(  # noqa: S603 - interpretador atual com código fixo, sem shell
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        encoding="utf-8",
        check=False,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    return set(json.loads(completed.stdout))


def test_package_modules_are_discovered() -> None:
    """Seção 7.1: os módulos da árvore do pacote são encontrados pela análise."""
    assert set(MODULE_RULES) <= set(MODULES_BY_NAME)
    for group in GROUP_RULES:
        assert group in MODULES_BY_NAME


def test_every_module_has_dependency_rule() -> None:
    """Seção 7.2: todo módulo do pacote está coberto por uma regra de dependência."""
    uncovered = [module.name for module in MODULES if _rule_for(module.name) is None]
    assert uncovered == []


@pytest.mark.parametrize("module", MODULES, ids=lambda module: module.name)
def test_layer_dependency_rules(module: SourceModule) -> None:
    """RNF-012: os imports do módulo respeitam as regras de dependência da seção 7.2."""
    assert _violations(module) == []


def test_std_logging_imported_only_by_logger() -> None:
    """RF-014: ``import logging`` ocorre apenas em ``core/logger.py``."""
    offenders = [
        module.name
        for module in MODULES
        if module.name != LOGGER
        and any(_top_level(name) == "logging" for name in _imported_names(module))
    ]
    assert offenders == []


def test_package_imports_do_not_load_heavy_modules() -> None:
    """RNF-017: importar os pacotes não carrega dependências pesadas em ``sys.modules``."""
    loaded = _modules_loaded_by("import rpa_3003, rpa_3003.core, rpa_3003.bots, rpa_3003.utils")
    heavy = sorted(
        name for name in loaded if any(_is_within(name, heavy) for heavy in HEAVY_MODULES)
    )
    assert heavy == []


@pytest.mark.parametrize(
    ("module_name", "drivers"),
    [
        ("rpa_3003.utils.database_utils", ("psycopg2", "duckdb")),
        ("rpa_3003.utils.email_utils", ("win32com", "pywintypes")),
        ("rpa_3003.utils.pdf_utils", ("pypdf", "pdfplumber", "pdfminer", "reportlab")),
    ],
)
def test_utils_modules_import_drivers_lazily(module_name: str, drivers: tuple[str, ...]) -> None:
    """RNF-004/RNF-017: drivers pesados só são importados quando a função é usada."""
    loaded = _modules_loaded_by(f"import {module_name}")
    assert module_name in loaded
    assert sorted(name for name in loaded if any(_is_within(name, d) for d in drivers)) == []


@pytest.mark.parametrize(
    ("statement", "expected", "absent"),
    [
        (
            "import rpa_3003.bots.desktop_bot",
            ("numpy",),
            ("cv2", "mss", "pyautogui", "pywinauto", "pyperclip", "psutil", "selenium"),
        ),
        (
            "from rpa_3003.bots import Browser",
            ("selenium",),
            ("cv2", "numpy", "mss", "pyautogui", "pywinauto", "pyperclip"),
        ),
    ],
)
def test_bots_load_heavy_modules_only_on_use(
    statement: str, expected: tuple[str, ...], absent: tuple[str, ...]
) -> None:
    """RNF-004/RNF-017: o desktop_bot importa só o numpy no nível do módulo (seção 8.22), e
    acessar `Browser` carrega o selenium sem as bibliotecas de automação desktop."""
    loaded = _modules_loaded_by(statement)
    assert all(name in loaded for name in expected)
    assert sorted(name for name in loaded if any(_is_within(name, a) for a in absent)) == []


def _docstring_nodes(tree: ast.Module) -> set[int]:
    """Identificadores (`id`) dos nós de docstring de módulo, classes e funções."""
    owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, owners) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                found.add(id(first.value))
    return found


def _path_portability_violations(module: SourceModule) -> list[str]:
    """`os.path.join` e literais com `\\` usados como separador de caminho (RNF-016)."""
    tree = ast.parse(module.path.read_text(encoding="utf-8"), filename=str(module.path))
    docstrings = _docstring_nodes(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        is_join = (
            isinstance(node, ast.Attribute)
            and node.attr == "join"
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "path"
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "os"
        )
        imports_join = (
            isinstance(node, ast.ImportFrom)
            and node.module == "os.path"
            and any(alias.name == "join" for alias in node.names)
        )
        if is_join or imports_join:
            found.append(f"{module.name}:{node.lineno}: os.path.join")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and BACKSLASH_SEPARATOR.search(node.value)
        ):
            found.append(f"{module.name}:{node.lineno}: {node.value!r}")
    return found


def test_paths_use_pathlib_without_hardcoded_separators() -> None:
    """RNF-016: sem `os.path.join` nem literais com `\\` como separador de caminho."""
    violations = [item for module in MODULES for item in _path_portability_violations(module)]
    assert violations == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import os\nos.path.join('a', 'b')\n", 1),
        ("from os.path import join\n", 1),
        ("PATH = 'C:\\\\rpa\\\\saida'\n", 1),
        ("CHARS = '<>:\"/\\\\|?*'\nPATTERN = r'[/\\\\]'\n", 0),
        ('"""Exemplo: C:\\\\Automação RPA\\\\rpa-framework."""\n', 0),
    ],
)
def test_path_portability_checker(tmp_path: Path, source: str, expected: int) -> None:
    """RNF-016: o verificador aponta os.path.join e separadores fixos, não conjuntos."""
    path = tmp_path / "amostra.py"
    path.write_text(source, encoding="utf-8")
    assert len(_path_portability_violations(SourceModule("amostra", path))) == expected


def test_core_package_does_not_load_vault_or_workflow() -> None:
    """Seção 8.3: ``rpa_3003.core`` não importa ``vault`` e ``workflow`` (nem hvac e filelock)."""
    loaded = _modules_loaded_by("import rpa_3003.core")
    forbidden = (VAULT, WORKFLOW, "hvac", "filelock")
    unexpected = sorted(
        name for name in loaded if any(_is_within(name, prefix) for prefix in forbidden)
    )
    assert unexpected == []


@pytest.mark.parametrize(
    ("module_name", "source", "expected"),
    [
        (CONFIG, "from rpa_3003.core import workflow\n", [WORKFLOW]),
        (LOGGER, "import rpa_3003.core.exceptions\n", [EXCEPTIONS]),
        (EXCEPTIONS, "import requests\n", ["requests"]),
        (CORE, "from rpa_3003.core import vault\n", [VAULT]),
        (CORE, "from rpa_3003 import cli\n", [CLI]),
        (
            "rpa_3003.utils.json_utils",
            "from rpa_3003.bots.browser_selenium import Browser\n",
            ["rpa_3003.bots.browser_selenium"],
        ),
        (
            "rpa_3003.utils.json_utils",
            "from ..bots import desktop_bot\n",
            ["rpa_3003.bots.desktop_bot"],
        ),
        (
            "rpa_3003.utils.database_utils",
            "from rpa_3003.core.workflow import BaseStep\n",
            [WORKFLOW],
        ),
        (
            "rpa_3003.bots.desktop_bot",
            "from rpa_3003.templates import create_project\n",
            [TEMPLATES],
        ),
        (TEMPLATES, "from rpa_3003.core.vault import Credentials\n", [VAULT]),
        (TEMPLATES, "import yaml\n", ["yaml"]),
        (CLI, "from rpa_3003.utils import csv_utils\n", ["rpa_3003.utils.csv_utils"]),
        (CLI, "def f():\n    from rpa_3003.bots import Browser\n", [BOTS]),
    ],
)
def test_checker_detects_violations(module_name: str, source: str, expected: list[str]) -> None:
    """Seção 7.2: a análise detecta imports proibidos (absolutos, relativos e locais)."""
    assert _violations(MODULES_BY_NAME[module_name], source) == expected


@pytest.mark.parametrize(
    ("module_name", "source"),
    [
        (
            "rpa_3003.utils.csv_utils",
            "import numpy\nfrom rpa_3003.utils import json_utils\n"
            "from rpa_3003.core.exceptions import RPAError\n",
        ),
        (
            VAULT,
            "from typing import TYPE_CHECKING\nimport hvac\nimport win32crypt\n"
            "from rpa_3003.core import config\nif TYPE_CHECKING:\n    import requests\n",
        ),
        (CONFIG, "from . import exceptions\nfrom .logger import LogLevel\nimport pydantic\n"),
        (VAULT, "from pydantic import SecretStr\n"),
        (
            WORKFLOW,
            "from typing import TYPE_CHECKING\nimport filelock\n"
            "if TYPE_CHECKING:\n    from loguru import Logger\n",
        ),
        (CLI, "import psutil\nfrom rpa_3003.core.workflow import split_passthrough_args\n"),
        (ROOT_PACKAGE, "from rpa_3003._version import __version__\n"),
    ],
)
def test_checker_accepts_allowed_imports(module_name: str, source: str) -> None:
    """Seção 7.2: a análise não aponta imports permitidos."""
    assert _violations(MODULES_BY_NAME[module_name], source) == []
