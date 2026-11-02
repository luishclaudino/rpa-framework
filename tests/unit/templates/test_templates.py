"""Testes de `rpa_3003.templates` e do template `example` (RF-012, RF-025, RF-048, RF-053 a
RF-055, seções 8.23 e 11).

Os testes do motor usam templates mínimos criados em `tmp_path`, apontados por `templates_root`
(substituído com `monkeypatch`). Os testes do `example` usam o template do pacote e verificam os
critérios da seção 11.12: os de `rpa3003 run` executam o projeto gerado em subprocesso real.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import py_compile
import shutil
import socket
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime
from importlib.resources.abc import Traversable
from pathlib import Path
from types import ModuleType
from typing import Any, NoReturn
from unittest.mock import MagicMock

import pytest
import time_machine
from pytest_mock import MockerFixture

from rpa_3003 import templates
from rpa_3003._version import __version__
from rpa_3003.cli import main
from rpa_3003.core.config import Environment, validate_project_structure
from rpa_3003.core.exceptions import (
    ConfigurationError,
    ExitCode,
    InvalidProjectNameError,
    ProjectAlreadyExistsError,
    TemplateNotFoundError,
    TemplateRenderError,
)
from rpa_3003.core.vault import Credentials
from rpa_3003.templates import (
    DEFAULT_TEMPLATE,
    DOTFILE_PREFIX,
    IGNORED_NAMES,
    PROJECT_NAME_PATTERN,
    TEMPLATE_METADATA_FILE,
    TEMPLATE_SUFFIX,
    TemplateInfo,
    build_context,
    create_project,
    get_template,
    list_templates,
    render_template,
    templates_root,
    validate_project_name,
)

pytestmark = pytest.mark.unit

FIXED_NOW = datetime(2026, 10, 31, 9, 30, 15)
EXAMPLE_DESCRIPTION = (
    "Projeto RPA padrão: workflows dev/prd, Vault, logging e exemplo de processamento de CSV"
)
MAIN_TPL = (
    '"""Projeto ${project_name} (${project_title}).\r\n'
    "\r\n"
    "Autor: ${author}; criado em ${created_at}; rpa_3003 ${framework_version}.\r\n"
    '"""\r\n'
    "\r\n"
    'PRECO = "R$$ 10,00"\r\n'
    'DESCRICAO = "Configuração de ${project_name}"\r\n'
)
STEP_TPL = '"""Step do projeto ${project_name}."""\n\n\nclass Step:\n    """Step."""\n'
ENV_TPL = "RPA3003_ENV=dev\n# Projeto ${project_name}\n"
BINARY_CONTENT = b"\x00\xff$${project_name}\r\n\x89PNG"
LITERAL_CONTENT = "valor=${project_name} (copiado sem renderizar)\n"
MINIMAL_FILES: dict[str, str | bytes] = {
    "main.py.tpl": MAIN_TPL,
    "dot_env.example.tpl": ENV_TPL,
    "workflows/__init__.py.tpl": '"""Workflows de ${project_name}."""\n',
    "workflows/dev/step_a.py.tpl": STEP_TPL,
    "workflows/template.toml": "ignorado = true\n",
    "dot_config/ajustes.txt": LITERAL_CONTENT,
    "dados.bin": BINARY_CONTENT,
    "__pycache__/main.cpython-312.pyc": b"\x00compilado",
    "antigo.pyc": b"\x00compilado",
}
EXPECTED_OUTPUTS = {
    "main.py",
    ".env.example",
    "workflows/__init__.py",
    "workflows/dev/step_a.py",
    ".config/ajustes.txt",
    "dados.bin",
    "logs/.gitkeep",
    "data/.gitkeep",
    "data/input/.gitkeep",
}

WORKSPACE_PYPROJECT = Path(__file__).resolve().parents[3] / "pyproject.toml"
EXAMPLE_STEPS = ("step_setup_env", "step_execute_task", "step_wrapup_execution")
EXAMPLE_TEMPLATE_FILES = {  # árvore da seção 11.1
    "template.toml",
    "main.py.tpl",
    "global_vars.py.tpl",
    "README.md.tpl",
    "dot_env.example.tpl",
    "workflows/__init__.py.tpl",
    *(f"workflows/{env}/__init__.py.tpl" for env in ("dev", "prd")),
    *(f"workflows/{env}/{step}.py.tpl" for env in ("dev", "prd") for step in EXAMPLE_STEPS),
}
EXAMPLE_OUTPUTS = {
    "main.py",
    "global_vars.py",
    "README.md",
    ".env.example",
    "workflows/__init__.py",
    *(f"workflows/{env}/__init__.py" for env in ("dev", "prd")),
    *(f"workflows/{env}/{step}.py" for env in ("dev", "prd") for step in EXAMPLE_STEPS),
    *(
        f"{folder}/.gitkeep"
        for folder in ("logs", "data", "data/input", "data/output", "data/images")
    ),
}

TemplateWriter = Callable[..., Path]


def _toml(
    name: str,
    *,
    description: str = "Template mínimo de teste",
    version: str = "0.1.0",
    ensure_dirs: tuple[str, ...] = ("logs", "data", "data/input"),
) -> str:
    dirs = ", ".join(f'"{item}"' for item in ensure_dirs)
    return (
        f'name = "{name}"\ndescription = "{description}"\nversion = "{version}"\n'
        f"ensure_dirs = [{dirs}]\n"
    )


@pytest.fixture
def template_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pasta de templates de teste no lugar de `rpa_3003/templates/`."""
    root = tmp_path / "templates_teste"
    root.mkdir()
    monkeypatch.setattr(templates, "templates_root", lambda: root)
    return root


@pytest.fixture
def write_template(template_root: Path) -> TemplateWriter:
    """Cria um template em `template_root` com os arquivos e o `template.toml` informados."""

    def write(
        name: str,
        files: dict[str, str | bytes] | None = None,
        *,
        metadata: str | bytes | None = None,
    ) -> Path:
        directory = template_root / name
        directory.mkdir(parents=True)
        content = _toml(name) if metadata is None else metadata
        raw = content if isinstance(content, bytes) else content.encode("utf-8")
        (directory / TEMPLATE_METADATA_FILE).write_bytes(raw)
        for relative, data in (MINIMAL_FILES if files is None else files).items():
            target = directory / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        return directory

    return write


@pytest.fixture
def minimal(write_template: TemplateWriter) -> TemplateInfo:
    write_template("minimo")
    return get_template("minimo")


@pytest.fixture
def context() -> dict[str, str]:
    return {
        "project_name": "conciliacao_bancaria",
        "project_title": "Conciliacao Bancaria",
        "author": "ana.souza",
        "created_at": "2026-10-31",
        "framework_version": __version__,
    }


@pytest.fixture
def log_messages() -> list[tuple[str, str]]:
    """Mensagens registradas no logger (nível, mensagem), a partir de DEBUG."""
    from loguru import logger

    from rpa_3003.core.logger import get_logger

    get_logger()
    records: list[tuple[str, str]] = []
    logger.add(
        lambda message: records.append((message.record["level"].name, message.record["message"])),
        level="DEBUG",
    )
    return records


def _relative_files(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


# --- Constantes e descoberta -----------------------------------------------------------------


def test_contract_constants() -> None:
    """RF-054: constantes públicas da seção 8.23."""
    assert TEMPLATE_METADATA_FILE == "template.toml"
    assert TEMPLATE_SUFFIX == ".tpl"
    assert DOTFILE_PREFIX == "dot_"
    assert DEFAULT_TEMPLATE == "example"
    assert PROJECT_NAME_PATTERN.pattern == r"^[a-z][a-z0-9_]{2,49}$"
    assert frozenset({"template.toml", "__pycache__"}) == IGNORED_NAMES


def test_templates_root_is_package_folder() -> None:
    """RF-055: templates_root() é a pasta `rpa_3003/templates` via importlib.resources."""
    root = templates_root()
    assert isinstance(root, Traversable)
    assert root.joinpath("example", TEMPLATE_METADATA_FILE).is_file()


def test_list_templates_includes_packaged_example() -> None:
    """RF-050/RF-055: o template `example` do pacote é listado com seus metadados."""
    infos = {info.name: info for info in list_templates()}
    example = infos["example"]
    assert example.description == EXAMPLE_DESCRIPTION
    assert example.version == "1.0.0"
    assert example.ensure_dirs == ("logs", "data", "data/input", "data/output", "data/images")


def test_get_template_example_has_runtime_dirs() -> None:
    """Seção 8.24: `ensure_dirs` do example inclui logs e data."""
    info = get_template("example")
    assert "logs" in info.ensure_dirs
    assert "data" in info.ensure_dirs
    assert info.path.joinpath("main.py.tpl").is_file()


def test_new_template_folder_is_discovered(
    write_template: TemplateWriter, template_root: Path
) -> None:
    """RF-055: nova pasta com template.toml válido é listada sem alteração de código."""
    write_template("web_basico", {})
    write_template("minimo", {})
    (template_root / "sem_metadados").mkdir()
    (template_root / "solto.txt").write_text("não é template", encoding="utf-8")
    infos = list_templates()
    assert [info.name for info in infos] == ["minimo", "web_basico"]
    assert infos[1].path == template_root / "web_basico"
    assert get_template("web_basico").description == "Template mínimo de teste"


def test_get_template_unknown_lists_available(write_template: TemplateWriter) -> None:
    """RF-055: template inexistente gera TemplateNotFoundError (código 3) com os disponíveis."""
    write_template("minimo", {})
    write_template("web_basico", {})
    with pytest.raises(TemplateNotFoundError) as info:
        get_template("inexistente")
    assert info.value.exit_code is ExitCode.CONFIGURATION_ERROR
    assert info.value.details == {"disponiveis": ["minimo", "web_basico"]}
    assert "inexistente" in str(info.value)


def test_get_template_ignores_folder_without_metadata(
    write_template: TemplateWriter, template_root: Path
) -> None:
    """RF-055: pasta sem template.toml não é template."""
    write_template("minimo", {})
    (template_root / "rascunho").mkdir()
    with pytest.raises(TemplateNotFoundError):
        get_template("rascunho")


@pytest.mark.parametrize(
    ("metadata", "fragment"),
    [
        ('name = "minimo"\ndescription = ', "Metadados ilegíveis"),
        ("\xff\xfe".encode("latin-1"), "Metadados ilegíveis"),
        ('name = "minimo"\ndescription = "x"\n', "Metadados ausentes"),
        (_toml("outro_nome"), "name deve ser igual ao nome da pasta"),
        (_toml("minimo", description=" "), "description deve ser um texto não vazio"),
        (
            'name = "minimo"\ndescription = "x"\nversion = 1\nensure_dirs = []\n',
            "version deve ser um texto não vazio",
        ),
        (
            'name = "minimo"\ndescription = "x"\nversion = "1"\nensure_dirs = "logs"\n',
            "ensure_dirs deve ser uma lista de textos",
        ),
        (
            'name = "minimo"\ndescription = "x"\nversion = "1"\nensure_dirs = [1]\n',
            "ensure_dirs deve ser uma lista de textos",
        ),
        (_toml("minimo", ensure_dirs=("../fora",)), "sem '..'"),
        (_toml("minimo", ensure_dirs=("/absoluto",)), "sem '..'"),
        (_toml("minimo", ensure_dirs=("C:/raiz",)), "sem '..'"),
        (_toml("minimo", ensure_dirs=("logs\\\\sub",)), "sem '..'"),
        (_toml("minimo", ensure_dirs=("",)), "sem '..'"),
    ],
)
def test_invalid_metadata_raises_with_template_name(
    write_template: TemplateWriter, metadata: str | bytes, fragment: str
) -> None:
    """RF-055: template.toml ausente de chaves ou inválido gera TemplateRenderError com o nome."""
    write_template("minimo", {}, metadata=metadata)
    for call in (list_templates, lambda: get_template("minimo")):
        with pytest.raises(TemplateRenderError) as info:
            call()
        assert "'minimo'" in info.value.message
        assert fragment in info.value.message
        assert info.value.details["arquivo"] == TEMPLATE_METADATA_FILE


def test_missing_metadata_keys_are_listed(write_template: TemplateWriter) -> None:
    """RF-055: a mensagem lista todas as chaves ausentes."""
    write_template("minimo", {}, metadata='name = "minimo"\ndescription = "x"\n')
    with pytest.raises(TemplateRenderError, match="version, ensure_dirs"):
        get_template("minimo")


# --- Nome do projeto e contexto --------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("abc", "abc"),
        ("conciliacao_bancaria", "conciliacao_bancaria"),
        ("  emissao_notas_2026 \n", "emissao_notas_2026"),
        ("a" * 50, "a" * 50),
        ("a__", "a__"),
    ],
)
def test_validate_project_name_accepts(raw: str, expected: str) -> None:
    """RF-048: nomes no padrão são aceitos após strip()."""
    assert validate_project_name(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "ab", "a" * 51, "1abc", "_abc", "Abc", "abc-def", "abc def", "ação", "abc.d", "abc\nx"],
)
def test_validate_project_name_rejects(raw: str) -> None:
    """RF-048: nome fora do padrão gera InvalidProjectNameError (código 2) com a regra."""
    with pytest.raises(InvalidProjectNameError) as info:
        validate_project_name(raw)
    assert info.value.exit_code is ExitCode.USAGE_ERROR
    assert (
        "letras minúsculas, dígitos e '_', iniciando por letra, 3 a 50 caracteres"
        in info.value.message
    )


@pytest.mark.parametrize("raw", ["con", "prn", "aux", "nul", "com1", "lpt9", " con "])
def test_validate_project_name_rejects_windows_reserved(raw: str) -> None:
    """RF-048/RNF-002: nomes de dispositivo do Windows não viram pasta de projeto."""
    with pytest.raises(InvalidProjectNameError, match="nome reservado do Windows"):
        validate_project_name(raw)


def test_build_context_values() -> None:
    """RF-054: variáveis de contexto da seção 8.23."""
    with time_machine.travel(FIXED_NOW.astimezone(), tick=False):
        context = build_context(" conciliacao_bancaria ", author="ana.souza")
    assert context == {
        "project_name": "conciliacao_bancaria",
        "project_title": "Conciliacao Bancaria",
        "author": "ana.souza",
        "created_at": "2026-10-31",
        "framework_version": __version__,
    }


@pytest.mark.parametrize("author", [None, "", "   ", "\n"])
def test_build_context_defaults_author_to_system_user(
    monkeypatch: pytest.MonkeyPatch, author: str | None
) -> None:
    """RF-054: sem autor, usa default_user()."""
    monkeypatch.setattr(templates, "default_user", lambda: "carlos.lima")
    assert build_context("emissao_notas", author=author)["author"] == "carlos.lima"


def test_build_context_cleans_author_for_python_strings() -> None:
    """RF-053: o autor não pode quebrar as docstrings geradas (barra invertida, aspas, quebras)."""
    context = build_context("emissao_notas", author=' CORP\\ana  "silva"\r\n\tFinanças ')
    assert context["author"] == "CORP/ana 'silva' Finanças"


def test_build_context_validates_name() -> None:
    """RF-048: build_context valida o nome do projeto."""
    with pytest.raises(InvalidProjectNameError):
        build_context("Nome Invalido")


# --- Renderização ----------------------------------------------------------------------------


def test_render_template_creates_expected_files(
    minimal: TemplateInfo, context: dict[str, str], tmp_path: Path
) -> None:
    """RF-054: .tpl sem sufixo, dot_ vira '.', ignorados fora, ensure_dirs com .gitkeep."""
    destination = tmp_path / "saida"
    created = render_template(minimal, destination, context)
    assert created == sorted(created)
    assert {path.relative_to(destination).as_posix() for path in created} == EXPECTED_OUTPUTS
    assert _relative_files(destination) == EXPECTED_OUTPUTS
    for name in ("logs/.gitkeep", "data/.gitkeep", "data/input/.gitkeep"):
        assert (destination / name).read_bytes() == b""


def test_render_template_substitutes_variables(
    minimal: TemplateInfo, context: dict[str, str], tmp_path: Path
) -> None:
    """RF-054: substitute com as variáveis, '$$' vira '$' e quebras de linha preservadas."""
    destination = tmp_path / "saida"
    render_template(minimal, destination, context)
    main = (destination / "main.py").read_bytes().decode("utf-8")
    assert main == (
        '"""Projeto conciliacao_bancaria (Conciliacao Bancaria).\r\n'
        "\r\n"
        f"Autor: ana.souza; criado em 2026-10-31; rpa_3003 {__version__}.\r\n"
        '"""\r\n'
        "\r\n"
        'PRECO = "R$ 10,00"\r\n'
        'DESCRICAO = "Configuração de conciliacao_bancaria"\r\n'
    )
    assert (destination / ".env.example").read_text(encoding="utf-8") == (
        "RPA3003_ENV=dev\n# Projeto conciliacao_bancaria\n"
    )


def test_render_template_leaves_no_placeholders_and_compiles(
    minimal: TemplateInfo, context: dict[str, str], tmp_path: Path
) -> None:
    """RF-053/RF-054: arquivos renderizados sem '${' e os .py gerados compilam."""
    destination = tmp_path / "saida"
    created = render_template(minimal, destination, context)
    rendered = [path for path in created if path.suffix in {".py", ".example"}]
    assert rendered
    for path in rendered:
        assert "${" not in path.read_text(encoding="utf-8")
    for path in destination.rglob("*.py"):
        py_compile.compile(
            str(path), cfile=str(tmp_path / "pyc" / f"{path.stem}.pyc"), doraise=True
        )


def test_render_template_copies_other_files_byte_by_byte(
    minimal: TemplateInfo, context: dict[str, str], tmp_path: Path
) -> None:
    """RF-054: arquivos sem .tpl são copiados sem renderização."""
    destination = tmp_path / "saida"
    render_template(minimal, destination, context)
    assert (destination / "dados.bin").read_bytes() == BINARY_CONTENT
    assert (destination / ".config" / "ajustes.txt").read_text(encoding="utf-8") == LITERAL_CONTENT


def test_render_template_keeps_gitkeep_provided_by_template(
    write_template: TemplateWriter, context: dict[str, str], tmp_path: Path
) -> None:
    """RF-054: .gitkeep vindo do template não é duplicado nem sobrescrito."""
    write_template("minimo", {"logs/dot_gitkeep": "mantido\n"})
    destination = tmp_path / "saida"
    created = render_template(get_template("minimo"), destination, context)
    relative = [path.relative_to(destination).as_posix() for path in created]
    assert relative.count("logs/.gitkeep") == 1
    assert (destination / "logs" / ".gitkeep").read_text(encoding="utf-8") == "mantido\n"


def test_render_template_missing_variable(
    write_template: TemplateWriter, context: dict[str, str], tmp_path: Path
) -> None:
    """RF-054: variável inexistente gera TemplateRenderError com arquivo e variável."""
    write_template("minimo", {"workflows/dev/step_a.py.tpl": "CLIENTE = '${cliente}'\n"})
    with pytest.raises(TemplateRenderError) as info:
        render_template(get_template("minimo"), tmp_path / "saida", context)
    assert info.value.details == {"arquivo": "workflows/dev/step_a.py.tpl", "variavel": "cliente"}
    assert "cliente" in str(info.value)
    assert "workflows/dev/step_a.py.tpl" in str(info.value)


@pytest.mark.parametrize(
    ("content", "placeholder", "line"),
    [
        ("linha 1\ncusto = '$1000'\n", "$1000'", 2),
        ("a\nb\nvalor $ 10\n", "$", 3),
        ("${nome-invalido}\n", "${nome-invalido}", 1),
    ],
)
def test_render_template_invalid_placeholder(
    write_template: TemplateWriter,
    context: dict[str, str],
    tmp_path: Path,
    content: str,
    placeholder: str,
    line: int,
) -> None:
    """RF-054: '$' sem '$$' gera TemplateRenderError com o trecho e a linha."""
    write_template("minimo", {"README.md.tpl": content})
    with pytest.raises(TemplateRenderError, match=r"use \$\$") as info:
        render_template(get_template("minimo"), tmp_path / "saida", context)
    assert info.value.details == {
        "arquivo": "README.md.tpl",
        "variavel": placeholder,
        "linha": line,
    }


def test_render_template_rejects_non_utf8_template(
    write_template: TemplateWriter, context: dict[str, str], tmp_path: Path
) -> None:
    """RF-054/RNF-011: .tpl deve estar em UTF-8."""
    write_template("minimo", {"README.md.tpl": "Configuração".encode("cp1252")})
    with pytest.raises(TemplateRenderError, match="UTF-8") as info:
        render_template(get_template("minimo"), tmp_path / "saida", context)
    assert info.value.details == {"arquivo": "README.md.tpl"}


def test_render_template_rejects_duplicate_targets(
    write_template: TemplateWriter, context: dict[str, str], tmp_path: Path
) -> None:
    """RF-054: dois arquivos que geram o mesmo destino não se sobrescrevem em silêncio."""
    write_template("minimo", {"config.txt": "a", "config.txt.tpl": "b"})
    with pytest.raises(TemplateRenderError, match="mesmo destino") as info:
        render_template(get_template("minimo"), tmp_path / "saida", context)
    assert info.value.details["arquivo"] == "config.txt.tpl"


@pytest.mark.parametrize("name", ["dot_", "dot_.tpl", ".tpl"])
def test_render_template_rejects_invalid_output_names(
    write_template: TemplateWriter, context: dict[str, str], tmp_path: Path, name: str
) -> None:
    """RF-054: nomes que viram '', '.' ou '..' são recusados."""
    write_template("minimo", {name: "x"})
    with pytest.raises(TemplateRenderError, match="Nome de arquivo inválido") as info:
        render_template(get_template("minimo"), tmp_path / "saida", context)
    assert info.value.details == {"arquivo": name}


def test_render_template_write_error(
    minimal: TemplateInfo,
    context: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RF-054: falha de gravação vira TemplateRenderError com o arquivo e o erro."""
    original = Path.write_bytes

    def failing_write(self: Path, data: bytes) -> int:
        if self.name == "main.py":
            raise PermissionError("arquivo bloqueado")
        return original(self, data)

    monkeypatch.setattr(Path, "write_bytes", failing_write)
    with pytest.raises(TemplateRenderError, match="Falha ao gravar") as info:
        render_template(minimal, tmp_path / "saida", context)
    assert info.value.details["arquivo"] == "main.py.tpl"
    assert info.value.details["erro"] == "PermissionError: arquivo bloqueado"


def test_render_template_read_error(
    minimal: TemplateInfo,
    context: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RF-054: falha de leitura do template vira TemplateRenderError."""
    original = Path.read_bytes

    def failing_read(self: Path) -> bytes:
        if self.name == "dados.bin":
            raise OSError("disco indisponível")
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", failing_read)
    with pytest.raises(TemplateRenderError, match="Falha ao ler") as info:
        render_template(minimal, tmp_path / "saida", context)
    assert info.value.details["arquivo"] == "dados.bin"


def test_render_template_logs_summary(
    minimal: TemplateInfo,
    context: dict[str, str],
    tmp_path: Path,
    log_messages: list[tuple[str, str]],
) -> None:
    """Seção 10.11: resumo da renderização em DEBUG."""
    destination = tmp_path / "saida"
    render_template(minimal, destination, context)
    assert (
        "DEBUG",
        f"Template renderizado | template=minimo | destino={destination} | arquivos=9",
    ) in log_messages


# --- Criação atômica do projeto --------------------------------------------------------------


def test_create_project_success(
    minimal: TemplateInfo,
    projects_dir: Path,
    log_messages: list[tuple[str, str]],
) -> None:
    """RF-048: projeto criado no destino, caminho absoluto e log 'Projeto criado'."""
    created = create_project(
        "conciliacao_bancaria", projects_dir, template="minimo", author="ana.souza"
    )
    expected = projects_dir / "conciliacao_bancaria"
    assert created == expected
    assert created.is_absolute()
    assert _relative_files(created) == EXPECTED_OUTPUTS
    assert "ana.souza" in (created / "main.py").read_text(encoding="utf-8")
    assert [child.name for child in projects_dir.iterdir()] == ["conciliacao_bancaria"]
    assert (
        "INFO",
        f"Projeto criado | nome=conciliacao_bancaria | template=minimo | caminho={expected}",
    ) in log_messages


def test_create_project_uses_default_template(
    write_template: TemplateWriter, projects_dir: Path
) -> None:
    """RF-048: o template padrão é `example`."""
    write_template("example")
    created = create_project("emissao_notas", projects_dir)
    assert (created / "main.py").is_file()


def test_create_project_creates_projects_dir_and_resolves_relative(
    minimal: TemplateInfo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RF-048: pasta de projetos relativa ao diretório atual, criada se ausente."""
    monkeypatch.chdir(tmp_path)
    created = create_project("emissao_notas", Path("novos") / "projects", template="minimo")
    assert created == tmp_path / "novos" / "projects" / "emissao_notas"
    assert created.is_dir()


def test_create_project_existing_without_force(minimal: TemplateInfo, projects_dir: Path) -> None:
    """RF-048: projeto existente sem --force gera código 3 e nada é alterado."""
    existing = projects_dir / "emissao_notas"
    existing.mkdir()
    (existing / "meu_codigo.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(ProjectAlreadyExistsError) as info:
        create_project("emissao_notas", projects_dir, template="minimo")
    assert info.value.exit_code is ExitCode.CONFIGURATION_ERROR
    assert info.value.details == {"caminho": str(existing)}
    assert [child.name for child in projects_dir.iterdir()] == ["emissao_notas"]
    assert _relative_files(existing) == {"meu_codigo.py"}


def test_create_project_force_keeps_backup(
    minimal: TemplateInfo, projects_dir: Path, log_messages: list[tuple[str, str]]
) -> None:
    """RF-048: com --force, a pasta antiga vira <nome>.bak-<AAAAMMDDHHMMSS> e a nova é criada."""
    existing = projects_dir / "emissao_notas"
    existing.mkdir()
    (existing / "meu_codigo.py").write_text("x = 1\n", encoding="utf-8")
    with time_machine.travel(FIXED_NOW.astimezone(), tick=False):
        created = create_project("emissao_notas", projects_dir, template="minimo", force=True)
    backup = projects_dir / "emissao_notas.bak-20261031093015"
    assert sorted(child.name for child in projects_dir.iterdir()) == [
        "emissao_notas",
        "emissao_notas.bak-20261031093015",
    ]
    assert _relative_files(backup) == {"meu_codigo.py"}
    assert _relative_files(created) == EXPECTED_OUTPUTS
    assert (
        "INFO",
        f"Projeto existente renomeado | nome=emissao_notas | backup={backup}",
    ) in log_messages


def test_create_project_force_without_existing(minimal: TemplateInfo, projects_dir: Path) -> None:
    """RF-048: --force sem projeto existente apenas cria."""
    create_project("emissao_notas", projects_dir, template="minimo", force=True)
    assert [child.name for child in projects_dir.iterdir()] == ["emissao_notas"]


def test_create_project_render_failure_leaves_nothing(
    write_template: TemplateWriter, projects_dir: Path
) -> None:
    """RF-048: falha durante a renderização não deixa pasta parcial em projects/."""
    files = dict(MINIMAL_FILES)
    files["workflows/dev/zz_ultimo.py.tpl"] = "${variavel_inexistente}"
    write_template("minimo", files)
    with pytest.raises(TemplateRenderError):
        create_project("emissao_notas", projects_dir, template="minimo")
    assert list(projects_dir.iterdir()) == []


def test_create_project_force_render_failure_keeps_existing(
    write_template: TemplateWriter, projects_dir: Path
) -> None:
    """RF-048: com --force, falha na renderização não renomeia o projeto existente."""
    write_template("minimo", {"main.py.tpl": "${variavel_inexistente}"})
    existing = projects_dir / "emissao_notas"
    existing.mkdir()
    (existing / "meu_codigo.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(TemplateRenderError):
        create_project("emissao_notas", projects_dir, template="minimo", force=True)
    assert [child.name for child in projects_dir.iterdir()] == ["emissao_notas"]
    assert _relative_files(existing) == {"meu_codigo.py"}


def test_create_project_interrupt_removes_temp_dir(
    minimal: TemplateInfo, projects_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RF-048: até KeyboardInterrupt remove a pasta temporária antes de propagar."""
    original = templates._write_file
    calls: list[str] = []

    def interrupted(target: Path, data: bytes, relative: str) -> None:
        calls.append(relative)
        if len(calls) == 3:
            raise KeyboardInterrupt
        original(target, data, relative)

    monkeypatch.setattr(templates, "_write_file", interrupted)
    with pytest.raises(KeyboardInterrupt):
        create_project("emissao_notas", projects_dir, template="minimo")
    assert list(projects_dir.iterdir()) == []


def test_create_project_final_rename_failure_restores_backup(
    minimal: TemplateInfo,
    projects_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RF-048: se a renomeação final falhar, o backup volta ao nome original."""
    existing = projects_dir / "emissao_notas"
    existing.mkdir()
    (existing / "meu_codigo.py").write_text("x = 1\n", encoding="utf-8")
    original = Path.rename

    def failing_rename(self: Path, target: str | Path) -> Path:
        if self.name.startswith(".emissao_notas.tmp-"):
            raise PermissionError("pasta em uso")
        return original(self, target)

    monkeypatch.setattr(Path, "rename", failing_rename)
    with pytest.raises(TemplateRenderError, match="Falha ao mover") as info:
        create_project("emissao_notas", projects_dir, template="minimo", force=True)
    assert info.value.details["erro"] == "PermissionError: pasta em uso"
    assert [child.name for child in projects_dir.iterdir()] == ["emissao_notas"]
    assert _relative_files(existing) == {"meu_codigo.py"}


def test_create_project_backup_restore_failure_is_logged(
    minimal: TemplateInfo,
    projects_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    log_messages: list[tuple[str, str]],
) -> None:
    """RF-048: falha ao restaurar o backup gera WARNING e preserva o erro original."""
    (projects_dir / "emissao_notas").mkdir()
    original = Path.rename

    def failing_rename(self: Path, target: str | Path) -> Path:
        if self.name.startswith(".emissao_notas.tmp-") or ".bak-" in self.name:
            raise PermissionError("pasta em uso")
        return original(self, target)

    monkeypatch.setattr(Path, "rename", failing_rename)
    with pytest.raises(TemplateRenderError, match="Falha ao mover"):
        create_project("emissao_notas", projects_dir, template="minimo", force=True)
    warnings = [message for level, message in log_messages if level == "WARNING"]
    assert len(warnings) == 1
    assert warnings[0].startswith("Backup não restaurado | backup=")
    assert next(child.name for child in projects_dir.iterdir()).startswith("emissao_notas.bak-")


def test_create_project_validates_before_touching_disk(
    minimal: TemplateInfo, tmp_path: Path
) -> None:
    """RF-048: nome inválido ou template inexistente não criam a pasta de projetos."""
    projects = tmp_path / "nao_criada"
    with pytest.raises(InvalidProjectNameError):
        create_project("Nome Invalido", projects, template="minimo")
    with pytest.raises(TemplateNotFoundError):
        create_project("emissao_notas", projects, template="inexistente")
    assert not projects.exists()


def test_create_project_projects_dir_is_a_file(minimal: TemplateInfo, tmp_path: Path) -> None:
    """RF-048: pasta de projetos que não pode ser criada gera ConfigurationError (código 3)."""
    blocker = tmp_path / "projects"
    blocker.write_text("não é pasta", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="diretório de projetos") as info:
        create_project("emissao_notas", blocker, template="minimo")
    assert info.value.exit_code is ExitCode.CONFIGURATION_ERROR
    assert info.value.details["caminho"] == str(blocker)


def test_create_project_final_rename_failure_without_backup(
    minimal: TemplateInfo,
    projects_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RF-048: falha na renomeação final sem projeto anterior não deixa pasta temporária."""

    def failing_rename(self: Path, target: str | Path) -> Path:
        raise PermissionError("pasta em uso")

    monkeypatch.setattr(Path, "rename", failing_rename)
    with pytest.raises(TemplateRenderError, match="Falha ao mover"):
        create_project("emissao_notas", projects_dir, template="minimo")
    assert list(projects_dir.iterdir()) == []


# --- Template example (seção 11) -------------------------------------------------------------


@pytest.fixture
def example_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Workspace com espaço e acentos no caminho (RNF-002), usado como diretório atual."""
    workspace = tmp_path / "Automação RPA" / "rpa-framework"
    workspace.mkdir(parents=True)
    monkeypatch.chdir(workspace)
    return workspace


def _template_files(directory: Traversable, prefix: str = "") -> set[str]:
    """Caminhos relativos dos arquivos de um template do pacote, sem `__pycache__`."""
    found: set[str] = set()
    for entry in directory.iterdir():
        if entry.name == "__pycache__":
            continue
        if entry.is_dir():
            found |= _template_files(entry, f"{prefix}{entry.name}/")
        else:
            found.add(f"{prefix}{entry.name}")
    return found


def _import_global_vars(project: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Importa o `global_vars.py` do projeto gerado como módulo `global_vars`."""
    spec = importlib.util.spec_from_file_location("global_vars", project / "global_vars.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "global_vars", module)
    spec.loader.exec_module(module)
    return module


def _read_single_json(directory: Path, pattern: str) -> dict[str, Any]:
    """Conteúdo do único arquivo JSON da pasta que casa com o padrão."""
    [path] = sorted(directory.glob(pattern))
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def test_example_template_has_section_11_tree() -> None:
    """RF-053/seção 11.1: o template example tem exatamente os arquivos da árvore normativa."""
    assert _template_files(get_template("example").path) == EXAMPLE_TEMPLATE_FILES


def test_example_prd_steps_mirror_dev() -> None:
    """Seção 11.9: steps de prd iguais aos de dev, exceto '| dev)' → '| prd)' na primeira linha."""
    workflows = get_template("example").path.joinpath("workflows")
    for step in EXAMPLE_STEPS:
        dev = workflows.joinpath("dev", f"{step}.py.tpl").read_text(encoding="utf-8")
        prd = workflows.joinpath("prd", f"{step}.py.tpl").read_text(encoding="utf-8")
        dev_first, dev_rest = dev.split("\n", 1)
        prd_first, prd_rest = prd.split("\n", 1)
        assert "(${project_name} | dev)." in dev_first
        assert prd_first == dev_first.replace("| dev)", "| prd)")
        assert prd_rest == dev_rest
    for name in ("__init__.py.tpl", *(f"{step}.py.tpl" for step in EXAMPLE_STEPS)):
        assert "workflows.dev" not in workflows.joinpath("prd", name).read_text(encoding="utf-8")


def test_example_project_has_required_structure(sample_project: Path) -> None:
    """RF-053/RF-001: o projeto gerado pelo example tem só os arquivos esperados e é válido."""
    assert _relative_files(sample_project) == EXAMPLE_OUTPUTS
    assert validate_project_structure(sample_project) == []


def test_example_renders_all_variables(projects_dir: Path) -> None:
    """Seção 11.12: nenhum arquivo gerado contém '${' e as variáveis aparecem substituídas."""
    with time_machine.travel(FIXED_NOW.astimezone(), tick=False):
        project = create_project("conciliacao_bancaria", projects_dir, author="ana.souza")
    for path in project.rglob("*"):
        if path.is_file():
            assert "${" not in path.read_text(encoding="utf-8"), path
    main_py = (project / "main.py").read_text(encoding="utf-8")
    assert f"Gerado por rpa_3003 {__version__} em 2026-10-31 (autor: ana.souza)." in main_py
    assert "rpa3003 run conciliacao_bancaria --env prd" in main_py
    global_vars = (project / "global_vars.py").read_text(encoding="utf-8")
    assert 'PROJECT_NAME: Final[str] = "conciliacao_bancaria"' in global_vars
    assert 'PROJECT_TITLE: Final[str] = "Conciliacao Bancaria"' in global_vars
    assert 'vault_secret_path="rpa/conciliacao_bancaria/prd/sistema"' in global_vars
    readme = (project / "README.md").read_text(encoding="utf-8").splitlines()
    assert readme[0] == "# Conciliacao Bancaria"
    assert readme[2] == (
        "Projeto RPA `conciliacao_bancaria` criado em 2026-10-31 por ana.souza com rpa_3003 "
        f"{__version__}."
    )
    env_example = (project / ".env.example").read_text(encoding="utf-8")
    assert (
        "# RPA3003_VAULT_LOCAL_SECRETS_FILE=C:/rpa/segredos/conciliacao_bancaria.secrets.json\n"
        in env_example
    )
    assert env_example.startswith("# Modelo de configuração do projeto conciliacao_bancaria.\n")
    for env in ("dev", "prd"):
        step = (project / "workflows" / env / "step_setup_env.py").read_text(encoding="utf-8")
        assert step.startswith(
            f'"""Step 1 - preparação do ambiente (conciliacao_bancaria | {env})."""\n'
        )


def test_example_python_files_compile_and_pass_ruff(sample_project: Path, tmp_path: Path) -> None:
    """RF-053/seção 11.12: os .py gerados compilam e passam no `ruff check` do workspace, também
    com um nome de projeto no tamanho máximo (50 caracteres, limite de linha do ruff)."""
    longest = create_project("r" * 50, sample_project.parent, author="maria.fernanda.souza")
    python_files = sorted([*sample_project.rglob("*.py"), *longest.rglob("*.py")])
    assert len(python_files) == 22
    for index, path in enumerate(python_files):
        py_compile.compile(str(path), cfile=str(tmp_path / "pyc" / f"{index}.pyc"), doraise=True)
    # Com --config, os padrões relativos do pyproject (ex.: per-file-ignores "projects/**")
    # partem do diretório atual; por isso o ruff roda na pasta que contém projects/.
    completed = subprocess.run(  # noqa: S603 - interpretador atual com argumentos fixos
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--no-cache",
            "--config",
            str(WORKSPACE_PYPROJECT),
            "projects",
        ],
        cwd=sample_project.parent.parent,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_example_has_no_secret_findings(sample_project: Path) -> None:
    """OBJ-05: o detect-secrets não aponta achados no template example nem no projeto gerado."""
    from detect_secrets import SecretsCollection
    from detect_secrets.settings import default_settings

    template_dir = Path(templates.__file__).resolve().parent / "example"
    findings = SecretsCollection()
    with default_settings():
        for root in (template_dir, sample_project):
            for path in sorted(root.rglob("*")):
                if path.is_file() and "__pycache__" not in path.parts:
                    findings.scan_file(str(path))
    assert findings.json() == {}


def test_example_global_vars_import_has_no_io(
    sample_project: Path, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture
) -> None:
    """RF-012: importar global_vars com sockets bloqueados não tenta conexão nem usa o Vault."""
    attempts: list[str] = []

    def blocked(*args: object, **kwargs: object) -> NoReturn:
        attempts.append(repr(args))
        raise OSError("rede bloqueada pelo teste")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    vault_factory = mocker.patch("rpa_3003.core.vault.get_vault_client")
    gv = _import_global_vars(sample_project, monkeypatch)
    assert attempts == []
    vault_factory.assert_not_called()
    assert set(gv.ENV_CONFIG) == set(Environment)
    assert gv.PROJECT_NAME == "projeto_teste"
    expected_paths = {
        "PROJECT_ROOT": sample_project,
        "DATA_DIR": sample_project / "data",
        "INPUT_DIR": sample_project / "data" / "input",
        "OUTPUT_DIR": sample_project / "data" / "output",
        "IMAGES_DIR": sample_project / "data" / "images",
        "LOGS_DIR": sample_project / "logs",
    }
    assert {name: getattr(gv, name) for name in expected_paths} == expected_paths
    dev = gv.get_env_config(Environment.DEV)
    prd = gv.get_env_config(Environment.PRD)
    assert dev.vault_secret_path is None
    assert prd.vault_secret_path == "rpa/projeto_teste/prd/sistema"  # noqa: S105 - caminho
    assert not dev.browser_enabled
    assert not prd.browser_enabled
    with pytest.raises(dataclasses.FrozenInstanceError):
        dev.base_url = "https://outro.example.com"


def test_example_get_credentials_dev_skips_vault(
    sample_project: Path, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture
) -> None:
    """RF-025: em dev (vault_secret_path=None) retorna None e nenhum cliente Vault é criado."""
    vault_factory = mocker.patch("rpa_3003.core.vault.get_vault_client")
    gv = _import_global_vars(sample_project, monkeypatch)
    assert gv.get_credentials(Environment.DEV) is None
    vault_factory.assert_not_called()


def test_example_get_credentials_prd_reads_vault(
    sample_project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_hvac: MagicMock,
) -> None:
    """RF-025: em prd, lê usuário e senha de rpa/<projeto>/prd/sistema no Vault simulado."""
    monkeypatch.chdir(tmp_path)  # sem .env
    monkeypatch.setenv("RPA3003_VAULT_URL", "https://vault.empresa.local:8200")
    monkeypatch.setenv("RPA3003_VAULT_ROLE_ID", "role")
    monkeypatch.setenv("RPA3003_VAULT_SECRET_ID", "secret-id")
    gv = _import_global_vars(sample_project, monkeypatch)
    credentials = gv.get_credentials(Environment.PRD)
    assert isinstance(credentials, Credentials)
    assert credentials.username == "robo.teste"
    assert credentials.password.get_secret_value() == "S3nh@Forte"
    fake_hvac.return_value.secrets.kv.v2.read_secret_version.assert_called_once_with(
        path="rpa/projeto_teste/prd/sistema", mount_point="secret", raise_on_deleted_version=True
    )


def test_example_validate_returns_zero_after_create(
    example_workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Seção 11.12: `rpa3003 validate <nome>` retorna 0 imediatamente após `create`."""
    assert main(["create", "projeto_teste"]) == ExitCode.SUCCESS
    assert main(["validate", "projeto_teste"]) == ExitCode.SUCCESS
    assert "Projeto 'projeto_teste' válido" in capsys.readouterr().out
    assert (example_workspace / "projects" / "projeto_teste" / "main.py").is_file()


def test_example_run_dev_succeeds_offline(example_workspace: Path, sample_csv: Path) -> None:
    """RF-053/seção 11.12/RNF-002: `run --env dev` retorna 0 sem Vault, navegador ou rede e
    gera relatorio_<run_id>.json e status_<run_id>.json em data/output."""
    project = create_project("projeto_teste", example_workspace / "projects", author="ana.souza")
    shutil.copyfile(sample_csv, project / "data" / "input" / "vendas.csv")
    assert main(["run", "projeto_teste", "--env", "dev"]) == ExitCode.SUCCESS
    output = project / "data" / "output"
    status = _read_single_json(output, "status_*.json")
    run_id = status["run_id"]
    assert sorted(path.name for path in output.iterdir()) == [
        ".gitkeep",
        f"relatorio_{run_id}.json",
        f"status_{run_id}.json",
    ]
    report = _read_single_json(output, "relatorio_*.json")
    assert report == {
        "run_id": run_id,
        "ambiente": "dev",
        "arquivos": 1,
        "linhas": 3,
        "detalhes": [{"arquivo": "vendas.csv", "linhas": 3}],
    }
    assert (status["projeto"], status["ambiente"]) == ("projeto_teste", "dev")
    assert status["sucesso"] is True
    assert status["erro"] is None
    assert status["relatorio"] == report


def test_example_run_prd_without_vault_returns_configuration_error(
    example_workspace: Path,
) -> None:
    """Seção 11.12: `run --env prd` sem variáveis do Vault retorna 3, executa o wrapup e grava
    status_<run_id>.json com "sucesso": false."""
    project = create_project("projeto_teste", example_workspace / "projects", author="ana.souza")
    assert main(["run", "projeto_teste", "--env", "prd"]) == ExitCode.CONFIGURATION_ERROR
    output = project / "data" / "output"
    status = _read_single_json(output, "status_*.json")
    assert sorted(path.name for path in output.iterdir()) == [
        ".gitkeep",
        f"status_{status['run_id']}.json",
    ]
    assert (status["projeto"], status["ambiente"]) == ("projeto_teste", "prd")
    assert status["sucesso"] is False
    assert status["erro"].startswith(
        "ConfigurationError: Parâmetros obrigatórios do Vault ausentes"
    )
    assert status["relatorio"] is None
