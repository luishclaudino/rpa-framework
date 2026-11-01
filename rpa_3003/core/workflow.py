"""Contrato de steps e orquestração de workflows.

`BaseStep` (Template Method), contexto de execução, bootstrap de projetos, lock de instância
única e orquestração sequencial com wrapup garantido (RF-004 a RF-008, seção 8.8).
"""

from __future__ import annotations

import contextlib
import importlib
import inspect
import os
import platform
import sys
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from types import ModuleType, TracebackType
from typing import TYPE_CHECKING, Any, ClassVar, Final, Self, final

from filelock import FileLock, Timeout

from rpa_3003._version import __version__
from rpa_3003.core.config import (
    Environment,
    FrameworkSettings,
    ProjectPaths,
    load_settings,
    resolve_environment,
)
from rpa_3003.core.exceptions import (
    BusinessRuleError,
    ExitCode,
    ProjectAlreadyRunningError,
    ProjectNotFoundError,
    ProjectStructureError,
    StepLoadError,
    exit_code_for,
)
from rpa_3003.core.logger import default_user, get_logger, setup_logger

if TYPE_CHECKING:
    from loguru import Logger

__all__: list[str] = [
    "DEFAULT_STEPS",
    "BaseStep",
    "ExecutionContext",
    "ProjectLock",
    "RunResult",
    "RunStatus",
    "StepResult",
    "StepStatus",
    "WorkflowRunner",
    "bootstrap",
    "split_passthrough_args",
]

DEFAULT_STEPS: Final[tuple[str, ...]] = (
    "step_setup_env",
    "step_execute_task",
    "step_wrapup_execution",
)

_STEP_PREFIX: Final[str] = "step_"
_PASSTHROUGH_SEPARATOR: Final[str] = "--"
_RUN_ID_LENGTH: Final[int] = 12
_PROJECT_MODULES: Final[tuple[str, ...]] = ("global_vars", "workflows")


class StepStatus(StrEnum):
    """Situação final de um step."""

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class RunStatus(StrEnum):
    """Situação final de uma execução."""

    SUCCESS = "SUCCESS"
    BUSINESS_ERROR = "BUSINESS_ERROR"
    FAILED = "FAILED"


@dataclass(slots=True)
class StepResult:
    """Resultado de um step: nome, situação, duração e exceção da falha definitiva."""

    name: str
    status: StepStatus
    duration_seconds: float
    error: BaseException | None = None


@dataclass(slots=True)
class RunResult:
    """Resultado de uma execução completa do workflow."""

    run_id: str
    status: RunStatus
    exit_code: ExitCode
    started_at: datetime
    finished_at: datetime
    steps: list[StepResult]

    @property
    def duration_seconds(self) -> float:
        """Duração total da execução, em segundos."""
        return (self.finished_at - self.started_at).total_seconds()


@dataclass(slots=True)
class ExecutionContext:
    """Contexto único compartilhado entre os steps de uma execução (RF-007).

    Attributes:
        state: Dados trocados entre steps (ex.: ``ctx.state["total"] = 10``).
        error: Primeira falha da execução, ou `None`.
    """

    project_name: str
    env: Environment
    paths: ProjectPaths
    settings: FrameworkSettings
    logger: Logger
    user: str
    run_id: str
    started_at: datetime
    extra_args: list[str] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    error: BaseException | None = None
    _cleanups: list[Callable[[], None]] = field(default_factory=list, repr=False)

    def register_cleanup(self, func: Callable[[], None]) -> None:
        """Registra uma rotina de limpeza, executada em ordem LIFO após todos os steps.

        Args:
            func: Função sem argumentos (ex.: ``browser.quit``).

        Raises:
            TypeError: Se `func` não for chamável.
        """
        if not callable(func):
            raise TypeError(f"Rotina de limpeza deve ser chamável: {func!r}")
        self._cleanups.append(func)

    def run_cleanups(self) -> None:
        """Executa em ordem LIFO; exceções viram WARNING; esvazia a lista ao final."""
        while self._cleanups:
            func = self._cleanups.pop()
            try:
                func()
            except Exception as exc:
                self.logger.warning(
                    "Falha na rotina de limpeza {}: {}: {}",
                    _callable_name(func),
                    type(exc).__name__,
                    exc,
                )


class BaseStep(ABC):
    """Contrato de step (Template Method, RF-005).

    Cada módulo `workflows/<env>/step_*.py` define ``class Step(BaseStep)`` com `run()` (P-20).

    Attributes:
        name: Nome exibido no log; vazio usa o nome do módulo sem o prefixo ``step_``.
        always_run: Executa mesmo após falha anterior (wrapup).
        retries: Novas tentativas de `run()` para exceções que não sejam `BusinessRuleError`.
        retry_delay: Espera, em segundos, entre as tentativas.
    """

    name: ClassVar[str] = ""  # vazio: nome do módulo sem o prefixo "step_"
    always_run: ClassVar[bool] = False
    retries: ClassVar[int] = 0
    retry_delay: ClassVar[float] = 5.0

    def __init__(self, ctx: ExecutionContext) -> None:
        """Cria o step.

        Args:
            ctx: Contexto compartilhado da execução.
        """
        self.ctx: ExecutionContext = ctx

    @property
    def log(self) -> Logger:
        """Logger da execução."""
        return self.ctx.logger

    @property
    def step_name(self) -> str:
        """`name` se definido; senão o nome do módulo sem 'step_'."""
        return _step_name(type(self))

    @final
    def execute(self) -> StepResult:
        """Template Method conforme RF-005.

        Registra o início, chama `before()`, `run()` (com retentativas) e `after()`, e registra o
        término. Em falha definitiva, chama `on_error()` uma única vez e retorna `FAILED` sem
        propagar a exceção. `KeyboardInterrupt` é propagado.

        Returns:
            Resultado do step.
        """
        name = self.step_name
        self.log.info(">>> Iniciando step '{}'", name)
        started = time.perf_counter()
        try:
            self.before()
            self._run_with_retries(name)
            self.after()
        except Exception as error:
            duration = time.perf_counter() - started
            self.log.opt(exception=error).error(
                "Step '{}' falhou após {:.2f}s: {}: {}",
                name,
                duration,
                type(error).__name__,
                error,
            )
            self._notify_error(name, error)
            return StepResult(name, StepStatus.FAILED, duration, error)
        duration = time.perf_counter() - started
        self.log.success("<<< Step '{}' concluído em {:.2f}s", name, duration)
        return StepResult(name, StepStatus.SUCCESS, duration)

    def before(self) -> None:  # noqa: B027 - hook opcional (seção 8.8)
        """Hook opcional executado uma vez antes de run()."""

    @abstractmethod
    def run(self) -> None:
        """Lógica principal do step."""

    def after(self) -> None:  # noqa: B027 - hook opcional (seção 8.8)
        """Hook opcional executado uma vez após run() bem-sucedido."""

    def on_error(self, error: BaseException) -> None:  # noqa: B027 - hook opcional (seção 8.8)
        """Hook opcional executado uma vez após falha definitiva."""

    def _run_with_retries(self, name: str) -> None:
        attempts = max(self.retries, 0) + 1
        for attempt in range(1, attempts + 1):
            try:
                self.run()
            except BusinessRuleError:
                raise
            except Exception as error:
                if attempt == attempts:
                    raise
                self.log.warning(
                    "Step '{}' falhou (tentativa {}/{}); nova tentativa em {}s: {}: {}",
                    name,
                    attempt,
                    attempts,
                    self.retry_delay,
                    type(error).__name__,
                    error,
                )
                time.sleep(self.retry_delay)
            else:
                return

    def _notify_error(self, name: str, error: BaseException) -> None:
        try:
            self.on_error(error)
        except Exception as hook_error:
            self.log.warning(
                "Falha no hook on_error do step '{}': {}: {}",
                name,
                type(hook_error).__name__,
                hook_error,
            )


class ProjectLock:
    """Lock de instância única por projeto em `<projeto>/.rpa3003.lock` (RF-004)."""

    def __init__(self, lock_file: Path) -> None:
        """Prepara o lock sem adquiri-lo.

        Args:
            lock_file: Caminho do arquivo de lock.
        """
        self._path = Path(lock_file)
        self._lock = FileLock(self._path, timeout=0)

    def __enter__(self) -> Self:
        """Adquire com timeout 0; ocupado → ProjectAlreadyRunningError."""
        try:
            self._lock.acquire(timeout=0, blocking=False)
        except Timeout as exc:
            raise ProjectAlreadyRunningError(
                "Projeto já está em execução", details={"lock": str(self._path)}
            ) from exc
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Libera o lock."""
        self._lock.release()


def split_passthrough_args(argv: Sequence[str]) -> tuple[list[str], list[str]]:
    """Divide no primeiro '--': (antes, depois). Sem '--': (argv, [])."""
    items = list(argv)
    if _PASSTHROUGH_SEPARATOR not in items:
        return items, []
    index = items.index(_PASSTHROUGH_SEPARATOR)
    return items[:index], items[index + 1 :]


def bootstrap(
    project_root: Path,
    env: str | Environment | None = None,
    *,
    user: str | None = None,
    log_level: str | None = None,
    extra_args: Sequence[str] = (),
) -> ExecutionContext:
    """Prepara a execução de um projeto (passos abaixo).

    1. Resolve os caminhos do projeto.
    2. Muda o diretório de trabalho para a raiz, insere a raiz em `sys.path` e remove módulos
       `global_vars` e `workflows*` de outros projetos de `sys.modules`.
    3. Reconfigura `stdout`/`stderr` para UTF-8 com `errors="replace"` (P-21).
    4. Carrega as configurações do ambiente e do `.env` do projeto.
    5. Resolve o ambiente (RF-010) e verifica `workflows/<env>/`.
    6. Cria os diretórios de runtime, configura o logger e gera o `run_id`.

    Args:
        project_root: Pasta raiz do projeto.
        env: Valor de `--env`; `None` usa `RPA3003_ENV`, `.env` ou `dev`.
        user: Valor de `--user`; `None` usa `RPA3003_LOG_USER` ou o usuário do Windows.
        log_level: Sobrescreve `RPA3003_LOG_LEVEL`.
        extra_args: Argumentos repassados ao projeto após `--`.

    Returns:
        Contexto pronto para o `WorkflowRunner`.

    Raises:
        ProjectNotFoundError: Se a pasta do projeto não existir.
        ProjectStructureError: Se `workflows/<env>/` não existir.
        InvalidEnvironmentError: Se o ambiente for inválido.
        ConfigurationError: Se alguma configuração for inválida.
    """
    paths = ProjectPaths.from_root(project_root)
    if not paths.root.is_dir():
        raise ProjectNotFoundError(
            "Pasta do projeto não encontrada", details={"caminho": str(paths.root)}
        )
    _activate_project(paths.root)
    _reconfigure_std_streams()
    overrides: dict[str, Any] = {} if log_level is None else {"log_level": log_level}
    settings = load_settings(env_file=paths.root / ".env", **overrides)
    environment = resolve_environment(env, settings)
    env_dir = paths.workflow_env_dir(environment)
    if not env_dir.is_dir():
        raise ProjectStructureError(
            f"Pasta de workflows do ambiente '{environment}' não encontrada",
            details={"caminho": str(env_dir)},
        )
    paths.ensure_runtime_dirs()
    resolved_user = _first_non_blank(user, settings.log_user) or default_user()
    setup_logger(
        paths.logs_dir,
        user=resolved_user,
        level=settings.log_level,
        file_prefix=paths.name,
        retention_days=settings.log_retention_days,
        compression=settings.log_compression,
        console=settings.log_console,
    )
    return ExecutionContext(
        project_name=paths.name,
        env=environment,
        paths=paths,
        settings=settings,
        logger=get_logger(),
        user=resolved_user,
        run_id=uuid.uuid4().hex[:_RUN_ID_LENGTH],
        started_at=datetime.now().astimezone(),
        extra_args=list(extra_args),
    )


class WorkflowRunner:
    """Orquestrador sequencial com wrapup garantido (Pipeline, RF-006)."""

    def __init__(
        self,
        ctx: ExecutionContext,
        steps: Sequence[str] | None = None,
        *,
        package: str = "workflows",
        use_lock: bool = True,
    ) -> None:
        """Prepara a execução.

        Args:
            ctx: Contexto criado por `bootstrap`.
            steps: Módulos de step, em ordem; `None` usa `DEFAULT_STEPS`.
            package: Pacote raiz dos workflows.
            use_lock: Adquire o lock de instância única do projeto.
        """
        self._ctx = ctx
        self._steps: tuple[str, ...] = DEFAULT_STEPS if steps is None else tuple(steps)
        self._package = package
        self._use_lock = use_lock

    def load_step(self, module_name: str) -> type[BaseStep]:
        """Importa f'{package}.{env}.{module_name}' e retorna o atributo Step validado.

        Args:
            module_name: Nome do módulo de step (ex.: ``step_setup_env``).

        Returns:
            A classe `Step` do módulo.

        Raises:
            StepLoadError: Se o módulo não puder ser importado ou não definir um `Step` válido.
        """
        module_path = f"{self._package}.{self._ctx.env.value}.{module_name}"
        details = {"modulo": module_path}
        try:
            module = importlib.import_module(module_path)
        except Exception as exc:
            raise StepLoadError(
                f"Falha ao importar o step '{module_name}': {type(exc).__name__}: {exc}",
                details=details,
            ) from exc
        return _validated_step_class(module, details)

    def run(self) -> RunResult:
        """Executa o workflow completo e retorna o resultado (seção 8.8).

        Returns:
            Resultado com status, código de saída e resultado de cada step.
        """
        ctx = self._ctx
        ctx.logger.info(
            "Iniciando execução | projeto={} | ambiente={} | run_id={} | usuario={} | "
            "python={} | rpa_3003={}",
            ctx.project_name,
            ctx.env,
            ctx.run_id,
            ctx.user,
            platform.python_version(),
            __version__,
        )
        with ExitStack() as stack:
            if self._use_lock:
                try:
                    stack.enter_context(ProjectLock(ctx.paths.lock_file))
                except ProjectAlreadyRunningError as exc:
                    ctx.logger.error("Projeto já está em execução | lock={}", ctx.paths.lock_file)
                    ctx.error = exc
                    return self._finish([])
            return self._finish(self._execute_steps())

    def _execute_steps(self) -> list[StepResult]:
        ctx = self._ctx
        importlib.invalidate_caches()
        try:
            step_classes = [self.load_step(module_name) for module_name in self._steps]
        except StepLoadError as exc:
            ctx.logger.opt(exception=exc).error("Falha ao carregar os steps: {}", exc)
            ctx.error = exc
            ctx.run_cleanups()
            return []
        results = [self._execute_step(step_class) for step_class in step_classes]
        ctx.run_cleanups()
        return results

    def _execute_step(self, step_class: type[BaseStep]) -> StepResult:
        ctx = self._ctx
        name = _step_name(step_class)
        if ctx.error is not None and not step_class.always_run:
            ctx.logger.warning("Step '{}' ignorado devido a falha anterior", name)
            return StepResult(name, StepStatus.SKIPPED, 0.0)
        started = time.perf_counter()
        try:
            result = step_class(ctx).execute()
        except KeyboardInterrupt as exc:
            duration = time.perf_counter() - started
            ctx.logger.error(
                "Execução interrompida pelo usuário no step '{}' após {:.2f}s", name, duration
            )
            ctx.error = exc
            return StepResult(name, StepStatus.FAILED, duration, exc)
        except Exception as exc:  # falha ao instanciar o step; execute() não propaga Exception
            duration = time.perf_counter() - started
            ctx.logger.opt(exception=exc).error(
                "Step '{}' falhou após {:.2f}s: {}: {}", name, duration, type(exc).__name__, exc
            )
            result = StepResult(name, StepStatus.FAILED, duration, exc)
        if result.status is StepStatus.FAILED and ctx.error is None:
            ctx.error = result.error
        return result

    def _finish(self, steps: list[StepResult]) -> RunResult:
        ctx = self._ctx
        error = ctx.error
        if error is None:
            status = RunStatus.SUCCESS
        elif isinstance(error, BusinessRuleError):
            status = RunStatus.BUSINESS_ERROR
        else:
            status = RunStatus.FAILED
        exit_code = exit_code_for(error)
        finished_at = datetime.now(ctx.started_at.tzinfo)
        duration = (finished_at - ctx.started_at).total_seconds()
        ctx.logger.log(
            "SUCCESS" if status is RunStatus.SUCCESS else "ERROR",
            "Execução finalizada | status={} | exit_code={} | duracao={:.2f}s | run_id={}",
            status,
            int(exit_code),
            duration,
            ctx.run_id,
        )
        ctx.logger.complete()
        return RunResult(
            run_id=ctx.run_id,
            status=status,
            exit_code=exit_code,
            started_at=ctx.started_at,
            finished_at=finished_at,
            steps=steps,
        )


def _step_name(step_class: type[BaseStep]) -> str:
    """`name` da classe ou nome do módulo sem o prefixo ``step_``."""
    if step_class.name:
        return step_class.name
    return step_class.__module__.rpartition(".")[2].removeprefix(_STEP_PREFIX)


def _first_non_blank(*values: str | None) -> str | None:
    """Primeiro valor não vazio, sem espaços nas pontas (precedência de usuário, RF-016)."""
    for value in values:
        if value and value.strip():
            return value.strip()
    return None


def _callable_name(func: Callable[[], None]) -> str:
    name = getattr(func, "__qualname__", None) or getattr(func, "__name__", None)
    return str(name) if name else repr(func)


def _validated_step_class(module: ModuleType, details: dict[str, str]) -> type[BaseStep]:
    """Retorna o atributo `Step` do módulo se for subclasse concreta de `BaseStep` (P-20)."""
    step_class = getattr(module, "Step", None)
    if step_class is None:
        raise StepLoadError("Classe 'Step' não encontrada no módulo", details=details)
    if not (inspect.isclass(step_class) and issubclass(step_class, BaseStep)):
        raise StepLoadError("'Step' deve ser uma subclasse de BaseStep", details=details)
    if inspect.isabstract(step_class):
        raise StepLoadError("Classe 'Step' não implementa run()", details=details)
    return step_class


def _activate_project(root: Path) -> None:
    """Diretório de trabalho, `sys.path` e `sys.modules` apontando para o projeto (P-06)."""
    os.chdir(root)
    root_entry = str(root)
    if root_entry not in sys.path:
        sys.path.insert(0, root_entry)
    for module_name in list(sys.modules):
        if _is_project_module(module_name) and not _belongs_to(sys.modules.get(module_name), root):
            sys.modules.pop(module_name, None)


def _belongs_to(module: ModuleType | None, root: Path) -> bool:
    """Indica se o arquivo do módulo está dentro de `root`."""
    module_file = getattr(module, "__file__", None)
    if not module_file:
        return False
    try:
        return Path(module_file).resolve().is_relative_to(root)
    except (OSError, ValueError):
        return False


def _reconfigure_std_streams() -> None:
    """Reconfigura `stdout` e `stderr` para UTF-8 com `errors="replace"` (P-21)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with contextlib.suppress(AttributeError, OSError, TypeError, ValueError):
                reconfigure(encoding="utf-8", errors="replace")


def _is_project_module(module_name: str) -> bool:
    """`global_vars`, `workflows` ou `workflows.*`."""
    return module_name in _PROJECT_MODULES or module_name.startswith("workflows.")
