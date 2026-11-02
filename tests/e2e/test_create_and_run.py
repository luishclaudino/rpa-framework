"""Teste ponta a ponta: `rpa3003 create` → `validate` → `run` em subprocesso real (seção 12.7).

Cada comando roda como o usuário o digita (console script `rpa3003`, ou `python -m rpa_3003` se o
script não existir), em processo próprio e num workspace com espaço e acentos no caminho
(`<tmp>/Automação RPA/rpa-framework`, RNF-002). Nenhum teste usa Vault, navegador ou rede.

As execuções `dev` e `prd` são feitas uma vez por módulo (fixtures `dev` e `prd`) e verificadas
por vários testes. O ambiente dos subprocessos não tem variáveis `RPA3003_*` nem `VAULT_*`.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import pytest
from filelock import FileLock

from rpa_3003.cli import build_child_env, build_run_command
from rpa_3003.core.config import Environment, ProjectPaths

pytestmark = pytest.mark.e2e

# Seção 10.4: expressão regular de conformidade do formato obrigatório.
LOG_LINE_REGEX: Final[re.Pattern[str]] = re.compile(
    r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] "
    r"\[(TRACE|DEBUG|INFO|SUCCESS|WARNING|ERROR|CRITICAL)\] "
    r"\[[^\]]+\] \[\d+\] \[[^\]]+\] .*$"
)
# Seção 10.4: linhas que não começam com "[" seguido de dígito pertencem à entrada anterior.
ENTRY_START: Final[re.Pattern[str]] = re.compile(r"^\[\d")
TIMESTAMP: Final[re.Pattern[str]] = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")
RUN_ID: Final[re.Pattern[str]] = re.compile(r"\brun_id=([0-9a-f]{12})\b")
# Seção 10.1: no console, cores ANSI são permitidas; são removidas antes da verificação.
ANSI_ESCAPE: Final[re.Pattern[str]] = re.compile(r"\x1b\[[0-9;]*m")
ISOLATED_ENV_PREFIXES: Final[tuple[str, ...]] = ("RPA3003_", "VAULT_")
PRESERVED_ENV_PREFIX: Final[str] = "RPA3003_TEST_"
COMMAND_TIMEOUT: Final[float] = 90.0
DEV_PROJECT: Final[str] = "conciliacao_bancaria"
PRD_PROJECT: Final[str] = "emissao_notas"
PRD_USER: Final[str] = "marcos.op"
CSV_CONTENT: Final[str] = (
    "id;nome;valor\r\n1;José;10,50\r\n2;Conceição;1.500,00\r\n3;Açaí Ltda;0,99\r\n"
)
# Limites do RNF-004, OBJ-02 e OBJ-10, em segundos.
IMPORT_LIMIT: Final[float] = 1.0
HELP_LIMIT: Final[float] = 1.5
CREATE_LIMIT: Final[float] = 5.0
BOOTSTRAP_LIMIT: Final[float] = 2.0


@dataclass(frozen=True, slots=True)
class Execution:
    """Resultado de `create` → `validate` → `run` de um projeto."""

    paths: ProjectPaths
    create: subprocess.CompletedProcess[str]
    create_seconds: float
    validate: subprocess.CompletedProcess[str]
    run: subprocess.CompletedProcess[str]
    log_lines: list[str]
    status: dict[str, Any]

    @property
    def run_id(self) -> str:
        """`run_id` extraído do nome do único arquivo `status_<run_id>.json`."""
        [status_file] = (self.paths.data_dir / "output").glob("status_*.json")
        return status_file.stem.removeprefix("status_")

    @property
    def output_files(self) -> list[str]:
        """Nomes dos arquivos em `data/output/`, em ordem alfabética."""
        return sorted(path.name for path in (self.paths.data_dir / "output").iterdir())


def _clean_env() -> dict[str, str]:
    """Ambiente do usuário sem configuração do framework nem do Vault, com UTF-8 (P-21)."""
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith(ISOLATED_ENV_PREFIXES)
        or name.upper().startswith(PRESERVED_ENV_PREFIX)
    }
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _cli_command() -> list[str]:
    """Console script `rpa3003` do ambiente atual; sem ele, `python -m rpa_3003` (seção 9.1)."""
    script = shutil.which("rpa3003", path=str(Path(sys.executable).parent))
    return [script] if script else [sys.executable, "-m", "rpa_3003"]


def _cli(workspace: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Executa `rpa3003 <args>` com o workspace como diretório atual."""
    return subprocess.run(  # noqa: S603 - comando do próprio framework, sem shell
        [*_cli_command(), *args],
        cwd=workspace,
        env=_clean_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=COMMAND_TIMEOUT,
        check=False,
    )


def _timed(command: Sequence[str], cwd: Path) -> float:
    """Tempo de parede, em segundos, de um comando que deve terminar com código 0."""
    started = time.perf_counter()
    completed = subprocess.run(  # noqa: S603 - comando do próprio framework, sem shell
        list(command),
        cwd=cwd,
        env=_clean_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=COMMAND_TIMEOUT,
        check=False,
    )
    elapsed = time.perf_counter() - started
    assert completed.returncode == 0, completed.stderr
    return elapsed


def _console(output: str) -> list[str]:
    """Linhas do console (stderr) sem as sequências de cor ANSI."""
    return ANSI_ESCAPE.sub("", output).splitlines()


def _read_log(paths: ProjectPaths) -> list[str]:
    """Linhas de todos os arquivos de log do projeto, em ordem de nome (data)."""
    lines: list[str] = []
    for log_file in sorted(paths.logs_dir.glob("*.log")):
        lines.extend(log_file.read_text(encoding="utf-8").splitlines())
    return lines


def _read_json(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _entries(lines: Sequence[str]) -> list[str]:
    """Linhas que iniciam uma entrada de log (excluídas as de traceback, seção 10.4)."""
    return [line for line in lines if ENTRY_START.match(line)]


def _continuations(lines: Sequence[str]) -> list[tuple[str, str]]:
    """Pares (entrada anterior, linha) para cada linha que não inicia uma entrada."""
    pairs: list[tuple[str, str]] = []
    previous = ""
    for line in lines:
        if ENTRY_START.match(line):
            previous = line
        else:
            pairs.append((previous, line))
    return pairs


def _single(lines: Sequence[str], fragment: str) -> str:
    """A única linha que contém `fragment`."""
    matches = [line for line in lines if fragment in line]
    assert len(matches) == 1, (fragment, matches)
    return matches[0]


def _index(lines: Sequence[str], fragment: str) -> int:
    """Índice da primeira linha que contém `fragment`."""
    return next(i for i, line in enumerate(lines) if fragment in line)


def _log_epoch(line: str) -> float:
    """Instante (hora local da máquina) do campo de data e hora de uma linha de log."""
    match = TIMESTAMP.match(line)
    assert match is not None, line
    return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").timestamp()


def _execute(workspace: Path, name: str, *run_args: str, csv: bool) -> Execution:
    """`create` → `validate` → (cópia de CSV para `data/input`) → `run` de um projeto."""
    started = time.perf_counter()
    create = _cli(workspace, "create", name)
    create_seconds = time.perf_counter() - started
    assert create.returncode == 0, create.stderr
    paths = ProjectPaths.from_root(workspace / "projects" / name)
    validate = _cli(workspace, "validate", name)
    if csv:
        (paths.data_dir / "input" / "extrato.csv").write_bytes(CSV_CONTENT.encode("utf-8-sig"))
    run = _cli(workspace, "run", name, *run_args)
    [status_file] = (paths.data_dir / "output").glob("status_*.json")
    return Execution(
        paths=paths,
        create=create,
        create_seconds=create_seconds,
        validate=validate,
        run=run,
        log_lines=_read_log(paths),
        status=_read_json(status_file),
    )


@pytest.fixture(scope="module")
def workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Workspace vazio com espaço e acentos no caminho (RNF-002); `projects/` não existe."""
    path = tmp_path_factory.mktemp("e2e") / "Automação RPA" / "rpa-framework"
    path.mkdir(parents=True)
    return path


@pytest.fixture(scope="module")
def dev(workspace: Path) -> Execution:
    """UC-01: projeto criado, validado e executado em `dev` com um CSV em `data/input`."""
    return _execute(workspace, DEV_PROJECT, "--env", "dev", csv=True)


@pytest.fixture(scope="module")
def prd(workspace: Path) -> Execution:
    """Projeto criado, validado e executado em `prd` sem nenhuma variável do Vault."""
    return _execute(workspace, PRD_PROJECT, "--env", "prd", "--user", PRD_USER, csv=False)


def test_create_validate_and_run_dev(dev: Execution, workspace: Path) -> None:
    """OBJ-01/UC-01/RF-048/RF-049/RF-053: create → validate → run --env dev retorna 0 e grava
    relatorio_<run_id>.json e status_<run_id>.json; o projeto aparece como OK em `list`."""
    root = dev.paths.root
    assert root == (workspace / "projects" / DEV_PROJECT).resolve()
    assert f"Projeto '{DEV_PROJECT}' criado em {root}" in dev.create.stdout
    assert "Template: example 1.0.0" in dev.create.stdout
    assert f"Execute: rpa3003 run {DEV_PROJECT} --env dev" in dev.create.stdout

    assert dev.validate.returncode == 0, dev.validate.stdout
    assert dev.validate.stdout.strip() == f"Projeto '{DEV_PROJECT}' válido"

    assert dev.run.returncode == 0, dev.run.stderr
    run_id = dev.run_id
    assert dev.output_files == [".gitkeep", f"relatorio_{run_id}.json", f"status_{run_id}.json"]
    report = _read_json(dev.paths.data_dir / "output" / f"relatorio_{run_id}.json")
    assert report == {
        "run_id": run_id,
        "ambiente": "dev",
        "arquivos": 1,
        "linhas": 3,
        "detalhes": [{"arquivo": "extrato.csv", "linhas": 3}],
    }
    assert dev.status["sucesso"] is True
    assert dev.status["erro"] is None
    assert (dev.status["projeto"], dev.status["ambiente"]) == (DEV_PROJECT, "dev")
    assert dev.status["relatorio"] == report

    started = datetime.fromisoformat(dev.status["inicio"])
    log_file = dev.paths.logs_dir / f"{DEV_PROJECT}_{started:%Y-%m-%d}.log"
    assert log_file.is_file()

    listing = _cli(workspace, "list")
    assert listing.returncode == 0, listing.stderr
    [row] = [line for line in listing.stdout.splitlines() if line.startswith(DEV_PROJECT)]
    assert row.split()[1] == "OK"


def test_create_finishes_within_five_seconds(dev: Execution) -> None:
    """OBJ-02/RNF-004: `rpa3003 create` em processo próprio leva no máximo 5 s."""
    assert dev.create_seconds <= CREATE_LIMIT


def test_all_log_lines_match_mandatory_format(dev: Execution, prd: Execution) -> None:
    """OBJ-04/RF-015: 100% das linhas de log (arquivo e console de create, validate e run)
    casam com a regex da seção 10.4; linhas de traceback só aparecem após entrada ERROR."""
    for execution in (dev, prd):
        console = _console(execution.run.stderr)
        outputs = [
            execution.log_lines,
            _console(execution.create.stderr),
            _console(execution.validate.stderr),
            console,
        ]
        entries = [line for lines in outputs for line in _entries(lines)]
        assert len(_entries(execution.log_lines)) >= 8
        assert len(_entries(console)) > len(_entries(execution.log_lines))  # CLI + projeto
        assert [line for line in entries if not LOG_LINE_REGEX.match(line)] == []
        for lines in outputs:
            assert all("] [ERROR] [" in entry for entry, _ in _continuations(lines))

    assert _continuations(dev.log_lines) == []
    assert _continuations(_console(dev.run.stderr)) == []
    for lines in (prd.log_lines, _console(prd.run.stderr)):
        tracebacks = [line for _, line in _continuations(lines)]
        assert "Traceback (most recent call last):" in tracebacks


def test_log_user_comes_from_run_option(prd: Execution) -> None:
    """RF-016/UC-04: com `--user marcos.op`, todas as entradas do log do projeto exibem
    `[marcos.op]`."""
    entries = _entries(prd.log_lines)
    assert entries
    assert all(f"] [{PRD_USER}] " in entry for entry in entries)


def test_same_run_id_in_start_end_and_status_file(dev: Execution, prd: Execution) -> None:
    """RNF-015: o mesmo run_id (12 hexadecimais) na linha de início, na linha de término e no
    nome do arquivo de status; cada step registra início, fim e duração."""
    for execution, env in ((dev, "dev"), (prd, "prd")):
        start = _single(execution.log_lines, "Iniciando execução |")
        end = _single(execution.log_lines, "Execução finalizada |")
        start_match = RUN_ID.search(start)
        end_match = RUN_ID.search(end)
        assert start_match is not None
        assert end_match is not None
        assert start_match.group(1) == end_match.group(1) == execution.run_id
        assert execution.status["run_id"] == execution.run_id
        assert f"| projeto={execution.paths.name} | ambiente={env} |" in start
        assert "| rpa_3003=1.0.0" in start

    for step in ("setup_env", "execute_task", "wrapup_execution"):
        _single(dev.log_lines, f">>> Iniciando step '{step}'")
        assert re.search(
            rf"<<< Step '{step}' concluído em \d+\.\d{{2}}s$",
            _single(dev.log_lines, f"<<< Step '{step}'"),
        )
    assert re.search(
        r"\[SUCCESS\] .* Execução finalizada \| status=SUCCESS \| exit_code=0 \| "
        r"duracao=\d+\.\d{2}s \| run_id=",
        _single(dev.log_lines, "Execução finalizada |"),
    )


def test_run_prd_without_vault_returns_3_and_runs_wrapup(prd: Execution) -> None:
    """Seção 11.12/RF-006/RNF-006/UC-02 A1: run --env prd sem Vault retorna 3; o step de
    execução é ignorado, o wrapup executa, o status registra a falha e o lock é liberado."""
    assert prd.validate.returncode == 0
    assert prd.run.returncode == 3, prd.run.stderr

    lines = prd.log_lines
    order = [
        ">>> Iniciando step 'setup_env'",
        "Step 'setup_env' falhou após",
        "Step 'execute_task' ignorado devido a falha anterior",
        ">>> Iniciando step 'wrapup_execution'",
        "<<< Step 'wrapup_execution' concluído",
        "Execução finalizada | status=FAILED | exit_code=3",
    ]
    positions = [_index(lines, fragment) for fragment in order]
    assert positions == sorted(positions)
    assert "ConfigurationError: Parâmetros obrigatórios do Vault ausentes" in (
        _single(lines, "Step 'setup_env' falhou após")
    )
    assert not any(">>> Iniciando step 'execute_task'" in line for line in lines)

    run_id = prd.run_id
    assert prd.output_files == [".gitkeep", f"status_{run_id}.json"]
    assert prd.status["sucesso"] is False
    assert prd.status["ambiente"] == "prd"
    assert prd.status["usuario"] == PRD_USER
    assert prd.status["erro"].startswith(
        "ConfigurationError: Parâmetros obrigatórios do Vault ausentes"
    )
    assert prd.status["relatorio"] is None

    lock = FileLock(prd.paths.lock_file, timeout=0)
    lock.acquire()
    lock.release()


def test_bootstrap_overhead_within_two_seconds(workspace: Path) -> None:
    """OBJ-10/RNF-004: do início de `main.py` até `>>> Iniciando step` leva no máximo 2 s, sem
    Vault. A chegada da linha no console (stderr) dá a medida precisa; a linha do arquivo de log
    confirma o instante com a resolução de 1 s do formato obrigatório."""
    name = "medicao_bootstrap"
    assert _cli(workspace, "create", name).returncode == 0
    paths = ProjectPaths.from_root(workspace / "projects" / name)
    command = build_run_command(paths, Environment.DEV)
    started = time.time()
    process = subprocess.Popen(  # noqa: S603 - mesmo comando montado pelo `rpa3003 run`
        command,
        cwd=paths.root,
        env=build_child_env(Environment.DEV),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    arrived: float | None = None
    assert process.stderr is not None
    with process.stderr:
        for line in process.stderr:
            if arrived is None and ">>> Iniciando step" in line:
                arrived = time.time()
    assert process.wait(timeout=COMMAND_TIMEOUT) == 0
    assert arrived is not None
    assert arrived - started <= BOOTSTRAP_LIMIT

    first_step = next(line for line in _read_log(paths) if ">>> Iniciando step" in line)
    assert math.floor(started) <= _log_epoch(first_step) <= arrived


def test_startup_times_within_rnf_004(workspace: Path) -> None:
    """RNF-004: `python -c "import rpa_3003"` em até 1,0 s e `rpa3003 --help` em até 1,5 s,
    medidos como processos novos (melhor de três execuções, para descontar oscilações)."""
    import_seconds = min(
        _timed([sys.executable, "-c", "import rpa_3003"], workspace) for _ in range(3)
    )
    help_seconds = min(_timed([*_cli_command(), "--help"], workspace) for _ in range(3))
    assert import_seconds <= IMPORT_LIMIT
    assert help_seconds <= HELP_LIMIT
