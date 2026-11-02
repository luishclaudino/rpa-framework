"""Verificações automáticas da documentação (RNF-018, RNF-019, seções 8.32 e 8.33).

A validação por um desenvolvedor que não participou da implementação continua manual (seção
12.1). Aqui se verificam a estrutura obrigatória, os links relativos (arquivos e âncoras) e a
sintaxe dos exemplos de código Python.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from functools import cache
from pathlib import Path
from typing import Final

import pytest

pytestmark = pytest.mark.unit

ROOT: Final[Path] = Path(__file__).resolve().parents[2]
README: Final[Path] = ROOT / "README.md"
GUIDE: Final[Path] = ROOT / "docs" / "GUIA_DO_DESENVOLVEDOR.md"
DEVIATIONS: Final[Path] = ROOT / "docs" / "DESVIOS.md"

README_SECTIONS: Final[tuple[str, ...]] = (
    "Visão geral",
    "Requisitos",
    "Instalação",
    "Início rápido",
    "Estrutura de um projeto",
    "Escrevendo steps",
    "Ambientes dev e prd",
    "Configuração",
    "HashiCorp Vault",
    "Navegadores",
    "Automação desktop",
    "CLI",
    "Logs",
    "Agendamento",
    "Testes",
    "Solução de problemas",
)
GUIDE_SECTIONS: Final[tuple[str, ...]] = (
    "Como esta documentação está organizada",
    "Ambiente de desenvolvimento",
    "Mapa do repositório",
    "Arquitetura",
    "Ciclo de uma execução",
    "Convenções de código",
    "Como estender",
    "Como escrever testes",
    "Fluxo de contribuição",
    "Problemas comuns no desenvolvimento",
)
# Um roteiro por ponto de extensão da seção 7.5.4 (exceto os que são uso, não extensão: novo
# step em um projeto e novo destino de log, cobertos pelo README).
EXTENSION_RECIPES: Final[tuple[str, ...]] = (
    "Novo navegador",
    "Novo banco de dados",
    "Novo provedor de segredos",
    "Novo canal de e-mail",
    "Novo template",
    "Novo utilitário",
)

FENCE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<indent>[ ]*)```(?P<lang>\w*)[ ]*\n(?P<body>.*?)^(?P=indent)```[ ]*$",
    re.MULTILINE | re.DOTALL,
)
LINK: Final[re.Pattern[str]] = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
HEADING: Final[re.Pattern[str]] = re.compile(r"^(#{1,6}) (.+?)[ ]*$", re.MULTILINE)
NOT_SLUG_CHAR: Final[re.Pattern[str]] = re.compile(r"[^\w\- ]")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _without_code(text: str) -> str:
    """Texto sem os blocos de código (cercados por ```), que podem conter `#` e links."""
    return FENCE.sub("", text)


def _code_blocks(text: str) -> Iterator[tuple[str, str]]:
    """Pares (linguagem, conteúdo sem o recuo da cerca) dos blocos de código."""
    for match in FENCE.finditer(text):
        indent = len(match.group("indent"))
        lines = [line[indent:] for line in match.group("body").splitlines()]
        yield match.group("lang"), "\n".join(lines) + "\n"


def _headings(text: str, level: int) -> list[str]:
    return [title for marks, title in HEADING.findall(_without_code(text)) if len(marks) == level]


def _slug(title: str) -> str:
    """Âncora gerada pelo GitHub: minúsculas, sem pontuação, espaços viram hífens."""
    return NOT_SLUG_CHAR.sub("", title.strip().lower()).replace(" ", "-")


@cache
def _anchors(path: Path) -> frozenset[str]:
    return frozenset(_slug(title) for _, title in HEADING.findall(_without_code(_text(path))))


def _section(text: str, title: str) -> str:
    """Conteúdo de uma seção de nível 2 ou 3, até o próximo título de nível igual ou maior."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if re.fullmatch(rf"#{{2,3}} {title}", line))
    level = len(lines[start].split(" ", 1)[0])
    end = next(
        (
            i
            for i in range(start + 1, len(lines))
            if re.match(rf"#{{2,{level}}} ", lines[i]) and not _inside_code(lines, i)
        ),
        len(lines),
    )
    return "\n".join(lines[start:end])


def _inside_code(lines: list[str], index: int) -> bool:
    """Indica se a linha `index` está dentro de um bloco de código cercado."""
    fences = sum(1 for line in lines[:index] if line.lstrip().startswith("```"))
    return fences % 2 == 1


def test_readme_has_mandatory_sections_in_order() -> None:
    """RNF-018/seção 8.32: o README tem os 16 títulos de nível 2 obrigatórios, nesta ordem."""
    assert tuple(_headings(_text(README), 2)) == README_SECTIONS


def test_readme_overview_points_to_developer_guide() -> None:
    """RNF-019/seção 8.32: a seção `Visão geral` do README aponta para o guia."""
    overview = _section(_text(README), "Visão geral")
    assert "](docs/GUIA_DO_DESENVOLVEDOR.md)" in overview


def test_guide_has_mandatory_sections_in_order() -> None:
    """RNF-019/seção 8.33: o guia tem os 10 títulos de nível 2 obrigatórios, nesta ordem."""
    assert tuple(_headings(_text(GUIDE), 2)) == GUIDE_SECTIONS


@pytest.mark.parametrize("recipe", EXTENSION_RECIPES)
def test_guide_has_recipe_per_extension_point(recipe: str) -> None:
    """RNF-019/seção 7.5.4: cada ponto de extensão tem roteiro com arquivos, código, teste e
    comando de verificação com o resultado esperado."""
    section = _section(_text(GUIDE), recipe)
    languages = [language for language, _ in _code_blocks(section)]
    assert "Arquivo `rpa_3003/" in section
    assert "Arquivo `tests/unit/" in section
    assert "python" in languages
    assert "powershell" in languages
    assert "Resultado esperado" in section


@pytest.mark.parametrize("doc", [README, GUIDE, DEVIATIONS], ids=lambda path: path.name)
def test_relative_links_point_to_existing_files(doc: Path) -> None:
    """RNF-019: links relativos apontam para arquivos e títulos existentes."""
    broken: list[str] = []
    for target in LINK.findall(_without_code(_text(doc))):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        path_part, _, anchor = target.partition("#")
        linked = (doc.parent / path_part).resolve() if path_part else doc
        if not linked.exists() or (anchor and anchor not in _anchors(linked)):
            broken.append(target)
    assert broken == []


@pytest.mark.parametrize("doc", [README, GUIDE], ids=lambda path: path.name)
def test_python_examples_are_valid_syntax(doc: Path) -> None:
    """RNF-019: os exemplos de código Python da documentação são sintaticamente válidos."""
    blocks = [body for language, body in _code_blocks(_text(doc)) if language == "python"]
    assert blocks
    for body in blocks:
        ast.parse(body)
