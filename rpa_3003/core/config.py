"""Gerenciamento de configurações.

Modelos de configuração do framework e do Vault, enum de ambiente, caminhos de projeto,
constantes de estrutura e funções de resolução e validação de projetos.
"""

from __future__ import annotations

import ast
import functools
import os
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Self, TypeAlias

from pydantic import AliasChoices, Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from rpa_3003.core.exceptions import (
    ConfigurationError,
    InvalidEnvironmentError,
    ProjectNotFoundError,
)
from rpa_3003.core.logger import LogLevel

__all__: list[str] = [
    "PROJECT_LOCK_FILENAME",
    "REQUIRED_PROJECT_ITEMS",
    "STEP_MODULES",
    "Environment",
    "FrameworkSettings",
    "PathLike",
    "ProjectPaths",
    "VaultAuthMethod",
    "VaultSettings",
    "find_project",
    "get_settings",
    "list_projects",
    "load_settings",
    "load_vault_settings",
    "reset_settings_cache",
    "resolve_environment",
    "resolve_projects_dir",
    "validate_project_structure",
]

PathLike: TypeAlias = str | os.PathLike[str]  # noqa: UP040 - convenção 7.7 usa TypeAlias

PROJECT_LOCK_FILENAME: Final[str] = ".rpa3003.lock"
REQUIRED_PROJECT_ITEMS: Final[tuple[str, ...]] = (
    "main.py",
    "global_vars.py",
    "logs/",
    "data/",
    "workflows/__init__.py",
    "workflows/dev/__init__.py",
    "workflows/dev/step_setup_env.py",
    "workflows/dev/step_execute_task.py",
    "workflows/dev/step_wrapup_execution.py",
    "workflows/prd/__init__.py",
    "workflows/prd/step_setup_env.py",
    "workflows/prd/step_execute_task.py",
    "workflows/prd/step_wrapup_execution.py",
)
STEP_MODULES: Final[tuple[str, ...]] = (
    "step_setup_env",
    "step_execute_task",
    "step_wrapup_execution",
)

_BACKUP_MARKER: Final[str] = ".bak-"
_STEP_PREFIX: Final[str] = "step_"
_DEV_PACKAGE: Final[str] = "workflows.dev"


class Environment(StrEnum):
    """Ambientes de execução de um projeto (RF-010)."""

    DEV = "dev"
    PRD = "prd"

    @classmethod
    def from_value(cls, value: str | Environment) -> Environment:
        """Normaliza (strip + lower). Inválido: InvalidEnvironmentError listando 'dev, prd'.

        Args:
            value: Texto informado pelo usuário (ex.: ``" PRD "``) ou um membro do enum.

        Returns:
            O membro correspondente.

        Raises:
            InvalidEnvironmentError: Se o valor não for `dev` nem `prd`.
        """
        if isinstance(value, Environment):
            return value
        normalized = str(value).strip().lower()
        try:
            return cls(normalized)
        except ValueError:
            accepted = ", ".join(member.value for member in cls)
            raise InvalidEnvironmentError(
                f"Ambiente inválido: '{value}'. Valores aceitos: {accepted}",
                details={"valor": value},
            ) from None


class FrameworkSettings(BaseSettings):
    """Configuração do framework lida de variáveis `RPA3003_*` e do `.env` (seção 8.5)."""

    model_config = SettingsConfigDict(
        env_prefix="RPA3003_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    env: Environment = Environment.DEV
    log_level: LogLevel = "INFO"
    log_user: str | None = None
    log_retention_days: int = Field(default=30, ge=1, le=3650)
    log_compression: bool = False
    log_console: bool = True
    projects_dir: Path = Path("projects")
    run_timeout: int | None = Field(default=None, ge=1)
    default_browser: str = "chrome"
    browser_headless: bool = False
    selenium_timeout: int = Field(default=30, ge=1)
    page_load_timeout: int = Field(default=60, ge=1)
    chromedriver_path: Path | None = None
    edgedriver_path: Path | None = None
    iedriver_path: Path | None = None
    edge_binary_path: Path | None = None
    screenshot_on_error: bool = True
    desktop_failsafe: bool = True
    desktop_confidence: float = Field(default=0.9, ge=0.5, le=1.0)

    @field_validator("env", mode="before")
    @classmethod
    def _normalize_env(cls, value: object) -> object:
        """Aceita o ambiente sem diferenciar maiúsculas e ignorando espaços (RF-010)."""
        return Environment.from_value(value) if isinstance(value, str) else value

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: object) -> object:
        """Aceita o nível de log sem diferenciar maiúsculas e ignorando espaços."""
        return value.strip().upper() if isinstance(value, str) else value


class VaultAuthMethod(StrEnum):
    """Métodos de autenticação suportados no HashiCorp Vault (RF-020)."""

    TOKEN = "token"  # noqa: S105 - nome do método de autenticação
    APPROLE = "approle"
    LDAP = "ldap"
    USERPASS = "userpass"


class VaultSettings(BaseSettings):
    """Configuração do Vault lida de variáveis `RPA3003_VAULT_*` (e aliases) e do `.env`."""

    model_config = SettingsConfigDict(
        env_prefix="RPA3003_VAULT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    url: str | None = Field(
        default=None, validation_alias=AliasChoices("RPA3003_VAULT_URL", "VAULT_ADDR")
    )
    auth_method: VaultAuthMethod = VaultAuthMethod.APPROLE
    token: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("RPA3003_VAULT_TOKEN", "VAULT_TOKEN")
    )
    role_id: str | None = None
    secret_id: SecretStr | None = None
    username: str | None = None
    password: SecretStr | None = None
    namespace: str | None = Field(
        default=None, validation_alias=AliasChoices("RPA3003_VAULT_NAMESPACE", "VAULT_NAMESPACE")
    )
    mount_point: str = "secret"
    kv_version: int = Field(default=2, ge=1, le=2)
    verify: bool | Path = Field(
        default=True, validation_alias=AliasChoices("RPA3003_VAULT_VERIFY", "VAULT_CACERT")
    )
    timeout: int = Field(default=10, ge=1, le=120)
    max_retries: int = Field(default=3, ge=1, le=10)
    backoff_base: float = Field(default=1.0, ge=0.0, le=30.0)
    cache_ttl: int = Field(default=300, ge=0)
    offline_cache: bool = False
    offline_cache_ttl_hours: int = Field(default=24, ge=1, le=168)
    local_secrets_file: Path | None = None

    @field_validator("auth_method", mode="before")
    @classmethod
    def _normalize_auth_method(cls, value: object) -> object:
        """Aceita o método de autenticação sem diferenciar maiúsculas e ignorando espaços."""
        return value.strip().lower() if isinstance(value, str) else value


@dataclass(frozen=True, slots=True)
class ProjectPaths:
    """Caminhos absolutos de um projeto RPA (RF-002)."""

    name: str
    root: Path
    workflows_dir: Path
    logs_dir: Path
    screenshots_dir: Path
    data_dir: Path
    main_file: Path
    global_vars_file: Path
    lock_file: Path

    @classmethod
    def from_root(cls, root: PathLike) -> Self:
        """Resolve todos os caminhos como absolutos; name = nome da pasta raiz.

        Args:
            root: Pasta raiz do projeto (relativa ao diretório atual ou absoluta).

        Returns:
            Os caminhos do projeto; a pasta não precisa existir.
        """
        resolved = Path(root).expanduser().resolve()
        logs_dir = resolved / "logs"
        return cls(
            name=resolved.name,
            root=resolved,
            workflows_dir=resolved / "workflows",
            logs_dir=logs_dir,
            screenshots_dir=logs_dir / "screenshots",
            data_dir=resolved / "data",
            main_file=resolved / "main.py",
            global_vars_file=resolved / "global_vars.py",
            lock_file=resolved / PROJECT_LOCK_FILENAME,
        )

    def ensure_runtime_dirs(self) -> None:
        """Cria logs/, logs/screenshots/ e data/ (idempotente)."""
        for directory in (self.logs_dir, self.screenshots_dir, self.data_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def workflow_env_dir(self, env: Environment) -> Path:
        """Retorna workflows/<env>/.

        Args:
            env: Ambiente desejado.

        Returns:
            Pasta dos steps do ambiente.
        """
        return self.workflows_dir / Environment.from_value(env).value


def load_settings(env_file: PathLike | None = None, **overrides: Any) -> FrameworkSettings:  # noqa: ANN401
    """Instancia sem cache. ValidationError vira ConfigurationError(details={"campos": [...]}).

    Precedência: `overrides` → variáveis de ambiente → `.env` → padrão.

    Args:
        env_file: Arquivo `.env` a ler; `None` usa `.env` do diretório atual.
        **overrides: Valores explícitos por nome de campo.

    Returns:
        Configuração validada.

    Raises:
        ConfigurationError: Se algum valor for inválido; a mensagem lista os campos.
        InvalidEnvironmentError: Se o ambiente informado não for `dev` nem `prd`.
    """
    return _build_settings(FrameworkSettings, env_file, overrides)


@functools.lru_cache(maxsize=1)
def get_settings() -> FrameworkSettings:
    """Instância em cache (functools.lru_cache(maxsize=1)) de load_settings()."""
    return load_settings()


def load_vault_settings(env_file: PathLike | None = None, **overrides: Any) -> VaultSettings:  # noqa: ANN401
    """Instancia `VaultSettings` sem cache, com as mesmas regras de `load_settings`.

    Args:
        env_file: Arquivo `.env` a ler; `None` usa `.env` do diretório atual.
        **overrides: Valores explícitos por nome de campo.

    Returns:
        Configuração do Vault validada.

    Raises:
        ConfigurationError: Se algum valor for inválido; a mensagem lista os campos.
    """
    return _build_settings(VaultSettings, env_file, overrides)


def reset_settings_cache() -> None:
    """Descarta a instância em cache de `get_settings()`."""
    get_settings.cache_clear()


def resolve_environment(
    value: str | Environment | None, settings: FrameworkSettings
) -> Environment:
    """value (se não None) → settings.env (já reflete RPA3003_ENV e .env).

    Args:
        value: Valor de `--env`, ou `None` quando não informado.
        settings: Configuração carregada do ambiente e do `.env`.

    Returns:
        Ambiente resolvido.

    Raises:
        InvalidEnvironmentError: Se `value` não for `dev` nem `prd`.
    """
    if value is not None:
        return Environment.from_value(value)
    return settings.env


def resolve_projects_dir(cli_value: PathLike | None, settings: FrameworkSettings) -> Path:
    """cli_value → settings.projects_dir (RPA3003_PROJECTS_DIR) → ./projects. Retorna absoluto.

    Args:
        cli_value: Valor de `--projects-dir`, ou `None`.
        settings: Configuração com `projects_dir` (padrão `projects`).

    Returns:
        Caminho absoluto resolvido.
    """
    chosen = Path(cli_value) if cli_value is not None else settings.projects_dir
    return chosen.expanduser().resolve()


def list_projects(projects_dir: PathLike) -> list[ProjectPaths]:
    """Subpastas que contêm main.py, ordenadas por nome; ignora nomes iniciados por '.'
    e pastas de backup (*.bak-*).

    Args:
        projects_dir: Pasta de projetos.

    Returns:
        Caminhos de cada projeto encontrado.

    Raises:
        ConfigurationError: Se a pasta de projetos não existir.
    """
    base = Path(projects_dir).expanduser().resolve()
    if not base.is_dir():
        raise ConfigurationError(
            "Diretório de projetos não encontrado", details={"caminho": str(base)}
        )
    projects = [
        ProjectPaths.from_root(child)
        for child in base.iterdir()
        if child.is_dir() and _is_project_dir_name(child.name) and (child / "main.py").is_file()
    ]
    return sorted(projects, key=lambda paths: paths.name)


def find_project(name: str, projects_dir: PathLike) -> ProjectPaths:
    """ProjectNotFoundError(details={"disponiveis": [...]}) se não existir.

    Args:
        name: Nome da pasta do projeto dentro de `projects_dir`.
        projects_dir: Pasta de projetos.

    Returns:
        Caminhos do projeto.

    Raises:
        ProjectNotFoundError: Se a pasta do projeto não existir.
    """
    base = Path(projects_dir).expanduser().resolve()
    candidate = base / name
    if _is_plain_name(name) and _is_project_dir_name(name) and candidate.is_dir():
        return ProjectPaths.from_root(candidate)
    available = [paths.name for paths in list_projects(base)] if base.is_dir() else []
    raise ProjectNotFoundError(
        f"Projeto não encontrado: '{name}'",
        details={"caminho": str(base), "disponiveis": available},
    )


def validate_project_structure(root: PathLike) -> list[str]:
    """Problemas conforme RF-003, na ordem: itens ausentes (ordem de REQUIRED_PROJECT_ITEMS),
    depois problemas por arquivo de step (dev antes de prd, ordem de STEP_MODULES).

    A verificação é estática (módulo `ast`): o código do projeto não é importado. Além dos steps
    padrão, verifica outros `step_*.py` de cada ambiente e, em `prd`, todos os módulos da pasta.

    Args:
        root: Pasta raiz do projeto.

    Returns:
        Lista de problemas; vazia quando a estrutura é válida.
    """
    base = Path(root).expanduser().resolve()
    problems = [
        f"ausente: {item}" for item in REQUIRED_PROJECT_ITEMS if not _item_exists(base, item)
    ]
    for env in Environment:
        env_dir = base / "workflows" / env.value
        for module_file in _modules_to_check(env_dir, env):
            problems.extend(_check_module(base, module_file, env))
    return problems


def _build_settings[S: BaseSettings](
    settings_class: type[S], env_file: PathLike | None, overrides: dict[str, Any]
) -> S:
    """Instancia a classe de settings convertendo `ValidationError` em `ConfigurationError`."""
    try:
        if env_file is None:
            return settings_class(**overrides)
        return settings_class(_env_file=Path(env_file), **overrides)
    except ValidationError as exc:
        fields = list(dict.fromkeys(_error_location(error["loc"]) for error in exc.errors()))
        raise ConfigurationError(
            f"Configuração inválida nos campos: {', '.join(fields)}",
            details={"campos": fields},
        ) from exc


def _error_location(location: tuple[int | str, ...]) -> str:
    """Nome do campo de um erro de validação (ex.: ``log_retention_days``)."""
    return ".".join(str(part) for part in location) or "__root__"


def _is_project_dir_name(name: str) -> bool:
    """Indica se o nome não é pasta oculta/temporária nem backup de `create --force`."""
    return not name.startswith(".") and _BACKUP_MARKER not in name


def _is_plain_name(name: str) -> bool:
    """Indica se o nome é um único componente de caminho (sem separadores nem '..')."""
    return bool(name) and Path(name).name == name and name not in {".", ".."}


def _item_exists(root: Path, item: str) -> bool:
    """Verifica um item de `REQUIRED_PROJECT_ITEMS`; entradas com '/' final são diretórios."""
    if item.endswith("/"):
        return (root / item.rstrip("/")).is_dir()
    return (root / item).is_file()


def _modules_to_check(env_dir: Path, env: Environment) -> Iterator[Path]:
    """Steps padrão (ordem de STEP_MODULES), outros `step_*.py` e, em prd, demais módulos."""
    if not env_dir.is_dir():
        return
    standard = [env_dir / f"{module}.py" for module in STEP_MODULES]
    yield from (path for path in standard if path.is_file())
    extra_steps = sorted(set(env_dir.glob(f"{_STEP_PREFIX}*.py")) - set(standard))
    yield from (path for path in extra_steps if path.is_file())
    if env is Environment.PRD:
        checked = set(standard) | set(extra_steps)
        yield from sorted(path for path in env_dir.rglob("*.py") if path not in checked)


def _check_module(root: Path, module_file: Path, env: Environment) -> list[str]:
    """Analisa um módulo com `ast` e retorna os problemas encontrados (RF-003)."""
    relative = module_file.relative_to(root).as_posix()
    try:
        tree = ast.parse(module_file.read_bytes(), filename=str(module_file))
    except SyntaxError as exc:
        return [f"{relative}: erro de sintaxe na linha {exc.lineno}"]
    except OSError as exc:
        return [f"{relative}: erro de leitura ({type(exc).__name__})"]
    problems: list[str] = []
    is_step = module_file.parent.name == env.value and module_file.name.startswith(_STEP_PREFIX)
    if is_step and not _defines_step_class(tree):
        problems.append(f"{relative}: classe 'Step' não encontrada")
    if env is Environment.PRD and _imports_dev_workflows(tree, module_file, root):
        problems.append(f"{relative}: importação proibida de workflows.dev em prd")
    return problems


def _defines_step_class(tree: ast.Module) -> bool:
    """Indica se o módulo define `class Step` no nível superior (P-20)."""
    return any(isinstance(node, ast.ClassDef) and node.name == "Step" for node in tree.body)


def _imports_dev_workflows(tree: ast.Module, module_file: Path, root: Path) -> bool:
    """Indica se o módulo importa `workflows.dev` (absoluto, relativo ou local)."""
    package = ".".join(module_file.parent.relative_to(root).parts)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = _absolute_module(package, node.level, node.module)
            names = [base, *(f"{base}.{alias.name}" for alias in node.names)]
        else:
            continue
        if any(name == _DEV_PACKAGE or name.startswith(f"{_DEV_PACKAGE}.") for name in names):
            return True
    return False


def _absolute_module(package: str, level: int, module: str | None) -> str:
    """Resolve o alvo de um import relativo a partir do pacote do módulo."""
    if level == 0:
        return module or ""
    parts = package.split(".")
    if level > 1:
        parts = parts[: -(level - 1)] if level - 1 < len(parts) else []
    return ".".join([*parts, module] if module else parts)
