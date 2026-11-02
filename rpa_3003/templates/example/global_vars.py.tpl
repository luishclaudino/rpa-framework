"""Variáveis globais do projeto ${project_name}.

NÃO coloque segredos neste arquivo. Credenciais são obtidas do HashiCorp Vault em tempo de
execução por get_credentials(). Este módulo não faz I/O ao ser importado.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from rpa_3003.core.config import Environment
from rpa_3003.core.vault import Credentials, get_vault_client

PROJECT_NAME: Final[str] = "${project_name}"
PROJECT_TITLE: Final[str] = "${project_title}"
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent
DATA_DIR: Final[Path] = PROJECT_ROOT / "data"
INPUT_DIR: Final[Path] = DATA_DIR / "input"
OUTPUT_DIR: Final[Path] = DATA_DIR / "output"
IMAGES_DIR: Final[Path] = DATA_DIR / "images"
LOGS_DIR: Final[Path] = PROJECT_ROOT / "logs"


@dataclass(frozen=True, slots=True)
class EnvConfig:
    """Parâmetros NÃO sensíveis de cada ambiente."""

    base_url: str
    vault_secret_path: str | None
    browser: str
    headless: bool
    browser_enabled: bool
    email_recipients: tuple[str, ...]


ENV_CONFIG: Final[dict[Environment, EnvConfig]] = {
    Environment.DEV: EnvConfig(
        base_url="https://example.com",
        vault_secret_path=None,
        browser="chrome",
        headless=False,
        browser_enabled=False,
        email_recipients=(),
    ),
    Environment.PRD: EnvConfig(
        base_url="https://example.com",
        # pragma: allowlist nextline secret (caminho do segredo no Vault, não é segredo)
        vault_secret_path="rpa/${project_name}/prd/sistema",
        browser="edge",
        headless=False,
        browser_enabled=False,
        email_recipients=(),
    ),
}


def get_env_config(env: Environment) -> EnvConfig:
    """Retorna a configuração do ambiente."""
    return ENV_CONFIG[env]


def get_credentials(env: Environment) -> Credentials | None:
    """Lê as credenciais do Vault; None quando o ambiente não define vault_secret_path."""
    path = get_env_config(env).vault_secret_path
    if path is None:
        return None
    return get_vault_client(env).get_credentials(path)
