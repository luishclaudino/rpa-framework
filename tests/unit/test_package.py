"""Testes da API exportada por `rpa_3003`, `rpa_3003.core` e `rpa_3003.utils` (seções 8.1, 8.3 e
8.9)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import rpa_3003
import rpa_3003.core
from rpa_3003.core import config, exceptions, logger

pytestmark = pytest.mark.unit


def test_root_package_exports() -> None:
    """Seção 8.1: rpa_3003 exporta __version__, Environment, get_logger e setup_logger."""
    assert rpa_3003.__all__ == ["Environment", "__version__", "get_logger", "setup_logger"]
    assert rpa_3003.__version__ == "1.0.0"
    assert rpa_3003.Environment is config.Environment
    assert rpa_3003.get_logger is logger.get_logger
    assert rpa_3003.setup_logger is logger.setup_logger


def test_core_package_exports() -> None:
    """Seção 8.3: rpa_3003.core reexporta a API estável de exceptions, logger e config."""
    expected = {
        "BusinessRuleError": exceptions.BusinessRuleError,
        "ConfigurationError": exceptions.ConfigurationError,
        "Environment": config.Environment,
        "ExitCode": exceptions.ExitCode,
        "FrameworkSettings": config.FrameworkSettings,
        "ProjectPaths": config.ProjectPaths,
        "RPAError": exceptions.RPAError,
        "get_logger": logger.get_logger,
        "get_settings": config.get_settings,
        "load_settings": config.load_settings,
        "register_secret": logger.register_secret,
        "setup_logger": logger.setup_logger,
    }
    assert sorted(rpa_3003.core.__all__) == sorted(expected)
    for name, value in expected.items():
        assert getattr(rpa_3003.core, name) is value


def test_utils_package_declares_modules_without_importing_them() -> None:
    """Seção 8.9 e RNF-017: rpa_3003.utils lista os módulos em __all__ sem importá-los."""
    import rpa_3003.utils

    expected = [
        "archive_utils",
        "calendar_utils",
        "crypt_utils",
        "csv_utils",
        "database_utils",
        "email_utils",
        "filesystem_utils",
        "images_utils",
        "json_utils",
        "pdf_utils",
    ]
    assert rpa_3003.utils.__all__ == expected
    package_dir = Path(rpa_3003.utils.__file__).parent
    assert all((package_dir / f"{name}.py").is_file() for name in expected)
    code = (
        "import sys, rpa_3003.utils\n"
        "print(sorted(m for m in sys.modules if m.startswith('rpa_3003.utils.')))"
    )
    completed = subprocess.run(  # noqa: S603 - interpretador atual com código fixo
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60
    )
    assert completed.stdout.strip() == "[]"


def test_root_package_usage_example(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Seção 8.1: exemplo de uso do pacote."""
    from rpa_3003 import Environment, get_logger

    log = get_logger()
    log.info("rpa_3003 versão {} | ambiente padrão={}", rpa_3003.__version__, Environment.DEV)
    assert "rpa_3003 versão 1.0.0 | ambiente padrão=dev" in capsys.readouterr().err


def test_core_package_usage_example() -> None:
    """Seção 8.3: exemplo de validação com BusinessRuleError."""
    from rpa_3003.core import BusinessRuleError, get_logger

    def validar_valor(valor: float) -> None:
        if valor <= 0:
            raise BusinessRuleError("Valor do boleto inválido", details={"valor": valor})
        get_logger().debug("Valor válido: {}", valor)

    validar_valor(10.0)
    with pytest.raises(BusinessRuleError, match=r"Valor do boleto inválido \| valor=-1"):
        validar_valor(-1)
