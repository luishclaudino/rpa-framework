"""Testes de `rpa_3003.core.logger` (RF-014 a RF-019, RNF-005, RNF-011, seção 10)."""

from __future__ import annotations

import getpass
import inspect
import logging
import os
import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import time_machine
from loguru import logger as loguru_logger
from loguru._ctime_functions import set_ctime
from pytest_mock import MockerFixture

from rpa_3003.core.logger import (
    CONSOLE_FORMAT,
    LOG_FORMAT,
    MASK,
    UNKNOWN_USER,
    InterceptHandler,
    clear_secrets,
    current_log_file,
    default_user,
    get_logger,
    is_configured,
    register_secret,
    reset_logger,
    setup_logger,
)

pytestmark = pytest.mark.unit

ReadLines = Callable[[Path], list[str]]

# Seção 10.4.
LOG_LINE_REGEX: re.Pattern[str] = re.compile(
    r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] "
    r"\[(TRACE|DEBUG|INFO|SUCCESS|WARNING|ERROR|CRITICAL)\] "
    r"\[[^\]]+\] \[\d+\] \[[^\]]+\] .*$"
)
ANSI_ESCAPE: re.Pattern[str] = re.compile(r"\x1b\[[0-9;]*m")


def _local(*args: int) -> datetime:
    """Datetime com o fuso local da máquina (time-machine trata datetimes ingênuos como UTC)."""
    return datetime(*args).astimezone()


def _next_line() -> int:
    """Número da linha seguinte à chamada deste helper, no arquivo do chamador."""
    frame = inspect.currentframe()
    assert frame is not None and frame.f_back is not None
    return frame.f_back.f_lineno + 1


def _user_of(line: str) -> str:
    """Extrai o quinto campo ([user]) de uma linha no formato obrigatório."""
    match = re.match(r"^(?:\[[^\]]*\] ){4}\[([^\]]+)\] ", line)
    assert match is not None, line
    return match.group(1)


def _align_file_times(path: Path, moment: datetime) -> None:
    """Alinha criação e modificação do arquivo ao relógio simulado.

    A rotação do loguru usa a data de criação do arquivo, gravada pelo sistema de arquivos com o
    relógio real; o alinhamento torna o teste independente da data real da máquina.
    """
    timestamp = moment.timestamp()
    set_ctime(str(path), timestamp)
    os.utime(path, (timestamp, timestamp))


# --- RF-015: formato obrigatório ---------------------------------------------------------------


def test_format_constants_are_the_mandatory_ones() -> None:
    """RF-015: LOG_FORMAT e CONSOLE_FORMAT são exatamente os da seção 10.3."""
    assert LOG_FORMAT == (
        "[{time:YYYY-MM-DD HH:mm:ss}] [{level}] [{file.name}] [{line}] [{extra[user]}] {message}"
    )
    assert CONSOLE_FORMAT == (
        "<green>[{time:YYYY-MM-DD HH:mm:ss}]</green> <level>[{level}]</level> "
        "[{file.name}] [{line}] [{extra[user]}] <level>{message}</level>"
    )
    assert re.sub(r"</?(green|level)>", "", CONSOLE_FORMAT) == LOG_FORMAT


def test_log_line_has_exact_mandatory_format(tmp_path: Path, read_log_lines: ReadLines) -> None:
    """RF-015: info('Olá') às 08:00:00 de 31/10/2026 com usuário ana gera a linha exata."""
    with time_machine.travel(_local(2026, 10, 31, 8, 0, 0), tick=False):
        setup_logger(tmp_path, user="ana", console=False, file_prefix="teste")
        line = _next_line()
        get_logger().info("Olá")
        lines = read_log_lines(tmp_path)
    assert lines == [f"[2026-10-31 08:00:00] [INFO] [test_logger.py] [{line}] [ana] Olá"]
    assert (tmp_path / "teste_2026-10-31.log").is_file()


def test_every_level_matches_conformance_regex(tmp_path: Path, read_log_lines: ReadLines) -> None:
    """RF-015: linhas de todos os níveis casam com a regex da seção 10.4."""
    setup_logger(tmp_path, user="ana", level="TRACE", console=False)
    log = get_logger()
    for level in ("TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"):
        log.log(level, "Mensagem | nivel={}", level)
    lines = read_log_lines(tmp_path)
    assert len(lines) == 7
    assert all(LOG_LINE_REGEX.match(line) for line in lines)


def test_console_output_matches_file_format(
    tmp_path: Path, read_log_lines: ReadLines, capsys: pytest.CaptureFixture[str]
) -> None:
    """RF-015: o console sem cores produz exatamente o mesmo texto do arquivo."""
    setup_logger(tmp_path, user="ana", console=True)
    get_logger().warning("Aviso no console")
    file_lines = read_log_lines(tmp_path)
    console_lines = ANSI_ESCAPE.sub("", capsys.readouterr().err).splitlines()
    assert console_lines == file_lines
    assert LOG_LINE_REGEX.match(console_lines[0])


def test_traceback_lines_follow_formatted_line(tmp_path: Path, read_log_lines: ReadLines) -> None:
    """Seção 10.10 e RF-018: exception gera linha formatada seguida do traceback, sem diagnose."""
    setup_logger(tmp_path, user="ana", console=False)
    local_value = "valor-local-sigiloso"

    def fail() -> None:
        raise ValueError(local_value[:0] + "falha no cálculo")

    try:
        fail()
    except ValueError:
        get_logger().exception("Falha no cálculo da taxa")
    lines = read_log_lines(tmp_path)
    assert LOG_LINE_REGEX.match(lines[0])
    assert lines[0].endswith("Falha no cálculo da taxa")
    assert lines[1].startswith("Traceback")
    assert "ValueError: falha no cálculo" in lines
    assert all(not line.startswith("[2") for line in lines[1:])
    assert "valor-local-sigiloso" not in "\n".join(lines)


def test_unicode_is_written_in_utf8(tmp_path: Path, read_log_lines: ReadLines) -> None:
    """RNF-011: mensagem com 'ção' e '€' é gravada corretamente em UTF-8."""
    setup_logger(tmp_path, user="joão", console=False, file_prefix="teste")
    get_logger().info("Conciliação concluída | total=€ 1.500,00")
    lines = read_log_lines(tmp_path)
    assert lines[0].endswith("[joão] Conciliação concluída | total=€ 1.500,00")
    raw = next(tmp_path.glob("teste_*.log")).read_bytes()
    assert "ção".encode() in raw and "€".encode() in raw


# --- RF-016: usuário parametrizável ------------------------------------------------------------


def test_default_user_from_getpass(
    tmp_path: Path, read_log_lines: ReadLines, mocker: MockerFixture
) -> None:
    """RF-016: setup_logger() sem usuário usa getpass.getuser()."""
    mocker.patch.object(getpass, "getuser", return_value="joao.silva")
    setup_logger(tmp_path, console=False)
    get_logger().info("x")
    assert "[joao.silva] x" in read_log_lines(tmp_path)[0]


def test_explicit_user_in_setup_logger(tmp_path: Path, read_log_lines: ReadLines) -> None:
    """RF-016: setup_logger(user='robo01') usa robo01 em todas as linhas."""
    setup_logger(tmp_path, user="robo01", console=False)
    get_logger().info("a")
    get_logger().warning("b")
    lines = read_log_lines(tmp_path)
    assert all("[robo01]" in line for line in lines)


def test_get_logger_user_overrides_only_returned_logger(
    tmp_path: Path, read_log_lines: ReadLines
) -> None:
    """RF-016: get_logger(user='auditoria') altera somente as linhas desse logger."""
    setup_logger(tmp_path, user="robo01", console=False)
    get_logger().info("antes")
    get_logger(user="auditoria").info("auditada")
    get_logger().info("depois")
    lines = read_log_lines(tmp_path)
    assert [_user_of(line) for line in lines] == ["robo01", "auditoria", "robo01"]


def test_get_logger_binds_extra_fields(tmp_path: Path, read_log_lines: ReadLines) -> None:
    """RF-014: get_logger(**extra) vincula campos extras sem alterar o usuário."""
    setup_logger(tmp_path, user="robo01", console=False)
    bound = get_logger(lote="L-1")
    records: list[dict[str, object]] = []
    loguru_logger.add(lambda message: records.append(dict(message.record["extra"])))
    bound.info("x")
    assert records == [{"user": "robo01", "lote": "L-1"}]


def test_user_from_username_when_getpass_fails(
    tmp_path: Path,
    read_log_lines: ReadLines,
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RF-016: getpass.getuser() lançando OSError e USERNAME=svc_rpa resulta em [svc_rpa]."""
    mocker.patch.object(getpass, "getuser", side_effect=OSError("sem usuário"))
    monkeypatch.setenv("USERNAME", "svc_rpa")
    setup_logger(tmp_path, console=False)
    get_logger().info("x")
    assert "[svc_rpa] x" in read_log_lines(tmp_path)[0]


def test_unknown_user_as_last_resort(
    mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RF-016: sem getpass e sem USERNAME, o usuário é 'desconhecido'."""
    mocker.patch.object(getpass, "getuser", side_effect=OSError("sem usuário"))
    monkeypatch.delenv("USERNAME", raising=False)
    assert default_user() == UNKNOWN_USER == "desconhecido"


def test_user_value_is_sanitized(tmp_path: Path, read_log_lines: ReadLines) -> None:
    """RF-015: usuário com colchetes ou vazio não quebra o campo [user] da regex."""
    setup_logger(tmp_path, user="  robo[01]  ", console=False)
    get_logger().info("a")
    get_logger(user="   ").info("b")
    lines = read_log_lines(tmp_path)
    assert all(LOG_LINE_REGEX.match(line) for line in lines)
    assert [_user_of(line) for line in lines] == ["robo_01_", "robo_01_"]


# --- RF-014: logger único e idempotente --------------------------------------------------------


def test_setup_logger_is_idempotent(tmp_path: Path, read_log_lines: ReadLines) -> None:
    """RF-014: setup_logger() duas vezes na mesma pasta grava uma única linha por mensagem."""
    setup_logger(tmp_path, user="ana", console=False)
    setup_logger(tmp_path, user="ana", console=False)
    get_logger().info("mensagem única")
    lines = read_log_lines(tmp_path)
    assert [line for line in lines if line.endswith("mensagem única")] == lines
    assert len(lines) == 1


def test_get_logger_returns_loguru_logger() -> None:
    """RF-014: get_logger() retorna objeto com info, bind, opt e exception do loguru."""
    log = get_logger()
    assert log is loguru_logger
    for method in ("info", "bind", "opt", "exception", "complete"):
        assert callable(getattr(log, method))


def test_get_logger_configures_console_implicitly(capsys: pytest.CaptureFixture[str]) -> None:
    """RF-014: get_logger() sem setup_logger configura console padrão, sem arquivo."""
    assert not is_configured()
    get_logger(user="ana").info("implícito")
    assert is_configured()
    assert current_log_file() is None
    err = ANSI_ESCAPE.sub("", capsys.readouterr().err)
    assert LOG_LINE_REGEX.match(err.splitlines()[0])
    assert err.rstrip().endswith("[ana] implícito")
    assert not any(isinstance(h, InterceptHandler) for h in logging.getLogger().handlers)


def test_setup_logger_returns_logger_and_marks_configured(tmp_path: Path) -> None:
    """RF-014: setup_logger retorna o logger global e marca o estado como configurado."""
    assert setup_logger(tmp_path, console=False) is loguru_logger
    assert is_configured()


def test_level_filters_messages(tmp_path: Path, read_log_lines: ReadLines) -> None:
    """RF-014: mensagens abaixo do nível configurado não são gravadas."""
    setup_logger(tmp_path, level="WARNING", console=False)
    get_logger().info("descartada")
    get_logger().error("gravada")
    lines = read_log_lines(tmp_path)
    assert len(lines) == 1 and lines[0].endswith("gravada")


def test_console_only_when_logs_dir_is_none(tmp_path: Path) -> None:
    """Seção 10.7: sem logs_dir, nenhum arquivo é criado e current_log_file() é None."""
    setup_logger(None, console=True)
    get_logger().info("x")
    assert current_log_file() is None
    assert list(tmp_path.iterdir()) == []


def test_current_log_file_points_to_today(tmp_path: Path) -> None:
    """P-04: current_log_file() é <logs_dir>/<prefixo>_<AAAA-MM-DD>.log do dia corrente."""
    with time_machine.travel(_local(2026, 10, 31, 9, 0, 0), tick=False):
        setup_logger(tmp_path / "logs", console=False, file_prefix="emissao_notas")
        expected = (tmp_path / "logs").resolve() / "emissao_notas_2026-10-31.log"
        assert current_log_file() == expected
        assert expected.is_file()


def test_default_file_prefix(tmp_path: Path) -> None:
    """P-04: fora de projetos, o prefixo do arquivo é 'rpa3003'."""
    setup_logger(tmp_path, console=False)
    log_file = current_log_file()
    assert log_file is not None and log_file.name.startswith("rpa3003_")


def test_invalid_retention_days_is_rejected(tmp_path: Path) -> None:
    """RF-017: retention_days menor que 1 é recusado."""
    with pytest.raises(ValueError, match="retention_days"):
        setup_logger(tmp_path, retention_days=0, console=False)


def test_reset_logger_restores_initial_state(tmp_path: Path) -> None:
    """Seção 7.3: reset_logger() remove handlers, interceptação e estado configurado."""
    root = logging.getLogger()
    original_level = root.level
    setup_logger(tmp_path, console=False, std_logging_level="ERROR")
    assert any(isinstance(h, InterceptHandler) for h in root.handlers)
    reset_logger()
    assert not is_configured()
    assert current_log_file() is None
    assert not any(isinstance(h, InterceptHandler) for h in root.handlers)
    assert root.level == original_level
    assert loguru_logger._core.handlers == {}  # type: ignore[attr-defined]


# --- RF-017: rotação diária e retenção ---------------------------------------------------------


def test_daily_rotation_creates_new_file_without_renaming(
    tmp_path: Path, read_log_lines: ReadLines
) -> None:
    """RF-017: mensagens às 23:59:59 e 00:00:01 vão para arquivos datados, sem renomeação."""
    before_midnight = _local(2026, 10, 31, 23, 59, 59)
    after_midnight = _local(2026, 11, 1, 0, 0, 1)
    with time_machine.travel(before_midnight, tick=False) as traveller:
        setup_logger(tmp_path, user="ana", console=False, file_prefix="teste")
        first_file = tmp_path / "teste_2026-10-31.log"
        _align_file_times(first_file, before_midnight)
        get_logger().info("antes da meia-noite")
        loguru_logger.complete()
        _align_file_times(first_file, before_midnight)
        traveller.move_to(after_midnight)
        get_logger().info("depois da meia-noite")
        loguru_logger.complete()
    files = sorted(path.name for path in tmp_path.iterdir())
    assert files == ["teste_2026-10-31.log", "teste_2026-11-01.log"]
    first = (tmp_path / "teste_2026-10-31.log").read_text(encoding="utf-8").splitlines()
    second = (tmp_path / "teste_2026-11-01.log").read_text(encoding="utf-8").splitlines()
    assert len(first) == 1 and first[0].startswith("[2026-10-31 23:59:59]")
    assert first[0].endswith("antes da meia-noite")
    assert len(second) == 1 and second[0].startswith("[2026-11-01 00:00:01]")
    assert second[0].endswith("depois da meia-noite")


def test_retention_removes_old_files_on_rotation(tmp_path: Path) -> None:
    """RF-017: com retention_days=1, arquivo modificado há 3 dias é removido na rotação."""
    before_midnight = _local(2026, 10, 31, 23, 59, 59)
    old_file = tmp_path / "teste_2026-10-28.log"
    old_file.write_text("antigo\n", encoding="utf-8")
    old_time = (before_midnight - timedelta(days=3)).timestamp()
    os.utime(old_file, (old_time, old_time))
    unrelated = tmp_path / "outro_2026-10-28.log"
    unrelated.write_text("outro prefixo\n", encoding="utf-8")
    os.utime(unrelated, (old_time, old_time))
    with time_machine.travel(before_midnight, tick=False) as traveller:
        setup_logger(tmp_path, console=False, file_prefix="teste", retention_days=1)
        current = tmp_path / "teste_2026-10-31.log"
        _align_file_times(current, before_midnight)
        get_logger().info("a")
        loguru_logger.complete()
        _align_file_times(current, before_midnight)
        traveller.move_to(_local(2026, 11, 1, 0, 0, 1))
        get_logger().info("b")
        loguru_logger.complete()
    assert not old_file.exists()
    assert unrelated.exists()
    assert (tmp_path / "teste_2026-10-31.log").exists()
    assert (tmp_path / "teste_2026-11-01.log").exists()


def test_compression_zips_previous_day_on_rotation(tmp_path: Path) -> None:
    """Seção 10.7: com compression=True, o arquivo do dia anterior vira <arquivo>.zip."""
    before_midnight = _local(2026, 10, 31, 23, 59, 59)
    with time_machine.travel(before_midnight, tick=False) as traveller:
        setup_logger(tmp_path, console=False, file_prefix="teste", compression=True)
        current = tmp_path / "teste_2026-10-31.log"
        _align_file_times(current, before_midnight)
        get_logger().info("a")
        loguru_logger.complete()
        _align_file_times(current, before_midnight)
        traveller.move_to(_local(2026, 11, 1, 0, 0, 1))
        get_logger().info("b")
        loguru_logger.complete()
    files = sorted(path.name for path in tmp_path.iterdir())
    assert files == ["teste_2026-10-31.log.zip", "teste_2026-11-01.log"]


def test_file_sink_options(tmp_path: Path, mocker: MockerFixture) -> None:
    """RF-017 e RNF-005: sink de arquivo com as opções obrigatórias da seção 10.6."""
    add = mocker.spy(loguru_logger, "add")
    setup_logger(tmp_path, console=True, file_prefix="teste", retention_days=7)
    console_call, file_call = add.call_args_list
    assert console_call.kwargs == {
        "format": CONSOLE_FORMAT,
        "level": "INFO",
        "colorize": None,
        "backtrace": True,
        "diagnose": False,
        "enqueue": False,
    }
    assert file_call.args == (tmp_path.resolve() / "teste_{time:YYYY-MM-DD}.log",)
    assert file_call.kwargs == {
        "format": LOG_FORMAT,
        "level": "INFO",
        "rotation": "00:00",
        "retention": "7 days",
        "compression": None,
        "encoding": "utf-8",
        "enqueue": True,
        "backtrace": True,
        "diagnose": False,
        "delay": False,
    }


# --- RF-018: mascaramento ----------------------------------------------------------------------


def test_registered_secret_is_masked(tmp_path: Path, read_log_lines: ReadLines) -> None:
    """RF-018: register_secret('S3nh@Forte') faz info('senha=S3nh@Forte') gravar senha=********."""
    setup_logger(tmp_path, console=False)
    register_secret("S3nh@Forte")
    get_logger().info("senha=S3nh@Forte")
    get_logger().info("formatada={} | de novo={}", "S3nh@Forte", "S3nh@Forte")
    lines = read_log_lines(tmp_path)
    assert lines[0].endswith(f"senha={MASK}")
    assert lines[1].endswith(f"formatada={MASK} | de novo={MASK}")
    assert MASK == "********"
    assert "S3nh@Forte" not in "\n".join(lines)


def test_secret_masked_for_bound_logger_and_console(
    tmp_path: Path, read_log_lines: ReadLines, capsys: pytest.CaptureFixture[str]
) -> None:
    """RF-018: o mascaramento vale para loggers vinculados e para o console."""
    setup_logger(tmp_path, console=True)
    register_secret("token-123456")
    get_logger(user="auditoria").warning("token=token-123456")
    assert read_log_lines(tmp_path)[0].endswith(f"token={MASK}")
    assert "token-123456" not in capsys.readouterr().err


def test_longest_secret_is_masked_first(tmp_path: Path, read_log_lines: ReadLines) -> None:
    """RF-018: segredos são substituídos em ordem decrescente de tamanho."""
    setup_logger(tmp_path, console=False)
    register_secret("abcd")
    register_secret("abcdefgh")
    register_secret("abcd")
    get_logger().info("valor=abcdefgh e abcd")
    assert read_log_lines(tmp_path)[0].endswith(f"valor={MASK} e {MASK}")


def test_short_secret_warns_and_is_not_registered(
    tmp_path: Path, read_log_lines: ReadLines
) -> None:
    """RF-018: valor com menos de 4 caracteres gera WARNING e não é mascarado."""
    setup_logger(tmp_path, console=False)
    register_secret("abc")
    get_logger().info("valor=abc")
    lines = read_log_lines(tmp_path)
    assert "[WARNING]" in lines[0]
    assert lines[0].endswith("Segredo com menos de 4 caracteres não registrado para mascaramento")
    assert lines[1].endswith("valor=abc")


@pytest.mark.parametrize("value", [None, ""])
def test_empty_secret_is_ignored(
    value: str | None, tmp_path: Path, read_log_lines: ReadLines
) -> None:
    """RF-018: None ou vazio são ignorados sem WARNING."""
    setup_logger(tmp_path, console=False)
    register_secret(value)
    get_logger().info("nada a mascarar")
    lines = read_log_lines(tmp_path)
    assert len(lines) == 1
    assert "[INFO]" in lines[0] and lines[0].endswith("nada a mascarar")


def test_clear_secrets_stops_masking(tmp_path: Path, read_log_lines: ReadLines) -> None:
    """RF-018: clear_secrets() remove os valores registrados."""
    setup_logger(tmp_path, console=False)
    register_secret("S3nh@Forte")
    clear_secrets()
    get_logger().info("senha=S3nh@Forte")
    assert read_log_lines(tmp_path)[0].endswith("senha=S3nh@Forte")


# --- RF-019: interceptação do logging padrão ---------------------------------------------------


def test_std_logging_is_intercepted_with_origin(tmp_path: Path, read_log_lines: ReadLines) -> None:
    """RF-019: logging.getLogger('teste').warning('x') gera [WARNING] [test_logger.py] [<linha>]."""
    setup_logger(tmp_path, user="ana", console=False)
    line = _next_line()
    logging.getLogger("teste").warning("x")
    lines = read_log_lines(tmp_path)
    assert len(lines) == 1
    assert f"[WARNING] [test_logger.py] [{line}] [ana] x" in lines[0]
    assert LOG_LINE_REGEX.match(lines[0])


def test_std_logging_below_level_is_dropped(tmp_path: Path, read_log_lines: ReadLines) -> None:
    """RF-019: logging.getLogger('urllib3').debug('y') não é registrado (abaixo de WARNING)."""
    setup_logger(tmp_path, level="DEBUG", console=False)
    logging.getLogger("urllib3").debug("y")
    logging.getLogger("urllib3").info("y")
    assert read_log_lines(tmp_path) == []


def test_std_logging_exception_and_masking(tmp_path: Path, read_log_lines: ReadLines) -> None:
    """RF-019 e RF-018: exceções e segredos do logging padrão seguem o formato e a máscara."""
    setup_logger(tmp_path, console=False)
    register_secret("S3nh@Forte")
    try:
        raise RuntimeError("erro de biblioteca")
    except RuntimeError:
        logging.getLogger("hvac").exception("falha com senha %s", "S3nh@Forte")
    lines = read_log_lines(tmp_path)
    assert "[ERROR] [test_logger.py]" in lines[0]
    assert lines[0].endswith(f"falha com senha {MASK}")
    assert "RuntimeError: erro de biblioteca" in lines


def test_std_logging_custom_level_uses_number(tmp_path: Path, read_log_lines: ReadLines) -> None:
    """RF-019: nível desconhecido pelo loguru é reemitido pelo número."""
    setup_logger(tmp_path, console=False)
    logging.getLogger("teste").log(35, "nível 35")
    lines = read_log_lines(tmp_path)
    assert "[Level 35]" in lines[0] and lines[0].endswith("nível 35")


def test_std_logging_level_accepts_loguru_only_levels(tmp_path: Path) -> None:
    """RF-019: std_logging_level aceita TRACE e SUCCESS (inexistentes no logging)."""
    setup_logger(tmp_path, console=False, std_logging_level="SUCCESS")
    assert logging.getLogger().level == 25
    setup_logger(tmp_path, console=False, std_logging_level="TRACE")
    assert logging.getLogger().level == 5


def test_interception_can_be_disabled(tmp_path: Path, read_log_lines: ReadLines) -> None:
    """RF-019: intercept_std_logging=False remove a interceptação instalada antes."""
    setup_logger(tmp_path, console=False)
    setup_logger(tmp_path, console=False, intercept_std_logging=False)
    assert not any(isinstance(h, InterceptHandler) for h in logging.getLogger().handlers)
    logging.getLogger("teste").error("não interceptada")
    assert read_log_lines(tmp_path) == []


# --- RNF-005: desempenho -----------------------------------------------------------------------


def test_ten_thousand_lines_in_under_three_seconds(
    tmp_path: Path, read_log_lines: ReadLines
) -> None:
    """RNF-005: 10.000 linhas INFO levam no máximo 3 s, incluindo logger.complete()."""
    setup_logger(tmp_path, console=False)
    register_secret("S3nh@Forte")
    log = get_logger()
    start = time.perf_counter()
    for index in range(10_000):
        log.info("Registro processado | indice={}", index)
    log.complete()
    elapsed = time.perf_counter() - start
    assert elapsed <= 3.0
    assert len(read_log_lines(tmp_path)) == 10_000
