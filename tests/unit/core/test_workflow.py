"""Testes de `rpa_3003.core.workflow` (RF-004 a RF-008, RF-011, RNF-006, RNF-015, seção 8.8).

Cada teste monta em `tmp_path` um projeto mínimo com `workflows/<env>/step_*.py`.
"""

from __future__ import annotations

import io
import re
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from filelock import FileLock

from rpa_3003._version import __version__
from rpa_3003.core.config import Environment, ProjectPaths, load_settings
from rpa_3003.core.exceptions import (
    BusinessRuleError,
    ConfigurationError,
    ExitCode,
    InvalidEnvironmentError,
    ProjectAlreadyRunningError,
    ProjectNotFoundError,
    ProjectStructureError,
    StepLoadError,
)
from rpa_3003.core.logger import get_logger, setup_logger
from rpa_3003.core.workflow import (
    DEFAULT_STEPS,
    BaseStep,
    ExecutionContext,
    ProjectLock,
    RunResult,
    RunStatus,
    StepResult,
    StepStatus,
    WorkflowRunner,
    bootstrap,
    split_passthrough_args,
)

pytestmark = pytest.mark.unit

ReadLines = Callable[[Path], list[str]]

LOG_LINE_REGEX: re.Pattern[str] = re.compile(
    r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] "
    r"\[(TRACE|DEBUG|INFO|SUCCESS|WARNING|ERROR|CRITICAL)\] "
    r"\[[^\]]+\] \[\d+\] \[[^\]]+\] .*$"
)
RUN_ID_REGEX: re.Pattern[str] = re.compile(r"^[0-9a-f]{12}$")
STEP_ORDER = ("setup_env", "execute_task", "wrapup_execution")


# --- Projeto mínimo ----------------------------------------------------------------------------


def step_source(
    run_body: str = "", *, attributes: str = "", methods: str = "", imports: str = ""
) -> str:
    """Código de um módulo de step que registra a execução em ``ctx.state['executados']``."""
    body = textwrap.indent(textwrap.dedent(run_body).strip(), " " * 8) if run_body.strip() else ""
    attrs = textwrap.indent(textwrap.dedent(attributes).strip(), " " * 4) if attributes else ""
    extra = textwrap.indent(textwrap.dedent(methods).strip(), " " * 4) if methods else ""
    return (
        '"""Step de teste."""\n\n'
        "from rpa_3003.core.exceptions import BusinessRuleError, VaultUnavailableError\n"
        "from rpa_3003.core.workflow import BaseStep\n"
        f"{imports}\n\n"
        "class Step(BaseStep):\n"
        f"{attrs}\n\n"
        "    def run(self) -> None:\n"
        '        self.ctx.state.setdefault("executados", []).append(self.step_name)\n'
        f"{body}\n\n"
        f"{extra}\n"
    )


WRAPUP = step_source(
    'self.ctx.state["erro_visto_no_wrapup"] = type(self.ctx.error).__name__',
    attributes="always_run = True",
)


def default_steps() -> dict[str, str]:
    return {
        "step_setup_env": step_source(),
        "step_execute_task": step_source(),
        "step_wrapup_execution": WRAPUP,
    }


def write_project(
    root: Path,
    steps: dict[str, str] | None = None,
    *,
    env: str = "dev",
    other_env: bool = True,
) -> Path:
    """Cria um projeto mínimo com os steps informados (padrão: os três steps do framework)."""
    envs = [env, "prd" if env == "dev" else "dev"] if other_env else [env]
    for env_name in envs:
        env_dir = root / "workflows" / env_name
        env_dir.mkdir(parents=True, exist_ok=True)
        (env_dir / "__init__.py").write_text('"""Steps."""\n', encoding="utf-8")
        for module, source in (steps if steps is not None else default_steps()).items():
            (env_dir / f"{module}.py").write_text(source, encoding="utf-8")
    (root / "workflows" / "__init__.py").write_text('"""Workflows."""\n', encoding="utf-8")
    return root


def _is_project_module(name: str) -> bool:
    return name in {"global_vars", "workflows"} or name.startswith("workflows.")


@pytest.fixture(autouse=True)
def _restore_process_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Restaura cwd, sys.path e módulos de projeto alterados por bootstrap/runner."""
    work = tmp_path / "cwd"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(sys, "stderr", sys.stderr)
    yield
    for name in [name for name in sys.modules if _is_project_module(name)]:
        del sys.modules[name]


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return write_project(tmp_path / "projeto_teste")


def run_project(
    root: Path, env: str = "dev", **runner_kwargs: Any
) -> tuple[RunResult, ExecutionContext]:
    ctx = bootstrap(root, env=env, user="ana")
    return WorkflowRunner(ctx, **runner_kwargs).run(), ctx


@pytest.fixture
def ctx(tmp_path: Path) -> ExecutionContext:
    """Contexto construído manualmente, com log em arquivo, para testar BaseStep isolado."""
    paths = ProjectPaths.from_root(tmp_path / "ctx_projeto")
    setup_logger(paths.logs_dir, user="ana", console=False, file_prefix=paths.name)
    return ExecutionContext(
        project_name=paths.name,
        env=Environment.DEV,
        paths=paths,
        settings=load_settings(),
        logger=get_logger(),
        user="ana",
        run_id="0123456789ab",
        started_at=datetime.now().astimezone(),
    )


def messages(lines: list[str]) -> list[str]:
    """Mensagens (sexto campo) das linhas de log no formato obrigatório."""
    return [line.split("] ", 5)[5] for line in lines if LOG_LINE_REGEX.match(line)]


# --- RF-005: BaseStep (Template Method) --------------------------------------------------------


class RecordingStep(BaseStep):
    """Step de teste que registra os hooks chamados."""

    name = "gravador"
    failures: int = 0
    error_factory: Callable[[], BaseException] = RuntimeError

    def __init__(self, ctx: ExecutionContext) -> None:
        super().__init__(ctx)
        self.calls: list[str] = []
        self.errors: list[BaseException] = []

    def before(self) -> None:
        self.calls.append("before")

    def run(self) -> None:
        self.calls.append("run")
        if self.calls.count("run") <= self.failures:
            raise self.error_factory()

    def after(self) -> None:
        self.calls.append("after")

    def on_error(self, error: BaseException) -> None:
        self.calls.append("on_error")
        self.errors.append(error)


def test_subclass_without_run_cannot_be_instantiated(ctx: ExecutionContext) -> None:
    """RF-005: subclasse sem run() gera TypeError ao ser instanciada."""

    class Incomplete(BaseStep):
        pass

    with pytest.raises(TypeError):
        Incomplete(ctx)  # type: ignore[abstract]


def test_successful_step(ctx: ExecutionContext, read_log_lines: ReadLines) -> None:
    """RF-005: step bem-sucedido retorna SUCCESS, duração > 0 e logs de início e término."""
    step = RecordingStep(ctx)
    result = step.execute()
    assert result == StepResult("gravador", StepStatus.SUCCESS, result.duration_seconds, None)
    assert result.duration_seconds > 0
    assert step.calls == ["before", "run", "after"]
    logged = messages(read_log_lines(ctx.paths.logs_dir))
    assert logged[0] == ">>> Iniciando step 'gravador'"
    assert re.fullmatch(r"<<< Step 'gravador' concluído em \d+\.\d{2}s", logged[1])
    lines = read_log_lines(ctx.paths.logs_dir)
    assert "[INFO] [workflow.py]" in lines[0] and "[SUCCESS] [workflow.py]" in lines[1]


def test_failing_step_calls_on_error_once(ctx: ExecutionContext, read_log_lines: ReadLines) -> None:
    """RF-005: run() com ValueError chama on_error uma vez, retorna FAILED e não propaga."""
    error = ValueError("valor inválido")

    class Failing(RecordingStep):
        error_factory = staticmethod(lambda: error)
        failures = 1

    step = Failing(ctx)
    result = step.execute()
    assert result.status is StepStatus.FAILED
    assert result.error is error
    assert step.errors == [error]
    assert step.calls == ["before", "run", "on_error"]
    lines = read_log_lines(ctx.paths.logs_dir)
    failure = next(line for line in lines if "[ERROR]" in line)
    assert re.search(
        r"Step 'gravador' falhou após \d+\.\d{2}s: ValueError: valor inválido$", failure
    )
    assert any(line.startswith("Traceback") for line in lines)


def test_retries_until_success(
    ctx: ExecutionContext, no_sleep: list[float], read_log_lines: ReadLines
) -> None:
    """RF-005: retries=2 com duas falhas e depois sucesso resulta em SUCCESS e 2 WARNING."""

    class Flaky(RecordingStep):
        retries = 2
        retry_delay = 10.0
        failures = 2

    step = Flaky(ctx)
    assert step.execute().status is StepStatus.SUCCESS
    assert step.calls == ["before", "run", "run", "run", "after"]
    assert no_sleep == [10.0, 10.0]
    warnings = [line for line in read_log_lines(ctx.paths.logs_dir) if "[WARNING]" in line]
    assert len(warnings) == 2
    assert warnings[0].endswith(
        "Step 'gravador' falhou (tentativa 1/3); nova tentativa em 10.0s: RuntimeError: "
    )
    assert "(tentativa 2/3)" in warnings[1]


def test_retries_exhausted(ctx: ExecutionContext, no_sleep: list[float]) -> None:
    """RF-005: esgotadas as tentativas, on_error é chamado uma vez e o resultado é FAILED."""

    class AlwaysFails(RecordingStep):
        retries = 2
        failures = 99

    step = AlwaysFails(ctx)
    assert step.execute().status is StepStatus.FAILED
    assert step.calls.count("run") == 3
    assert step.calls.count("on_error") == 1
    assert no_sleep == [5.0, 5.0]


def test_business_rule_error_is_not_retried(ctx: ExecutionContext, no_sleep: list[float]) -> None:
    """RF-005: BusinessRuleError com retries=2 não gera novas tentativas."""

    class Business(RecordingStep):
        retries = 2
        failures = 99
        error_factory = staticmethod(lambda: BusinessRuleError("valor do boleto inválido"))

    step = Business(ctx)
    result = step.execute()
    assert result.status is StepStatus.FAILED
    assert isinstance(result.error, BusinessRuleError)
    assert step.calls.count("run") == 1
    assert no_sleep == []


def test_on_error_failure_keeps_original_error(
    ctx: ExecutionContext, read_log_lines: ReadLines
) -> None:
    """RF-005: on_error() que lança exceção gera WARNING e o resultado mantém o erro original."""
    original = RuntimeError("erro original")

    class BrokenHook(BaseStep):
        name = "hook_quebrado"

        def run(self) -> None:
            raise original

        def on_error(self, error: BaseException) -> None:
            raise OSError("screenshot falhou")

    result = BrokenHook(ctx).execute()
    assert result.status is StepStatus.FAILED
    assert result.error is original
    warnings = [line for line in read_log_lines(ctx.paths.logs_dir) if "[WARNING]" in line]
    assert len(warnings) == 1
    assert warnings[0].endswith(
        "Falha no hook on_error do step 'hook_quebrado': OSError: screenshot falhou"
    )


def test_keyboard_interrupt_is_propagated(ctx: ExecutionContext, no_sleep: list[float]) -> None:
    """RF-005: KeyboardInterrupt em run() é propagado sem novas tentativas nem on_error."""

    class Interrupted(RecordingStep):
        retries = 3
        failures = 99
        error_factory = KeyboardInterrupt

    step = Interrupted(ctx)
    with pytest.raises(KeyboardInterrupt):
        step.execute()
    assert step.calls == ["before", "run"]
    assert no_sleep == []


def test_before_failure_skips_run_and_after(ctx: ExecutionContext) -> None:
    """RF-005: falha em before() resulta em FAILED sem chamar run() nem after()."""

    class BeforeFails(RecordingStep):
        def before(self) -> None:
            self.calls.append("before")
            raise RuntimeError("pré-condição")

    step = BeforeFails(ctx)
    assert step.execute().status is StepStatus.FAILED
    assert step.calls == ["before", "on_error"]


def test_after_failure_marks_step_failed(ctx: ExecutionContext) -> None:
    """RF-005: falha em after() resulta em FAILED com on_error chamado."""

    class AfterFails(RecordingStep):
        def after(self) -> None:
            raise RuntimeError("pós-condição")

    step = AfterFails(ctx)
    assert step.execute().status is StepStatus.FAILED
    assert step.calls == ["before", "run", "on_error"]


def test_step_name_and_log(ctx: ExecutionContext) -> None:
    """Seção 8.8: step_name usa `name` ou o módulo sem 'step_'; log é o logger do contexto."""

    class Unnamed(BaseStep):
        def run(self) -> None:
            pass

    Unnamed.__module__ = "workflows.dev.step_conciliar"
    assert Unnamed(ctx).step_name == "conciliar"
    assert RecordingStep(ctx).step_name == "gravador"
    assert RecordingStep(ctx).log is ctx.logger
    assert (BaseStep.always_run, BaseStep.retries, BaseStep.retry_delay) == (False, 0, 5.0)


# --- RF-006 e RNF-006: orquestração e wrapup garantido -----------------------------------------


def test_successful_run(project: Path, read_log_lines: ReadLines) -> None:
    """RF-008: execução sem erros retorna código 0 e status SUCCESS."""
    result, ctx = run_project(project)
    assert result.exit_code is ExitCode.SUCCESS
    assert result.status is RunStatus.SUCCESS
    assert [(step.name, step.status) for step in result.steps] == [
        (name, StepStatus.SUCCESS) for name in STEP_ORDER
    ]
    assert ctx.state["executados"] == list(STEP_ORDER)
    assert ctx.state["erro_visto_no_wrapup"] == "NoneType"
    assert result.run_id == ctx.run_id
    assert result.duration_seconds >= 0
    assert all(LOG_LINE_REGEX.match(line) for line in read_log_lines(ctx.paths.logs_dir))


FAILING_BODY = 'raise RuntimeError("falha injetada")'


@pytest.mark.parametrize(
    ("failing_module", "expected_statuses"),
    [
        ("step_setup_env", ["FAILED", "SKIPPED", "SUCCESS"]),
        ("step_execute_task", ["SUCCESS", "FAILED", "SUCCESS"]),
        ("step_wrapup_execution", ["SUCCESS", "SUCCESS", "FAILED"]),
    ],
)
def test_wrapup_always_runs_and_lock_is_released(
    tmp_path: Path, failing_module: str, expected_statuses: list[str]
) -> None:
    """RF-006 e RNF-006: falha injetada em cada step executa o wrapup e libera o lock."""
    steps = default_steps()
    attributes = "always_run = True" if failing_module == "step_wrapup_execution" else ""
    steps[failing_module] = step_source(FAILING_BODY, attributes=attributes)
    root = write_project(tmp_path / "falhas", steps)
    result, ctx = run_project(root)
    assert [step.status.value for step in result.steps] == expected_statuses
    assert "wrapup_execution" in ctx.state["executados"]
    assert result.exit_code is ExitCode.TECHNICAL_ERROR
    assert result.status is RunStatus.FAILED
    with FileLock(ctx.paths.lock_file, timeout=0):
        pass


@pytest.mark.parametrize(
    ("raise_statement", "expected_code", "expected_status"),
    [
        ('raise RuntimeError("x")', ExitCode.TECHNICAL_ERROR, RunStatus.FAILED),
        ('raise BusinessRuleError("x")', ExitCode.BUSINESS_ERROR, RunStatus.BUSINESS_ERROR),
        ('raise VaultUnavailableError("x")', ExitCode.CONFIGURATION_ERROR, RunStatus.FAILED),
    ],
)
def test_setup_failure_skips_execute_and_sets_exit_code(
    tmp_path: Path,
    raise_statement: str,
    expected_code: ExitCode,
    expected_status: RunStatus,
    read_log_lines: ReadLines,
) -> None:
    """RF-006: setup falha → execute SKIPPED, wrapup SUCCESS e código do erro do setup."""
    steps = default_steps()
    steps["step_setup_env"] = step_source(raise_statement)
    result, ctx = run_project(write_project(tmp_path / "p", steps))
    assert [step.status for step in result.steps] == [
        StepStatus.FAILED,
        StepStatus.SKIPPED,
        StepStatus.SUCCESS,
    ]
    assert result.exit_code is expected_code
    assert result.status is expected_status
    assert ctx.state["executados"] == ["setup_env", "wrapup_execution"]
    assert "Step 'execute_task' ignorado devido a falha anterior" in messages(
        read_log_lines(ctx.paths.logs_dir)
    )


def test_first_error_defines_exit_code(tmp_path: Path) -> None:
    """RF-006: o código de saída corresponde à primeira falha (wrapup falhando depois)."""
    steps = default_steps()
    steps["step_setup_env"] = step_source('raise BusinessRuleError("regra")')
    steps["step_wrapup_execution"] = step_source(
        'raise RuntimeError("wrapup")', attributes="always_run = True"
    )
    result, _ = run_project(write_project(tmp_path / "p", steps))
    assert result.exit_code is ExitCode.BUSINESS_ERROR
    assert result.steps[2].status is StepStatus.FAILED


@pytest.mark.parametrize(
    "broken_source",
    [
        "x = 1\n",
        "def quebrado(:\n",
        "import modulo_que_nao_existe\n",
        "Step = 42\n",
        "class Step:\n    def run(self):\n        pass\n",
        "from rpa_3003.core.workflow import BaseStep\n\n\nclass Step(BaseStep):\n    pass\n",
        "raise RuntimeError('erro ao importar')\n",
    ],
    ids=["sem_step", "sintaxe", "import", "nao_classe", "nao_basestep", "abstrata", "erro_import"],
)
def test_invalid_step_module_prevents_any_execution(
    tmp_path: Path, broken_source: str, read_log_lines: ReadLines
) -> None:
    """RF-006: módulo de step inválido → nenhum step executa e o código é 3."""
    steps = default_steps()
    steps["step_execute_task"] = broken_source
    result, ctx = run_project(write_project(tmp_path / "p", steps))
    assert result.exit_code is ExitCode.CONFIGURATION_ERROR
    assert result.steps == []
    assert "executados" not in ctx.state
    assert isinstance(ctx.error, StepLoadError)
    assert any("Falha ao carregar os steps" in line for line in read_log_lines(ctx.paths.logs_dir))


def test_missing_step_module_is_load_error(tmp_path: Path) -> None:
    """RF-006: módulo de step inexistente gera StepLoadError (código 3)."""
    steps = default_steps()
    del steps["step_execute_task"]
    result, _ = run_project(write_project(tmp_path / "p", steps))
    assert result.exit_code is ExitCode.CONFIGURATION_ERROR
    assert result.steps == []


def test_load_step_returns_step_class(project: Path) -> None:
    """Seção 8.8: load_step importa workflows.<env>.<módulo> e retorna Step."""
    ctx = bootstrap(project, env="dev")
    step_class = WorkflowRunner(ctx).load_step("step_setup_env")
    assert issubclass(step_class, BaseStep)
    assert step_class.__module__ == "workflows.dev.step_setup_env"
    with pytest.raises(StepLoadError) as info:
        WorkflowRunner(ctx).load_step("step_inexistente")
    assert info.value.details == {"modulo": "workflows.dev.step_inexistente"}


def test_custom_steps_and_package(tmp_path: Path) -> None:
    """Seção 7.5.4: sequência de steps e pacote configuráveis."""
    root = tmp_path / "custom"
    env_dir = root / "fluxos" / "dev"
    env_dir.mkdir(parents=True)
    (root / "fluxos" / "__init__.py").write_text("", encoding="utf-8")
    (env_dir / "__init__.py").write_text("", encoding="utf-8")
    (env_dir / "step_notificar.py").write_text(step_source(), encoding="utf-8")
    (root / "workflows" / "dev").mkdir(parents=True)
    ctx = bootstrap(root, env="dev")
    result = WorkflowRunner(ctx, steps=["step_notificar"], package="fluxos").run()
    assert result.exit_code is ExitCode.SUCCESS
    assert [step.name for step in result.steps] == ["notificar"]
    sys.modules.pop("fluxos.dev.step_notificar", None)
    sys.modules.pop("fluxos.dev", None)
    sys.modules.pop("fluxos", None)


def test_step_constructor_failure_is_step_failure(tmp_path: Path) -> None:
    """RNF-006: erro ao instanciar o step é falha do step; o wrapup executa."""
    steps = default_steps()
    steps["step_execute_task"] = step_source(
        methods="def __init__(self, ctx):\n    raise RuntimeError('construtor')"
    )
    result, ctx = run_project(write_project(tmp_path / "p", steps))
    assert [step.status for step in result.steps] == [
        StepStatus.SUCCESS,
        StepStatus.FAILED,
        StepStatus.SUCCESS,
    ]
    assert result.exit_code is ExitCode.TECHNICAL_ERROR
    assert ctx.state["erro_visto_no_wrapup"] == "RuntimeError"


# --- RF-008: códigos de saída e resumo ---------------------------------------------------------


@pytest.mark.parametrize("failing_module", DEFAULT_STEPS[:2])
def test_business_error_in_any_step(tmp_path: Path, failing_module: str) -> None:
    """RF-008: BusinessRuleError em qualquer step resulta em código 4 e BUSINESS_ERROR."""
    steps = default_steps()
    steps[failing_module] = step_source('raise BusinessRuleError("regra violada")')
    result, _ = run_project(write_project(tmp_path / "p", steps))
    assert result.exit_code is ExitCode.BUSINESS_ERROR
    assert result.status is RunStatus.BUSINESS_ERROR


def test_keyboard_interrupt_runs_wrapup_and_returns_130(
    tmp_path: Path, read_log_lines: ReadLines
) -> None:
    """RF-008: KeyboardInterrupt no step de execução executa o wrapup e retorna 130."""
    steps = default_steps()
    steps["step_execute_task"] = step_source("raise KeyboardInterrupt")
    steps["step_extra"] = step_source()
    root = write_project(tmp_path / "p", steps)
    ctx = bootstrap(root, env="dev")
    result = WorkflowRunner(
        ctx, steps=["step_setup_env", "step_execute_task", "step_extra", "step_wrapup_execution"]
    ).run()
    assert result.exit_code is ExitCode.INTERRUPTED
    assert int(result.exit_code) == 130
    assert result.status is RunStatus.FAILED
    assert [step.status for step in result.steps] == [
        StepStatus.SUCCESS,
        StepStatus.FAILED,
        StepStatus.SKIPPED,
        StepStatus.SUCCESS,
    ]
    assert ctx.state["erro_visto_no_wrapup"] == "KeyboardInterrupt"
    assert any("interrompida" in line for line in read_log_lines(ctx.paths.logs_dir))


def test_start_and_end_lines(project: Path, read_log_lines: ReadLines) -> None:
    """RF-008 e RNF-015: linhas de início e término com o mesmo run_id de 12 hexadecimais."""
    _, ctx = run_project(project)
    lines = read_log_lines(ctx.paths.logs_dir)
    logged = messages(lines)
    assert RUN_ID_REGEX.match(ctx.run_id)
    python_version = ".".join(str(part) for part in sys.version_info[:3])
    assert logged[0] == (
        f"Iniciando execução | projeto=projeto_teste | ambiente=dev | run_id={ctx.run_id} | "
        f"usuario=ana | python={python_version} | rpa_3003={__version__}"
    )
    assert re.fullmatch(
        rf"Execução finalizada \| status=SUCCESS \| exit_code=0 \| duracao=\d+\.\d{{2}}s \| "
        rf"run_id={ctx.run_id}",
        logged[-1],
    )
    assert "[SUCCESS] [workflow.py]" in lines[-1]
    assert lines[0].split("] ")[4] == "[ana"


def test_failed_run_end_line_is_error(tmp_path: Path, read_log_lines: ReadLines) -> None:
    """RF-008: execução com falha termina com linha ERROR, status FAILED e exit_code=1."""
    steps = default_steps()
    steps["step_execute_task"] = step_source(FAILING_BODY)
    _, ctx = run_project(write_project(tmp_path / "p", steps))
    last = read_log_lines(ctx.paths.logs_dir)[-1]
    assert "[ERROR] [workflow.py]" in last
    assert "Execução finalizada | status=FAILED | exit_code=1 | duracao=" in last


def test_run_result_duration() -> None:
    """Seção 8.8: RunResult.duration_seconds é a diferença entre término e início."""
    start = datetime(2026, 10, 31, 7, 0, 0).astimezone()
    result = RunResult(
        "abc", RunStatus.SUCCESS, ExitCode.SUCCESS, start, start + timedelta(seconds=38.04), []
    )
    assert result.duration_seconds == pytest.approx(38.04)


# --- RF-007: contexto compartilhado e limpeza --------------------------------------------------


def test_state_is_shared_between_steps(tmp_path: Path) -> None:
    """RF-007: ctx.state['total'] = 10 no setup é lido pelo step de execução."""
    steps = default_steps()
    steps["step_setup_env"] = step_source('self.ctx.state["total"] = 10')
    steps["step_execute_task"] = step_source('self.ctx.state["lido"] = self.ctx.state["total"]')
    _, ctx = run_project(write_project(tmp_path / "p", steps))
    assert ctx.state["lido"] == 10


CLEANUP_SETUP = """
eventos = self.ctx.state.setdefault("eventos", [])
self.ctx.register_cleanup(lambda: eventos.append("A"))
self.ctx.register_cleanup(lambda: eventos.append("B"))
"""


def test_cleanups_run_lifo_after_all_steps(tmp_path: Path) -> None:
    """RF-007: rotinas A e B registradas nessa ordem executam B antes de A, após o wrapup."""
    steps = default_steps()
    steps["step_setup_env"] = step_source(CLEANUP_SETUP)
    steps["step_wrapup_execution"] = step_source(
        'self.ctx.state["eventos"].append("wrapup")', attributes="always_run = True"
    )
    _, ctx = run_project(write_project(tmp_path / "p", steps))
    assert ctx.state["eventos"] == ["wrapup", "B", "A"]


def test_failing_cleanup_does_not_stop_others(tmp_path: Path, read_log_lines: ReadLines) -> None:
    """RF-007: rotina que lança exceção gera WARNING; as demais executam e o código não muda."""
    steps = default_steps()
    steps["step_setup_env"] = step_source(
        CLEANUP_SETUP + "self.ctx.register_cleanup(self.quebrar)",
        methods="def quebrar(self) -> None:\n    raise OSError('driver já encerrado')",
    )
    result, ctx = run_project(write_project(tmp_path / "p", steps))
    assert ctx.state["eventos"] == ["B", "A"]
    assert result.exit_code is ExitCode.SUCCESS
    warnings = [line for line in read_log_lines(ctx.paths.logs_dir) if "[WARNING]" in line]
    assert len(warnings) == 1
    assert warnings[0].endswith(
        "Falha na rotina de limpeza Step.quebrar: OSError: driver já encerrado"
    )


def test_cleanups_run_even_after_failures(tmp_path: Path) -> None:
    """RF-007 e RNF-006: rotinas de limpeza executam mesmo com falha de step."""
    steps = default_steps()
    steps["step_setup_env"] = step_source(CLEANUP_SETUP + FAILING_BODY)
    _, ctx = run_project(write_project(tmp_path / "p", steps))
    assert ctx.state["eventos"] == ["B", "A"]


def test_run_cleanups_empties_list_and_validates(ctx: ExecutionContext) -> None:
    """RF-007: run_cleanups esvazia a lista; register_cleanup exige chamável."""
    calls: list[str] = []
    ctx.register_cleanup(lambda: calls.append("x"))
    ctx.run_cleanups()
    ctx.run_cleanups()
    assert calls == ["x"]
    with pytest.raises(TypeError):
        ctx.register_cleanup("não chamável")  # type: ignore[arg-type]


# --- RF-004: instância única -------------------------------------------------------------------


def test_lock_held_by_other_holder_returns_6(project: Path, read_log_lines: ReadLines) -> None:
    """RF-004: lock ocupado → código 6, log ERROR 'Projeto já está em execução' e nenhum step."""
    ctx = bootstrap(project, env="dev")
    with FileLock(ctx.paths.lock_file):
        started = time.perf_counter()
        result = WorkflowRunner(ctx).run()
        elapsed = time.perf_counter() - started
    assert elapsed <= 2
    assert result.exit_code is ExitCode.ALREADY_RUNNING
    assert result.steps == []
    assert "executados" not in ctx.state
    assert isinstance(ctx.error, ProjectAlreadyRunningError)
    lines = read_log_lines(ctx.paths.logs_dir)
    error = next(line for line in lines if "[ERROR]" in line)
    assert error.endswith(f"Projeto já está em execução | lock={ctx.paths.lock_file}")
    assert "status=FAILED | exit_code=6" in lines[-1]


def test_lock_held_by_another_process_returns_6(project: Path) -> None:
    """RF-004: com outro processo segurando o lock, a execução termina em até 2 s com código 6."""
    lock_file = ProjectPaths.from_root(project).lock_file
    holder = subprocess.Popen(  # noqa: S603 - interpretador atual com código fixo
        [
            sys.executable,
            "-c",
            "import sys, time\nfrom filelock import FileLock\n"
            "lock = FileLock(sys.argv[1]); lock.acquire()\n"
            "print('ok', flush=True); time.sleep(60)",
            str(lock_file),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "ok"
        started = time.perf_counter()
        result, _ = run_project(project)
        assert time.perf_counter() - started <= 2
        assert result.exit_code is ExitCode.ALREADY_RUNNING
    finally:
        holder.kill()
        holder.wait(timeout=30)


def test_lock_is_acquired_after_failed_run(tmp_path: Path) -> None:
    """RF-004: após execução com falha, nova execução adquire o lock normalmente."""
    steps = default_steps()
    steps["step_execute_task"] = step_source(FAILING_BODY)
    root = write_project(tmp_path / "p", steps)
    assert run_project(root)[0].exit_code is ExitCode.TECHNICAL_ERROR
    assert run_project(root)[0].exit_code is ExitCode.TECHNICAL_ERROR


LOCK_PROBE = 'self.ctx.state["lock_existe"] = self.ctx.paths.lock_file.exists()'


def test_lock_file_exists_during_run(tmp_path: Path) -> None:
    """RF-004: com use_lock=True, o arquivo de lock existe durante a execução."""
    steps = default_steps()
    steps["step_setup_env"] = step_source(LOCK_PROBE)
    _, ctx = run_project(write_project(tmp_path / "p", steps))
    assert ctx.state["lock_existe"] is True


def test_use_lock_false_creates_no_lock_file(tmp_path: Path) -> None:
    """RF-004: WorkflowRunner(ctx, use_lock=False) não cria arquivo de lock."""
    steps = default_steps()
    steps["step_setup_env"] = step_source(LOCK_PROBE)
    root = write_project(tmp_path / "p", steps)
    result, ctx = run_project(root, use_lock=False)
    assert result.exit_code is ExitCode.SUCCESS
    assert ctx.state["lock_existe"] is False
    assert not ctx.paths.lock_file.exists()


def test_project_lock_context_manager(tmp_path: Path) -> None:
    """RF-004: ProjectLock adquire com timeout 0 e falha com ProjectAlreadyRunningError."""
    lock_file = tmp_path / ".rpa3003.lock"
    with ProjectLock(lock_file) as lock:
        assert isinstance(lock, ProjectLock)
        with pytest.raises(ProjectAlreadyRunningError) as info, ProjectLock(lock_file):
            pass
        assert info.value.details == {"lock": str(lock_file)}
        assert info.value.exit_code is ExitCode.ALREADY_RUNNING
    with ProjectLock(lock_file):
        pass


# --- RF-011: isolamento dev/prd ----------------------------------------------------------------


@pytest.mark.parametrize(("env", "other"), [("dev", "prd"), ("prd", "dev")])
def test_only_selected_environment_is_imported(project: Path, env: str, other: str) -> None:
    """RF-006 e RF-011: com env=dev, nenhum workflows.prd* fica em sys.modules (e vice-versa)."""
    result, _ = run_project(project, env=env)
    assert result.exit_code is ExitCode.SUCCESS
    loaded = [name for name in sys.modules if name.startswith("workflows.")]
    assert f"workflows.{env}.step_setup_env" in loaded
    assert not [name for name in loaded if name.startswith(f"workflows.{other}")]


# --- bootstrap ---------------------------------------------------------------------------------


def test_bootstrap_prepares_context(project: Path, read_log_lines: ReadLines) -> None:
    """Seção 8.8: bootstrap ajusta cwd, sys.path, diretórios, logger e gera run_id."""
    before = datetime.now().astimezone()
    ctx = bootstrap(project, env=" PRD ", user="robo01", extra_args=["--filial", "12"])
    paths = ProjectPaths.from_root(project)
    assert Path.cwd() == paths.root
    assert sys.path[0] == str(paths.root)
    assert ctx.project_name == "projeto_teste"
    assert ctx.env is Environment.PRD
    assert ctx.paths == paths
    assert ctx.user == "robo01"
    assert ctx.extra_args == ["--filial", "12"]
    assert RUN_ID_REGEX.match(ctx.run_id)
    assert ctx.started_at.tzinfo is not None and ctx.started_at >= before
    assert ctx.state == {} and ctx.error is None
    assert paths.logs_dir.is_dir() and paths.screenshots_dir.is_dir() and paths.data_dir.is_dir()
    ctx.logger.info("depois do bootstrap")
    (log_file,) = paths.logs_dir.glob("projeto_teste_*.log")
    assert re.fullmatch(r"projeto_teste_\d{4}-\d{2}-\d{2}\.log", log_file.name)
    assert read_log_lines(paths.logs_dir)[-1].endswith("[robo01] depois do bootstrap")


def test_bootstrap_generates_unique_run_ids(project: Path) -> None:
    """RNF-015: cada bootstrap gera um run_id diferente."""
    assert bootstrap(project).run_id != bootstrap(project).run_id


def test_bootstrap_does_not_duplicate_sys_path(project: Path) -> None:
    """Seção 8.8: a raiz só é inserida em sys.path se ausente."""
    bootstrap(project)
    bootstrap(project)
    assert sys.path.count(str(ProjectPaths.from_root(project).root)) == 1


def test_bootstrap_purges_modules_from_other_projects(tmp_path: Path, project: Path) -> None:
    """Seção 8.8: global_vars e workflows* de outra raiz são removidos de sys.modules."""
    foreign = ModuleType("workflows")
    foreign.__file__ = str(tmp_path / "outro" / "workflows" / "__init__.py")
    sys.modules["workflows"] = foreign
    sys.modules["workflows.dev"] = ModuleType("workflows.dev")
    sys.modules["global_vars"] = ModuleType("global_vars")
    own = ModuleType("workflows.prd")
    own.__file__ = str(project / "workflows" / "prd" / "__init__.py")
    sys.modules["workflows.prd"] = own
    unrelated = ModuleType("workflows_utils")
    sys.modules["workflows_utils"] = unrelated
    try:
        bootstrap(project)
        assert "workflows" not in sys.modules
        assert "workflows.dev" not in sys.modules
        assert "global_vars" not in sys.modules
        assert sys.modules["workflows.prd"] is own
        assert sys.modules["workflows_utils"] is unrelated
    finally:
        sys.modules.pop("workflows_utils", None)


def test_switching_projects_imports_new_steps(tmp_path: Path) -> None:
    """Seção 8.8: dois projetos no mesmo processo usam cada um os próprios steps."""
    first_steps = default_steps()
    first_steps["step_setup_env"] = step_source('self.ctx.state["projeto"] = "primeiro"')
    second_steps = default_steps()
    second_steps["step_setup_env"] = step_source('self.ctx.state["projeto"] = "segundo"')
    first = write_project(tmp_path / "primeiro", first_steps)
    second = write_project(tmp_path / "segundo", second_steps)
    assert run_project(first)[1].state["projeto"] == "primeiro"
    assert run_project(second)[1].state["projeto"] == "segundo"


def test_bootstrap_user_precedence(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RF-016: --user → RPA3003_LOG_USER → usuário do sistema."""
    monkeypatch.setattr("rpa_3003.core.workflow.default_user", lambda: "usuario.windows")
    assert bootstrap(project).user == "usuario.windows"
    monkeypatch.setenv("RPA3003_LOG_USER", "da_variavel")
    assert bootstrap(project).user == "da_variavel"
    assert bootstrap(project, user="do_argumento").user == "do_argumento"
    assert bootstrap(project, user="   ").user == "da_variavel"


def test_bootstrap_reads_project_dotenv(project: Path) -> None:
    """RF-010 e RF-013: o .env da pasta do projeto define ambiente e nível de log."""
    (project / ".env").write_text("RPA3003_ENV=prd\nRPA3003_LOG_LEVEL=WARNING\n", encoding="utf-8")
    ctx = bootstrap(project)
    assert ctx.env is Environment.PRD
    assert ctx.settings.log_level == "WARNING"
    assert bootstrap(project, env="dev", log_level="DEBUG").settings.log_level == "DEBUG"


def test_bootstrap_environment_precedence(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RF-010: --env → RPA3003_ENV → .env → dev."""
    assert bootstrap(project).env is Environment.DEV
    (project / ".env").write_text("RPA3003_ENV=prd\n", encoding="utf-8")
    assert bootstrap(project).env is Environment.PRD
    monkeypatch.setenv("RPA3003_ENV", "dev")
    assert bootstrap(project).env is Environment.DEV
    assert bootstrap(project, env="prd").env is Environment.PRD


def test_bootstrap_invalid_environment(project: Path) -> None:
    """RF-010: --env hml gera InvalidEnvironmentError (código 2)."""
    with pytest.raises(InvalidEnvironmentError) as info:
        bootstrap(project, env="hml")
    assert int(info.value.exit_code) == 2


def test_bootstrap_invalid_settings(project: Path) -> None:
    """RF-013: configuração inválida no .env do projeto gera ConfigurationError."""
    (project / ".env").write_text("RPA3003_LOG_RETENTION_DAYS=0\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="log_retention_days"):
        bootstrap(project)


def test_bootstrap_missing_environment_folder(tmp_path: Path) -> None:
    """Seção 8.8: sem workflows/<env>/, bootstrap lança ProjectStructureError (código 3)."""
    root = write_project(tmp_path / "so_dev", other_env=False)
    with pytest.raises(ProjectStructureError) as info:
        bootstrap(root, env="prd")
    assert info.value.exit_code is ExitCode.CONFIGURATION_ERROR


def test_bootstrap_missing_project(tmp_path: Path) -> None:
    """Seção 8.8: pasta de projeto inexistente gera ProjectNotFoundError (código 3)."""
    with pytest.raises(ProjectNotFoundError):
        bootstrap(tmp_path / "nao_existe")


def test_bootstrap_reconfigures_console_to_utf8(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P-21 e RNF-011: stdout/stderr em cp850 passam a UTF-8 com errors='replace'."""
    fake_stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp850")
    fake_stderr = io.TextIOWrapper(io.BytesIO(), encoding="cp850")
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(sys, "stderr", fake_stderr)
    ctx = bootstrap(project)
    for stream in (fake_stdout, fake_stderr):
        assert stream.encoding == "utf-8"
        assert stream.errors == "replace"
    ctx.logger.info("Conciliação concluída | total=€ 1.500,00")
    ctx.logger.complete()
    fake_stderr.flush()
    written = fake_stderr.buffer.getvalue().decode("utf-8")  # type: ignore[attr-defined]
    assert "Conciliação concluída | total=€ 1.500,00" in written


def test_bootstrap_ignores_streams_without_reconfigure(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P-21: streams sem reconfigure (ex.: pythonw) não impedem o bootstrap."""
    monkeypatch.setenv("RPA3003_LOG_CONSOLE", "false")
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    assert bootstrap(project).settings.log_console is False


# --- split_passthrough_args e constantes -------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--env", "prd"], (["--env", "prd"], [])),
        (["--env", "prd", "--", "--filial", "12"], (["--env", "prd"], ["--filial", "12"])),
        (["--", "a", "--", "b"], ([], ["a", "--", "b"])),
        ([], ([], [])),
        (("--env", "dev", "--"), (["--env", "dev"], [])),
    ],
)
def test_split_passthrough_args(argv: list[str], expected: tuple[list[str], list[str]]) -> None:
    """Seção 8.8: divide no primeiro '--'; sem '--', retorna (argv, [])."""
    assert split_passthrough_args(argv) == expected


def test_default_steps() -> None:
    """RF-006: sequência padrão de steps."""
    assert DEFAULT_STEPS == ("step_setup_env", "step_execute_task", "step_wrapup_execution")


def test_whole_log_matches_mandatory_format(tmp_path: Path, read_log_lines: ReadLines) -> None:
    """RF-015: todas as linhas (exceto traceback) de uma execução com falha casam com a regex."""
    steps = default_steps()
    steps["step_execute_task"] = step_source(FAILING_BODY)
    _, ctx = run_project(write_project(tmp_path / "p", steps))
    lines = read_log_lines(ctx.paths.logs_dir)
    log_lines = [line for line in lines if re.match(r"^\[\d", line)]
    assert len(log_lines) < len(lines)
    assert all(LOG_LINE_REGEX.match(line) for line in log_lines)
