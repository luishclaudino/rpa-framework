"""Testes de `rpa_3003.cli` e `rpa_3003.__main__` (RF-048 a RF-052, RF-055, seções 8.2 e 9).

Os comandos são chamados por `main(argv)`. O `subprocess.Popen` é simulado nos testes de `run`,
exceto nos que verificam o encerramento real da árvore de processos (RF-052). Os templates vêm
de uma pasta em `tmp_path` apontada por `templates_root`; o conteúdo do `example` é da Sprint 6.
"""

from __future__ import annotations

import os
import runpy
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

import psutil
import pytest
from pytest_mock import MockerFixture

from rpa_3003 import cli, templates
from rpa_3003._version import __version__
from rpa_3003.cli import (
    CHILD_WAIT_ON_INTERRUPT,
    LOG_LEVELS,
    PROG,
    TERMINATE_GRACE_SECONDS,
    build_child_env,
    build_parser,
    build_run_command,
    kill_process_tree,
    main,
    run_child,
)
from rpa_3003.core.config import (
    REQUIRED_PROJECT_ITEMS,
    Environment,
    ProjectPaths,
    validate_project_structure,
)
from rpa_3003.core.exceptions import ExitCode, WorkflowError

pytestmark = pytest.mark.unit

STEP_SOURCE = '''"""Step de teste."""

from rpa_3003.core.workflow import BaseStep


class Step(BaseStep):
    def run(self) -> None:
        self.log.info("ok")
'''
STEP_TEMPLATE = '''"""Step do projeto ${project_name} (autor: ${author})."""

from rpa_3003.core.workflow import BaseStep


class Step(BaseStep):
    def run(self) -> None:
        self.log.info("ok")
'''
EXAMPLE_DESCRIPTION = (
    "Projeto RPA padrão: workflows dev/prd, Vault, logging e exemplo de processamento de CSV"
)
TIMEOUT_MAIN = '''"""Projeto de teste do tempo limite (RF-052)."""

import os
import runpy
import sys
from pathlib import Path

from rpa_3003.core.workflow import DEFAULT_STEPS, WorkflowRunner, bootstrap, split_passthrough_args

ROOT = Path(__file__).resolve().parent


def main() -> int:
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "main.pid").write_text(str(os.getpid()), encoding="utf-8")
    own, extra = split_passthrough_args(sys.argv[1:])
    ctx = bootstrap(ROOT, env=own[own.index("--env") + 1], extra_args=extra)
    return int(WorkflowRunner(ctx, steps=DEFAULT_STEPS).run().exit_code)


if __name__ == "__main__":
    sys.exit(main())
'''
SLEEPING_STEP = '''"""Step que inicia um processo neto e dorme 60 s."""

import subprocess
import sys
import time

from rpa_3003.core.workflow import BaseStep

GRANDCHILD = (
    "import os, pathlib, sys, time; "
    "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"
)


class Step(BaseStep):
    def run(self) -> None:
        pid_file = self.ctx.paths.data_dir / "neto.pid"
        subprocess.Popen([sys.executable, "-c", GRANDCHILD, str(pid_file)])
        time.sleep(60)
'''
GRANDCHILD_SCRIPT = """import os
import pathlib
import sys
import time

pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding="utf-8")
time.sleep(60)
"""
PARENT_SCRIPT = """import subprocess
import sys
import time

subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])
time.sleep(60)
"""

TemplateWriter = Callable[..., Path]


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Diretório atual em `tmp_path`: `./projects` e `.env` do CLI ficam isolados."""
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def template_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pasta de templates de teste no lugar de `rpa_3003/templates/`."""
    root = tmp_path / "templates_teste"
    root.mkdir()
    monkeypatch.setattr(templates, "templates_root", lambda: root)
    return root


@pytest.fixture
def write_template(template_root: Path) -> TemplateWriter:
    """Cria um template que gera a estrutura obrigatória do RF-001."""

    def write(
        name: str,
        *,
        version: str = "1.0.0",
        description: str = "Template de teste",
        extra: dict[str, str] | None = None,
        skip: tuple[str, ...] = (),
    ) -> Path:
        directory = template_root / name
        directory.mkdir()
        (directory / "template.toml").write_text(
            f'name = "{name}"\ndescription = "{description}"\nversion = "{version}"\n'
            'ensure_dirs = ["logs", "data", "data/input"]\n',
            encoding="utf-8",
        )
        files = {
            f"{item}.tpl": STEP_TEMPLATE if "step_" in item else '"""${project_name}."""\n'
            for item in REQUIRED_PROJECT_ITEMS
            if not item.endswith("/") and item not in skip
        }
        files["dot_env.example.tpl"] = "RPA3003_ENV=dev\n"
        files.update(extra or {})
        for relative, content in files.items():
            target = directory / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return directory

    return write


@pytest.fixture
def popen(mocker: MockerFixture) -> MagicMock:
    """`subprocess.Popen` simulado; o processo retornado termina com código 0."""
    process = MagicMock(name="processo")
    process.pid = 4242
    process.wait.return_value = 0
    process.poll.return_value = 0
    return mocker.patch("subprocess.Popen", return_value=process)


@pytest.fixture
def killer(mocker: MockerFixture) -> MagicMock:
    return mocker.patch.object(cli, "kill_process_tree")


def make_project(projects_dir: Path, name: str, *, valid: bool = True) -> Path:
    """Cria um projeto com a estrutura do RF-001; inválido sem um step de prd e sem classe."""
    root = projects_dir / name
    for item in REQUIRED_PROJECT_ITEMS:
        path = root / item
        if item.endswith("/"):
            path.mkdir(parents=True, exist_ok=True)
            continue
        if not valid and item == "workflows/prd/step_execute_task.py":
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        content = STEP_SOURCE if path.name.startswith("step_") else '"""Módulo."""\n'
        if not valid and item == "workflows/dev/step_setup_env.py":
            content = '"""Sem classe Step."""\n'
        path.write_text(content, encoding="utf-8")
    return root


def _lines(text: str) -> list[str]:
    return text.splitlines()


def _wait_for(condition: Callable[[], bool], timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.1)
    return condition()


def _read_pid(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(text) if text else None


def _running_with_marker(pid: int, marker: str) -> bool:
    """Processo ativo cuja linha de comando contém `marker` (evita falso positivo por reuso)."""
    try:
        process = psutil.Process(pid)
        return process.is_running() and marker in " ".join(process.cmdline())
    except psutil.Error:
        return False


# --- Parser, versão e ajuda (RF-051) ---------------------------------------------------------


def test_contract_constants() -> None:
    """Seção 8.2: constantes públicas do CLI."""
    assert PROG == "rpa3003"
    assert CHILD_WAIT_ON_INTERRUPT == 10.0
    assert TERMINATE_GRACE_SECONDS == 5.0
    assert LOG_LEVELS == ("TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL")


def test_version_prints_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """RF-051: --version imprime 'rpa3003 1.0.0' e encerra com 0 (SystemExit do argparse)."""
    with pytest.raises(SystemExit) as info:
        main(["--version"])
    assert info.value.code == 0
    assert capsys.readouterr().out == f"rpa3003 {__version__}\n"
    assert __version__ == "1.0.0"


@pytest.mark.parametrize(
    "argv",
    [
        ["-h"],
        ["--help"],
        ["create", "-h"],
        ["run", "--help"],
        ["list", "-h"],
        ["templates", "-h"],
        ["validate", "-h"],
    ],
)
def test_help_at_every_level(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    """RF-051: -h/--help em todos os níveis, em pt-BR, retorna 0."""
    with pytest.raises(SystemExit) as info:
        main(argv)
    assert info.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("uso: rpa3003")
    assert "Exibe esta ajuda e encerra" in out


def test_run_help_documents_passthrough(capsys: pytest.CaptureFixture[str]) -> None:
    """Seção 9.4.2: a ajuda de run usa os nomes da seção 9 e explica o '--'."""
    with pytest.raises(SystemExit):
        main(["run", "-h"])
    out = capsys.readouterr().out
    for fragment in ("--env {dev,prd}", "--user USUARIO", "--timeout SEGUNDOS", "CAMINHO", "'--'"):
        assert fragment in out


def test_no_command_prints_help_and_returns_2(capsys: pytest.CaptureFixture[str]) -> None:
    """Seção 8.2: sem subcomando, imprime a ajuda e retorna 2."""
    assert main([]) == ExitCode.USAGE_ERROR
    assert "<comando>" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["comando-inexistente"],
        ["run"],
        ["run", "proj", "--log-level", "VERBOSO"],
        ["run", "proj", "--timeout", "abc"],
        ["create", "proj", "--opcao-inexistente"],
    ],
)
def test_usage_errors_exit_2(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    """RF-051: erro de uso do argparse encerra com 2 e mensagem 'erro:' em stderr."""
    with pytest.raises(SystemExit) as info:
        main(argv)
    assert info.value.code == ExitCode.USAGE_ERROR
    err = capsys.readouterr().err
    assert "uso: rpa3003" in err
    assert "rpa3003" in err
    assert ": erro: " in err


def test_build_parser_defaults_and_handlers() -> None:
    """Seção 9.7: padrões dos argumentos e handler de cada subcomando."""
    parser = build_parser()
    create = parser.parse_args(["create", "proj"])
    assert (create.name, create.template, create.projects_dir) == ("proj", "example", None)
    assert (create.author, create.force, create.verbose) == (None, False, False)
    assert create.handler is cli.cmd_create
    run = parser.parse_args(["-v", "run", "proj"])
    assert (run.env, run.user, run.log_level, run.timeout, run.projects_dir) == (
        None,
        None,
        None,
        None,
        None,
    )
    assert run.verbose is True
    assert run.handler is cli.cmd_run
    assert parser.parse_args(["run", "p", "--timeout", "30"]).timeout == 30
    assert parser.parse_args(["list"]).handler is cli.cmd_list
    assert parser.parse_args(["templates"]).handler is cli.cmd_templates
    assert parser.parse_args(["validate", "p"]).handler is cli.cmd_validate


def test_main_returns_int_without_exiting(projects_dir: Path) -> None:
    """RF-051: cli.main(["list", "--projects-dir", tmp]) retorna int sem encerrar o processo."""
    result = main(["list", "--projects-dir", str(projects_dir)])
    assert isinstance(result, int)
    assert result == 0


def test_main_reads_sys_argv_when_none(
    projects_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Seção 8.2: argv=None usa sys.argv[1:] (console script)."""
    monkeypatch.setattr(sys, "argv", ["rpa3003", "list", "--projects-dir", str(projects_dir)])
    assert main() == 0
    assert "Nenhum projeto encontrado" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("argv", "level"), [(["templates"], "INFO"), (["-v", "templates"], "DEBUG")]
)
def test_main_configures_console_logger(
    mocker: MockerFixture, template_root: Path, argv: list[str], level: str
) -> None:
    """Seção 8.2: setup_logger somente no console, em DEBUG com --verbose."""
    setup = mocker.patch.object(cli, "setup_logger")
    assert main(argv) == 0
    setup.assert_called_once_with(logs_dir=None, level=level)


def test_main_reconfigures_std_streams(mocker: MockerFixture, template_root: Path) -> None:
    """Seção 8.2: stdout e stderr reconfigurados para UTF-8 com errors='replace'."""
    reconfigure = mocker.patch.object(cli, "_reconfigure_std_streams")
    main(["templates"])
    reconfigure.assert_called_once_with()


def test_reconfigure_std_streams_tolerates_unusual_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P-21: streams sem reconfigure ou que recusam a troca não interrompem o CLI."""
    stdout = MagicMock()
    stderr = MagicMock()
    stderr.reconfigure.side_effect = ValueError("stream fechado")
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    cli._reconfigure_std_streams()
    stdout.reconfigure.assert_called_once_with(encoding="utf-8", errors="replace")
    monkeypatch.setattr(sys, "stdout", object())
    cli._reconfigure_std_streams()


def test_main_converts_rpa_error_to_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Seção 8.2: RPAError vira log ERROR com a mensagem e o código da exceção."""
    missing = tmp_path / "inexistente"
    assert main(["list", "--projects-dir", str(missing)]) == ExitCode.CONFIGURATION_ERROR
    err = capsys.readouterr().err
    assert "[ERROR]" in err
    assert f"Diretório de projetos não encontrado | caminho={missing}" in err


def test_main_keyboard_interrupt_returns_130(mocker: MockerFixture) -> None:
    """Seção 9.5: Ctrl+C durante um comando retorna 130."""
    mocker.patch.object(cli, "cmd_templates", side_effect=KeyboardInterrupt)
    assert main(["templates"]) == ExitCode.INTERRUPTED


# --- create (RF-048) -------------------------------------------------------------------------


def test_create_success_prints_summary(
    write_template: TemplateWriter, projects_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """RF-048: nome válido cria o projeto em ≤ 5 s, validate retorna [] e o código é 0."""
    write_template("example")
    started = time.perf_counter()
    assert main(["create", "conciliacao_bancaria"]) == 0
    assert time.perf_counter() - started <= 5
    root = projects_dir.resolve() / "conciliacao_bancaria"
    assert validate_project_structure(root) == []
    assert _lines(capsys.readouterr().out) == [
        f"Projeto 'conciliacao_bancaria' criado em {root}",
        "Template: example 1.0.0",
        "Próximos passos:",
        "  1. Revise global_vars.py (ENV_CONFIG)",
        "  2. Copie .env.example para .env e ajuste as variáveis",
        "  3. Execute: rpa3003 run conciliacao_bancaria --env dev",
    ]
    assert (root / ".env.example").is_file()


def test_create_with_projects_dir_option(
    write_template: TemplateWriter, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Seção 9.4.1: --projects-dir cria lá e o próximo passo repete a opção."""
    write_template("example")
    target = tmp_path / "robos" / "projects"
    assert main(["create", "emissao_notas", "--projects-dir", str(target)]) == 0
    assert (target / "emissao_notas" / "main.py").is_file()
    out = _lines(capsys.readouterr().out)
    assert out[-1] == f'  3. Execute: rpa3003 run emissao_notas --env dev --projects-dir "{target}"'


def test_create_uses_projects_dir_from_environment(
    write_template: TemplateWriter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Seção 9.4.1: sem --projects-dir, usa RPA3003_PROJECTS_DIR."""
    write_template("example")
    target = tmp_path / "via_env"
    monkeypatch.setenv("RPA3003_PROJECTS_DIR", str(target))
    assert main(["create", "emissao_notas"]) == 0
    assert (target / "emissao_notas").is_dir()
    assert (
        _lines(capsys.readouterr().out)[-1] == "  3. Execute: rpa3003 run emissao_notas --env dev"
    )


def test_create_with_new_template_and_author(
    write_template: TemplateWriter, projects_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """RF-055/RF-048: --template usa o template novo e --author vai para os arquivos."""
    write_template("example")
    write_template("web_basico", version="2.0.0")
    argv = ["create", "emissao_notas", "--template", "web_basico", "--author", "carlos.lima"]
    assert main(argv) == 0
    step = projects_dir / "emissao_notas" / "workflows" / "dev" / "step_setup_env.py"
    assert "(autor: carlos.lima)" in step.read_text(encoding="utf-8")
    assert "Template: web_basico 2.0.0" in capsys.readouterr().out


def test_create_invalid_name_returns_2(
    write_template: TemplateWriter, projects_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Seção 9.4.1: InvalidProjectNameError → código 2, sem criar nada."""
    write_template("example")
    assert main(["create", "Projeto-Invalido"]) == ExitCode.USAGE_ERROR
    assert "letras minúsculas" in capsys.readouterr().err
    assert list(projects_dir.iterdir()) == []


def test_create_unknown_template_returns_3(
    write_template: TemplateWriter, projects_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """RF-055: template inexistente gera código 3 listando os disponíveis."""
    write_template("example")
    write_template("web_basico")
    assert main(["create", "emissao_notas", "--template", "nada"]) == ExitCode.CONFIGURATION_ERROR
    assert "disponiveis=['example', 'web_basico']" in capsys.readouterr().err
    assert list(projects_dir.iterdir()) == []


def test_create_existing_project_returns_3_unchanged(
    write_template: TemplateWriter, projects_dir: Path
) -> None:
    """RF-048: projeto existente sem --force gera código 3 e nada é alterado."""
    write_template("example")
    existing = projects_dir / "emissao_notas"
    existing.mkdir()
    (existing / "meu_codigo.py").write_text("x = 1\n", encoding="utf-8")
    assert main(["create", "emissao_notas"]) == ExitCode.CONFIGURATION_ERROR
    assert [path.name for path in existing.iterdir()] == ["meu_codigo.py"]
    assert [path.name for path in projects_dir.iterdir()] == ["emissao_notas"]


def test_create_force_renames_existing(write_template: TemplateWriter, projects_dir: Path) -> None:
    """RF-048: --force renomeia a pasta antiga para <nome>.bak-<AAAAMMDDHHMMSS> e recria."""
    write_template("example")
    existing = projects_dir / "emissao_notas"
    existing.mkdir()
    (existing / "meu_codigo.py").write_text("x = 1\n", encoding="utf-8")
    assert main(["create", "emissao_notas", "--force"]) == 0
    names = sorted(path.name for path in projects_dir.iterdir())
    assert names[0] == "emissao_notas"
    assert len(names) == 2
    assert names[1].startswith("emissao_notas.bak-")
    assert len(names[1].removeprefix("emissao_notas.bak-")) == 14
    assert (projects_dir / names[1] / "meu_codigo.py").is_file()
    assert validate_project_structure(projects_dir / "emissao_notas") == []


def test_create_render_failure_leaves_no_partial_folder(
    write_template: TemplateWriter, projects_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """RF-048: falha na renderização retorna 3 e não deixa pasta parcial."""
    write_template("example", extra={"workflows/prd/zz.py.tpl": "${variavel_inexistente}"})
    assert main(["create", "emissao_notas"]) == ExitCode.CONFIGURATION_ERROR
    assert "variavel=variavel_inexistente" in capsys.readouterr().err
    assert list(projects_dir.iterdir()) == []


def test_create_invalid_structure_returns_3(
    write_template: TemplateWriter, projects_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Seção 9.4.1: projeto criado que não passa em validate gera ProjectStructureError (3)."""
    write_template("example", skip=("workflows/prd/step_execute_task.py",))
    assert main(["create", "emissao_notas"]) == ExitCode.CONFIGURATION_ERROR
    err = capsys.readouterr().err
    assert (
        "Projeto 'emissao_notas' inválido:\n- ausente: workflows/prd/step_execute_task.py\n" in err
    )
    assert "Estrutura do projeto 'emissao_notas' inválida" in err


# --- run (RF-049, RF-052) --------------------------------------------------------------------


def test_build_run_command_minimal(projects_dir: Path) -> None:
    """RF-049: [python, main.py absoluto, '--env', env] sem opções não informadas."""
    paths = ProjectPaths.from_root(make_project(projects_dir, "proj"))
    assert build_run_command(paths, Environment.DEV) == [
        sys.executable,
        str(paths.main_file),
        "--env",
        "dev",
    ]


def test_build_run_command_full(projects_dir: Path) -> None:
    """RF-049: --user, --log-level e '-- extras' acrescentados quando informados."""
    paths = ProjectPaths.from_root(make_project(projects_dir, "proj"))
    command = build_run_command(
        paths,
        Environment.PRD,
        user="marcos.op",
        log_level="DEBUG",
        extra_args=["--data-ref", "2026-10-31"],
    )
    assert command == [
        sys.executable,
        str(paths.main_file),
        "--env",
        "prd",
        "--user",
        "marcos.op",
        "--log-level",
        "DEBUG",
        "--",
        "--data-ref",
        "2026-10-31",
    ]


def test_build_child_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Seção 9.6: cópia do ambiente com RPA3003_ENV, PYTHONUTF8=1 e PYTHONIOENCODING=utf-8."""
    monkeypatch.setenv("RPA3003_LOG_USER", "svc_rpa")
    monkeypatch.setenv("PYTHONUTF8", "0")
    child = build_child_env(Environment.PRD)
    assert child["RPA3003_ENV"] == "prd"
    assert child["PYTHONUTF8"] == "1"
    assert child["PYTHONIOENCODING"] == "utf-8"
    assert child["RPA3003_LOG_USER"] == "svc_rpa"
    assert os.environ["PYTHONUTF8"] == "0"
    assert "RPA3003_ENV" not in os.environ


def test_run_starts_child_in_project_folder(
    popen: MagicMock, projects_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """RF-049: subprocesso com cwd na pasta do projeto e argumentos main.py --env <env>."""
    root = make_project(projects_dir, "proj")
    assert main(["run", "proj"]) == 0
    args, kwargs = popen.call_args
    assert args == ([sys.executable, str(root / "main.py"), "--env", "dev"],)
    assert kwargs["cwd"] == root
    assert kwargs["env"]["RPA3003_ENV"] == "dev"
    assert kwargs["env"]["PYTHONUTF8"] == "1"
    assert kwargs["env"]["PYTHONIOENCODING"] == "utf-8"
    assert set(kwargs) == {"cwd", "env"}
    popen.return_value.wait.assert_called_once_with(timeout=None)
    assert (
        "Executando projeto | nome=proj | ambiente=dev | timeout=sem limite"
        in capsys.readouterr().err
    )


@pytest.mark.parametrize("child_code", [0, 1, 3, 4, 6])
def test_run_propagates_child_exit_code(
    popen: MagicMock, projects_dir: Path, child_code: int
) -> None:
    """RF-049: o código de saída do CLI é igual ao do subprocesso."""
    make_project(projects_dir, "proj")
    popen.return_value.wait.return_value = child_code
    assert main(["run", "proj"]) == child_code


def test_run_forwards_options_and_passthrough(popen: MagicMock, projects_dir: Path) -> None:
    """RF-049: argumentos após '--' chegam ao projeto (ctx.extra_args)."""
    root = make_project(projects_dir, "proj")
    argv = [
        "run",
        "proj",
        "--env",
        "prd",
        "--user",
        "marcos.op",
        "--log-level",
        "DEBUG",
        "--projects-dir",
        str(projects_dir),
        "--",
        "--data-ref",
        "2026-10-31",
    ]
    assert main(argv) == 0
    assert popen.call_args.args[0] == [
        sys.executable,
        str(root / "main.py"),
        "--env",
        "prd",
        "--user",
        "marcos.op",
        "--log-level",
        "DEBUG",
        "--",
        "--data-ref",
        "2026-10-31",
    ]


@pytest.mark.parametrize(
    ("argv_env", "os_env", "project_dotenv", "cli_dotenv", "expected"),
    [
        (None, None, None, None, "dev"),
        (" PRD ", None, None, None, "prd"),
        ("dev", "prd", None, None, "dev"),
        (None, "PRD", None, None, "prd"),
        (None, None, "prd", None, "prd"),
        (None, "dev", "prd", None, "dev"),
        (None, None, None, "prd", "dev"),
    ],
)
def test_run_resolves_environment_like_child(
    popen: MagicMock,
    projects_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    argv_env: str | None,
    os_env: str | None,
    project_dotenv: str | None,
    cli_dotenv: str | None,
    expected: str,
) -> None:
    """RF-010: --env → RPA3003_ENV → .env do projeto → dev; o valor resolvido vai ao filho."""
    root = make_project(projects_dir, "proj")
    if os_env is not None:
        monkeypatch.setenv("RPA3003_ENV", os_env)
    if project_dotenv is not None:
        (root / ".env").write_text(f"RPA3003_ENV={project_dotenv}\n", encoding="utf-8")
    if cli_dotenv is not None:
        (tmp_path / ".env").write_text(f"RPA3003_ENV={cli_dotenv}\n", encoding="utf-8")
    argv = ["run", "proj"] + ([] if argv_env is None else ["--env", argv_env])
    assert main(argv) == 0
    command = popen.call_args.args[0]
    assert command[2:4] == ["--env", expected]
    assert popen.call_args.kwargs["env"]["RPA3003_ENV"] == expected


def test_run_invalid_env_returns_2(
    popen: MagicMock, projects_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """RF-010: --env hml gera código 2 e a mensagem lista 'dev, prd'."""
    make_project(projects_dir, "proj")
    assert main(["run", "proj", "--env", "hml"]) == ExitCode.USAGE_ERROR
    assert "dev, prd" in capsys.readouterr().err
    popen.assert_not_called()


def test_run_project_not_found_lists_available(
    popen: MagicMock, projects_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """RF-049: projeto inexistente gera código 3 e a mensagem lista os disponíveis."""
    make_project(projects_dir, "alfa")
    make_project(projects_dir, "beta")
    assert main(["run", "inexistente"]) == ExitCode.CONFIGURATION_ERROR
    err = capsys.readouterr().err
    assert "Projeto não encontrado: 'inexistente'" in err
    assert "disponiveis=['alfa', 'beta']" in err
    popen.assert_not_called()


def test_run_invalid_structure_prints_problems(
    popen: MagicMock, projects_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Seção 9.4.2: estrutura inválida imprime os problemas e retorna 3 sem executar."""
    make_project(projects_dir, "proj", valid=False)
    assert main(["run", "proj"]) == ExitCode.CONFIGURATION_ERROR
    err = capsys.readouterr().err
    assert "Projeto 'proj' inválido:\n- ausente: workflows/prd/step_execute_task.py\n" in err
    popen.assert_not_called()


@pytest.mark.parametrize("value", ["0", "-5"])
def test_run_timeout_must_be_positive(
    popen: MagicMock, projects_dir: Path, value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Seção 9.7: --timeout menor ou igual a zero é erro de uso (código 2)."""
    make_project(projects_dir, "proj")
    assert main(["run", "proj", "--timeout", value]) == ExitCode.USAGE_ERROR
    assert f"Tempo limite inválido: {value}" in capsys.readouterr().err
    popen.assert_not_called()


@pytest.mark.parametrize(
    ("argv_timeout", "env_timeout", "expected"),
    [(["--timeout", "30"], None, 30), ([], "60", 60), (["--timeout", "15"], "60", 15)],
)
def test_run_timeout_option_and_environment(
    popen: MagicMock,
    projects_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv_timeout: list[str],
    env_timeout: str | None,
    expected: int,
) -> None:
    """RF-052: --timeout ou RPA3003_RUN_TIMEOUT definem a espera pelo filho."""
    make_project(projects_dir, "proj")
    if env_timeout is not None:
        monkeypatch.setenv("RPA3003_RUN_TIMEOUT", env_timeout)
    assert main(["run", "proj", *argv_timeout]) == 0
    popen.return_value.wait.assert_called_once_with(timeout=expected)
    assert f"timeout={expected}s" in capsys.readouterr().err


def test_run_child_timeout_kills_tree(
    popen: MagicMock, killer: MagicMock, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """RF-052: tempo limite excedido encerra a árvore do filho, registra ERROR e retorna 5."""
    process = popen.return_value
    process.wait.side_effect = [subprocess.TimeoutExpired("python", 2), 1]
    cli.setup_logger(logs_dir=None)
    assert run_child(["python"], cwd=tmp_path, env={}, timeout=2) == ExitCode.TIMEOUT
    killer.assert_called_once_with(4242)
    assert process.wait.call_args_list[1].kwargs == {"timeout": TERMINATE_GRACE_SECONDS}
    err = capsys.readouterr().err
    assert "[ERROR]" in err
    assert "Tempo limite excedido | timeout=2s | pid=4242" in err


@pytest.mark.parametrize(
    "wait_effects",
    [
        [KeyboardInterrupt, subprocess.TimeoutExpired("python", 10), 1],
        [KeyboardInterrupt, KeyboardInterrupt, 1],
    ],
)
def test_run_child_interrupt_waits_then_kills(
    popen: MagicMock, killer: MagicMock, tmp_path: Path, wait_effects: list[object]
) -> None:
    """RF-052: Ctrl+C aguarda o filho até 10 s; se continuar ativo, encerra a árvore; 130."""
    process = popen.return_value
    process.wait.side_effect = wait_effects
    process.poll.return_value = None
    assert run_child(["python"], cwd=tmp_path, env={}, timeout=None) == ExitCode.INTERRUPTED
    assert process.wait.call_args_list[1].kwargs == {"timeout": CHILD_WAIT_ON_INTERRUPT}
    killer.assert_called_once_with(4242)


def test_run_child_interrupt_child_finishes_wrapup(
    popen: MagicMock, killer: MagicMock, tmp_path: Path
) -> None:
    """RF-052: se o filho termina dentro da espera, nenhum processo é encerrado à força."""
    process = popen.return_value
    process.wait.side_effect = [KeyboardInterrupt, 130]
    process.poll.return_value = 130
    assert run_child(["python"], cwd=tmp_path, env={}, timeout=None) == ExitCode.INTERRUPTED
    killer.assert_not_called()


def test_run_child_start_failure(popen: MagicMock, tmp_path: Path) -> None:
    """Seção 8.2: falha ao iniciar o processo vira WorkflowError (código 1)."""
    popen.side_effect = FileNotFoundError("python inexistente")
    with pytest.raises(WorkflowError) as info:
        run_child(["python-inexistente"], cwd=tmp_path, env={}, timeout=None)
    assert info.value.exit_code is ExitCode.TECHNICAL_ERROR
    assert info.value.details == {
        "comando": "python-inexistente",
        "erro": "FileNotFoundError: python inexistente",
    }


# --- kill_process_tree (RF-052) --------------------------------------------------------------


def test_kill_process_tree_terminates_real_tree(tmp_path: Path) -> None:
    """RF-052: filho e neto reais são encerrados (verificado com psutil)."""
    pid_file = tmp_path / "neto.pid"
    parent_script = tmp_path / "pai.py"
    grandchild_script = tmp_path / "neto.py"
    parent_script.write_text(PARENT_SCRIPT, encoding="utf-8")
    grandchild_script.write_text(GRANDCHILD_SCRIPT, encoding="utf-8")
    parent = subprocess.Popen(  # noqa: S603 - interpretador atual com scripts do teste
        [sys.executable, str(parent_script), str(grandchild_script), str(pid_file)]
    )
    try:
        assert _wait_for(lambda: _read_pid(pid_file) is not None)
        grandchild = psutil.Process(_read_pid(pid_file) or 0)
        kill_process_tree(parent.pid, grace_seconds=2)
        assert parent.wait(timeout=5) is not None
        assert not grandchild.is_running()
    finally:
        if parent.poll() is None:
            parent.kill()


def test_kill_process_tree_ignores_finished_process(mocker: MockerFixture) -> None:
    """RF-052: processo já encerrado não gera erro."""
    mocker.patch("psutil.Process", side_effect=psutil.NoSuchProcess(999_999))
    wait_procs = mocker.patch("psutil.wait_procs")
    kill_process_tree(999_999)
    wait_procs.assert_not_called()


def _fake_process(pid: int) -> MagicMock:
    process = MagicMock(name=f"processo_{pid}")
    process.pid = pid
    return process


def test_kill_process_tree_terminates_then_kills_survivors(
    mocker: MockerFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """RF-052: terminate em todos, espera grace_seconds, kill nos remanescentes."""
    root, child, grandchild = _fake_process(10), _fake_process(11), _fake_process(12)
    root.children.return_value = [child, grandchild]
    child.terminate.side_effect = psutil.NoSuchProcess(11)
    grandchild.terminate.side_effect = psutil.AccessDenied(12)
    grandchild.kill.side_effect = psutil.NoSuchProcess(12)
    mocker.patch("psutil.Process", return_value=root)
    wait_procs = mocker.patch(
        "psutil.wait_procs", side_effect=[([child], [grandchild, root]), ([grandchild], [root])]
    )
    cli.setup_logger(logs_dir=None, level="DEBUG")
    kill_process_tree(10, grace_seconds=3)
    root.children.assert_called_once_with(recursive=True)
    root.terminate.assert_called_once_with()
    root.kill.assert_called_once_with()
    child.kill.assert_not_called()
    assert wait_procs.call_args_list[0].args == ([child, grandchild, root],)
    assert wait_procs.call_args_list[0].kwargs == {"timeout": 3}
    assert wait_procs.call_args_list[1].args == ([grandchild, root],)
    err = capsys.readouterr().err
    assert "Encerrando árvore de processos | pid=10 | processos=3" in err
    assert "Processo não encerrado | pid=12 | erro=AccessDenied" in err
    assert "Processo continua ativo após kill | pid=10" in err


def test_kill_process_tree_without_access_to_children(mocker: MockerFixture) -> None:
    """RF-052: sem acesso à lista de filhos, encerra ao menos o processo raiz."""
    root = _fake_process(20)
    root.children.side_effect = psutil.AccessDenied(20)
    mocker.patch("psutil.Process", return_value=root)
    wait_procs = mocker.patch("psutil.wait_procs", return_value=([root], []))
    kill_process_tree(20)
    root.terminate.assert_called_once_with()
    root.kill.assert_not_called()
    assert wait_procs.call_args.args == ([root],)
    assert wait_procs.call_args.kwargs == {"timeout": TERMINATE_GRACE_SECONDS}


def test_run_timeout_kills_real_project_tree(projects_dir: Path) -> None:
    """RF-052: step que dorme 60 s com --timeout 2 retorna 5 em até 10 s, sem processos."""
    root = make_project(projects_dir, "dorminhoco")
    (root / "main.py").write_text(TIMEOUT_MAIN, encoding="utf-8")
    for env in ("dev", "prd"):
        (root / "workflows" / env / "step_execute_task.py").write_text(
            SLEEPING_STEP, encoding="utf-8"
        )
    started = time.monotonic()
    result = main(["run", "dorminhoco", "--timeout", "2"])
    elapsed = time.monotonic() - started
    assert result == ExitCode.TIMEOUT
    assert elapsed <= 10
    main_pid = _read_pid(root / "data" / "main.pid")
    grandchild_pid = _read_pid(root / "data" / "neto.pid")
    assert main_pid is not None
    assert grandchild_pid is not None
    assert not _running_with_marker(main_pid, str(root))
    assert not _running_with_marker(grandchild_pid, "neto.pid")
    leftovers = [
        process
        for process in psutil.Process().children(recursive=True)
        if _running_with_marker(process.pid, str(root))
    ]
    assert leftovers == []


# --- list, templates e validate (RF-050) -----------------------------------------------------


def test_list_shows_structure_status(
    projects_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """RF-050: uma linha por projeto com OK ou INVÁLIDA; temporárias e backups ignorados."""
    valid = make_project(projects_dir, "conciliacao_bancaria")
    invalid = make_project(projects_dir, "emissao_notas", valid=False)
    make_project(projects_dir, ".novo.tmp-1a2b3c4d")
    make_project(projects_dir, "emissao_notas.bak-20261031120000")
    assert main(["list"]) == 0
    assert _lines(capsys.readouterr().out) == [
        "PROJETO               ESTRUTURA  CAMINHO",
        f"conciliacao_bancaria  OK         {valid}",
        f"emissao_notas         INVÁLIDA   {invalid}",
    ]


def test_list_without_projects(projects_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Seção 9.4.3: nenhum projeto imprime a mensagem e retorna 0."""
    assert main(["list"]) == 0
    assert capsys.readouterr().out == f"Nenhum projeto encontrado em {projects_dir.resolve()}\n"


def test_list_uses_projects_dir_option(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Seção 9.4.3: --projects-dir aponta outra pasta."""
    other = tmp_path / "robos"
    make_project(other, "alfa")
    assert main(["list", "--projects-dir", str(other)]) == 0
    assert "alfa" in capsys.readouterr().out


def test_templates_lists_packaged_example(capsys: pytest.CaptureFixture[str]) -> None:
    """RF-050: rpa3003 templates exibe example com versão e descrição."""
    assert main(["templates"]) == 0
    lines = _lines(capsys.readouterr().out)
    assert lines[0] == "TEMPLATE  VERSÃO  DESCRIÇÃO"
    assert f"example   1.0.0   {EXAMPLE_DESCRIPTION}" in lines


def test_templates_lists_new_template(
    write_template: TemplateWriter, capsys: pytest.CaptureFixture[str]
) -> None:
    """RF-055: template novo aparece em rpa3003 templates sem alteração de código."""
    write_template("example")
    write_template("web_basico", version="2.10.0", description="Portal web básico")
    assert main(["templates"]) == 0
    assert _lines(capsys.readouterr().out) == [
        "TEMPLATE    VERSÃO  DESCRIÇÃO",
        "example     1.0.0   Template de teste",
        "web_basico  2.10.0  Portal web básico",
    ]


def test_templates_without_templates(
    template_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Seção 9.4.4: pasta sem templates imprime aviso e retorna 0."""
    assert main(["templates"]) == 0
    assert capsys.readouterr().out == "Nenhum template disponível\n"


def test_templates_with_invalid_metadata_returns_3(
    template_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """RF-055: template.toml inválido gera código 3 com o nome do template."""
    (template_root / "quebrado").mkdir()
    (template_root / "quebrado" / "template.toml").write_text("name = ", encoding="utf-8")
    assert main(["templates"]) == ExitCode.CONFIGURATION_ERROR
    assert "Metadados ilegíveis no template 'quebrado'" in capsys.readouterr().err


def test_validate_valid_project(projects_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """RF-050: validate retorna 0 para projeto válido."""
    make_project(projects_dir, "conciliacao_bancaria")
    assert main(["validate", "conciliacao_bancaria"]) == 0
    assert capsys.readouterr().out == "Projeto 'conciliacao_bancaria' válido\n"


def test_validate_invalid_project(projects_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """RF-050: validate retorna 3 e imprime um problema por linha, precedido de '- '."""
    make_project(projects_dir, "emissao_notas", valid=False)
    assert main(["validate", "emissao_notas"]) == ExitCode.CONFIGURATION_ERROR
    assert _lines(capsys.readouterr().out) == [
        "Projeto 'emissao_notas' inválido:",
        "- ausente: workflows/prd/step_execute_task.py",
        "- workflows/dev/step_setup_env.py: classe 'Step' não encontrada",
    ]


def test_validate_reports_prd_importing_dev(
    projects_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """RF-011: arquivo de prd que importa workflows.dev é reportado por validate (código 3)."""
    root = make_project(projects_dir, "proj")
    step = root / "workflows" / "prd" / "step_setup_env.py"
    step.write_text(f"from workflows.dev import step_setup_env\n\n{STEP_SOURCE}", encoding="utf-8")
    assert main(["validate", "proj", "--projects-dir", str(projects_dir)]) == 3
    assert (
        "- workflows/prd/step_setup_env.py: importação proibida de workflows.dev em prd"
        in capsys.readouterr().out
    )


def test_validate_unknown_project(projects_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Seção 9.4.5: projeto inexistente gera código 3."""
    assert main(["validate", "nada"]) == ExitCode.CONFIGURATION_ERROR
    assert "Projeto não encontrado: 'nada'" in capsys.readouterr().err


# --- python -m rpa_3003 (RF-051) -------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "code", "stdout"),
    [
        (["--version"], 0, f"rpa3003 {__version__}\n"),
        (["list", "--projects-dir", "."], 0, "Nenhum projeto encontrado em "),
        (["comando-inexistente"], 2, ""),
    ],
)
def test_python_dash_m_is_equivalent(
    tmp_path: Path, args: list[str], code: int, stdout: str
) -> None:
    """RF-051: python -m rpa_3003 equivale a rpa3003."""
    completed = subprocess.run(  # noqa: S603 - interpretador atual, sem shell
        [sys.executable, "-m", "rpa_3003", *args],
        capture_output=True,
        cwd=tmp_path,
        timeout=60,
        check=False,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert completed.returncode == code
    assert completed.stdout.decode("utf-8").replace("\r\n", "\n").startswith(stdout)


def test_dunder_main_calls_cli(
    projects_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """RF-051: `python -m rpa_3003` executa cli.main e encerra com o código retornado."""
    monkeypatch.setattr(sys, "argv", ["rpa3003", "list", "--projects-dir", str(projects_dir)])
    with pytest.raises(SystemExit) as info:
        runpy.run_module("rpa_3003", run_name="__main__")
    assert info.value.code == 0
    assert "Nenhum projeto encontrado" in capsys.readouterr().out
