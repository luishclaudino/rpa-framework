"""Integração com HashiCorp Vault real (RF-018, RF-020 a RF-022, RF-025, seção 8.7, P-07).

Requer `RPA3003_TEST_VAULT_URL`, `RPA3003_TEST_VAULT_TOKEN` e `RPA3003_TEST_VAULT_PATH`; sem
elas, os testes são pulados (seção 12.4). Execute com `pytest -m integration`.

O segredo em `RPA3003_TEST_VAULT_PATH` (KV v2) deve ter as chaves `username` e `password`, com a
senha de pelo menos 4 caracteres (mínimo do mascaramento). Variáveis opcionais
(`docs/DESVIOS.md`, D-13):

- `RPA3003_TEST_VAULT_ROLE_ID` e `RPA3003_TEST_VAULT_SECRET_ID`: habilitam os testes de AppRole,
  o método padrão dos robôs (P-07). A política do AppRole deve permitir ler o segredo.
- `RPA3003_TEST_VAULT_MOUNT_POINT`: ponto de montagem do KV v2 (padrão `secret`).

A verificação de certificado TLS fica ativa (RNF-009). Para uma CA corporativa, defina
`REQUESTS_CA_BUNDLE` com o caminho do bundle antes de executar.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from pytest_mock import MockerFixture

from rpa_3003.core.config import Environment, VaultAuthMethod, VaultSettings
from rpa_3003.core.exceptions import (
    SecretNotFoundError,
    VaultAuthenticationError,
    VaultUnavailableError,
)
from rpa_3003.core.logger import MASK, get_logger, setup_logger
from rpa_3003.core.vault import (
    Credentials,
    HashiCorpVaultProvider,
    VaultClient,
    get_vault_client,
)

pytestmark = pytest.mark.integration

PREFIX = "RPA3003_TEST_VAULT_"
REQUIRED = ("URL", "TOKEN", "PATH")
OPTIONAL = ("ROLE_ID", "SECRET_ID", "MOUNT_POINT")
DEFAULT_MOUNT_POINT = "secret"


@dataclass(frozen=True, slots=True)
class LiveVault:
    """Parâmetros do Vault de homologação lidos das variáveis `RPA3003_TEST_VAULT_*`."""

    url: str
    token: str
    path: str
    mount_point: str
    role_id: str
    secret_id: str

    @property
    def has_approle(self) -> bool:
        """Indica se `ROLE_ID` e `SECRET_ID` foram informados."""
        return bool(self.role_id and self.secret_id)


@pytest.fixture
def live() -> LiveVault:
    """Parâmetros do Vault; pula o teste se faltar alguma variável obrigatória."""
    values = {name: os.environ.get(PREFIX + name, "").strip() for name in (*REQUIRED, *OPTIONAL)}
    missing = [PREFIX + name for name in REQUIRED if not values[name]]
    if missing:
        pytest.skip(f"variáveis ausentes: {', '.join(missing)}")
    return LiveVault(
        url=values["URL"],
        token=values["TOKEN"],
        path=values["PATH"],
        mount_point=values["MOUNT_POINT"] or DEFAULT_MOUNT_POINT,
        role_id=values["ROLE_ID"],
        secret_id=values["SECRET_ID"],
    )


@pytest.fixture
def approle(live: LiveVault) -> LiveVault:
    """Parâmetros do Vault com AppRole; pula o teste sem `ROLE_ID` e `SECRET_ID`."""
    if not live.has_approle:
        pytest.skip(f"variáveis ausentes: {PREFIX}ROLE_ID, {PREFIX}SECRET_ID")
    return live


def _settings(live: LiveVault, method: VaultAuthMethod, **overrides: Any) -> VaultSettings:
    """Configuração KV v2 sem `.env`; as credenciais dependem do método de autenticação."""
    values: dict[str, Any] = {
        "url": live.url,
        "auth_method": method,
        "mount_point": live.mount_point,
        "kv_version": 2,
    }
    if method is VaultAuthMethod.TOKEN:
        values["token"] = SecretStr(live.token)
    else:
        values["role_id"] = live.role_id
        values["secret_id"] = SecretStr(live.secret_id)
    values.update(overrides)
    return VaultSettings(_env_file=None, **values)  # type: ignore[call-arg]


def _client(live: LiveVault, method: VaultAuthMethod, **overrides: Any) -> VaultClient:
    return VaultClient(_settings(live, method, **overrides), env=Environment.PRD)


def test_vault_is_available(live: LiveVault) -> None:
    """RF-022: `is_available()` retorna True para o Vault de homologação (inicializado e não
    selado)."""
    assert _client(live, VaultAuthMethod.TOKEN).is_available() is True


def test_token_reads_kv_v2_and_masks_values(
    live: LiveVault, tmp_path: Path, read_log_lines: Callable[[Path], list[str]]
) -> None:
    """RF-020/RF-021/RF-018: autenticação por token, leitura KV v2 com valores `str` e
    mascaramento automático dos valores lidos."""
    setup_logger(tmp_path, user="teste", console=False, file_prefix="vault_live")
    data = _client(live, VaultAuthMethod.TOKEN).read_secret(live.path)
    assert {"username", "password"} <= set(data)
    assert all(isinstance(value, str) for value in data.values())
    get_logger().info("Valor lido | senha={}", data["password"])
    lines = read_log_lines(tmp_path)
    assert any(f"senha={MASK}" in line for line in lines)
    assert not any(data["password"] in line for line in lines)


def test_approle_get_credentials(approle: LiveVault) -> None:
    """RF-020/RF-021/RNF-008: AppRole e KV v2 retornam `Credentials` com a senha em `SecretStr`,
    fora do `repr()`."""
    credentials = _client(approle, VaultAuthMethod.APPROLE).get_credentials(approle.path)
    assert isinstance(credentials, Credentials)
    assert credentials.username
    assert isinstance(credentials.password, SecretStr)
    secret = credentials.password.get_secret_value()
    assert secret
    assert secret not in repr(credentials)


def test_get_vault_client_reads_settings_from_environment(
    live: LiveVault, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """RF-025/UC-02: `get_vault_client(prd)` lê a configuração das variáveis `RPA3003_VAULT_*`
    (AppRole quando disponível) e retorna as credenciais do caminho de `global_vars.py`."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RPA3003_VAULT_URL", live.url)
    monkeypatch.setenv("RPA3003_VAULT_MOUNT_POINT", live.mount_point)
    if live.has_approle:
        monkeypatch.setenv("RPA3003_VAULT_AUTH_METHOD", "approle")
        monkeypatch.setenv("RPA3003_VAULT_ROLE_ID", live.role_id)
        monkeypatch.setenv("RPA3003_VAULT_SECRET_ID", live.secret_id)
    else:
        monkeypatch.setenv("RPA3003_VAULT_AUTH_METHOD", "token")
        monkeypatch.setenv("RPA3003_VAULT_TOKEN", live.token)
    client = get_vault_client(Environment.PRD)
    assert client is get_vault_client(Environment.PRD)
    assert client.get_credentials(live.path).username


def test_memory_cache_avoids_second_request(live: LiveVault, mocker: MockerFixture) -> None:
    """RF-022: a segunda leitura vem do cache em memória; `use_cache=False` consulta o Vault."""
    spy = mocker.spy(HashiCorpVaultProvider, "read_secret")
    client = _client(live, VaultAuthMethod.TOKEN, cache_ttl=300)
    first = client.read_secret(live.path)
    assert client.read_secret(live.path) == first
    assert spy.call_count == 1
    assert client.read_secret(live.path, use_cache=False) == first
    assert spy.call_count == 2


def test_missing_key_lists_only_key_names(live: LiveVault) -> None:
    """RF-021: chave ausente gera `SecretNotFoundError` com os nomes das chaves, sem valores."""
    client = _client(live, VaultAuthMethod.TOKEN)
    data = client.read_secret(live.path)
    missing_key = f"rpa3003_inexistente_{uuid.uuid4().hex[:8]}"
    with pytest.raises(SecretNotFoundError) as info:
        client.get_secret(live.path, missing_key)
    assert missing_key in str(info.value)
    assert "password" in info.value.details["chaves_disponiveis"]
    assert data["password"] not in str(info.value)


def test_invalid_token_raises_authentication_error(live: LiveVault) -> None:
    """RF-020: token inválido gera `VaultAuthenticationError` (código de saída 3)."""
    client = _client(live, VaultAuthMethod.TOKEN, token=SecretStr(f"hvs.{uuid.uuid4().hex}"))
    with pytest.raises(VaultAuthenticationError) as info:
        client.read_secret(live.path)
    assert int(info.value.exit_code) == 3


def test_invalid_secret_id_raises_authentication_error(approle: LiveVault) -> None:
    """RF-020: AppRole com `secret_id` inválido gera `VaultAuthenticationError`."""
    client = _client(approle, VaultAuthMethod.APPROLE, secret_id=SecretStr(str(uuid.uuid4())))
    with pytest.raises(VaultAuthenticationError):
        client.read_secret(approle.path)


def test_unreachable_vault_retries_with_backoff(no_sleep: list[float]) -> None:
    """RF-022/RNF-007: sem resposta (conexão recusada), 3 tentativas com esperas de 1 s e 2 s e
    `VaultUnavailableError` no fim. Não depende das variáveis `RPA3003_TEST_VAULT_*`."""
    settings = VaultSettings(  # type: ignore[call-arg]
        _env_file=None,
        url="http://127.0.0.1:9",
        auth_method=VaultAuthMethod.APPROLE,
        role_id="role-inexistente",
        secret_id=SecretStr("secret-inexistente"),
        timeout=2,
    )
    client = VaultClient(settings, env=Environment.PRD)
    with pytest.raises(VaultUnavailableError) as info:
        client.read_secret("rpa/inexistente/prd/sistema")
    assert int(info.value.exit_code) == 3
    assert no_sleep == [1.0, 2.0]
