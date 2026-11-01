"""Testes de `rpa_3003.core.exceptions` (RF-009, seções 8.6 e 9.5)."""

from __future__ import annotations

import pytest

from rpa_3003.core import exceptions as exc_module
from rpa_3003.core.exceptions import (
    ArchiveError,
    BrowserError,
    BrowserNotStartedError,
    BrowserNotSupportedError,
    BusinessRuleError,
    ConfigurationError,
    CryptoError,
    DatabaseError,
    DataFormatError,
    DesktopAutomationError,
    DownloadTimeoutError,
    DriverInitializationError,
    ElementNotFoundError,
    EmailError,
    ExecutionTimeoutError,
    ExitCode,
    FileOperationError,
    ImageNotFoundError,
    ImageProcessingError,
    InvalidEnvironmentError,
    InvalidProjectNameError,
    PDFError,
    ProjectAlreadyExistsError,
    ProjectAlreadyRunningError,
    ProjectNotFoundError,
    ProjectStructureError,
    RPAError,
    SecretNotFoundError,
    StepLoadError,
    TemplateNotFoundError,
    TemplateRenderError,
    UtilsError,
    VaultAuthenticationError,
    VaultError,
    VaultUnavailableError,
    WindowNotFoundError,
    WorkflowError,
    exit_code_for,
)

pytestmark = pytest.mark.unit

# Tabela "Hierarquia obrigatória" da seção 8.6: (classe, superclasse direta, exit_code).
HIERARCHY: list[tuple[type[RPAError], type[Exception], int]] = [
    (RPAError, Exception, 1),
    (ConfigurationError, RPAError, 3),
    (InvalidEnvironmentError, ConfigurationError, 2),
    (InvalidProjectNameError, ConfigurationError, 2),
    (ProjectNotFoundError, ConfigurationError, 3),
    (ProjectAlreadyExistsError, ConfigurationError, 3),
    (ProjectStructureError, ConfigurationError, 3),
    (TemplateNotFoundError, ConfigurationError, 3),
    (TemplateRenderError, ConfigurationError, 3),
    (ProjectAlreadyRunningError, RPAError, 6),
    (ExecutionTimeoutError, RPAError, 5),
    (VaultError, RPAError, 3),
    (VaultUnavailableError, VaultError, 3),
    (VaultAuthenticationError, VaultError, 3),
    (SecretNotFoundError, VaultError, 3),
    (BusinessRuleError, RPAError, 4),
    (WorkflowError, RPAError, 1),
    (StepLoadError, WorkflowError, 3),
    (BrowserError, RPAError, 1),
    (BrowserNotSupportedError, BrowserError, 3),
    (DriverInitializationError, BrowserError, 1),
    (BrowserNotStartedError, BrowserError, 1),
    (ElementNotFoundError, BrowserError, 1),
    (DownloadTimeoutError, BrowserError, 1),
    (DesktopAutomationError, RPAError, 1),
    (ImageNotFoundError, DesktopAutomationError, 1),
    (WindowNotFoundError, DesktopAutomationError, 1),
    (UtilsError, RPAError, 1),
    (FileOperationError, UtilsError, 1),
    (DataFormatError, UtilsError, 1),
    (ArchiveError, UtilsError, 1),
    (CryptoError, UtilsError, 1),
    (DatabaseError, UtilsError, 1),
    (EmailError, UtilsError, 1),
    (ImageProcessingError, UtilsError, 1),
    (PDFError, UtilsError, 1),
]


def test_exit_code_values() -> None:
    """Seção 9.5: valores numéricos de ExitCode."""
    assert {code.name: int(code) for code in ExitCode} == {
        "SUCCESS": 0,
        "TECHNICAL_ERROR": 1,
        "USAGE_ERROR": 2,
        "CONFIGURATION_ERROR": 3,
        "BUSINESS_ERROR": 4,
        "TIMEOUT": 5,
        "ALREADY_RUNNING": 6,
        "INTERRUPTED": 130,
    }


def test_str_with_details() -> None:
    """RF-009: str(RPAError('Falha', details={'arquivo': 'a.csv'})) == 'Falha | arquivo=a.csv'."""
    assert str(RPAError("Falha", details={"arquivo": "a.csv"})) == "Falha | arquivo=a.csv"


def test_str_without_details() -> None:
    """RF-009: sem details, str retorna apenas a mensagem."""
    error = RPAError("Falha simples")
    assert str(error) == "Falha simples"
    assert error.message == "Falha simples"
    assert error.details == {}
    assert error.args == ("Falha simples",)


def test_str_with_multiple_details_keeps_insertion_order() -> None:
    """RF-009: vários details separados por vírgula, na ordem de inserção."""
    error = BusinessRuleError("CNPJ inválido", details={"cnpj": "00.000", "linha": 7, "a": None})
    assert str(error) == "CNPJ inválido | cnpj=00.000, linha=7, a=None"


def test_details_are_copied() -> None:
    """RF-009: details é uma cópia independente do mapeamento recebido."""
    original = {"arquivo": "a.csv"}
    error = RPAError("Falha", details=original)
    original["arquivo"] = "b.csv"
    assert error.details == {"arquivo": "a.csv"}
    assert isinstance(error.details, dict)


@pytest.mark.parametrize(
    ("cls", "parent", "code"), HIERARCHY, ids=[cls.__name__ for cls, _, _ in HIERARCHY]
)
def test_hierarchy_and_exit_codes(cls: type[RPAError], parent: type[Exception], code: int) -> None:
    """RF-009: cada classe da seção 8.6 tem a superclasse e o exit_code da tabela."""
    assert cls.__bases__ == (parent,)
    assert issubclass(cls, RPAError)
    assert cls.exit_code == code
    assert isinstance(cls.exit_code, ExitCode)
    instance = cls("mensagem", details={"chave": "valor"})
    assert instance.exit_code == code
    assert exit_code_for(instance) is ExitCode(code)
    assert str(instance) == "mensagem | chave=valor"
    assert cls.__doc__


def test_module_exports_every_exception_of_the_table() -> None:
    """RF-009: todas as exceções da tabela 8.6 estão no módulo e em __all__."""
    exported = set(exc_module.__all__)
    for cls, _, _ in HIERARCHY:
        assert cls.__name__ in exported
        assert getattr(exc_module, cls.__name__) is cls
    defined = {
        name
        for name, value in vars(exc_module).items()
        if isinstance(value, type) and issubclass(value, RPAError)
    }
    assert defined == {cls.__name__ for cls, _, _ in HIERARCHY}


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (None, ExitCode.SUCCESS),
        (KeyboardInterrupt(), ExitCode.INTERRUPTED),
        (ValueError(), ExitCode.TECHNICAL_ERROR),
        (RuntimeError("x"), ExitCode.TECHNICAL_ERROR),
        (SystemExit(2), ExitCode.TECHNICAL_ERROR),
        (BusinessRuleError("x"), ExitCode.BUSINESS_ERROR),
        (VaultUnavailableError("x"), ExitCode.CONFIGURATION_ERROR),
        (InvalidEnvironmentError("x"), ExitCode.USAGE_ERROR),
        (ProjectAlreadyRunningError("x"), ExitCode.ALREADY_RUNNING),
    ],
    ids=lambda value: type(value).__name__,
)
def test_exit_code_for(error: BaseException | None, expected: ExitCode) -> None:
    """RF-009: exit_code_for(KeyboardInterrupt()) é INTERRUPTED; ValueError é TECHNICAL_ERROR."""
    assert exit_code_for(error) is expected


def test_business_rule_error_example() -> None:
    """Seção 8.6: exemplo de uso com BusinessRuleError."""
    try:
        raise BusinessRuleError("CNPJ inválido", details={"cnpj": "00.000.000/0000-00"})
    except BusinessRuleError as exc:
        assert exit_code_for(exc) is ExitCode.BUSINESS_ERROR
        assert str(exc) == "CNPJ inválido | cnpj=00.000.000/0000-00"
