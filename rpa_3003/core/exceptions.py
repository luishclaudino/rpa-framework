"""Exceções customizadas.

Hierarquia de exceções do framework e mapeamento para códigos de saída (seções 8.6 e 9.5).
Cada classe define, por herança ou explicitamente, o atributo de classe `exit_code`, usado como
código de saída do processo quando a exceção encerra a execução.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import IntEnum
from typing import Any, ClassVar

__all__: list[str] = [
    "ArchiveError",
    "BrowserError",
    "BrowserNotStartedError",
    "BrowserNotSupportedError",
    "BusinessRuleError",
    "ConfigurationError",
    "CryptoError",
    "DataFormatError",
    "DatabaseError",
    "DesktopAutomationError",
    "DownloadTimeoutError",
    "DriverInitializationError",
    "ElementNotFoundError",
    "EmailError",
    "ExecutionTimeoutError",
    "ExitCode",
    "FileOperationError",
    "ImageNotFoundError",
    "ImageProcessingError",
    "InvalidEnvironmentError",
    "InvalidProjectNameError",
    "PDFError",
    "ProjectAlreadyExistsError",
    "ProjectAlreadyRunningError",
    "ProjectNotFoundError",
    "ProjectStructureError",
    "RPAError",
    "SecretNotFoundError",
    "StepLoadError",
    "TemplateNotFoundError",
    "TemplateRenderError",
    "UtilsError",
    "VaultAuthenticationError",
    "VaultError",
    "VaultUnavailableError",
    "WindowNotFoundError",
    "WorkflowError",
    "exit_code_for",
]


class ExitCode(IntEnum):
    """Códigos de saída padronizados do CLI e de `python main.py` (seção 9.5)."""

    SUCCESS = 0
    TECHNICAL_ERROR = 1
    USAGE_ERROR = 2
    CONFIGURATION_ERROR = 3
    BUSINESS_ERROR = 4
    TIMEOUT = 5
    ALREADY_RUNNING = 6
    INTERRUPTED = 130


class RPAError(Exception):
    """Raiz de todas as exceções do framework.

    Attributes:
        exit_code: Código de saída associado à classe da exceção.
        message: Mensagem legível, sem segredos.
        details: Dados de contexto (nunca valores de segredos), exibidos após a mensagem.

    Example:
        >>> str(RPAError("Falha", details={"arquivo": "a.csv"}))
        'Falha | arquivo=a.csv'
    """

    exit_code: ClassVar[ExitCode] = ExitCode.TECHNICAL_ERROR

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        """Cria a exceção.

        Args:
            message: Mensagem legível, sem segredos.
            details: Dados de contexto; a ordem de inserção é preservada em `__str__`.
        """
        super().__init__(message)
        self.message: str = message
        self.details: dict[str, Any] = dict(details or {})

    def __str__(self) -> str:
        """'mensagem' ou 'mensagem | chave=valor, chave=valor' (ordem de inserção)."""
        if not self.details:
            return self.message
        pairs = ", ".join(f"{key}={value}" for key, value in self.details.items())
        return f"{self.message} | {pairs}"


class ConfigurationError(RPAError):
    """Configuração inválida ou ausente."""

    exit_code = ExitCode.CONFIGURATION_ERROR


class InvalidEnvironmentError(ConfigurationError):
    """Ambiente diferente de `dev`/`prd`."""

    exit_code = ExitCode.USAGE_ERROR


class InvalidProjectNameError(ConfigurationError):
    """Nome de projeto fora do padrão."""

    exit_code = ExitCode.USAGE_ERROR


class ProjectNotFoundError(ConfigurationError):
    """Projeto inexistente."""


class ProjectAlreadyExistsError(ConfigurationError):
    """Destino já existe em `create`."""


class ProjectStructureError(ConfigurationError):
    """Estrutura de projeto inválida (RF-003)."""


class TemplateNotFoundError(ConfigurationError):
    """Template inexistente."""


class TemplateRenderError(ConfigurationError):
    """Variável ausente ou erro de escrita na renderização de template."""


class ProjectAlreadyRunningError(RPAError):
    """Lock de instância única ocupado por outra execução do projeto."""

    exit_code = ExitCode.ALREADY_RUNNING


class ExecutionTimeoutError(RPAError):
    """Tempo limite de execução excedido no CLI."""

    exit_code = ExitCode.TIMEOUT


class VaultError(RPAError):
    """Raiz de erros do Vault."""

    exit_code = ExitCode.CONFIGURATION_ERROR


class VaultUnavailableError(VaultError):
    """Vault inacessível: rede, timeout ou HTTP 5xx."""


class VaultAuthenticationError(VaultError):
    """Credenciais do Vault inválidas."""


class SecretNotFoundError(VaultError):
    """Caminho ou chave inexistente no Vault."""


class BusinessRuleError(RPAError):
    """Regra de negócio violada."""

    exit_code = ExitCode.BUSINESS_ERROR


class WorkflowError(RPAError):
    """Raiz de erros de orquestração."""


class StepLoadError(WorkflowError):
    """Módulo de step ou classe `Step` inválidos."""

    exit_code = ExitCode.CONFIGURATION_ERROR


class BrowserError(RPAError):
    """Raiz de erros de automação web."""


class BrowserNotSupportedError(BrowserError):
    """Tipo de navegador não registrado."""

    exit_code = ExitCode.CONFIGURATION_ERROR


class DriverInitializationError(BrowserError):
    """Falha ao iniciar o driver ou o navegador."""


class BrowserNotStartedError(BrowserError):
    """Uso de `driver` antes de `start()`."""


class ElementNotFoundError(BrowserError):
    """Tempo limite de espera por elemento excedido."""


class DownloadTimeoutError(BrowserError):
    """Download não concluído no prazo."""


class DesktopAutomationError(RPAError):
    """Raiz de erros de automação desktop."""


class ImageNotFoundError(DesktopAutomationError):
    """Imagem não encontrada na tela no prazo."""


class WindowNotFoundError(DesktopAutomationError):
    """Janela não encontrada no prazo."""


class UtilsError(RPAError):
    """Raiz de erros dos utilitários."""


class FileOperationError(UtilsError):
    """Erro de filesystem."""


class DataFormatError(UtilsError):
    """JSON, CSV ou data inválidos."""


class ArchiveError(UtilsError):
    """Arquivo ZIP/TAR inválido ou inseguro."""


class CryptoError(UtilsError):
    """Chave inválida, dado corrompido ou algoritmo proibido."""


class DatabaseError(UtilsError):
    """Erro de conexão, de consulta SQL ou instrução bloqueada pelo acesso somente leitura."""


class EmailError(UtilsError):
    """Erro de SMTP, IMAP ou Outlook."""


class ImageProcessingError(UtilsError):
    """Erro de processamento de imagem (Pillow)."""


class PDFError(UtilsError):
    """Erro de pypdf, pdfplumber ou reportlab."""


def exit_code_for(error: BaseException | None) -> ExitCode:
    """Mapeia uma exceção para o código de saída do processo.

    Args:
        error: Exceção que encerrou a execução, ou `None` quando não houve erro.

    Returns:
        `SUCCESS` para `None`; `error.exit_code` para `RPAError`; `INTERRUPTED` para
        `KeyboardInterrupt`; `TECHNICAL_ERROR` para as demais exceções.
    """
    match error:
        case None:
            return ExitCode.SUCCESS
        case RPAError():
            return error.exit_code
        case KeyboardInterrupt():
            return ExitCode.INTERRUPTED
        case _:
            return ExitCode.TECHNICAL_ERROR
