"""Interface de linha de comando `rpa3003`.

Interpreta argumentos, despacha os subcomandos create, run, list, templates e validate e
executa projetos em subprocesso com timeout e propagação do código de saída (seção 9).
"""

from __future__ import annotations

import argparse
import contextlib
import os
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, Final, NoReturn, TextIO

from rpa_3003._version import __version__
from rpa_3003.core.config import (
    Environment,
    ProjectPaths,
    find_project,
    get_settings,
    list_projects,
    load_settings,
    resolve_environment,
    resolve_projects_dir,
    validate_project_structure,
)
from rpa_3003.core.exceptions import ExitCode, ProjectStructureError, RPAError, WorkflowError
from rpa_3003.core.logger import get_logger, setup_logger
from rpa_3003.core.workflow import split_passthrough_args
from rpa_3003.templates import (
    DEFAULT_TEMPLATE,
    create_project,
    get_template,
    list_templates,
    validate_project_name,
)

__all__: list[str] = [
    "CHILD_WAIT_ON_INTERRUPT",
    "LOG_LEVELS",
    "PROG",
    "TERMINATE_GRACE_SECONDS",
    "build_child_env",
    "build_parser",
    "build_run_command",
    "cmd_create",
    "cmd_list",
    "cmd_run",
    "cmd_templates",
    "cmd_validate",
    "kill_process_tree",
    "main",
    "run_child",
]

PROG: Final[str] = "rpa3003"
CHILD_WAIT_ON_INTERRUPT: Final[float] = 10.0
TERMINATE_GRACE_SECONDS: Final[float] = 5.0
LOG_LEVELS: Final[tuple[str, ...]] = (
    "TRACE",
    "DEBUG",
    "INFO",
    "SUCCESS",
    "WARNING",
    "ERROR",
    "CRITICAL",
)

_PASSTHROUGH_SEPARATOR: Final[str] = "--"
_PROJECT_ENV_FILE: Final[str] = ".env"
_NO_TIMEOUT: Final[str] = "sem limite"
_COLUMN_GAP: Final[int] = 2
_STRUCTURE_OK: Final[str] = "OK"
_STRUCTURE_INVALID: Final[str] = "INVÁLIDA"
_USAGE_PREFIX: Final[str] = "uso: "


class _HelpFormatter(argparse.HelpFormatter):
    """Formatador da ajuda com o prefixo de uso em pt-BR."""

    def add_usage(
        self,
        usage: str | None,
        actions: Iterable[argparse.Action],
        groups: Iterable[argparse._MutuallyExclusiveGroup],
        prefix: str | None = None,
    ) -> None:
        """Usa `uso: ` como prefixo padrão da linha de uso."""
        super().add_usage(usage, actions, groups, _USAGE_PREFIX if prefix is None else prefix)


class _ArgumentParser(argparse.ArgumentParser):
    """`ArgumentParser` com títulos, ajuda e mensagens de erro em pt-BR.

    Os subcomandos herdam a classe (`add_subparsers` usa o tipo do parser principal). Os textos
    internos do argparse (ex.: "invalid choice") continuam em inglês.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        """Cria o parser com `-h/--help` em pt-BR e o formatador `_HelpFormatter`."""
        kwargs.setdefault("formatter_class", _HelpFormatter)
        kwargs["add_help"] = False
        super().__init__(*args, **kwargs)
        self._positionals.title = "argumentos"
        self._optionals.title = "opções"
        self.add_argument("-h", "--help", action="help", help="Exibe esta ajuda e encerra")

    def error(self, message: str) -> NoReturn:
        """Imprime o uso e o erro em `stderr` e encerra com o código 2 (erro de uso)."""
        self.print_usage(sys.stderr)
        self.exit(int(ExitCode.USAGE_ERROR), f"{self.prog}: erro: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    """Constrói o parser com subcomandos create, run, list, templates e validate.

    Returns:
        Parser cujos subcomandos definem `handler` com a função `cmd_<nome>` (seção 9.7).
    """
    parser = _ArgumentParser(prog=PROG, description="Framework RPA rpa_3003")
    parser.add_argument(
        "--version",
        action="version",
        version=f"{PROG} {__version__}",
        help="Exibe a versão e encerra",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Console do CLI em DEBUG")
    sub = parser.add_subparsers(dest="command", metavar="<comando>", title="comandos")

    p_create = sub.add_parser(
        "create",
        help="Cria projeto a partir de template",
        description="Cria um projeto RPA a partir de um template.",
    )
    p_create.add_argument(
        "name", metavar="nome", help="Nome do projeto (minúsculas, dígitos e '_', 3 a 50)"
    )
    p_create.add_argument(
        "--template",
        default=DEFAULT_TEMPLATE,
        metavar="NOME",
        help="Template a usar (padrão: example; veja rpa3003 templates)",
    )
    _add_projects_dir(p_create)
    p_create.add_argument(
        "--author",
        default=None,
        metavar="NOME",
        help="Autor gravado no projeto (padrão: usuário do Windows)",
    )
    p_create.add_argument(
        "--force",
        action="store_true",
        help="Se o projeto existir, renomeia para <nome>.bak-<AAAAMMDDHHMMSS> e recria",
    )
    p_create.set_defaults(handler=cmd_create)

    p_run = sub.add_parser(
        "run",
        help="Executa projeto",
        description="Valida e executa um projeto em subprocesso.",
        epilog="Argumentos após '--' são repassados ao projeto (ctx.extra_args).",
    )
    p_run.add_argument("name", metavar="nome", help="Projeto na pasta de projetos")
    p_run.add_argument(
        "--env",
        default=None,
        metavar="{dev,prd}",
        help="Ambiente (padrão: RPA3003_ENV, .env do projeto ou dev)",
    )
    p_run.add_argument(
        "--user",
        default=None,
        metavar="USUARIO",
        help="Usuário do log (padrão: RPA3003_LOG_USER ou usuário do Windows)",
    )
    p_run.add_argument(
        "--log-level",
        default=None,
        choices=LOG_LEVELS,
        metavar="NIVEL",
        help=f"Nível de log do projeto: {', '.join(LOG_LEVELS)} (padrão: RPA3003_LOG_LEVEL "
        "ou INFO)",
    )
    p_run.add_argument(
        "--timeout",
        type=int,
        default=None,
        metavar="SEGUNDOS",
        help="Tempo máximo de execução (padrão: RPA3003_RUN_TIMEOUT ou sem limite)",
    )
    _add_projects_dir(p_run)
    p_run.set_defaults(handler=cmd_run)

    p_list = sub.add_parser(
        "list", help="Lista projetos", description="Lista os projetos e a situação da estrutura."
    )
    _add_projects_dir(p_list)
    p_list.set_defaults(handler=cmd_list)

    p_templates = sub.add_parser(
        "templates", help="Lista templates", description="Lista os templates disponíveis."
    )
    p_templates.set_defaults(handler=cmd_templates)

    p_validate = sub.add_parser(
        "validate",
        help="Valida estrutura de projeto",
        description="Valida a estrutura de um projeto (RF-003).",
    )
    p_validate.add_argument("name", metavar="nome", help="Projeto na pasta de projetos")
    _add_projects_dir(p_validate)
    p_validate.set_defaults(handler=cmd_validate)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Ponto de entrada do console script. Retorna o código de saída; nunca chama sys.exit.

    Erros de uso detectados pelo argparse, `--help` e `--version` encerram com `SystemExit`
    (comportamento nativo do argparse, seção 9.7).

    Args:
        argv: Argumentos sem o nome do programa; `None` usa `sys.argv[1:]`. O que vier depois
            do primeiro `--` fica em `args.passthrough`.

    Returns:
        Código de saída da seção 9.5.
    """
    _reconfigure_std_streams()
    own_args, passthrough = split_passthrough_args(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(own_args)
    args.passthrough = passthrough
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help(sys.stderr)
        return int(ExitCode.USAGE_ERROR)
    setup_logger(logs_dir=None, level="DEBUG" if args.verbose else "INFO")
    try:
        return int(handler(args))
    except RPAError as exc:
        get_logger().error("{}", exc)
        return int(exc.exit_code)
    except KeyboardInterrupt:
        get_logger().warning("Comando interrompido pelo usuário")
        return int(ExitCode.INTERRUPTED)


def cmd_create(args: argparse.Namespace) -> int:
    """Cria um projeto a partir de um template e valida a estrutura (RF-048, seção 9.4.1).

    Args:
        args: Argumentos `name`, `template`, `projects_dir`, `author` e `force`.

    Returns:
        `0` após imprimir o resumo e os próximos passos em `stdout`.

    Raises:
        InvalidProjectNameError: Se o nome for inválido (código 2).
        TemplateNotFoundError: Se o template não existir (código 3).
        ProjectAlreadyExistsError: Se o projeto existir sem `--force` (código 3).
        TemplateRenderError: Se a renderização falhar (código 3).
        ProjectStructureError: Se o projeto criado não passar em `validate` (código 3).
    """
    name = validate_project_name(args.name)
    projects_dir = resolve_projects_dir(args.projects_dir, get_settings())
    template = get_template(args.template)
    root = create_project(
        name, projects_dir, template=template.name, author=args.author, force=args.force
    )
    _ensure_valid_structure(name, root)
    run_hint = f"{PROG} run {name} --env dev"
    if args.projects_dir is not None:
        run_hint += f' --projects-dir "{projects_dir}"'
    print(f"Projeto '{name}' criado em {root}")
    print(f"Template: {template.name} {template.version}")
    print("Próximos passos:")
    print("  1. Revise global_vars.py (ENV_CONFIG)")
    print("  2. Copie .env.example para .env e ajuste as variáveis")
    print(f"  3. Execute: {run_hint}")
    return int(ExitCode.SUCCESS)


def cmd_run(args: argparse.Namespace) -> int:
    """Valida o projeto e o executa em subprocesso (RF-049, RF-052, seção 9.4.2).

    Sem `--env`, o ambiente é resolvido como no processo filho (RF-010): `RPA3003_ENV`, depois
    o `.env` do projeto, depois `dev`. O valor resolvido é repassado ao filho.

    Args:
        args: Argumentos `name`, `env`, `user`, `log_level`, `timeout`, `projects_dir` e
            `passthrough`.

    Returns:
        Código de saída do projeto; `5` em tempo limite, `130` em interrupção e `2` para
        `--timeout` menor ou igual a zero.

    Raises:
        InvalidEnvironmentError: Se `--env` for inválido (código 2).
        ProjectNotFoundError: Se o projeto não existir; lista os disponíveis (código 3).
        ProjectStructureError: Se a estrutura for inválida; os problemas são impressos
            (código 3).
        ConfigurationError: Se a configuração do ambiente ou do projeto for inválida.
    """
    if args.timeout is not None and args.timeout <= 0:
        get_logger().error(
            "Tempo limite inválido: {} (informe um inteiro maior que zero)", args.timeout
        )
        return int(ExitCode.USAGE_ERROR)
    settings = get_settings()
    requested_env = None if args.env is None else Environment.from_value(args.env)
    paths = find_project(args.name, resolve_projects_dir(args.projects_dir, settings))
    _ensure_valid_structure(paths.name, paths.root)
    env = _project_environment(paths) if requested_env is None else requested_env
    timeout = settings.run_timeout if args.timeout is None else args.timeout
    command = build_run_command(
        paths,
        env,
        user=args.user,
        log_level=args.log_level,
        extra_args=getattr(args, "passthrough", ()),
    )
    get_logger().info(
        "Executando projeto | nome={} | ambiente={} | timeout={}",
        paths.name,
        env,
        _describe_timeout(timeout),
    )
    return run_child(command, cwd=paths.root, env=build_child_env(env), timeout=timeout)


def cmd_list(args: argparse.Namespace) -> int:
    """Lista os projetos com a situação da estrutura (RF-050, seção 9.4.3).

    Args:
        args: Argumento `projects_dir`.

    Returns:
        `0`, inclusive quando não há projetos.

    Raises:
        ConfigurationError: Se a pasta de projetos não existir (código 3).
    """
    projects_dir = resolve_projects_dir(args.projects_dir, get_settings())
    projects = list_projects(projects_dir)
    if not projects:
        print(f"Nenhum projeto encontrado em {projects_dir}")
        return int(ExitCode.SUCCESS)
    rows = [
        (
            paths.name,
            _STRUCTURE_INVALID if validate_project_structure(paths.root) else _STRUCTURE_OK,
            str(paths.root),
        )
        for paths in projects
    ]
    _print_table(("PROJETO", "ESTRUTURA", "CAMINHO"), rows)
    return int(ExitCode.SUCCESS)


def cmd_templates(args: argparse.Namespace) -> int:
    """Lista os templates disponíveis com versão e descrição (RF-050, seção 9.4.4).

    Args:
        args: Argumentos do subcomando (não usados).

    Returns:
        `0`.

    Raises:
        TemplateRenderError: Se o `template.toml` de algum template for inválido (código 3).
    """
    templates = list_templates()
    if not templates:
        print("Nenhum template disponível")
        return int(ExitCode.SUCCESS)
    rows = [(info.name, info.version, info.description) for info in templates]
    _print_table(("TEMPLATE", "VERSÃO", "DESCRIÇÃO"), rows)
    return int(ExitCode.SUCCESS)


def cmd_validate(args: argparse.Namespace) -> int:
    """Valida a estrutura de um projeto (RF-050, seção 9.4.5).

    Args:
        args: Argumentos `name` e `projects_dir`.

    Returns:
        `0` para projeto válido; `3` para projeto inválido, com um problema por linha.

    Raises:
        ProjectNotFoundError: Se o projeto não existir (código 3).
    """
    paths = find_project(args.name, resolve_projects_dir(args.projects_dir, get_settings()))
    problems = validate_project_structure(paths.root)
    if problems:
        _print_problems(paths.name, problems, sys.stdout)
        return int(ExitCode.CONFIGURATION_ERROR)
    print(f"Projeto '{paths.name}' válido")
    return int(ExitCode.SUCCESS)


def build_run_command(
    paths: ProjectPaths,
    env: Environment,
    *,
    user: str | None = None,
    log_level: str | None = None,
    extra_args: Sequence[str] = (),
) -> list[str]:
    """Retorna [sys.executable, str(paths.main_file), "--env", env.value, ...].

    Args:
        paths: Caminhos do projeto.
        env: Ambiente repassado ao `main.py`.
        user: Valor de `--user`, incluído somente quando informado.
        log_level: Valor de `--log-level`, incluído somente quando informado.
        extra_args: Argumentos do projeto, incluídos após `--` somente quando houver.

    Returns:
        Comando para `subprocess.Popen`.
    """
    command = [sys.executable, str(paths.main_file), "--env", Environment.from_value(env).value]
    if user is not None:
        command += ["--user", user]
    if log_level is not None:
        command += ["--log-level", log_level]
    extras = list(extra_args)
    if extras:
        command += [_PASSTHROUGH_SEPARATOR, *extras]
    return command


def build_child_env(env: Environment) -> dict[str, str]:
    """Cópia de os.environ com RPA3003_ENV, PYTHONUTF8=1 e PYTHONIOENCODING=utf-8.

    Args:
        env: Ambiente gravado em `RPA3003_ENV`.

    Returns:
        Variáveis de ambiente do processo filho.
    """
    child_env = dict(os.environ)
    child_env["RPA3003_ENV"] = Environment.from_value(env).value
    child_env["PYTHONUTF8"] = "1"
    child_env["PYTHONIOENCODING"] = "utf-8"
    return child_env


def run_child(
    command: Sequence[str], *, cwd: Path, env: dict[str, str], timeout: float | None
) -> int:
    """Executa com subprocess.Popen, aplica timeout e trata Ctrl+C. Retorna o código do filho,
    5 em timeout ou 130 em interrupção.

    O filho herda `stdout` e `stderr`. No tempo limite, a árvore de processos é encerrada com
    `kill_process_tree`. No Ctrl+C, o filho (que também recebe o sinal e executa o wrapup) é
    aguardado por até `CHILD_WAIT_ON_INTERRUPT` segundos; se continuar ativo, a árvore é
    encerrada.

    Args:
        command: Comando e argumentos (sem shell).
        cwd: Diretório de trabalho do filho.
        env: Variáveis de ambiente do filho.
        timeout: Tempo máximo em segundos; `None` aguarda sem limite.

    Returns:
        Código de saída do filho, `5` (tempo limite) ou `130` (interrupção).

    Raises:
        WorkflowError: Se o processo não puder ser iniciado.
    """
    try:
        process = subprocess.Popen(list(command), cwd=cwd, env=env)
    except OSError as exc:
        raise WorkflowError(
            "Falha ao iniciar o processo do projeto",
            details={"comando": command[0], "erro": f"{type(exc).__name__}: {exc}"},
        ) from exc
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        get_logger().error(
            "Tempo limite excedido | timeout={} | pid={}", _describe_timeout(timeout), process.pid
        )
        kill_process_tree(process.pid)
        _reap(process)
        return int(ExitCode.TIMEOUT)
    except KeyboardInterrupt:
        get_logger().warning(
            "Interrupção recebida; aguardando o projeto | pid={} | espera={}s",
            process.pid,
            CHILD_WAIT_ON_INTERRUPT,
        )
        with contextlib.suppress(subprocess.TimeoutExpired, KeyboardInterrupt):
            process.wait(timeout=CHILD_WAIT_ON_INTERRUPT)
        if process.poll() is None:
            kill_process_tree(process.pid)
            _reap(process)
        return int(ExitCode.INTERRUPTED)


def kill_process_tree(pid: int, *, grace_seconds: float = TERMINATE_GRACE_SECONDS) -> None:
    """Termina filhos (recursivo) e o processo; após grace_seconds, mata os remanescentes.

    Processos que já terminaram são ignorados; falta de permissão gera `WARNING`.

    Args:
        pid: Processo raiz da árvore.
        grace_seconds: Espera após `terminate()` antes do `kill()`, e após o `kill()`.
    """
    import psutil

    try:
        root = psutil.Process(pid)
    except psutil.NoSuchProcess:
        get_logger().debug("Processo já encerrado | pid={}", pid)
        return
    try:
        children = root.children(recursive=True)
    except psutil.Error:
        children = []
    processes = [*children, root]
    get_logger().debug(
        "Encerrando árvore de processos | pid={} | processos={}", pid, len(processes)
    )
    _signal_processes(processes, kill=False)
    _gone, alive = psutil.wait_procs(processes, timeout=grace_seconds)
    if not alive:
        return
    _signal_processes(alive, kill=True)
    _gone, remaining = psutil.wait_procs(alive, timeout=grace_seconds)
    for process in remaining:
        get_logger().warning("Processo continua ativo após kill | pid={}", process.pid)


def _add_projects_dir(parser: argparse.ArgumentParser) -> None:
    """Adiciona a opção `--projects-dir` comum a create, run, list e validate."""
    parser.add_argument(
        "--projects-dir",
        default=None,
        metavar="CAMINHO",
        help="Pasta de projetos (padrão: RPA3003_PROJECTS_DIR ou ./projects)",
    )


def _signal_processes(processes: Sequence[Any], *, kill: bool) -> None:
    """Chama `terminate()` ou `kill()` em cada processo `psutil`, tolerando os já encerrados."""
    import psutil

    for process in processes:
        try:
            if kill:
                process.kill()
            else:
                process.terminate()
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as exc:
            get_logger().warning(
                "Processo não encerrado | pid={} | erro={}", process.pid, type(exc).__name__
            )


def _reap(process: subprocess.Popen[bytes]) -> None:
    """Coleta o código de saída do filho já encerrado, sem bloquear indefinidamente."""
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=TERMINATE_GRACE_SECONDS)


def _project_environment(paths: ProjectPaths) -> Environment:
    """Ambiente pela precedência do filho: `RPA3003_ENV`, `.env` do projeto e `dev` (RF-010)."""
    return resolve_environment(None, load_settings(env_file=paths.root / _PROJECT_ENV_FILE))


def _ensure_valid_structure(name: str, root: Path) -> None:
    """Imprime os problemas de estrutura em `stderr` e lança `ProjectStructureError`."""
    problems = validate_project_structure(root)
    if not problems:
        return
    _print_problems(name, problems, sys.stderr)
    raise ProjectStructureError(
        f"Estrutura do projeto '{name}' inválida",
        details={"caminho": str(root), "problemas": len(problems)},
    )


def _print_problems(name: str, problems: Sequence[str], stream: TextIO) -> None:
    """Imprime `Projeto '<nome>' inválido:` e um problema por linha, precedido de `- `."""
    print(f"Projeto '{name}' inválido:", file=stream)
    for problem in problems:
        print(f"- {problem}", file=stream)


def _print_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    """Imprime uma tabela alinhada à esquerda; a última coluna não recebe preenchimento."""
    table = [headers, *rows]
    widths = [max(len(row[index]) for row in table) for index in range(len(headers) - 1)]
    for row in table:
        cells = [cell.ljust(width + _COLUMN_GAP) for cell, width in zip(row, widths, strict=False)]
        print("".join(cells) + row[-1])


def _describe_timeout(timeout: float | None) -> str:
    """Texto do tempo limite para os logs."""
    return _NO_TIMEOUT if timeout is None else f"{timeout}s"


def _reconfigure_std_streams() -> None:
    """Reconfigura `stdout` e `stderr` para UTF-8 com `errors="replace"`."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with contextlib.suppress(AttributeError, OSError, TypeError, ValueError):
                reconfigure(encoding="utf-8", errors="replace")
