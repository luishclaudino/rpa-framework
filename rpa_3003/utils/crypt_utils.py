"""Criptografia.

Criptografia simétrica, derivação de chaves, DPAPI do Windows, hashes, HMAC, tokens e Base64.

Usa somente primitivas de `cryptography`, `hashlib`, `hmac`, `secrets` e DPAPI (RNF-009). MD5 e
SHA-1 só são aceitos como checksum, com `allow_insecure=True`. O `pywin32` é importado apenas
quando o DPAPI é usado.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
import sys
from pathlib import Path
from types import ModuleType
from typing import Final, Protocol, Self

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from rpa_3003.core.config import PathLike
from rpa_3003.core.exceptions import CryptoError, FileOperationError

__all__: list[str] = [
    "INSECURE_HASH_ALGORITHMS",
    "PBKDF2_ITERATIONS",
    "SALT_SIZE",
    "SECURE_HASH_ALGORITHMS",
    "FernetCipher",
    "b64decode",
    "b64encode",
    "derive_key",
    "dpapi_protect",
    "dpapi_unprotect",
    "generate_key",
    "generate_salt",
    "hash_text",
    "hmac_sign",
    "hmac_verify",
    "random_token",
]

PBKDF2_ITERATIONS: Final[int] = 600_000
SALT_SIZE: Final[int] = 16
SECURE_HASH_ALGORITHMS: Final[frozenset[str]] = frozenset(
    {"sha256", "sha384", "sha512", "sha3_256", "sha3_512", "blake2b"}
)
INSECURE_HASH_ALGORITHMS: Final[frozenset[str]] = frozenset({"md5", "sha1"})

_DERIVED_KEY_LENGTH: Final[int] = 32
_ENCRYPTED_SUFFIX: Final[str] = ".enc"
_DECRYPTED_SUFFIX: Final[str] = ".dec"
_DPAPI_FLAGS: Final[int] = 0
_DPAPI_WINDOWS_ONLY: Final[str] = "DPAPI disponível apenas no Windows"


class _Hasher(Protocol):
    """Interface mínima dos objetos de hash de `hashlib`."""

    def update(self, data: bytes, /) -> None: ...

    def hexdigest(self) -> str: ...


def generate_key() -> str:
    """Gera uma chave Fernet aleatória.

    Returns:
        Chave Fernet (32 bytes em Base64 URL-safe) como texto ASCII.
    """
    return Fernet.generate_key().decode("ascii")


def generate_salt(size: int = SALT_SIZE) -> bytes:
    """Gera um sal aleatório com `secrets.token_bytes`.

    Args:
        size: Tamanho do sal em bytes.

    Returns:
        Sal aleatório.

    Raises:
        ValueError: Se `size` não for positivo.
    """
    if size < 1:
        raise ValueError(f"size deve ser positivo (recebido: {size})")
    return secrets.token_bytes(size)


def derive_key(password: str, salt: bytes, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    """Deriva uma chave Fernet de uma senha com PBKDF2-HMAC-SHA256 (32 bytes).

    Args:
        password: Senha de origem (codificada em UTF-8).
        salt: Sal aleatório; guarde-o junto do dado cifrado.
        iterations: Iterações do PBKDF2.

    Returns:
        Chave em Base64 URL-safe, compatível com `FernetCipher`.

    Raises:
        CryptoError: Se a senha for vazia.
        ValueError: Se `iterations` não for positivo.
    """
    if not password:
        raise CryptoError("Senha vazia não pode derivar chave")
    if iterations < 1:
        raise ValueError(f"iterations deve ser positivo (recebido: {iterations})")
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=_DERIVED_KEY_LENGTH, salt=salt, iterations=iterations
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8"))).decode("ascii")


class FernetCipher:
    """Criptografia simétrica autenticada (Fernet: AES-128-CBC com HMAC-SHA256).

    Textos são codificados e decodificados em UTF-8. Arquivos são processados inteiros em
    memória.

    Example:
        >>> cifra = FernetCipher(generate_key())
        >>> cifra.decrypt(cifra.encrypt("conteúdo"))
        'conteúdo'
    """

    def __init__(self, key: str | bytes) -> None:
        """Cria a cifra.

        Args:
            key: Chave Fernet (ver `generate_key` e `derive_key`).

        Raises:
            CryptoError: Se a chave for inválida.
        """
        try:
            raw = key.encode("ascii") if isinstance(key, str) else key
            self._fernet = Fernet(raw)
        except (ValueError, TypeError) as exc:  # binascii.Error e UnicodeError são ValueError
            raise CryptoError("Chave Fernet inválida") from exc

    def __repr__(self) -> str:
        """Representação sem a chave."""
        return f"{type(self).__name__}(key=********)"

    @classmethod
    def from_password(
        cls, password: str, salt: bytes, *, iterations: int = PBKDF2_ITERATIONS
    ) -> Self:
        """Cria a cifra com chave derivada de senha (`derive_key`).

        Args:
            password: Senha de origem.
            salt: Sal usado na derivação.
            iterations: Iterações do PBKDF2.

        Returns:
            Cifra pronta para uso.

        Raises:
            CryptoError: Se a senha for vazia.
        """
        return cls(derive_key(password, salt, iterations=iterations))

    def encrypt(self, data: str | bytes) -> str:
        """Cifra texto ou bytes.

        Args:
            data: Conteúdo a cifrar; texto é codificado em UTF-8.

        Returns:
            Token Fernet em texto ASCII.
        """
        return self.encrypt_bytes(_to_bytes(data)).decode("ascii")

    def decrypt(self, token: str | bytes, *, ttl: int | None = None) -> str:
        """Decifra um token e retorna o texto original.

        Args:
            token: Token Fernet.
            ttl: Idade máxima do token em segundos.

        Returns:
            Texto decifrado.

        Raises:
            CryptoError: Se a chave for outra, o token estiver corrompido ou expirado, ou o
                conteúdo não for texto UTF-8.
        """
        plain = self.decrypt_bytes(_to_bytes(token), ttl=ttl)
        try:
            return plain.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CryptoError("Dado decifrado não é texto UTF-8; use decrypt_bytes") from exc

    def encrypt_bytes(self, data: bytes) -> bytes:
        """Cifra bytes.

        Args:
            data: Conteúdo a cifrar.

        Returns:
            Token Fernet em bytes.
        """
        return self._fernet.encrypt(data)

    def decrypt_bytes(self, token: bytes, *, ttl: int | None = None) -> bytes:
        """Decifra um token em bytes.

        Args:
            token: Token Fernet.
            ttl: Idade máxima do token em segundos.

        Returns:
            Conteúdo decifrado.

        Raises:
            CryptoError: Se a chave for outra, o token estiver corrompido ou expirado.
        """
        try:
            return self._fernet.decrypt(token, ttl=ttl)
        except (InvalidToken, TypeError) as exc:
            raise CryptoError("Chave inválida ou dado corrompido") from exc

    def encrypt_file(self, src: PathLike, dst: PathLike | None = None) -> Path:
        """Cifra um arquivo.

        Args:
            src: Arquivo de origem.
            dst: Arquivo cifrado; padrão `<src>.enc`.

        Returns:
            Caminho absoluto do arquivo cifrado.

        Raises:
            FileOperationError: Se a leitura ou a gravação falhar.
        """
        source = Path(src)
        target = Path(dst) if dst is not None else source.with_name(source.name + _ENCRYPTED_SUFFIX)
        _write_bytes(target, self.encrypt_bytes(_read_bytes(source)))
        return target.resolve()

    def decrypt_file(self, src: PathLike, dst: PathLike | None = None) -> Path:
        """Decifra um arquivo.

        Args:
            src: Arquivo cifrado.
            dst: Arquivo decifrado; por padrão remove o sufixo `.enc` ou, sem esse sufixo,
                acrescenta `.dec`.

        Returns:
            Caminho absoluto do arquivo decifrado.

        Raises:
            CryptoError: Se a chave for outra ou o arquivo estiver corrompido.
            FileOperationError: Se a leitura ou a gravação falhar.
        """
        source = Path(src)
        target = Path(dst) if dst is not None else _decrypted_path(source)
        _write_bytes(target, self.decrypt_bytes(_read_bytes(source)))
        return target.resolve()


def dpapi_protect(data: bytes, *, description: str = "rpa_3003") -> bytes:
    """Cifra com DPAPI no escopo do usuário Windows atual.

    Args:
        data: Conteúdo a cifrar.
        description: Descrição gravada no blob.

    Returns:
        Blob cifrado, legível apenas pelo mesmo usuário na mesma máquina.

    Raises:
        CryptoError: Fora do Windows, sem `pywin32` ou em falha da API.
    """
    win32crypt = _import_win32crypt()
    try:
        blob = win32crypt.CryptProtectData(data, description, None, None, None, _DPAPI_FLAGS)
    except Exception as exc:  # pywintypes.error não herda de OSError
        raise CryptoError("Falha ao cifrar com DPAPI") from exc
    return bytes(blob)


def dpapi_unprotect(data: bytes) -> bytes:
    """Decifra um blob DPAPI do usuário Windows atual.

    Args:
        data: Blob gerado por `dpapi_protect`.

    Returns:
        Conteúdo original.

    Raises:
        CryptoError: Fora do Windows, sem `pywin32`, com blob inválido ou de outro usuário.
    """
    win32crypt = _import_win32crypt()
    try:
        result = win32crypt.CryptUnprotectData(data, None, None, None, _DPAPI_FLAGS)
    except Exception as exc:  # pywintypes.error não herda de OSError
        raise CryptoError("Falha ao decifrar com DPAPI") from exc
    return bytes(result[1])


def hash_text(text: str | bytes, algorithm: str = "sha256", *, allow_insecure: bool = False) -> str:
    """Calcula o hash hexadecimal de um texto (UTF-8) ou bytes.

    Args:
        text: Conteúdo de entrada.
        algorithm: Algoritmo de `SECURE_HASH_ALGORITHMS` ou, com `allow_insecure`, de
            `INSECURE_HASH_ALGORITHMS`.
        allow_insecure: Permite `md5` e `sha1`, apenas como checksum (RNF-009).

    Returns:
        Hash em hexadecimal.

    Raises:
        CryptoError: Se o algoritmo for inseguro sem `allow_insecure` ou não suportado.
    """
    hasher = _new_hash(algorithm, allow_insecure=allow_insecure)
    hasher.update(_to_bytes(text))
    return hasher.hexdigest()


def hmac_sign(key: bytes, message: str | bytes, *, algorithm: str = "sha256") -> str:
    """Assina uma mensagem com HMAC.

    Args:
        key: Chave secreta.
        message: Mensagem (texto em UTF-8).
        algorithm: Algoritmo de `SECURE_HASH_ALGORITHMS`.

    Returns:
        Assinatura em hexadecimal.

    Raises:
        CryptoError: Se a chave for vazia ou o algoritmo não for seguro.
    """
    if not key:
        raise CryptoError("Chave HMAC vazia")
    name = _normalize_algorithm(algorithm)
    if name not in SECURE_HASH_ALGORITHMS:
        raise _unsupported_algorithm(algorithm, SECURE_HASH_ALGORITHMS)
    return hmac.new(key, _to_bytes(message), name).hexdigest()


def hmac_verify(
    key: bytes, message: str | bytes, signature: str, *, algorithm: str = "sha256"
) -> bool:
    """Verifica uma assinatura HMAC com comparação em tempo constante (`hmac.compare_digest`).

    Args:
        key: Chave secreta.
        message: Mensagem assinada.
        signature: Assinatura hexadecimal recebida (maiúsculas ou minúsculas).
        algorithm: Algoritmo de `SECURE_HASH_ALGORITHMS`.

    Returns:
        `True` se a assinatura for válida.

    Raises:
        CryptoError: Se a chave for vazia ou o algoritmo não for seguro.
    """
    expected = hmac_sign(key, message, algorithm=algorithm)
    try:
        return hmac.compare_digest(expected, signature.strip().lower())
    except TypeError:  # assinatura com caracteres fora do ASCII
        return False


def random_token(nbytes: int = 32) -> str:
    """Gera um token aleatório URL-safe com `secrets.token_urlsafe`.

    Args:
        nbytes: Bytes de entropia.

    Returns:
        Token em Base64 URL-safe.

    Raises:
        ValueError: Se `nbytes` não for positivo.
    """
    if nbytes < 1:
        raise ValueError(f"nbytes deve ser positivo (recebido: {nbytes})")
    return secrets.token_urlsafe(nbytes)


def b64encode(data: str | bytes) -> str:
    """Codifica em Base64 padrão.

    Args:
        data: Conteúdo; texto é codificado em UTF-8.

    Returns:
        Texto Base64 ASCII.
    """
    return base64.b64encode(_to_bytes(data)).decode("ascii")


def b64decode(data: str) -> bytes:
    """Decodifica Base64 padrão, ignorando espaços e quebras de linha.

    Args:
        data: Texto Base64.

    Returns:
        Bytes decodificados.

    Raises:
        CryptoError: Se o texto não for Base64 válido.
    """
    try:
        return base64.b64decode("".join(data.split()), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CryptoError("Base64 inválido") from exc


def _new_hash(algorithm: str, *, allow_insecure: bool) -> _Hasher:
    """Cria o objeto de hash aplicando a política de algoritmos (RNF-009).

    Usado também por `filesystem_utils.file_hash`, para manter uma única política.
    """
    name = _normalize_algorithm(algorithm)
    if name in INSECURE_HASH_ALGORITHMS:
        if not allow_insecure:
            raise CryptoError(
                "Algoritmo inseguro; use allow_insecure=True apenas para checksum",
                details={"algoritmo": algorithm},
            )
        return hashlib.new(name, usedforsecurity=False)
    if name not in SECURE_HASH_ALGORITHMS:
        raise _unsupported_algorithm(algorithm, SECURE_HASH_ALGORITHMS | INSECURE_HASH_ALGORITHMS)
    return hashlib.new(name)


def _normalize_algorithm(algorithm: str) -> str:
    return algorithm.strip().lower().replace("-", "_")


def _unsupported_algorithm(algorithm: str, supported: frozenset[str]) -> CryptoError:
    return CryptoError(
        "Algoritmo de hash não suportado",
        details={"algoritmo": algorithm, "suportados": ", ".join(sorted(supported))},
    )


def _to_bytes(data: str | bytes) -> bytes:
    return data.encode("utf-8") if isinstance(data, str) else bytes(data)


def _decrypted_path(source: Path) -> Path:
    if source.suffix.lower() == _ENCRYPTED_SUFFIX:
        return source.with_suffix("")
    return source.with_name(source.name + _DECRYPTED_SUFFIX)


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise _file_error("Falha ao ler arquivo", path, exc) from exc


def _write_bytes(path: Path, data: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError as exc:
        raise _file_error("Falha ao gravar arquivo", path, exc) from exc


def _file_error(message: str, path: Path, exc: OSError) -> FileOperationError:
    reason = f"{type(exc).__name__}: {exc.strerror}" if exc.strerror else type(exc).__name__
    return FileOperationError(message, details={"caminho": str(path.absolute()), "erro": reason})


def _is_windows() -> bool:
    return sys.platform == "win32"


def _import_win32crypt() -> ModuleType:
    if not _is_windows():
        raise CryptoError(_DPAPI_WINDOWS_ONLY)
    try:
        import win32crypt
    except ImportError as exc:
        raise CryptoError("DPAPI requer o pacote pywin32") from exc
    module: ModuleType = win32crypt
    return module
