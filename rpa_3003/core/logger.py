"""Sistema de logging centralizado (loguru).

Configuração única de formato, sinks, rotação, retenção, usuário, mascaramento de segredos e
interceptação do módulo `logging` padrão (seção 10). Este é o único módulo do framework que
adiciona sinks ao loguru e o único que importa `logging`.
"""

from __future__ import annotations

import getpass
import inspect
import logging
import os
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, TypeAlias

from loguru import logger

if TYPE_CHECKING:
    from loguru import Logger, Record

__all__: list[str] = [
    "CONSOLE_FORMAT",
    "DEFAULT_FILE_PREFIX",
    "LOG_FORMAT",
    "MASK",
    "MIN_SECRET_LENGTH",
    "UNKNOWN_USER",
    "InterceptHandler",
    "LogLevel",
    "clear_secrets",
    "current_log_file",
    "default_user",
    "get_logger",
    "is_configured",
    "register_secret",
    "reset_logger",
    "setup_logger",
]

LogLevel: TypeAlias = Literal[  # noqa: UP040 - convenção 7.7 usa TypeAlias
    "TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"
]

LOG_FORMAT: Final[str] = (
    "[{time:YYYY-MM-DD HH:mm:ss}] [{level}] [{file.name}] [{line}] [{extra[user]}] {message}"
)
CONSOLE_FORMAT: Final[str] = (
    "<green>[{time:YYYY-MM-DD HH:mm:ss}]</green> <level>[{level}]</level> "
    "[{file.name}] [{line}] [{extra[user]}] <level>{message}</level>"
)
MASK: Final[str] = "********"
MIN_SECRET_LENGTH: Final[int] = 4
DEFAULT_FILE_PREFIX: Final[str] = "rpa3003"
UNKNOWN_USER: Final[str] = "desconhecido"

# Caracteres que quebrariam o campo [user] do formato obrigatório (regex da seção 10.4).
_USER_FORBIDDEN_CHARS: Final[dict[int, str]] = str.maketrans(
    {"[": "_", "]": "_", "\r": " ", "\n": " "}
)


class _SecretRegistry:
    """Conjunto global de segredos a mascarar, protegido por `threading.Lock`.

    Mantém um instantâneo ordenado por tamanho decrescente, para que um segredo que contém outro
    seja substituído por inteiro.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: set[str] = set()
        self._ordered: tuple[str, ...] = ()

    def add(self, value: str) -> None:
        with self._lock:
            if value in self._values:
                return
            self._values.add(value)
            self._ordered = tuple(sorted(self._values, key=len, reverse=True))

    def clear(self) -> None:
        with self._lock:
            self._values.clear()
            self._ordered = ()

    def snapshot(self) -> tuple[str, ...]:
        with self._lock:
            return self._ordered


@dataclass(slots=True)
class _LoggerState:
    """Estado da configuração feita por `setup_logger`."""

    configured: bool = False
    logs_dir: Path | None = None
    file_prefix: str = DEFAULT_FILE_PREFIX
    previous_root_level: int | None = None


_SECRETS: Final[_SecretRegistry] = _SecretRegistry()
_STATE_LOCK: Final[threading.RLock] = threading.RLock()
_state: Final[_LoggerState] = _LoggerState()


class InterceptHandler(logging.Handler):
    """Redireciona registros do módulo logging para o loguru preservando a origem."""

    def emit(self, record: logging.LogRecord) -> None:
        """Reemite o registro no loguru com o nível, o arquivo e a linha de origem.

        Args:
            record: Registro produzido pelo módulo `logging`.
        """
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = inspect.currentframe(), 0
        while frame is not None and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def default_user() -> str:
    """getpass.getuser(); em erro, os.environ["USERNAME"]; por fim, UNKNOWN_USER.

    Returns:
        Nome do usuário usado no campo `user` do log quando nenhum outro é informado.
    """
    try:
        user = _clean_user(getpass.getuser())
    except (OSError, ImportError, KeyError):  # 3.13+: OSError; versões anteriores: pwd
        user = None
    return user or _clean_user(os.environ.get("USERNAME")) or UNKNOWN_USER


def setup_logger(
    logs_dir: str | os.PathLike[str] | None = None,
    *,
    user: str | None = None,
    level: LogLevel = "INFO",
    file_prefix: str = DEFAULT_FILE_PREFIX,
    retention_days: int = 30,
    compression: bool = False,
    console: bool = True,
    intercept_std_logging: bool = True,
    std_logging_level: LogLevel = "WARNING",
) -> Logger:
    """Configura o logger global (idempotente) e o retorna.

    Cada chamada remove todos os handlers anteriores e aplica a nova configuração.

    Args:
        logs_dir: Pasta dos arquivos de log; `None` registra somente no console.
        user: Valor do campo `user`; `None` usa `default_user()`.
        level: Nível mínimo dos sinks.
        file_prefix: Prefixo do arquivo diário `<prefixo>_<AAAA-MM-DD>.log`.
        retention_days: Dias de retenção dos arquivos de log (mínimo 1).
        compression: Compacta em ZIP o arquivo do dia anterior na rotação.
        console: Habilita o sink de console (`sys.stderr`).
        intercept_std_logging: Redireciona o módulo `logging` padrão para o loguru.
        std_logging_level: Nível mínimo do logger raiz do módulo `logging`.

    Returns:
        O logger global do loguru.

    Raises:
        ValueError: Se `retention_days` for menor que 1 ou um nível for inválido.
    """
    if retention_days < 1:
        raise ValueError(f"retention_days deve ser maior ou igual a 1 (recebido: {retention_days})")
    resolved_user = _clean_user(user) or default_user()
    with _STATE_LOCK:
        logger.remove()
        logger.configure(extra={"user": resolved_user}, patcher=_mask_secrets)
        if console:
            logger.add(
                sys.stderr,
                format=CONSOLE_FORMAT,
                level=level,
                colorize=None,
                backtrace=True,
                diagnose=False,
                enqueue=False,
            )
        resolved_dir = _add_file_sink(logs_dir, level, file_prefix, retention_days, compression)
        if intercept_std_logging:
            _install_intercept_handler(std_logging_level)
        else:
            _uninstall_intercept_handler()
        _state.configured = True
        _state.logs_dir = resolved_dir
        _state.file_prefix = file_prefix
    return logger


def get_logger(user: str | None = None, **extra: Any) -> Logger:  # noqa: ANN401
    """Retorna o logger global; configura console padrão se setup_logger ainda não foi chamado.
    Com `user`, retorna logger vinculado (bind) apenas com o usuário sobrescrito.

    A configuração implícita registra somente no console, em `INFO`, sem interceptar o módulo
    `logging` padrão.

    Args:
        user: Usuário exibido apenas nas mensagens do logger retornado.
        **extra: Campos adicionais vinculados ao logger retornado.

    Returns:
        O logger global, ou um logger vinculado quando `user` ou `extra` são informados.
    """
    if not _state.configured:
        with _STATE_LOCK:
            if not _state.configured:
                setup_logger(intercept_std_logging=False)
    cleaned = _clean_user(user)
    if cleaned is not None:
        extra["user"] = cleaned
    return logger.bind(**extra) if extra else logger


def register_secret(value: str | None) -> None:
    """Registra valor para mascaramento. None/vazio: ignora. len < 4: WARNING e ignora.

    Args:
        value: Segredo a substituir por `MASK` em todas as mensagens de log.
    """
    if not value:
        return
    if len(value) < MIN_SECRET_LENGTH:
        get_logger().warning(
            "Segredo com menos de {} caracteres não registrado para mascaramento",
            MIN_SECRET_LENGTH,
        )
        return
    _SECRETS.add(value)


def clear_secrets() -> None:
    """Remove todos os segredos registrados para mascaramento."""
    _SECRETS.clear()


def is_configured() -> bool:
    """Indica se o logger já foi configurado (explícita ou implicitamente).

    Returns:
        `True` após `setup_logger` ou após a configuração implícita de `get_logger`.
    """
    return _state.configured


def current_log_file() -> Path | None:
    """Caminho do arquivo de log do dia corrente, ou None se não há sink de arquivo."""
    with _STATE_LOCK:
        if _state.logs_dir is None:
            return None
        return _state.logs_dir / f"{_state.file_prefix}_{datetime.now():%Y-%m-%d}.log"


def reset_logger() -> None:
    """Remove todos os handlers e restaura o estado inicial (uso em testes)."""
    with _STATE_LOCK:
        logger.remove()
        logger.configure(extra={})
        _uninstall_intercept_handler()
        _state.configured = False
        _state.logs_dir = None
        _state.file_prefix = DEFAULT_FILE_PREFIX


def _clean_user(value: str | None) -> str | None:
    """Normaliza o usuário para o campo `[user]`; vazio vira `None`."""
    if value is None:
        return None
    cleaned = value.translate(_USER_FORBIDDEN_CHARS).strip()
    return cleaned or None


def _mask_secrets(record: Record) -> None:
    """Patcher do loguru: substitui os segredos registrados por `MASK` na mensagem."""
    secrets = _SECRETS.snapshot()
    if not secrets:
        return
    message = record["message"]
    for secret in secrets:
        if secret in message:
            message = message.replace(secret, MASK)
    record["message"] = message


def _add_file_sink(
    logs_dir: str | os.PathLike[str] | None,
    level: LogLevel,
    file_prefix: str,
    retention_days: int,
    compression: bool,
) -> Path | None:
    """Adiciona o sink de arquivo diário (seção 10.6) e retorna a pasta resolvida."""
    if logs_dir is None:
        return None
    resolved = Path(logs_dir).resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    logger.add(
        resolved / f"{file_prefix}_{{time:YYYY-MM-DD}}.log",
        format=LOG_FORMAT,
        level=level,
        rotation="00:00",
        retention=f"{retention_days} days",
        compression="zip" if compression else None,
        encoding="utf-8",
        enqueue=True,
        backtrace=True,
        diagnose=False,
        delay=False,
    )
    return resolved


def _install_intercept_handler(level: LogLevel) -> None:
    """Instala `InterceptHandler` como único handler do logger raiz de `logging`."""
    root = logging.getLogger()
    if _state.previous_root_level is None:
        _state.previous_root_level = root.level
    # TRACE e SUCCESS não existem no módulo logging; usa o número do nível do loguru.
    logging.basicConfig(handlers=[InterceptHandler()], level=logger.level(level).no, force=True)


def _uninstall_intercept_handler() -> None:
    """Remove os `InterceptHandler` do logger raiz e restaura o nível anterior."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, InterceptHandler):
            root.removeHandler(handler)
            handler.close()
    if _state.previous_root_level is not None:
        root.setLevel(_state.previous_root_level)
        _state.previous_root_level = None
