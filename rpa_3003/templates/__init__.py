"""Motor de descoberta e renderização de templates.

Descoberta de templates, validação do nome de projeto e renderização atômica do scaffold
(RF-054, RF-055, seção 8.23).
"""

from __future__ import annotations

import re
import shutil
import string
import tomllib
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path, PurePosixPath
from typing import Any, Final

from rpa_3003._version import __version__
from rpa_3003.core.config import PathLike
from rpa_3003.core.exceptions import (
    ConfigurationError,
    InvalidProjectNameError,
    ProjectAlreadyExistsError,
    TemplateNotFoundError,
    TemplateRenderError,
)
from rpa_3003.core.logger import default_user, get_logger

__all__: list[str] = [
    "DEFAULT_TEMPLATE",
    "DOTFILE_PREFIX",
    "IGNORED_NAMES",
    "PROJECT_NAME_PATTERN",
    "TEMPLATE_METADATA_FILE",
    "TEMPLATE_SUFFIX",
    "TemplateInfo",
    "build_context",
    "create_project",
    "get_template",
    "list_templates",
    "render_template",
    "templates_root",
    "validate_project_name",
]

TEMPLATE_METADATA_FILE: Final[str] = "template.toml"
TEMPLATE_SUFFIX: Final[str] = ".tpl"
DOTFILE_PREFIX: Final[str] = "dot_"
DEFAULT_TEMPLATE: Final[str] = "example"
PROJECT_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]{2,49}$")
IGNORED_NAMES: Final[frozenset[str]] = frozenset({TEMPLATE_METADATA_FILE, "__pycache__"})

_PACKAGE: Final[str] = "rpa_3003.templates"
_PROJECT_NAME_RULE: Final[str] = (
    "letras minúsculas, dígitos e '_', iniciando por letra, 3 a 50 caracteres"
)
# Nomes de dispositivo do Windows: casam com PROJECT_NAME_PATTERN, mas não servem como pasta.
_WINDOWS_RESERVED_NAMES: Final[frozenset[str]] = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
)
_METADATA_KEYS: Final[tuple[str, ...]] = ("name", "description", "version", "ensure_dirs")
_COMPILED_SUFFIX: Final[str] = ".pyc"
_GITKEEP: Final[str] = ".gitkeep"
_TEMP_MARKER: Final[str] = ".tmp-"
_TEMP_ID_LENGTH: Final[int] = 8
_BACKUP_MARKER: Final[str] = ".bak-"
_BACKUP_TIMESTAMP: Final[str] = "%Y%m%d%H%M%S"
_DATE_FORMAT: Final[str] = "%Y-%m-%d"
_INVALID_OUTPUT_NAMES: Final[frozenset[str]] = frozenset({"", ".", ".."})
_PLACEHOLDER_MAX_LENGTH: Final[int] = 20
# Barra invertida e aspas duplas quebrariam as strings Python geradas (ex.: "DOMINIO\usuario").
_AUTHOR_REPLACEMENTS: Final[dict[int, str]] = str.maketrans({"\\": "/", '"': "'"})


@dataclass(frozen=True, slots=True)
class TemplateInfo:
    """Metadados de um template, lidos do seu `template.toml`.

    Attributes:
        name: Nome do template (igual ao nome da pasta).
        description: Descrição exibida por `rpa3003 templates`.
        version: Versão do template.
        ensure_dirs: Pastas relativas criadas com `.gitkeep` no projeto gerado.
        path: Pasta do template dentro do pacote.
    """

    name: str
    description: str
    version: str
    ensure_dirs: tuple[str, ...]
    path: Traversable


def templates_root() -> Traversable:
    """Pasta dos templates embutidos no pacote.

    Returns:
        `importlib.resources.files("rpa_3003.templates")`.
    """
    return resources.files(_PACKAGE)


def list_templates() -> list[TemplateInfo]:
    """Lista os templates disponíveis: subpastas de `templates_root()` com `template.toml`.

    Uma pasta nova com `template.toml` válido passa a ser listada sem alteração de código
    (RF-055).

    Returns:
        Metadados de cada template, ordenados por nome.

    Raises:
        TemplateRenderError: Se o `template.toml` de algum template estiver ilegível, incompleto
            ou inválido; a mensagem traz o nome do template.
    """
    return [_load_template(directory) for directory in _template_dirs()]


def get_template(name: str) -> TemplateInfo:
    """Retorna os metadados do template pelo nome da pasta.

    Args:
        name: Nome do template (ex.: ``"example"``).

    Returns:
        Metadados do template.

    Raises:
        TemplateNotFoundError: Se o template não existir; `details["disponiveis"]` lista os
            templates existentes.
        TemplateRenderError: Se o `template.toml` do template estiver inválido.
    """
    directories = _template_dirs()
    for directory in directories:
        if directory.name == name:
            return _load_template(directory)
    raise TemplateNotFoundError(
        f"Template não encontrado: '{name}'",
        details={"disponiveis": [directory.name for directory in directories]},
    )


def validate_project_name(name: str) -> str:
    """Valida o nome de um projeto após `strip()`.

    Args:
        name: Nome informado pelo usuário.

    Returns:
        O nome sem espaços nas pontas.

    Raises:
        InvalidProjectNameError: Se o nome não casar com `PROJECT_NAME_PATTERN` (letras
            minúsculas, dígitos e '_', iniciando por letra, 3 a 50 caracteres) ou for um nome
            reservado do Windows (ex.: ``con``).
    """
    candidate = name.strip()
    if PROJECT_NAME_PATTERN.fullmatch(candidate) is None:
        raise InvalidProjectNameError(
            f"Nome de projeto inválido: '{name}'. Use {_PROJECT_NAME_RULE}"
        )
    if candidate in _WINDOWS_RESERVED_NAMES:
        raise InvalidProjectNameError(
            f"Nome de projeto inválido: '{candidate}' é um nome reservado do Windows"
        )
    return candidate


def build_context(project_name: str, *, author: str | None = None) -> dict[str, str]:
    """Monta as variáveis de renderização dos arquivos `.tpl`.

    Args:
        project_name: Nome do projeto; é validado com `validate_project_name`.
        author: Autor gravado nos arquivos; `None` ou vazio usa `default_user()`. Caracteres
            não imprimíveis viram espaço, `\\` vira `/` e `"` vira `'`, para não quebrar as strings
            Python geradas.

    Returns:
        Dicionário com `project_name`, `project_title`, `author`, `created_at` (AAAA-MM-DD) e
        `framework_version`.

    Raises:
        InvalidProjectNameError: Se o nome do projeto for inválido.
    """
    name = validate_project_name(project_name)
    return {
        "project_name": name,
        "project_title": name.replace("_", " ").title(),
        "author": _clean_author(author) or default_user(),
        "created_at": datetime.now().strftime(_DATE_FORMAT),
        "framework_version": __version__,
    }


def render_template(
    template: TemplateInfo, destination: Path, context: Mapping[str, str]
) -> list[Path]:
    """Renderiza os arquivos do template em `destination` (RF-054).

    Percorre o template recursivamente, ignorando `IGNORED_NAMES` e `*.pyc`. Arquivos `.tpl`
    perdem o sufixo e têm o conteúdo UTF-8 renderizado com `string.Template.substitute`; os
    demais são copiados byte a byte. O prefixo `dot_` de arquivos e pastas vira `.`. Por fim,
    cria as pastas de `ensure_dirs` com um `.gitkeep` vazio. As quebras de linha dos arquivos
    do template são preservadas.

    Args:
        template: Template a renderizar.
        destination: Pasta de destino; é criada se não existir.
        context: Valores das variáveis `${nome}` dos arquivos `.tpl`.

    Returns:
        Arquivos criados, ordenados.

    Raises:
        TemplateRenderError: Se uma variável não estiver em `context` (`details` com `arquivo`
            e `variavel`), se houver marcador `$` inválido, arquivo `.tpl` fora de UTF-8, nome
            de saída inválido ou repetido, ou erro de leitura ou gravação.
    """
    target_root = Path(destination)
    created: list[Path] = []
    targets: set[str] = set()
    for source, parts in _iter_template_files(template.path):
        relative = "/".join(parts)
        target = target_root.joinpath(*_output_parts(parts, relative))
        if str(target).casefold() in targets:
            raise TemplateRenderError(
                "Dois arquivos do template geram o mesmo destino",
                details={"arquivo": relative, "destino": str(target)},
            )
        targets.add(str(target).casefold())
        data = _read_bytes(source, relative)
        if parts[-1].endswith(TEMPLATE_SUFFIX):
            data = _render_text(data, context, relative).encode("utf-8")
        _write_file(target, data, relative)
        created.append(target)
    created.extend(_ensure_dirs(target_root, template.ensure_dirs))
    get_logger().debug(
        "Template renderizado | template={} | destino={} | arquivos={}",
        template.name,
        target_root,
        len(created),
    )
    return sorted(created)


def create_project(
    name: str,
    projects_dir: PathLike,
    *,
    template: str = DEFAULT_TEMPLATE,
    author: str | None = None,
    force: bool = False,
) -> Path:
    """Cria um projeto a partir de um template de forma atômica (RF-048, RF-054).

    O template é renderizado em `projects_dir/.<nome>.tmp-<id>` e a pasta é renomeada para o
    destino só no fim. Em qualquer exceção, a pasta temporária é removida e a exceção é
    relançada; com `force`, o projeto existente só é renomeado para
    `<nome>.bak-<AAAAMMDDHHMMSS>` depois que a renderização termina sem erro.

    Args:
        name: Nome do projeto.
        projects_dir: Pasta de projetos; é criada se não existir.
        template: Nome do template.
        author: Autor gravado nos arquivos; `None` usa o usuário do sistema.
        force: Se o projeto existir, renomeia a pasta existente para backup e recria.

    Returns:
        Caminho absoluto do projeto criado.

    Raises:
        InvalidProjectNameError: Se o nome for inválido.
        TemplateNotFoundError: Se o template não existir.
        ProjectAlreadyExistsError: Se o projeto existir e `force` for `False`.
        TemplateRenderError: Se a renderização ou a movimentação das pastas falhar.
        ConfigurationError: Se a pasta de projetos não puder ser criada.
    """
    project_name = validate_project_name(name)
    info = get_template(template)
    base = _prepare_projects_dir(projects_dir)
    destination = base / project_name
    if destination.exists(follow_symlinks=False) and not force:
        raise ProjectAlreadyExistsError(
            f"Projeto já existe: '{project_name}'", details={"caminho": str(destination)}
        )
    context = build_context(project_name, author=author)
    temp_dir = base / f".{project_name}{_TEMP_MARKER}{uuid.uuid4().hex[:_TEMP_ID_LENGTH]}"
    try:
        render_template(info, temp_dir, context)
        _move_into_place(temp_dir, destination, force=force)
    except BaseException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    get_logger().info(
        "Projeto criado | nome={} | template={} | caminho={}", project_name, info.name, destination
    )
    return destination


def _template_dirs() -> list[Traversable]:
    """Subpastas de `templates_root()` que contêm `template.toml`, ordenadas por nome."""
    directories = [
        entry
        for entry in templates_root().iterdir()
        if entry.is_dir() and entry.joinpath(TEMPLATE_METADATA_FILE).is_file()
    ]
    return sorted(directories, key=lambda entry: entry.name)


def _load_template(directory: Traversable) -> TemplateInfo:
    """Lê e valida o `template.toml` de uma pasta de template."""
    name = directory.name
    try:
        metadata = tomllib.loads(
            directory.joinpath(TEMPLATE_METADATA_FILE).read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:  # TOMLDecodeError e UnicodeDecodeError são ValueError
        raise TemplateRenderError(
            f"Metadados ilegíveis no template '{name}'",
            details={"arquivo": TEMPLATE_METADATA_FILE, "erro": _describe(exc)},
        ) from exc
    missing = [key for key in _METADATA_KEYS if key not in metadata]
    if missing:
        raise TemplateRenderError(
            f"Metadados ausentes no template '{name}': {', '.join(missing)}",
            details={"arquivo": TEMPLATE_METADATA_FILE},
        )
    if metadata["name"] != name:
        raise _invalid_metadata(name, "name deve ser igual ao nome da pasta do template")
    return TemplateInfo(
        name=name,
        description=_text_field(metadata, "description", name),
        version=_text_field(metadata, "version", name),
        ensure_dirs=_ensure_dirs_field(metadata, name),
        path=directory,
    )


def _text_field(metadata: Mapping[str, Any], key: str, template: str) -> str:
    """Campo de texto obrigatório e não vazio do `template.toml`."""
    value = metadata[key]
    if not isinstance(value, str) or not value.strip():
        raise _invalid_metadata(template, f"{key} deve ser um texto não vazio")
    return value


def _ensure_dirs_field(metadata: Mapping[str, Any], template: str) -> tuple[str, ...]:
    """Valida `ensure_dirs`: lista de caminhos relativos no formato POSIX, sem '..'."""
    value = metadata["ensure_dirs"]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _invalid_metadata(template, "ensure_dirs deve ser uma lista de textos")
    for item in value:
        if not _is_safe_relative_dir(item):
            raise _invalid_metadata(
                template, f"ensure_dirs aceita só caminhos relativos sem '..': '{item}'"
            )
    return tuple(value)


def _is_safe_relative_dir(value: str) -> bool:
    """Indica se o caminho é relativo, não vazio, sem '..', drive nem barra invertida."""
    path = PurePosixPath(value)
    return (
        bool(path.parts)
        and not path.is_absolute()
        and ".." not in path.parts
        and "\\" not in value
        and ":" not in value
    )


def _invalid_metadata(template: str, problem: str) -> TemplateRenderError:
    """Erro de metadado inválido com o nome do template na mensagem."""
    return TemplateRenderError(
        f"Metadado inválido no template '{template}': {problem}",
        details={"arquivo": TEMPLATE_METADATA_FILE},
    )


def _iter_template_files(
    directory: Traversable, parents: tuple[str, ...] = ()
) -> Iterator[tuple[Traversable, tuple[str, ...]]]:
    """Arquivos do template com o caminho relativo em partes, em ordem determinística."""
    for entry in sorted(directory.iterdir(), key=lambda item: item.name):
        if entry.name in IGNORED_NAMES or entry.name.endswith(_COMPILED_SUFFIX):
            continue
        parts = (*parents, entry.name)
        if entry.is_dir():
            yield from _iter_template_files(entry, parts)
        elif entry.is_file():
            yield entry, parts


def _output_parts(parts: tuple[str, ...], relative: str) -> list[str]:
    """Nomes de saída: sem o sufixo `.tpl` no arquivo e com `dot_` trocado por `.`."""
    *directories, file_name = parts
    output = [*directories, file_name.removesuffix(TEMPLATE_SUFFIX)]
    converted = [
        f".{part.removeprefix(DOTFILE_PREFIX)}" if part.startswith(DOTFILE_PREFIX) else part
        for part in output
    ]
    if any(part in _INVALID_OUTPUT_NAMES for part in converted):
        raise TemplateRenderError(
            "Nome de arquivo inválido no template", details={"arquivo": relative}
        )
    return converted


def _read_bytes(source: Traversable, relative: str) -> bytes:
    """Lê um arquivo do template convertendo `OSError` em `TemplateRenderError`."""
    try:
        return source.read_bytes()
    except OSError as exc:
        raise TemplateRenderError(
            "Falha ao ler arquivo do template",
            details={"arquivo": relative, "erro": _describe(exc)},
        ) from exc


def _render_text(data: bytes, context: Mapping[str, str], relative: str) -> str:
    """Renderiza o conteúdo de um `.tpl` com `string.Template.substitute`."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TemplateRenderError(
            "Arquivo de template não está em UTF-8", details={"arquivo": relative}
        ) from exc
    try:
        return string.Template(text).substitute(context)
    except KeyError as exc:
        variable = str(exc.args[0])
        raise TemplateRenderError(
            f"Variável de template não definida: '{variable}'",
            details={"arquivo": relative, "variavel": variable},
        ) from exc
    except ValueError as exc:
        placeholder, line = _invalid_placeholder(text)
        raise TemplateRenderError(
            "Marcador '$' inválido no template (use $$ para '$' literal)",
            details={"arquivo": relative, "variavel": placeholder, "linha": line},
        ) from exc


def _invalid_placeholder(text: str) -> tuple[str, int]:
    """Trecho e linha do primeiro marcador `$` inválido (só chamado após `ValueError`)."""
    match = next(
        found
        for found in string.Template.pattern.finditer(text)
        if found.group("invalid") is not None
    )
    start = match.start()
    snippet = text[start:].split(maxsplit=1)[0][:_PLACEHOLDER_MAX_LENGTH]
    return snippet, text.count("\n", 0, start) + 1


def _write_file(target: Path, data: bytes, relative: str) -> None:
    """Grava um arquivo gerado criando as pastas intermediárias."""
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    except OSError as exc:
        raise TemplateRenderError(
            "Falha ao gravar arquivo do projeto",
            details={"arquivo": relative, "destino": str(target), "erro": _describe(exc)},
        ) from exc


def _ensure_dirs(root: Path, directories: tuple[str, ...]) -> list[Path]:
    """Cria as pastas de `ensure_dirs` com `.gitkeep`; retorna os `.gitkeep` criados."""
    created: list[Path] = []
    for relative in directories:
        gitkeep = root.joinpath(*PurePosixPath(relative).parts, _GITKEEP)
        if gitkeep.exists():
            continue
        _write_file(gitkeep, b"", f"{relative}/{_GITKEEP}")
        created.append(gitkeep)
    return created


def _prepare_projects_dir(projects_dir: PathLike) -> Path:
    """Resolve a pasta de projetos como caminho absoluto, criando-a se não existir."""
    base = Path(projects_dir).expanduser().resolve()
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigurationError(
            "Não foi possível criar o diretório de projetos",
            details={"caminho": str(base), "erro": _describe(exc)},
        ) from exc
    return base


def _move_into_place(source: Path, destination: Path, *, force: bool) -> None:
    """Renomeia a pasta renderizada para o destino, fazendo backup do existente com `force`.

    Se a renomeação final falhar, o backup volta para o nome original.
    """
    backup: Path | None = None
    if force and destination.exists(follow_symlinks=False):
        backup = destination.with_name(
            f"{destination.name}{_BACKUP_MARKER}{datetime.now():{_BACKUP_TIMESTAMP}}"
        )
        _rename(destination, backup)
        get_logger().info(
            "Projeto existente renomeado | nome={} | backup={}", destination.name, backup
        )
    try:
        _rename(source, destination)
    except TemplateRenderError:
        if backup is not None:
            _restore_backup(backup, destination)
        raise


def _restore_backup(backup: Path, destination: Path) -> None:
    """Devolve o nome original ao projeto renomeado para backup (melhor esforço)."""
    try:
        backup.rename(destination)
    except OSError as exc:
        get_logger().warning("Backup não restaurado | backup={} | erro={}", backup, _describe(exc))


def _rename(source: Path, target: Path) -> None:
    """`Path.rename` convertendo `OSError` em `TemplateRenderError`."""
    try:
        source.rename(target)
    except OSError as exc:
        raise TemplateRenderError(
            "Falha ao mover a pasta do projeto",
            details={"origem": str(source), "destino": str(target), "erro": _describe(exc)},
        ) from exc


def _clean_author(author: str | None) -> str:
    """Normaliza o autor para os arquivos gerados; retorna vazio se nada sobrar."""
    if author is None:
        return ""
    # Caracteres não imprimíveis (quebra de linha, tabulação) quebrariam a linha da docstring.
    printable = "".join(char if char.isprintable() else " " for char in author)
    return " ".join(printable.translate(_AUTHOR_REPLACEMENTS).split())


def _describe(exc: BaseException) -> str:
    """Classe e mensagem de uma exceção para `details`."""
    return f"{type(exc).__name__}: {exc}"
