"""Cliente HashiCorp Vault.

Obtenção de segredos do Vault (ou do provedor local em `dev`) com autenticação, retentativas,
cache em memória, cache offline opcional e registro automático para mascaramento.

As bibliotecas `hvac` e `pywin32` são importadas somente quando usadas, para que importar
`global_vars.py` de um projeto não carregue o cliente HTTP.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, Final, Protocol, runtime_checkable

from pydantic import SecretStr

from rpa_3003.core.config import (
    Environment,
    VaultAuthMethod,
    VaultSettings,
    get_settings,
    load_vault_settings,
)
from rpa_3003.core.exceptions import (
    ConfigurationError,
    SecretNotFoundError,
    VaultAuthenticationError,
    VaultError,
    VaultUnavailableError,
)
from rpa_3003.core.logger import get_logger, register_secret

if TYPE_CHECKING:
    import hvac

__all__: list[str] = [
    "Credentials",
    "HashiCorpVaultProvider",
    "LocalFileSecretProvider",
    "OfflineSecretCache",
    "SecretProvider",
    "VaultClient",
    "get_vault_client",
    "reset_vault_clients",
]

# Campo de VaultSettings e variável de ambiente exigidos por método de autenticação (RF-020).
_REQUIRED_BY_METHOD: Final[dict[VaultAuthMethod, tuple[tuple[str, str], ...]]] = {
    VaultAuthMethod.TOKEN: (("token", "RPA3003_VAULT_TOKEN"),),
    VaultAuthMethod.APPROLE: (
        ("role_id", "RPA3003_VAULT_ROLE_ID"),
        ("secret_id", "RPA3003_VAULT_SECRET_ID"),
    ),
    VaultAuthMethod.LDAP: (
        ("username", "RPA3003_VAULT_USERNAME"),
        ("password", "RPA3003_VAULT_PASSWORD"),
    ),
    VaultAuthMethod.USERPASS: (
        ("username", "RPA3003_VAULT_USERNAME"),
        ("password", "RPA3003_VAULT_PASSWORD"),
    ),
}
_OFFLINE_CACHE_SUFFIX: Final[str] = ".bin"
_DPAPI_DESCRIPTION: Final[str] = "rpa_3003 vault cache"
_CRYPTPROTECT_UI_FORBIDDEN: Final[int] = 0x1
_HEALTHY_STATUS_CODE: Final[int] = 200


@dataclass(frozen=True, slots=True)
class Credentials:
    """Usuário e senha lidos do Vault; a senha nunca aparece em `repr()`."""

    username: str
    password: SecretStr


@runtime_checkable
class SecretProvider(Protocol):
    """Fonte de segredos plugável do `VaultClient` (Strategy)."""

    def read_secret(self, path: str) -> dict[str, str]:
        """Retorna os pares chave/valor do segredo em `path`."""
        ...

    def is_available(self) -> bool:
        """Indica se a fonte de segredos está acessível."""
        ...


class HashiCorpVaultProvider:
    """Provedor de segredos do HashiCorp Vault via `hvac` (KV v1/v2, RF-020 a RF-022)."""

    def __init__(self, settings: VaultSettings) -> None:
        """Valida parâmetros obrigatórios do auth_method (ConfigurationError listando variáveis).

        Args:
            settings: Configuração do Vault.

        Raises:
            ConfigurationError: Se faltar a URL ou algum parâmetro do método de autenticação.
        """
        missing = _missing_variables(settings)
        if missing:
            raise ConfigurationError(
                f"Parâmetros obrigatórios do Vault ausentes: {', '.join(missing)}",
                details={"auth_method": settings.auth_method.value, "variaveis": missing},
            )
        self._settings = settings
        self._client: hvac.Client | None = None
        self._lock = threading.RLock()

    def authenticate(self) -> None:
        """Cria hvac.Client(url, namespace, verify, timeout) e autentica conforme auth_method.

        Raises:
            VaultAuthenticationError: Se as credenciais forem recusadas.
            VaultUnavailableError: Se o Vault não responder.
        """
        try:
            self._authenticate()
        except _transient_errors() as exc:
            raise VaultUnavailableError(
                "Vault indisponível na autenticação", details={"url": self._settings.url}
            ) from exc

    def read_secret(self, path: str) -> dict[str, str]:
        """Lê o segredo com autenticação preguiçosa, retentativas e uma reautenticação.

        Args:
            path: Caminho do segredo no KV (ex.: ``rpa/projeto/prd/sistema``).

        Returns:
            Pares chave/valor com os valores convertidos por `str()`.

        Raises:
            SecretNotFoundError: Se o caminho não existir.
            VaultAuthenticationError: Se a autenticação ou o acesso forem recusados.
            VaultUnavailableError: Se o Vault não responder após `max_retries` tentativas.
        """
        attempts = self._settings.max_retries
        last_error: BaseException | None = None
        for attempt in range(1, attempts + 1):
            try:
                return self._read_authenticated(path)
            except _transient_errors() as exc:
                last_error = exc
                get_logger().warning(
                    "Vault indisponível (tentativa {}/{}): {}", attempt, attempts, _describe(exc)
                )
                if attempt < attempts:
                    time.sleep(self._settings.backoff_base * 2 ** (attempt - 1))
        raise VaultUnavailableError(
            f"Vault indisponível após {attempts} tentativas",
            details={"url": self._settings.url, "caminho": path},
        ) from last_error

    def is_available(self) -> bool:
        """True se sys/health responde (inicializado e não selado) dentro do timeout."""
        import hvac

        try:
            status = self._new_client().sys.read_health_status(
                method="GET", standby_ok=True, perfstandby_ok=True
            )
        except (*_transient_errors(), hvac.exceptions.VaultError):
            return False
        if isinstance(status, Mapping):
            return bool(status.get("initialized")) and not bool(status.get("sealed"))
        return getattr(status, "status_code", None) == _HEALTHY_STATUS_CODE

    def _new_client(self) -> hvac.Client:
        """Cria o cliente `hvac` com URL, namespace, verificação TLS e timeout."""
        import hvac

        settings = self._settings
        verify = str(settings.verify) if isinstance(settings.verify, Path) else settings.verify
        token = None
        if settings.auth_method is VaultAuthMethod.TOKEN:
            token = _reveal(settings.token)
        return hvac.Client(
            url=settings.url,
            token=token,
            namespace=settings.namespace,
            verify=verify,
            timeout=settings.timeout,
        )

    def _authenticate(self) -> None:
        """Autentica e guarda o cliente; erros transitórios são propagados sem conversão."""
        import hvac

        with self._lock:
            client = self._new_client()
            try:
                self._login(client)
            except _transient_errors():
                raise
            except hvac.exceptions.VaultError as exc:
                raise VaultAuthenticationError(
                    "Falha na autenticação no Vault",
                    details={"url": self._settings.url, "metodo": self._settings.auth_method.value},
                ) from exc
            for secret in (self._settings.token, self._settings.secret_id, self._settings.password):
                register_secret(_reveal(secret))
            client_token = getattr(client, "token", None)
            if isinstance(client_token, str):
                register_secret(client_token)
            self._client = client

    def _login(self, client: hvac.Client) -> None:
        """Executa o login do método configurado (seção 8.7)."""
        settings = self._settings
        match settings.auth_method:
            case VaultAuthMethod.TOKEN:
                if not client.is_authenticated():
                    raise VaultAuthenticationError(
                        "Token do Vault inválido ou expirado", details={"url": settings.url}
                    )
            case VaultAuthMethod.APPROLE:
                client.auth.approle.login(
                    role_id=settings.role_id, secret_id=_reveal(settings.secret_id)
                )
            case VaultAuthMethod.LDAP:
                client.auth.ldap.login(
                    username=settings.username, password=_reveal(settings.password)
                )
            case VaultAuthMethod.USERPASS:
                client.auth.userpass.login(
                    username=settings.username, password=_reveal(settings.password)
                )

    def _read_authenticated(self, path: str) -> dict[str, str]:
        """Lê com o cliente autenticado; em 401/403, reautentica uma única vez."""
        import hvac

        with self._lock:
            if self._client is None:
                self._authenticate()
            client = self._client
        try:
            return self._read_once(client, path)
        except (hvac.exceptions.Unauthorized, hvac.exceptions.Forbidden):
            get_logger().warning("Vault recusou o token; nova autenticação | caminho={}", path)
        self._authenticate()
        try:
            return self._read_once(self._client, path)
        except (hvac.exceptions.Unauthorized, hvac.exceptions.Forbidden) as exc:
            raise VaultAuthenticationError(
                "Acesso ao segredo negado pelo Vault", details={"caminho": path}
            ) from exc

    def _read_once(self, client: hvac.Client, path: str) -> dict[str, str]:
        """Uma leitura KV v1 ou v2, com mapeamento dos erros não transitórios."""
        import hvac

        mount = self._settings.mount_point
        try:
            if self._settings.kv_version == 2:
                response = client.secrets.kv.v2.read_secret_version(
                    path=path, mount_point=mount, raise_on_deleted_version=True
                )
                data = response["data"]["data"]
            else:
                data = client.secrets.kv.v1.read_secret(path=path, mount_point=mount)["data"]
        except hvac.exceptions.InvalidPath as exc:
            raise SecretNotFoundError(
                "Segredo não encontrado no Vault", details={"caminho": path, "mount_point": mount}
            ) from exc
        except (*_transient_errors(), hvac.exceptions.Unauthorized, hvac.exceptions.Forbidden):
            raise
        except (hvac.exceptions.VaultError, KeyError, TypeError) as exc:
            raise VaultError(
                "Resposta inválida do Vault ao ler segredo", details={"caminho": path}
            ) from exc
        return _stringify(data, path)


class LocalFileSecretProvider:
    """Provedor de segredos em arquivo JSON local, permitido somente em `dev` (RF-024).

    Formato do arquivo: ``{"<caminho>": {"<chave>": "<valor>"}}``.
    """

    def __init__(self, file_path: Path) -> None:
        """Cria o provedor.

        Args:
            file_path: Arquivo JSON de segredos (lido a cada consulta).
        """
        self._file_path = Path(file_path).expanduser().resolve()

    def read_secret(self, path: str) -> dict[str, str]:
        """Retorna o segredo `path` do arquivo local.

        Args:
            path: Chave de primeiro nível do JSON.

        Returns:
            Pares chave/valor com os valores convertidos por `str()`.

        Raises:
            SecretNotFoundError: Se o caminho não existir no arquivo.
            ConfigurationError: Se o arquivo não existir ou não tiver o formato esperado.
        """
        secrets = self._load()
        if path not in secrets:
            raise SecretNotFoundError(
                "Segredo não encontrado no arquivo local",
                details={"caminho": path, "arquivo": str(self._file_path)},
            )
        return _stringify(secrets[path], path)

    def is_available(self) -> bool:
        """Indica se o arquivo de segredos existe."""
        return self._file_path.is_file()

    def _load(self) -> dict[str, Any]:
        """Lê e valida o JSON de segredos."""
        details = {"arquivo": str(self._file_path)}
        try:
            content: Any = json.loads(self._file_path.read_text(encoding="utf-8-sig"))
        except OSError as exc:
            raise ConfigurationError(
                "Arquivo local de segredos não encontrado ou ilegível", details=details
            ) from exc
        except ValueError as exc:
            raise ConfigurationError(
                "Arquivo local de segredos não é um JSON válido", details=details
            ) from exc
        if not isinstance(content, dict):
            raise ConfigurationError(
                "Arquivo local de segredos deve conter um objeto JSON", details=details
            )
        return content


class OfflineSecretCache:
    """Cache offline de segredos cifrado com DPAPI no escopo do usuário Windows (RF-023)."""

    def __init__(self, cache_dir: Path, ttl_hours: int) -> None:
        """Cria o cache.

        Args:
            cache_dir: Pasta dos arquivos `<sha256(chave)>.bin`.
            ttl_hours: Idade máxima, em horas, de uma entrada utilizável.
        """
        self._cache_dir = Path(cache_dir)
        self._ttl_seconds = ttl_hours * 3600

    def get(self, key: str) -> dict[str, str] | None:
        """Retorna dados se existirem e idade <= ttl; caso contrário None."""
        entry = self._load(key)
        return None if entry is None else entry[1]

    def put(self, key: str, data: Mapping[str, str]) -> None:
        """Grava os dados cifrados com DPAPI, com o timestamp atual.

        Args:
            key: Chave do cache (``<mount>/<caminho>``).
            data: Pares chave/valor do segredo.
        """
        payload = json.dumps({"timestamp": time.time(), "data": dict(data)}, ensure_ascii=False)
        blob = _dpapi_protect(payload.encode("utf-8"))
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        target = self._path_for(key)
        temporary = target.with_name(f"{target.name}.tmp")
        temporary.write_bytes(blob)
        os.replace(temporary, target)

    def clear(self) -> int:
        """Remove todas as entradas do cache e retorna quantas foram removidas."""
        if not self._cache_dir.is_dir():
            return 0
        removed = 0
        for entry in self._cache_dir.glob(f"*{_OFFLINE_CACHE_SUFFIX}"):
            entry.unlink(missing_ok=True)
            removed += 1
        return removed

    def _path_for(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self._cache_dir / f"{digest}{_OFFLINE_CACHE_SUFFIX}"

    def _load(self, key: str) -> tuple[float, dict[str, str]] | None:
        """Retorna (idade em segundos, dados) da entrada válida, ou None."""
        path = self._path_for(key)
        if not path.is_file():
            return None
        try:
            payload: Any = json.loads(_dpapi_unprotect(path.read_bytes()).decode("utf-8"))
            timestamp = float(payload["timestamp"])
            data = {str(name): str(value) for name, value in payload["data"].items()}
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            ConfigurationError,
        ) as exc:
            get_logger().warning("Cache offline do Vault ignorado: {}", type(exc).__name__)
            return None
        age = time.time() - timestamp
        if not 0 <= age <= self._ttl_seconds:
            return None
        return age, data


class VaultClient:
    """Fachada de leitura de segredos com cache em memória, cache offline e mascaramento."""

    def __init__(
        self,
        settings: VaultSettings | None = None,
        *,
        env: Environment = Environment.DEV,
        provider: SecretProvider | None = None,
    ) -> None:
        """Seleciona o provedor: explícito → arquivo local (somente dev) → HashiCorp Vault.

        Args:
            settings: Configuração do Vault; `None` lê variáveis e `.env` do diretório atual.
            env: Ambiente da execução (o provedor local é proibido em `prd`).
            provider: Provedor explícito de segredos.

        Raises:
            ConfigurationError: Se `RPA3003_VAULT_LOCAL_SECRETS_FILE` estiver definida em `prd`
                ou faltarem parâmetros do Vault.
        """
        self._settings = settings if settings is not None else load_vault_settings()
        self._env = Environment.from_value(env)
        self._provider: SecretProvider = (
            provider if provider is not None else self._select_provider()
        )
        self._cache: dict[str, tuple[float, dict[str, str]]] = {}
        self._lock = threading.Lock()
        self._offline: OfflineSecretCache | None = None
        if self._settings.offline_cache:
            self._offline = OfflineSecretCache(
                _default_offline_cache_dir(), self._settings.offline_cache_ttl_hours
            )

    def read_secret(self, path: str, *, use_cache: bool = True) -> dict[str, str]:
        """Lê um segredo e registra todos os valores para mascaramento.

        Args:
            path: Caminho do segredo.
            use_cache: Consulta o cache em memória antes do provedor.

        Returns:
            Cópia dos pares chave/valor.

        Raises:
            SecretNotFoundError: Caminho inexistente.
            VaultAuthenticationError: Credenciais recusadas.
            VaultUnavailableError: Vault indisponível e sem cache offline válido.
        """
        key = f"{self._settings.mount_point}/{path}"
        cached = self._from_memory(key) if use_cache else None
        if cached is not None:
            return _publish(cached)
        try:
            data = dict(self._provider.read_secret(path))
        except VaultUnavailableError:
            offline = self._from_offline(key, path)
            if offline is None:
                raise
            return _publish(offline)
        _publish(data)
        self._store(key, data)
        get_logger().debug("Segredo lido do Vault | caminho={} | chaves={}", path, len(data))
        return dict(data)

    def get_secret(self, path: str, key: str, *, use_cache: bool = True) -> str:
        """Retorna o valor de uma chave do segredo.

        Raises:
            SecretNotFoundError: Se a chave não existir; a mensagem lista só os nomes das chaves.
        """
        data = self.read_secret(path, use_cache=use_cache)
        if key not in data:
            raise _missing_keys_error(path, [key], data)
        return data[key]

    def get_credentials(
        self, path: str, *, username_key: str = "username", password_key: str = "password"
    ) -> Credentials:
        """Retorna usuário e senha (`SecretStr`) do segredo.

        Raises:
            SecretNotFoundError: Se faltar a chave de usuário ou de senha.
        """
        data = self.read_secret(path)
        missing = [name for name in (username_key, password_key) if name not in data]
        if missing:
            raise _missing_keys_error(path, missing, data)
        return Credentials(username=data[username_key], password=SecretStr(data[password_key]))

    def is_available(self) -> bool:
        """Indica se o provedor de segredos está acessível."""
        return self._provider.is_available()

    def clear_cache(self) -> None:
        """Descarta o cache em memória."""
        with self._lock:
            self._cache.clear()

    def _select_provider(self) -> SecretProvider:
        local_file = self._settings.local_secrets_file
        if local_file is None:
            return HashiCorpVaultProvider(self._settings)
        if self._env is Environment.PRD:
            raise ConfigurationError(
                "RPA3003_VAULT_LOCAL_SECRETS_FILE não é permitida em prd; use o Vault",
                details={"ambiente": self._env.value},
            )
        get_logger().warning(
            "Usando provedor local de segredos (somente dev) | arquivo={}", local_file
        )
        return LocalFileSecretProvider(local_file)

    def _from_memory(self, key: str) -> dict[str, str] | None:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            expires_at, data = entry
            if time.time() >= expires_at:
                del self._cache[key]
                return None
            return dict(data)

    def _store(self, key: str, data: dict[str, str]) -> None:
        if self._settings.cache_ttl > 0:
            with self._lock:
                self._cache[key] = (time.time() + self._settings.cache_ttl, dict(data))
        if self._offline is not None:
            try:
                self._offline.put(key, data)
            except (OSError, ConfigurationError, ValueError) as exc:
                get_logger().warning(
                    "Falha ao gravar cache offline do Vault: {}", type(exc).__name__
                )

    def _from_offline(self, key: str, path: str) -> dict[str, str] | None:
        if self._offline is None:
            return None
        entry = self._offline._load(key)
        if entry is None:
            return None
        age_seconds, data = entry
        get_logger().warning(
            "Vault indisponível; usando cache offline com {:.1f}h de idade | caminho={}",
            age_seconds / 3600,
            path,
        )
        return data


_CLIENTS: Final[dict[Environment, VaultClient]] = {}
_CLIENTS_LOCK: Final[threading.Lock] = threading.Lock()


def get_vault_client(env: Environment | None = None) -> VaultClient:
    """Instância por ambiente (cache em dicionário protegido por lock); env None usa
    get_settings().env.

    Args:
        env: Ambiente; `None` usa `RPA3003_ENV`/`.env` via `get_settings()`.

    Returns:
        O mesmo `VaultClient` para chamadas com o mesmo ambiente.

    Raises:
        ConfigurationError: Se a configuração do Vault for inválida para o ambiente.
    """
    resolved = get_settings().env if env is None else Environment.from_value(env)
    with _CLIENTS_LOCK:
        client = _CLIENTS.get(resolved)
        if client is None:
            client = VaultClient(env=resolved)
            _CLIENTS[resolved] = client
        return client


def reset_vault_clients() -> None:
    """Descarta os clientes criados por `get_vault_client` (uso em testes)."""
    with _CLIENTS_LOCK:
        _CLIENTS.clear()


def _missing_variables(settings: VaultSettings) -> list[str]:
    """Variáveis obrigatórias ausentes para a URL e o método de autenticação configurados."""
    missing = [] if settings.url else ["RPA3003_VAULT_URL"]
    for field_name, variable in _REQUIRED_BY_METHOD[settings.auth_method]:
        value = getattr(settings, field_name)
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        if not value:
            missing.append(variable)
    return missing


def _reveal(secret: SecretStr | None) -> str | None:
    return None if secret is None else secret.get_secret_value()


def _transient_errors() -> tuple[type[BaseException], ...]:
    """Erros de rede, timeout e HTTP 5xx que provocam nova tentativa (RF-022)."""
    import hvac
    import requests

    return (
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
        hvac.exceptions.VaultDown,
        hvac.exceptions.InternalServerError,
        hvac.exceptions.BadGateway,
    )


def _describe(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


def _stringify(data: object, path: str) -> dict[str, str]:
    """Converte os valores do segredo para `str` (RF-021)."""
    if not isinstance(data, Mapping):
        raise VaultError("Segredo com formato inválido", details={"caminho": path})
    return {str(name): str(value) for name, value in data.items()}


def _publish(data: Mapping[str, str]) -> dict[str, str]:
    """Registra os valores para mascaramento e devolve uma cópia (RF-018)."""
    for value in data.values():
        register_secret(value)
    return dict(data)


def _missing_keys_error(path: str, keys: list[str], data: Mapping[str, str]) -> SecretNotFoundError:
    """Erro de chave ausente que expõe apenas os nomes das chaves disponíveis (RF-021)."""
    return SecretNotFoundError(
        f"Chave não encontrada no segredo: {', '.join(keys)}",
        details={"caminho": path, "chaves_disponiveis": ", ".join(sorted(data))},
    )


def _default_offline_cache_dir() -> Path:
    """`%LOCALAPPDATA%/rpa_3003/vault_cache`, montado a partir da variável (RNF-016)."""
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Local"
    return root / "rpa_3003" / "vault_cache"


def _dpapi_protect(data: bytes) -> bytes:
    """Cifra com DPAPI no escopo do usuário (`win32crypt.CryptProtectData`)."""
    win32crypt = _import_win32crypt()
    protected: bytes = win32crypt.CryptProtectData(
        data, _DPAPI_DESCRIPTION, None, None, None, _CRYPTPROTECT_UI_FORBIDDEN
    )
    return bytes(protected)


def _dpapi_unprotect(blob: bytes) -> bytes:
    """Decifra com DPAPI; falhas viram `ValueError`."""
    win32crypt = _import_win32crypt()
    try:
        _description, data = win32crypt.CryptUnprotectData(
            blob, None, None, None, _CRYPTPROTECT_UI_FORBIDDEN
        )
    except Exception as exc:  # pywintypes.error não herda de OSError
        raise ValueError("Falha ao decifrar com DPAPI") from exc
    return bytes(data)


def _import_win32crypt() -> ModuleType:
    try:
        import win32crypt
    except ImportError as exc:
        raise ConfigurationError(
            "Cache offline do Vault requer Windows com pywin32 (DPAPI)",
            details={"variavel": "RPA3003_VAULT_OFFLINE_CACHE"},
        ) from exc
    module: ModuleType = win32crypt
    return module
