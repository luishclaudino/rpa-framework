"""Testes de `rpa_3003.core.vault` (RF-018, RF-020 a RF-024, RNF-007, RNF-008, seção 8.7)."""

# Credenciais e tokens deste arquivo são fictícios (valores de teste, não segredos).
# ruff: noqa: S105, S106

from __future__ import annotations

import hashlib
import json
import sys
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import hvac
import pytest
import requests
import time_machine
from pydantic import SecretStr
from pytest_mock import MockerFixture

from rpa_3003.core.config import Environment, VaultAuthMethod, VaultSettings
from rpa_3003.core.exceptions import (
    ConfigurationError,
    ExitCode,
    SecretNotFoundError,
    VaultAuthenticationError,
    VaultError,
    VaultUnavailableError,
)
from rpa_3003.core.logger import MASK, get_logger, setup_logger
from rpa_3003.core.vault import (
    Credentials,
    HashiCorpVaultProvider,
    LocalFileSecretProvider,
    OfflineSecretCache,
    SecretProvider,
    VaultClient,
    get_vault_client,
    reset_vault_clients,
)

pytestmark = pytest.mark.unit

ReadLines = Callable[[Path], list[str]]
SECRET_PATH = "rpa/emissao_notas/prd/sistema_legado"
URL = "https://vault.empresa.local:8200"


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pasta de trabalho vazia (sem `.env`) e LOCALAPPDATA dentro de tmp_path."""
    work = tmp_path / "cwd"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))


@pytest.fixture
def logs_dir(tmp_path: Path) -> Path:
    """Logger em arquivo para verificar mensagens e mascaramento."""
    path = tmp_path / "logs"
    setup_logger(path, user="teste", level="DEBUG", console=False)
    return path


@pytest.fixture
def fake_dpapi(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Substitui `win32crypt` por uma cifra reversível (DPAPI simulado)."""

    def protect(data: bytes, description: str, *_args: object) -> bytes:
        return b"DPAPI" + bytes(byte ^ 0x5A for byte in data)

    def unprotect(blob: bytes, *_args: object) -> tuple[str, bytes]:
        if not blob.startswith(b"DPAPI"):
            raise RuntimeError("blob inválido")  # simula pywintypes.error
        return "rpa_3003 vault cache", bytes(byte ^ 0x5A for byte in blob[5:])

    module = SimpleNamespace(CryptProtectData=protect, CryptUnprotectData=unprotect)
    monkeypatch.setitem(sys.modules, "win32crypt", module)
    return module


def vault_settings(**overrides: Any) -> VaultSettings:
    """VaultSettings de AppRole válido, com sobrescritas."""
    values: dict[str, Any] = {"url": URL, "role_id": "role-123", "secret_id": "secret-id-456"}
    values.update(overrides)
    return VaultSettings(**values)


def client_of(fake_hvac: MagicMock) -> MagicMock:
    client: MagicMock = fake_hvac.return_value
    return client


def kv2(client: MagicMock) -> MagicMock:
    reader: MagicMock = client.secrets.kv.v2.read_secret_version
    return reader


def kv2_response(**data: object) -> dict[str, Any]:
    return {"data": {"data": data, "metadata": {"version": 1}}}


# --- RF-020: autenticação ----------------------------------------------------------------------


def test_approle_logs_in_once_for_three_reads(fake_hvac: MagicMock) -> None:
    """RF-020: AppRole com role_id e secret_id autentica uma única vez para três leituras."""
    vault = VaultClient(vault_settings())
    for path in ("rpa/a", "rpa/b", "rpa/c"):
        vault.read_secret(path)
    client = client_of(fake_hvac)
    client.auth.approle.login.assert_called_once_with(role_id="role-123", secret_id="secret-id-456")
    assert kv2(client).call_count == 3
    fake_hvac.assert_called_once()


def test_authentication_is_lazy(fake_hvac: MagicMock) -> None:
    """RF-020: criar o cliente não acessa o Vault; a autenticação ocorre na primeira leitura."""
    vault = VaultClient(vault_settings())
    fake_hvac.assert_not_called()
    vault.read_secret("rpa/a")
    fake_hvac.assert_called_once()


def test_hvac_client_receives_tls_namespace_and_timeout(fake_hvac: MagicMock) -> None:
    """RF-020 e RNF-007: URL, namespace, verificação TLS e timeout explícito no hvac.Client."""
    VaultClient(vault_settings(namespace="financeiro")).read_secret("rpa/a")
    fake_hvac.assert_called_once_with(
        url=URL, token=None, namespace="financeiro", verify=True, timeout=10
    )


def test_ca_bundle_path_is_passed_as_string(fake_hvac: MagicMock, tmp_path: Path) -> None:
    """RF-020: caminho de CA em `verify` é repassado ao hvac como texto."""
    ca_bundle = tmp_path / "ca.pem"
    VaultClient(vault_settings(verify=ca_bundle, timeout=7)).read_secret("rpa/a")
    assert fake_hvac.call_args.kwargs["verify"] == str(ca_bundle)
    assert fake_hvac.call_args.kwargs["timeout"] == 7


def test_token_authentication(fake_hvac: MagicMock) -> None:
    """RF-020: método token cria o cliente com o token e verifica is_authenticated()."""
    settings = VaultSettings(url=URL, auth_method="token", token="s.token-valido")
    VaultClient(settings).read_secret("rpa/a")
    assert fake_hvac.call_args.kwargs["token"] == "s.token-valido"
    client_of(fake_hvac).is_authenticated.assert_called_once_with()


def test_invalid_token_raises_authentication_error(fake_hvac: MagicMock) -> None:
    """RF-020: token inválido gera VaultAuthenticationError (código 3)."""
    client_of(fake_hvac).is_authenticated.return_value = False
    settings = VaultSettings(url=URL, auth_method="token", token="s.token-invalido")
    with pytest.raises(VaultAuthenticationError) as info:
        VaultClient(settings).read_secret("rpa/a")
    assert info.value.exit_code is ExitCode.CONFIGURATION_ERROR
    kv2(client_of(fake_hvac)).assert_not_called()


@pytest.mark.parametrize("method", ["ldap", "userpass"])
def test_username_password_methods(fake_hvac: MagicMock, method: str) -> None:
    """RF-020: ldap e userpass autenticam com usuário e senha."""
    settings = VaultSettings(url=URL, auth_method=method, username="svc", password="senha-svc")
    VaultClient(settings).read_secret("rpa/a")
    login = getattr(client_of(fake_hvac).auth, method).login
    login.assert_called_once_with(username="svc", password="senha-svc")


@pytest.mark.parametrize(
    "error",
    [hvac.exceptions.InvalidRequest(), hvac.exceptions.Forbidden(), hvac.exceptions.Unauthorized()],
)
def test_rejected_login_raises_authentication_error(
    fake_hvac: MagicMock, error: Exception, no_sleep: list[float]
) -> None:
    """RF-020: login recusado pelo Vault gera VaultAuthenticationError sem novas tentativas."""
    client_of(fake_hvac).auth.approle.login.side_effect = error
    with pytest.raises(VaultAuthenticationError) as info:
        VaultClient(vault_settings()).read_secret("rpa/a")
    assert info.value.__cause__ is error
    assert no_sleep == []


@pytest.mark.parametrize(
    ("method", "overrides", "expected"),
    [
        ("approle", {"role_id": "r"}, ["RPA3003_VAULT_SECRET_ID"]),
        ("approle", {}, ["RPA3003_VAULT_ROLE_ID", "RPA3003_VAULT_SECRET_ID"]),
        ("token", {}, ["RPA3003_VAULT_TOKEN"]),
        ("ldap", {"username": "svc"}, ["RPA3003_VAULT_PASSWORD"]),
        ("userpass", {"password": "x"}, ["RPA3003_VAULT_USERNAME"]),
        ("approle", {"url": None, "role_id": "r", "secret_id": "s"}, ["RPA3003_VAULT_URL"]),
        ("approle", {"role_id": "r", "secret_id": ""}, ["RPA3003_VAULT_SECRET_ID"]),
    ],
)
def test_missing_parameters_raise_configuration_error(
    method: str, overrides: dict[str, Any], expected: list[str]
) -> None:
    """RF-020: AppRole sem secret_id gera ConfigurationError citando RPA3003_VAULT_SECRET_ID."""
    values: dict[str, Any] = {"url": URL, "auth_method": method, **overrides}
    with pytest.raises(ConfigurationError) as info:
        HashiCorpVaultProvider(VaultSettings(**values))
    assert info.value.details["variaveis"] == expected
    for variable in expected:
        assert variable in str(info.value)


def test_explicit_authenticate_maps_connection_error(fake_hvac: MagicMock) -> None:
    """RF-020: authenticate() direto converte falha de rede em VaultUnavailableError."""
    client_of(fake_hvac).auth.approle.login.side_effect = requests.exceptions.ConnectionError("x")
    with pytest.raises(VaultUnavailableError):
        HashiCorpVaultProvider(vault_settings()).authenticate()


def test_providers_implement_secret_provider(tmp_path: Path) -> None:
    """Seção 7.5.4: provedores satisfazem o protocolo SecretProvider."""
    assert isinstance(HashiCorpVaultProvider(vault_settings()), SecretProvider)
    assert isinstance(LocalFileSecretProvider(tmp_path / "x.json"), SecretProvider)


# --- RF-021: leitura KV v1/v2 ------------------------------------------------------------------


def test_kv2_read_converts_values_to_str(fake_hvac: MagicMock) -> None:
    """RF-021: KV v2 usa read_secret_version(raise_on_deleted_version=True) e retorna str."""
    kv2(client_of(fake_hvac)).return_value = kv2_response(usuario="robo", porta=5432, ativo=True)
    data = VaultClient(vault_settings(mount_point="kv-rpa")).read_secret(SECRET_PATH)
    assert data == {"usuario": "robo", "porta": "5432", "ativo": "True"}
    kv2(client_of(fake_hvac)).assert_called_once_with(
        path=SECRET_PATH, mount_point="kv-rpa", raise_on_deleted_version=True
    )


def test_kv1_read(fake_hvac: MagicMock) -> None:
    """RF-021: KV v1 usa secrets.kv.v1.read_secret."""
    client = client_of(fake_hvac)
    client.secrets.kv.v1.read_secret.return_value = {"data": {"api_key": "chave-v1-abcd"}}
    data = VaultClient(vault_settings(kv_version=1)).read_secret("legado/api")
    assert data == {"api_key": "chave-v1-abcd"}
    client.secrets.kv.v1.read_secret.assert_called_once_with(
        path="legado/api", mount_point="secret"
    )
    kv2(client).assert_not_called()


def test_missing_path_raises_secret_not_found(fake_hvac: MagicMock, no_sleep: list[float]) -> None:
    """RF-021: caminho inexistente gera SecretNotFoundError com o caminho em details."""
    kv2(client_of(fake_hvac)).side_effect = hvac.exceptions.InvalidPath()
    with pytest.raises(SecretNotFoundError) as info:
        VaultClient(vault_settings()).read_secret("rpa/nao/existe")
    assert info.value.details["caminho"] == "rpa/nao/existe"
    assert info.value.exit_code is ExitCode.CONFIGURATION_ERROR
    assert "S3nh@Forte" not in str(info.value)
    assert no_sleep == []


def test_missing_key_lists_only_key_names(fake_hvac: MagicMock) -> None:
    """RF-021: chave inexistente lista apenas os nomes das chaves disponíveis."""
    vault = VaultClient(vault_settings())
    with pytest.raises(SecretNotFoundError) as info:
        vault.get_secret(SECRET_PATH, "token")
    message = str(info.value)
    assert "token" in message
    assert "password" in message and "username" in message
    assert "S3nh@Forte" not in message and "robo.teste" not in message


def test_get_secret_returns_value(fake_hvac: MagicMock) -> None:
    """RF-021: get_secret retorna o valor de uma chave."""
    assert VaultClient(vault_settings()).get_secret(SECRET_PATH, "username") == "robo.teste"


def test_get_credentials(fake_hvac: MagicMock) -> None:
    """RF-021 e RNF-008: get_credentials retorna Credentials e repr() não mostra a senha."""
    credentials = VaultClient(vault_settings()).get_credentials(SECRET_PATH)
    assert isinstance(credentials, Credentials)
    assert credentials.username == "robo.teste"
    assert isinstance(credentials.password, SecretStr)
    assert credentials.password.get_secret_value() == "S3nh@Forte"
    assert "S3nh@Forte" not in repr(credentials)
    assert "S3nh@Forte" not in str(credentials)


def test_get_credentials_with_custom_keys(fake_hvac: MagicMock) -> None:
    """RF-021: get_credentials aceita nomes de chave personalizados."""
    kv2(client_of(fake_hvac)).return_value = kv2_response(login="ana", senha="senha-da-ana")
    credentials = VaultClient(vault_settings()).get_credentials(
        SECRET_PATH, username_key="login", password_key="senha"
    )
    assert credentials.username == "ana"
    assert credentials.password.get_secret_value() == "senha-da-ana"


def test_get_credentials_with_missing_keys(fake_hvac: MagicMock) -> None:
    """RF-021: chaves de usuário/senha ausentes geram SecretNotFoundError sem valores."""
    kv2(client_of(fake_hvac)).return_value = kv2_response(token="tok-123456")
    with pytest.raises(SecretNotFoundError) as info:
        VaultClient(vault_settings()).get_credentials(SECRET_PATH)
    assert "username" in str(info.value) and "password" in str(info.value)
    assert "tok-123456" not in str(info.value)


@pytest.mark.parametrize(
    "response",
    [{"data": None}, {"sem_data": {}}, {"data": {"data": ["lista"]}}],
)
def test_unexpected_response_raises_vault_error(fake_hvac: MagicMock, response: object) -> None:
    """Seção 7.7: resposta malformada do hvac vira VaultError."""
    kv2(client_of(fake_hvac)).return_value = response
    with pytest.raises(VaultError):
        VaultClient(vault_settings()).read_secret(SECRET_PATH)


def test_other_hvac_error_raises_vault_error(fake_hvac: MagicMock) -> None:
    """Seção 7.7: outros erros do hvac na leitura viram VaultError."""
    kv2(client_of(fake_hvac)).side_effect = hvac.exceptions.InvalidRequest("pedido inválido")
    with pytest.raises(VaultError) as info:
        VaultClient(vault_settings()).read_secret(SECRET_PATH)
    assert type(info.value) is VaultError


# --- RF-022: retentativas, reautenticação e cache em memória -----------------------------------


def test_retries_with_exponential_backoff(
    fake_hvac: MagicMock, no_sleep: list[float], logs_dir: Path, read_log_lines: ReadLines
) -> None:
    """RF-022: falha 2 vezes e depois responde, com esperas de 1 s e 2 s."""
    kv2(client_of(fake_hvac)).side_effect = [
        requests.exceptions.ConnectionError("recusada"),
        requests.exceptions.Timeout("lento"),
        kv2_response(usuario="robo"),
    ]
    assert VaultClient(vault_settings()).read_secret(SECRET_PATH) == {"usuario": "robo"}
    assert no_sleep == [1.0, 2.0]
    warnings = [line for line in read_log_lines(logs_dir) if "[WARNING]" in line]
    assert len(warnings) == 2
    assert "Vault indisponível (tentativa 1/3): ConnectionError: recusada" in warnings[0]
    assert "Vault indisponível (tentativa 2/3): Timeout: lento" in warnings[1]


def test_always_failing_raises_unavailable_after_three_attempts(
    fake_hvac: MagicMock, no_sleep: list[float]
) -> None:
    """RF-022: mock que sempre falha gera VaultUnavailableError após 3 tentativas."""
    error = requests.exceptions.ConnectionError("fora do ar")
    kv2(client_of(fake_hvac)).side_effect = error
    with pytest.raises(VaultUnavailableError) as info:
        VaultClient(vault_settings()).read_secret(SECRET_PATH)
    assert kv2(client_of(fake_hvac)).call_count == 3
    assert no_sleep == [1.0, 2.0]
    assert info.value.__cause__ is error
    assert info.value.exit_code is ExitCode.CONFIGURATION_ERROR


@pytest.mark.parametrize(
    "error",
    [
        hvac.exceptions.VaultDown(),
        hvac.exceptions.InternalServerError(),
        hvac.exceptions.BadGateway(),
        requests.exceptions.ReadTimeout(),
    ],
    ids=lambda error: type(error).__name__,
)
def test_server_errors_are_retried(
    fake_hvac: MagicMock, no_sleep: list[float], error: Exception
) -> None:
    """RF-022: HTTP 5xx e timeout são retentados."""
    kv2(client_of(fake_hvac)).side_effect = [error, kv2_response(a="valor-a")]
    assert VaultClient(vault_settings()).read_secret(SECRET_PATH) == {"a": "valor-a"}
    assert no_sleep == [1.0]


def test_connection_error_during_login_is_retried(
    fake_hvac: MagicMock, no_sleep: list[float]
) -> None:
    """RF-022: falha de rede na autenticação também é retentada."""
    client_of(fake_hvac).auth.approle.login.side_effect = [
        requests.exceptions.ConnectionError("x"),
        None,
    ]
    VaultClient(vault_settings()).read_secret(SECRET_PATH)
    assert no_sleep == [1.0]
    assert client_of(fake_hvac).auth.approle.login.call_count == 2


def test_custom_retries_and_backoff(fake_hvac: MagicMock, no_sleep: list[float]) -> None:
    """RF-022: max_retries e backoff_base controlam tentativas e esperas."""
    kv2(client_of(fake_hvac)).side_effect = hvac.exceptions.VaultDown()
    with pytest.raises(VaultUnavailableError):
        VaultClient(vault_settings(max_retries=4, backoff_base=0.5)).read_secret(SECRET_PATH)
    assert no_sleep == [0.5, 1.0, 2.0]


def test_forbidden_triggers_single_reauthentication(fake_hvac: MagicMock) -> None:
    """RF-022: 403 com token expirado provoca uma reautenticação e nova leitura."""
    kv2(client_of(fake_hvac)).side_effect = [hvac.exceptions.Forbidden(), kv2_response(a="ok-ok")]
    assert VaultClient(vault_settings()).read_secret(SECRET_PATH) == {"a": "ok-ok"}
    assert client_of(fake_hvac).auth.approle.login.call_count == 2


def test_second_forbidden_raises_authentication_error(
    fake_hvac: MagicMock, no_sleep: list[float]
) -> None:
    """RF-022: após a reautenticação, novo 401/403 gera VaultAuthenticationError."""
    kv2(client_of(fake_hvac)).side_effect = [
        hvac.exceptions.Forbidden(),
        hvac.exceptions.Unauthorized(),
    ]
    with pytest.raises(VaultAuthenticationError):
        VaultClient(vault_settings()).read_secret(SECRET_PATH)
    assert client_of(fake_hvac).auth.approle.login.call_count == 2
    assert no_sleep == []


def test_memory_cache_respects_ttl(fake_hvac: MagicMock) -> None:
    """RF-022: leitura dentro do TTL não chama a rede; após o TTL, chama de novo."""
    start = datetime(2026, 10, 31, 7, 0, 0).astimezone()
    with time_machine.travel(start, tick=False) as traveller:
        vault = VaultClient(vault_settings(cache_ttl=300))
        vault.read_secret(SECRET_PATH)
        traveller.move_to(start + timedelta(seconds=299))
        vault.read_secret(SECRET_PATH)
        assert kv2(client_of(fake_hvac)).call_count == 1
        traveller.move_to(start + timedelta(seconds=301))
        vault.read_secret(SECRET_PATH)
        assert kv2(client_of(fake_hvac)).call_count == 2


def test_cache_returns_copies(fake_hvac: MagicMock) -> None:
    """RF-022: o chamador não altera o conteúdo em cache."""
    vault = VaultClient(vault_settings())
    vault.read_secret(SECRET_PATH)["username"] = "alterado"
    assert vault.read_secret(SECRET_PATH)["username"] == "robo.teste"


def test_cache_is_keyed_by_mount_and_path(fake_hvac: MagicMock) -> None:
    """RF-022: caminhos diferentes não compartilham cache."""
    vault = VaultClient(vault_settings())
    vault.read_secret("rpa/a")
    vault.read_secret("rpa/b")
    vault.read_secret("rpa/a")
    assert kv2(client_of(fake_hvac)).call_count == 2


def test_cache_bypass_disable_and_clear(fake_hvac: MagicMock) -> None:
    """RF-022: use_cache=False, cache_ttl=0 e clear_cache() forçam nova leitura."""
    vault = VaultClient(vault_settings())
    vault.read_secret(SECRET_PATH)
    vault.read_secret(SECRET_PATH, use_cache=False)
    assert kv2(client_of(fake_hvac)).call_count == 2
    vault.clear_cache()
    vault.read_secret(SECRET_PATH)
    assert kv2(client_of(fake_hvac)).call_count == 3
    no_cache = VaultClient(vault_settings(cache_ttl=0))
    no_cache.read_secret(SECRET_PATH)
    no_cache.read_secret(SECRET_PATH)
    assert kv2(client_of(fake_hvac)).call_count == 5


# --- RF-018: mascaramento e log ----------------------------------------------------------------


def test_values_read_are_masked(
    fake_hvac: MagicMock, logs_dir: Path, read_log_lines: ReadLines
) -> None:
    """RF-018: segredo lido pelo VaultClient é mascarado quando aparece em mensagem."""
    password = VaultClient(vault_settings()).get_credentials(SECRET_PATH).password
    get_logger().info("Digitando senha {}", password.get_secret_value())
    lines = read_log_lines(logs_dir)
    assert lines[-1].endswith(f"Digitando senha {MASK}")
    assert "S3nh@Forte" not in "\n".join(lines)


def test_cached_values_are_registered_again(fake_hvac: MagicMock, logs_dir: Path) -> None:
    """RF-018: leituras do cache também registram os valores para mascaramento."""
    from rpa_3003.core.logger import clear_secrets

    vault = VaultClient(vault_settings())
    vault.read_secret(SECRET_PATH)
    clear_secrets()
    vault.read_secret(SECRET_PATH)
    records: list[str] = []
    from loguru import logger

    logger.add(lambda message: records.append(message.record["message"]))
    get_logger().info("S3nh@Forte")
    assert records == [MASK]


def test_debug_log_shows_path_and_key_count_only(
    fake_hvac: MagicMock, logs_dir: Path, read_log_lines: ReadLines
) -> None:
    """Seção 8.7: log DEBUG com caminho e quantidade de chaves, nunca valores."""
    VaultClient(vault_settings()).read_secret(SECRET_PATH)
    lines = read_log_lines(logs_dir)
    assert any(
        "[DEBUG] [vault.py]" in line
        and line.endswith(f"Segredo lido do Vault | caminho={SECRET_PATH} | chaves=2")
        for line in lines
    )
    assert "robo.teste" not in "\n".join(lines)


# --- is_available ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ({"initialized": True, "sealed": False}, True),
        ({"initialized": True, "sealed": True}, False),
        ({"initialized": False, "sealed": False}, False),
        (SimpleNamespace(status_code=200), True),
        (SimpleNamespace(status_code=503), False),
    ],
)
def test_is_available_checks_health(fake_hvac: MagicMock, status: object, expected: bool) -> None:
    """Seção 8.7: is_available() consulta sys/health (inicializado e não selado)."""
    client_of(fake_hvac).sys.read_health_status.return_value = status
    assert VaultClient(vault_settings()).is_available() is expected


@pytest.mark.parametrize(
    "error", [requests.exceptions.ConnectTimeout(), hvac.exceptions.VaultDown()]
)
def test_is_available_false_on_errors(fake_hvac: MagicMock, error: Exception) -> None:
    """Seção 8.7: falha de rede ou do Vault em sys/health resulta em False."""
    client_of(fake_hvac).sys.read_health_status.side_effect = error
    assert VaultClient(vault_settings()).is_available() is False


# --- RF-023: cache offline com DPAPI -----------------------------------------------------------


def _offline_file(tmp_path: Path, mount: str, path: str) -> Path:
    digest = hashlib.sha256(f"{mount}/{path}".encode()).hexdigest()
    return tmp_path / "localappdata" / "rpa_3003" / "vault_cache" / f"{digest}.bin"


def test_offline_cache_used_when_vault_is_down(
    fake_hvac: MagicMock,
    fake_dpapi: SimpleNamespace,
    no_sleep: list[float],
    tmp_path: Path,
    logs_dir: Path,
    read_log_lines: ReadLines,
) -> None:
    """RF-023: Vault indisponível e cache com 2 h retorna o valor com WARNING 'cache offline'."""
    start = datetime(2026, 10, 31, 5, 0, 0).astimezone()
    with time_machine.travel(start, tick=False) as traveller:
        VaultClient(vault_settings(offline_cache=True)).read_secret(SECRET_PATH)
        assert _offline_file(tmp_path, "secret", SECRET_PATH).is_file()
        traveller.move_to(start + timedelta(hours=2))
        kv2(client_of(fake_hvac)).side_effect = requests.exceptions.ConnectionError("fora")
        data = VaultClient(vault_settings(offline_cache=True)).read_secret(SECRET_PATH)
    assert data == {"username": "robo.teste", "password": "S3nh@Forte"}
    warnings = [line for line in read_log_lines(logs_dir) if "cache offline" in line]
    assert len(warnings) == 1 and "[WARNING]" in warnings[0]
    assert f"usando cache offline com 2.0h de idade | caminho={SECRET_PATH}" in warnings[0]


def test_offline_cache_older_than_ttl_is_not_used(
    fake_hvac: MagicMock, fake_dpapi: SimpleNamespace, no_sleep: list[float]
) -> None:
    """RF-023: cache com idade acima do TTL resulta em VaultUnavailableError."""
    start = datetime(2026, 10, 31, 5, 0, 0).astimezone()
    settings = vault_settings(offline_cache=True, offline_cache_ttl_hours=24)
    with time_machine.travel(start, tick=False) as traveller:
        VaultClient(settings).read_secret(SECRET_PATH)
        traveller.move_to(start + timedelta(hours=24, seconds=1))
        kv2(client_of(fake_hvac)).side_effect = hvac.exceptions.VaultDown()
        with pytest.raises(VaultUnavailableError):
            VaultClient(settings).read_secret(SECRET_PATH)


def test_offline_file_does_not_contain_plaintext(
    fake_hvac: MagicMock, fake_dpapi: SimpleNamespace, tmp_path: Path
) -> None:
    """RF-023: o .bin não contém os bytes do segredo em texto claro."""
    VaultClient(vault_settings(offline_cache=True)).read_secret(SECRET_PATH)
    content = _offline_file(tmp_path, "secret", SECRET_PATH).read_bytes()
    assert b"S3nh@Forte" not in content
    assert b"robo.teste" not in content
    assert content.startswith(b"DPAPI")


@pytest.mark.parametrize(
    "error", [hvac.exceptions.InvalidPath(), hvac.exceptions.Forbidden()], ids=["path", "auth"]
)
def test_offline_cache_not_used_for_auth_or_missing_secret(
    fake_hvac: MagicMock, fake_dpapi: SimpleNamespace, error: Exception
) -> None:
    """RF-023: o cache offline nunca é usado em erro de autenticação ou segredo inexistente."""
    VaultClient(vault_settings(offline_cache=True)).read_secret(SECRET_PATH)
    kv2(client_of(fake_hvac)).side_effect = error
    with pytest.raises((SecretNotFoundError, VaultAuthenticationError)):
        VaultClient(vault_settings(offline_cache=True)).read_secret(SECRET_PATH)


def test_offline_cache_disabled_by_default(
    fake_hvac: MagicMock, fake_dpapi: SimpleNamespace, tmp_path: Path
) -> None:
    """RF-023: sem RPA3003_VAULT_OFFLINE_CACHE, nada é gravado em disco."""
    VaultClient(vault_settings()).read_secret(SECRET_PATH)
    assert not (tmp_path / "localappdata").exists()


def test_offline_cache_get_put_clear(fake_dpapi: SimpleNamespace, tmp_path: Path) -> None:
    """RF-023: OfflineSecretCache grava, lê, ignora entradas corrompidas e limpa."""
    cache = OfflineSecretCache(tmp_path / "cache", ttl_hours=1)
    assert cache.get("secret/a") is None
    assert cache.clear() == 0
    cache.put("secret/a", {"k": "valor-a"})
    cache.put("secret/b", {"k": "valor-b"})
    assert cache.get("secret/a") == {"k": "valor-a"}
    corrupted = tmp_path / "cache" / f"{hashlib.sha256(b'secret/b').hexdigest()}.bin"
    corrupted.write_bytes(b"lixo")
    assert cache.get("secret/b") is None
    assert cache.clear() == 2
    assert cache.get("secret/a") is None


def test_offline_cache_write_failure_does_not_break_read(
    fake_hvac: MagicMock,
    mocker: MockerFixture,
    logs_dir: Path,
    read_log_lines: ReadLines,
) -> None:
    """RF-023: falha ao gravar o cache offline gera WARNING sem afetar a leitura."""
    mocker.patch.object(OfflineSecretCache, "put", side_effect=OSError("disco cheio"))
    assert VaultClient(vault_settings(offline_cache=True)).read_secret(SECRET_PATH)
    assert any(
        "Falha ao gravar cache offline do Vault" in line for line in read_log_lines(logs_dir)
    )


def test_offline_cache_requires_dpapi(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """RF-023: sem win32crypt, gravar no cache offline gera ConfigurationError."""
    monkeypatch.setitem(sys.modules, "win32crypt", None)
    with pytest.raises(ConfigurationError, match="DPAPI"):
        OfflineSecretCache(tmp_path, ttl_hours=1).put("secret/a", {"k": "v"})
    (tmp_path / f"{hashlib.sha256(b'secret/a').hexdigest()}.bin").write_bytes(b"x")
    assert OfflineSecretCache(tmp_path, ttl_hours=1).get("secret/a") is None


@pytest.mark.windows
def test_offline_cache_with_real_dpapi(tmp_path: Path) -> None:
    """RF-023: ida e volta com DPAPI real, sem texto claro no arquivo."""
    cache = OfflineSecretCache(tmp_path, ttl_hours=1)
    cache.put("secret/rpa/x", {"password": "S3nh@Forte-DPAPI"})
    assert cache.get("secret/rpa/x") == {"password": "S3nh@Forte-DPAPI"}
    (blob,) = tmp_path.glob("*.bin")
    assert b"S3nh@Forte-DPAPI" not in blob.read_bytes()
    assert "S3nh@Forte-DPAPI".encode("utf-16-le") not in blob.read_bytes()


# --- RF-024: provedor local em dev -------------------------------------------------------------


def _write_local_secrets(tmp_path: Path, content: object) -> Path:
    path = tmp_path / "dev.secrets.json"
    path.write_text(json.dumps(content), encoding="utf-8")
    return path


def test_local_provider_in_dev(
    fake_hvac: MagicMock, tmp_path: Path, logs_dir: Path, read_log_lines: ReadLines
) -> None:
    """RF-024: env=dev com arquivo válido retorna os segredos e os registra para mascaramento."""
    secrets_file = _write_local_secrets(
        tmp_path, {SECRET_PATH: {"username": "ana.dev", "password": "senha-local-1234", "porta": 1}}
    )
    vault = VaultClient(VaultSettings(local_secrets_file=secrets_file), env=Environment.DEV)
    credentials = vault.get_credentials(SECRET_PATH)
    assert credentials.username == "ana.dev"
    assert vault.get_secret(SECRET_PATH, "porta") == "1"
    get_logger().info("senha={}", credentials.password.get_secret_value())
    lines = read_log_lines(logs_dir)
    assert lines[-1].endswith(f"senha={MASK}")
    assert any("[WARNING]" in line and "provedor local de segredos" in line for line in lines)
    fake_hvac.assert_not_called()
    assert vault.is_available() is True


def test_local_provider_forbidden_in_prd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RF-024: env=prd com a variável definida: get_vault_client(PRD) gera ConfigurationError."""
    secrets_file = _write_local_secrets(tmp_path, {})
    monkeypatch.setenv("RPA3003_VAULT_LOCAL_SECRETS_FILE", str(secrets_file))
    with pytest.raises(ConfigurationError, match="prd") as info:
        get_vault_client(Environment.PRD)
    assert info.value.exit_code is ExitCode.CONFIGURATION_ERROR


def test_local_provider_from_environment_variable_in_dev(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RF-024: get_vault_client(DEV) usa o arquivo de RPA3003_VAULT_LOCAL_SECRETS_FILE."""
    secrets_file = _write_local_secrets(tmp_path, {"rpa/x": {"token": "token-local-xyz"}})
    monkeypatch.setenv("RPA3003_VAULT_LOCAL_SECRETS_FILE", str(secrets_file))
    assert get_vault_client(Environment.DEV).get_secret("rpa/x", "token") == "token-local-xyz"


def test_local_provider_missing_path(tmp_path: Path) -> None:
    """RF-024: caminho ausente no arquivo local gera SecretNotFoundError."""
    provider = LocalFileSecretProvider(_write_local_secrets(tmp_path, {"rpa/a": {"k": "v"}}))
    with pytest.raises(SecretNotFoundError) as info:
        provider.read_secret("rpa/b")
    assert info.value.details["caminho"] == "rpa/b"


@pytest.mark.parametrize(
    "content",
    ["{ não é json", "[1, 2]", json.dumps({"rpa/a": "texto"})],
    ids=["json", "lista", "valor"],
)
def test_local_provider_invalid_file(tmp_path: Path, content: str) -> None:
    """RF-024: arquivo local com formato inválido gera ConfigurationError ou VaultError."""
    path = tmp_path / "x.secrets.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises((ConfigurationError, VaultError)):
        LocalFileSecretProvider(path).read_secret("rpa/a")


def test_local_provider_missing_file(tmp_path: Path) -> None:
    """RF-024: arquivo local inexistente gera ConfigurationError e is_available() False."""
    provider = LocalFileSecretProvider(tmp_path / "nao_existe.json")
    assert provider.is_available() is False
    with pytest.raises(ConfigurationError):
        provider.read_secret("rpa/a")


# --- VaultClient e get_vault_client ------------------------------------------------------------


def test_explicit_provider_takes_precedence(tmp_path: Path) -> None:
    """Seção 7.5.4: provider explícito é usado mesmo com arquivo local configurado em prd."""

    class MemoryProvider:
        def read_secret(self, path: str) -> dict[str, str]:
            return {"chave": f"valor de {path}"}

        def is_available(self) -> bool:
            return True

    settings = VaultSettings(local_secrets_file=tmp_path / "x.json")
    vault = VaultClient(settings, env=Environment.PRD, provider=MemoryProvider())
    assert vault.get_secret("rpa/a", "chave") == "valor de rpa/a"
    assert vault.is_available() is True


def test_default_client_requires_vault_url() -> None:
    """Seção 11.12: sem variáveis do Vault, get_vault_client(PRD) gera ConfigurationError (3)."""
    with pytest.raises(ConfigurationError, match="RPA3003_VAULT_URL") as info:
        get_vault_client(Environment.PRD)
    assert int(info.value.exit_code) == 3


def test_get_vault_client_is_cached_per_environment(
    monkeypatch: pytest.MonkeyPatch, fake_hvac: MagicMock
) -> None:
    """Seção 8.7: uma instância por ambiente; reset_vault_clients() descarta o cache."""
    monkeypatch.setenv("RPA3003_VAULT_URL", URL)
    monkeypatch.setenv("RPA3003_VAULT_ROLE_ID", "role")
    monkeypatch.setenv("RPA3003_VAULT_SECRET_ID", "secret-id")
    dev = get_vault_client(Environment.DEV)
    assert get_vault_client(Environment.DEV) is dev
    assert get_vault_client(Environment.PRD) is not dev
    assert get_vault_client() is dev
    reset_vault_clients()
    assert get_vault_client(Environment.DEV) is not dev


def test_get_vault_client_without_env_uses_settings(
    monkeypatch: pytest.MonkeyPatch, fake_hvac: MagicMock
) -> None:
    """Seção 8.7: env None usa get_settings().env (RPA3003_ENV)."""
    monkeypatch.setenv("RPA3003_ENV", "prd")
    monkeypatch.setenv("RPA3003_VAULT_URL", URL)
    monkeypatch.setenv("RPA3003_VAULT_ROLE_ID", "role")
    monkeypatch.setenv("RPA3003_VAULT_SECRET_ID", "secret-id")
    assert get_vault_client() is get_vault_client(Environment.PRD)


def test_auth_secrets_are_registered_for_masking(
    fake_hvac: MagicMock, logs_dir: Path, read_log_lines: ReadLines
) -> None:
    """RF-018: credenciais de autenticação do Vault também são mascaradas."""
    client_of(fake_hvac).token = "hvs.token-emitido-pelo-login"
    VaultClient(vault_settings()).read_secret(SECRET_PATH)
    get_logger().info("secret_id=secret-id-456 token=hvs.token-emitido-pelo-login")
    assert read_log_lines(logs_dir)[-1].endswith(f"secret_id={MASK} token={MASK}")


def test_time_is_not_slept_without_failures(fake_hvac: MagicMock, no_sleep: list[float]) -> None:
    """RF-022: leitura bem-sucedida não espera."""
    started = time.perf_counter()
    VaultClient(vault_settings()).read_secret(SECRET_PATH)
    assert no_sleep == []
    assert time.perf_counter() - started < 5


def test_vault_auth_method_enum() -> None:
    """RF-020: os quatro métodos de autenticação."""
    assert [method.value for method in VaultAuthMethod] == ["token", "approle", "ldap", "userpass"]
