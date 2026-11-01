"""Testes de `rpa_3003.utils.crypt_utils` (RF-042, RNF-009, seção 8.14)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import time_machine
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from pytest_mock import MockerFixture

from rpa_3003.core.exceptions import CryptoError, FileOperationError
from rpa_3003.utils import crypt_utils

pytestmark = pytest.mark.unit

FAST_ITERATIONS = 1_000
SALT = b"0123456789abcdef"


@pytest.fixture
def cipher() -> crypt_utils.FernetCipher:
    return crypt_utils.FernetCipher(crypt_utils.generate_key())


@pytest.fixture
def fake_win32crypt(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Substitui o módulo win32crypt por um MagicMock reversível."""
    module = MagicMock(name="win32crypt")
    module.CryptProtectData.side_effect = lambda data, *_args: b"DPAPI:" + data
    module.CryptUnprotectData.side_effect = lambda blob, *_args: ("rpa_3003", blob[6:])
    monkeypatch.setitem(sys.modules, "win32crypt", module)
    monkeypatch.setattr(sys, "platform", "win32")
    return module


# --- chaves e Fernet --------------------------------------------------------------------------


def test_encrypt_decrypt_roundtrip(cipher: crypt_utils.FernetCipher) -> None:
    """RF-042: ida e volta encrypt/decrypt retorna o texto original (UTF-8)."""
    token = cipher.encrypt("conteúdo sigiloso")
    assert isinstance(token, str)
    assert "sigiloso" not in token
    assert cipher.decrypt(token) == "conteúdo sigiloso"
    assert cipher.decrypt(token.encode("ascii")) == "conteúdo sigiloso"
    assert cipher.decrypt(cipher.encrypt(b"\x00bytes")) == "\x00bytes"


def test_decrypt_with_wrong_key_raises(cipher: crypt_utils.FernetCipher) -> None:
    """RF-042: chave errada lança CryptoError."""
    token = cipher.encrypt("conteúdo")
    other = crypt_utils.FernetCipher(crypt_utils.generate_key())
    with pytest.raises(CryptoError, match=r"^Chave inválida ou dado corrompido$"):
        other.decrypt(token)
    with pytest.raises(CryptoError, match="Chave inválida ou dado corrompido"):
        cipher.decrypt("nao-e-um-token")
    with pytest.raises(CryptoError, match="Chave inválida ou dado corrompido"):
        cipher.decrypt_bytes(token.encode("ascii")[:-4] + b"AAAA")


@pytest.mark.parametrize("key", ["curta", b"x" * 44, "ç" * 44, 123])
def test_invalid_key_raises(key: object) -> None:
    """RF-042: chave inválida lança CryptoError("Chave Fernet inválida")."""
    with pytest.raises(CryptoError, match=r"^Chave Fernet inválida$"):
        crypt_utils.FernetCipher(key)  # type: ignore[arg-type]


def test_cipher_repr_hides_key() -> None:
    """RNF-009: a representação da cifra não expõe a chave."""
    key = crypt_utils.generate_key()
    assert key not in repr(crypt_utils.FernetCipher(key))
    assert repr(crypt_utils.FernetCipher(key)) == "FernetCipher(key=********)"


def test_generate_key_is_valid_fernet_key() -> None:
    """RF-042: generate_key retorna chave Fernet em texto ASCII."""
    key = crypt_utils.generate_key()
    assert len(base64.urlsafe_b64decode(key)) == 32
    Fernet(key)
    assert key != crypt_utils.generate_key()


def test_generate_salt() -> None:
    """RF-042: generate_salt usa SALT_SIZE por padrão e é aleatório."""
    assert len(crypt_utils.generate_salt()) == crypt_utils.SALT_SIZE == 16
    assert len(crypt_utils.generate_salt(32)) == 32
    assert crypt_utils.generate_salt() != crypt_utils.generate_salt()
    with pytest.raises(ValueError, match="size"):
        crypt_utils.generate_salt(0)


def test_derive_key_is_deterministic_and_salt_dependent() -> None:
    """RF-042: mesma senha e sal geram a mesma chave; sal diferente gera chave diferente."""
    first = crypt_utils.derive_key("frase-do-cofre", SALT, iterations=FAST_ITERATIONS)
    again = crypt_utils.derive_key("frase-do-cofre", SALT, iterations=FAST_ITERATIONS)
    other_salt = crypt_utils.derive_key(
        "frase-do-cofre", b"fedcba9876543210", iterations=FAST_ITERATIONS
    )
    other_password = crypt_utils.derive_key("outra-frase", SALT, iterations=FAST_ITERATIONS)
    assert first == again
    assert first not in (other_salt, other_password)
    assert len(base64.urlsafe_b64decode(first)) == 32
    expected = hashlib.pbkdf2_hmac("sha256", b"frase-do-cofre", SALT, FAST_ITERATIONS, 32)
    assert first == base64.urlsafe_b64encode(expected).decode("ascii")


def test_derive_key_defaults_to_pbkdf2_sha256_600k(mocker: MockerFixture) -> None:
    """RF-042: PBKDF2-HMAC-SHA256 com 600.000 iterações e 32 bytes por padrão."""
    kdf_class = mocker.patch.object(crypt_utils, "PBKDF2HMAC")
    kdf_class.return_value.derive.return_value = b"k" * 32
    key = crypt_utils.derive_key("senha", SALT)
    kwargs = kdf_class.call_args.kwargs
    assert kwargs["iterations"] == crypt_utils.PBKDF2_ITERATIONS == 600_000
    assert kwargs["length"] == 32
    assert kwargs["salt"] == SALT
    assert isinstance(kwargs["algorithm"], hashes.SHA256)
    kdf_class.return_value.derive.assert_called_once_with(b"senha")
    assert key == base64.urlsafe_b64encode(b"k" * 32).decode("ascii")


def test_derive_key_validations() -> None:
    """RF-042: senha vazia gera CryptoError; iterações não positivas, ValueError."""
    with pytest.raises(CryptoError, match="Senha vazia"):
        crypt_utils.derive_key("", SALT)
    with pytest.raises(ValueError, match="iterations"):
        crypt_utils.derive_key("senha", SALT, iterations=0)


def test_from_password_roundtrip() -> None:
    """RF-042: cifra derivada da mesma senha e sal decifra o token."""
    writer = crypt_utils.FernetCipher.from_password("frase", SALT, iterations=FAST_ITERATIONS)
    reader = crypt_utils.FernetCipher.from_password("frase", SALT, iterations=FAST_ITERATIONS)
    wrong = crypt_utils.FernetCipher.from_password("errada", SALT, iterations=FAST_ITERATIONS)
    token = writer.encrypt("dado")
    assert reader.decrypt(token) == "dado"
    with pytest.raises(CryptoError):
        wrong.decrypt(token)


def test_decrypt_non_utf8_payload_requires_decrypt_bytes(cipher: crypt_utils.FernetCipher) -> None:
    """RF-042: conteúdo binário não UTF-8 só é lido com decrypt_bytes."""
    token = cipher.encrypt_bytes(b"\xff\xfe")
    assert cipher.decrypt_bytes(token) == b"\xff\xfe"
    with pytest.raises(CryptoError, match="UTF-8"):
        cipher.decrypt(token)


def test_decrypt_ttl(cipher: crypt_utils.FernetCipher) -> None:
    """RF-042: token mais antigo que ttl gera CryptoError."""
    start = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    with time_machine.travel(start, tick=False):
        token = cipher.encrypt("temporário")
    with time_machine.travel(start + timedelta(seconds=120), tick=False):
        assert cipher.decrypt(token, ttl=300) == "temporário"
        with pytest.raises(CryptoError, match="Chave inválida ou dado corrompido"):
            cipher.decrypt(token, ttl=60)


def test_encrypt_and_decrypt_file_default_names(
    cipher: crypt_utils.FernetCipher, tmp_path: Path
) -> None:
    """RF-042: encrypt_file gera <src>.enc; decrypt_file remove .enc (ou acrescenta .dec)."""
    source = tmp_path / "relatorio.csv"
    source.write_text("id;valor\n1;ação\n", encoding="utf-8")
    encrypted = cipher.encrypt_file(source)
    assert encrypted == (tmp_path / "relatorio.csv.enc").resolve()
    assert b"valor" not in encrypted.read_bytes()
    source.unlink()
    decrypted = cipher.decrypt_file(encrypted)
    assert decrypted == source.resolve()
    assert decrypted.read_text(encoding="utf-8") == "id;valor\n1;ação\n"
    renamed = encrypted.rename(tmp_path / "pacote.bin")
    assert cipher.decrypt_file(renamed) == (tmp_path / "pacote.bin.dec").resolve()
    upper = tmp_path / "dados.ENC"
    upper.write_bytes(encrypted.with_name("pacote.bin").read_bytes())
    assert cipher.decrypt_file(upper) == (tmp_path / "dados").resolve()


def test_encrypt_and_decrypt_file_explicit_destination(
    cipher: crypt_utils.FernetCipher, tmp_path: Path
) -> None:
    """RF-042: destinos explícitos e criação da pasta de destino."""
    source = tmp_path / "foto.png"
    source.write_bytes(b"\x89PNG binario")
    encrypted = cipher.encrypt_file(source, tmp_path / "cofre" / "foto.cifrada")
    restored = cipher.decrypt_file(encrypted, tmp_path / "restaurado" / "foto.png")
    assert restored.read_bytes() == b"\x89PNG binario"


def test_file_errors(cipher: crypt_utils.FernetCipher, tmp_path: Path) -> None:
    """RF-042: arquivo inexistente gera FileOperationError; corrompido, CryptoError."""
    with pytest.raises(FileOperationError, match="Falha ao ler") as info:
        cipher.encrypt_file(tmp_path / "nada.txt")
    assert info.value.details["caminho"] == str(tmp_path / "nada.txt")
    corrupted = tmp_path / "ruim.enc"
    corrupted.write_bytes(b"lixo")
    with pytest.raises(CryptoError):
        cipher.decrypt_file(corrupted)
    assert not (tmp_path / "ruim").exists()
    (tmp_path / "pasta").mkdir()
    source = tmp_path / "a.txt"
    source.write_text("a", encoding="utf-8")
    with pytest.raises(FileOperationError, match="Falha ao gravar"):
        cipher.encrypt_file(source, tmp_path / "pasta")


# --- DPAPI ------------------------------------------------------------------------------------


def test_dpapi_calls_win32crypt_with_contract_arguments(fake_win32crypt: MagicMock) -> None:
    """RF-042: dpapi_* usam CryptProtectData/CryptUnprotectData com os argumentos do contrato."""
    blob = crypt_utils.dpapi_protect(b"cache local")
    fake_win32crypt.CryptProtectData.assert_called_once_with(
        b"cache local", "rpa_3003", None, None, None, 0
    )
    assert crypt_utils.dpapi_unprotect(blob) == b"cache local"
    fake_win32crypt.CryptUnprotectData.assert_called_once_with(blob, None, None, None, 0)
    crypt_utils.dpapi_protect(b"x", description="vault")
    assert fake_win32crypt.CryptProtectData.call_args.args[1] == "vault"


def test_dpapi_api_failures_become_crypto_error(fake_win32crypt: MagicMock) -> None:
    """RF-042: falhas da API (pywintypes.error) viram CryptoError."""
    fake_win32crypt.CryptProtectData.side_effect = RuntimeError("pywintypes.error")
    fake_win32crypt.CryptUnprotectData.side_effect = RuntimeError("pywintypes.error")
    with pytest.raises(CryptoError, match="cifrar"):
        crypt_utils.dpapi_protect(b"x")
    with pytest.raises(CryptoError, match="decifrar"):
        crypt_utils.dpapi_unprotect(b"x")


def test_dpapi_outside_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """RF-042: fora do Windows, CryptoError("DPAPI disponível apenas no Windows")."""
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(CryptoError, match=r"^DPAPI disponível apenas no Windows$"):
        crypt_utils.dpapi_protect(b"x")
    with pytest.raises(CryptoError, match=r"^DPAPI disponível apenas no Windows$"):
        crypt_utils.dpapi_unprotect(b"x")


def test_dpapi_without_pywin32(monkeypatch: pytest.MonkeyPatch) -> None:
    """RF-042: Windows sem pywin32 gera CryptoError."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "win32crypt", None)
    with pytest.raises(CryptoError, match="pywin32"):
        crypt_utils.dpapi_protect(b"x")


@pytest.mark.windows
def test_dpapi_real_roundtrip() -> None:
    """RF-042: dpapi_unprotect(dpapi_protect(b"x")) == b"x" (Windows)."""
    blob = crypt_utils.dpapi_protect(b"x")
    assert blob != b"x"
    assert crypt_utils.dpapi_unprotect(blob) == b"x"
    with pytest.raises(CryptoError):
        crypt_utils.dpapi_unprotect(b"blob invalido")


# --- hashes, HMAC, tokens e Base64 ------------------------------------------------------------


@pytest.mark.parametrize("algorithm", sorted(crypt_utils.SECURE_HASH_ALGORITHMS))
def test_hash_text_secure_algorithms(algorithm: str) -> None:
    """RF-042: hash_text em hexadecimal com cada algoritmo seguro."""
    expected = hashlib.new(algorithm, "ação".encode()).hexdigest()
    assert crypt_utils.hash_text("ação", algorithm) == expected
    assert crypt_utils.hash_text("ação".encode(), algorithm) == expected


def test_hash_text_normalizes_algorithm_name() -> None:
    """RF-042: nomes de algoritmo aceitam maiúsculas e hífen."""
    assert crypt_utils.hash_text("a", "SHA3-256") == hashlib.sha3_256(b"a").hexdigest()
    assert crypt_utils.hash_text("a", " SHA256 ") == hashlib.sha256(b"a").hexdigest()


@pytest.mark.parametrize("algorithm", ["md5", "sha1", "MD5"])
def test_hash_text_insecure_requires_allow_insecure(algorithm: str) -> None:
    """RNF-009: hash_text com md5/sha1 só funciona com allow_insecure=True."""
    with pytest.raises(CryptoError, match="inseguro") as info:
        crypt_utils.hash_text("a", algorithm)
    assert info.value.details["algoritmo"] == algorithm
    expected = hashlib.new(algorithm.lower(), b"a", usedforsecurity=False).hexdigest()
    assert crypt_utils.hash_text("a", algorithm, allow_insecure=True) == expected


@pytest.mark.parametrize("algorithm", ["sha224", "crc32", "shake_128", ""])
def test_hash_text_rejects_algorithms_outside_policy(algorithm: str) -> None:
    """RF-042: algoritmos fora dos dois conjuntos geram CryptoError."""
    with pytest.raises(CryptoError, match="não suportado"):
        crypt_utils.hash_text("a", algorithm, allow_insecure=True)


def test_hmac_sign_and_verify() -> None:
    """RF-042: hmac_sign gera hexadecimal; hmac_verify confere a assinatura."""
    signature = crypt_utils.hmac_sign(b"chave", "payload")
    assert signature == hmac.new(b"chave", b"payload", "sha256").hexdigest()
    assert crypt_utils.hmac_verify(b"chave", "payload", signature) is True
    assert crypt_utils.hmac_verify(b"chave", b"payload", signature.upper()) is True
    assert crypt_utils.hmac_verify(b"chave", "payload!", signature) is False
    assert crypt_utils.hmac_verify(b"outra", "payload", signature) is False
    assert crypt_utils.hmac_verify(b"chave", "payload", "ção") is False
    sha512 = crypt_utils.hmac_sign(b"chave", "payload", algorithm="sha512")
    assert crypt_utils.hmac_verify(b"chave", "payload", sha512, algorithm="sha512") is True


def test_hmac_verify_uses_constant_time_comparison(mocker: MockerFixture) -> None:
    """RF-042: hmac_verify usa comparação em tempo constante (hmac.compare_digest)."""
    spy = mocker.spy(hmac, "compare_digest")
    signature = crypt_utils.hmac_sign(b"chave", "payload")
    crypt_utils.hmac_verify(b"chave", "payload", signature)
    spy.assert_called_once_with(signature, signature)


@pytest.mark.parametrize("algorithm", ["md5", "sha1", "sha224"])
def test_hmac_rejects_insecure_or_unknown_algorithms(algorithm: str) -> None:
    """RNF-009: HMAC aceita apenas SECURE_HASH_ALGORITHMS."""
    with pytest.raises(CryptoError, match="não suportado"):
        crypt_utils.hmac_sign(b"chave", "payload", algorithm=algorithm)


def test_hmac_rejects_empty_key() -> None:
    """RNF-009: chave HMAC vazia gera CryptoError."""
    with pytest.raises(CryptoError, match="vazia"):
        crypt_utils.hmac_sign(b"", "payload")


def test_random_token_uses_secrets(mocker: MockerFixture) -> None:
    """RF-042: random_token usa secrets.token_urlsafe."""
    spy = mocker.spy(secrets, "token_urlsafe")
    token = crypt_utils.random_token()
    spy.assert_called_once_with(32)
    assert len(token) == 43
    assert token != crypt_utils.random_token()
    assert len(crypt_utils.random_token(16)) == 22
    with pytest.raises(ValueError, match="nbytes"):
        crypt_utils.random_token(0)


def test_base64_roundtrip_and_invalid_input() -> None:
    """RF-042: b64encode/b64decode padrão; espaços ignorados; inválido gera CryptoError."""
    encoded = crypt_utils.b64encode("ação")
    assert encoded == base64.b64encode("ação".encode()).decode("ascii")
    assert crypt_utils.b64decode(encoded) == "ação".encode()
    assert crypt_utils.b64encode(b"\x00\xff") == "AP8="
    assert crypt_utils.b64decode("AP\n8=\r\n") == b"\x00\xff"
    for invalid in ("@@@", "AP8", "çã=="):
        with pytest.raises(CryptoError, match="Base64 inválido"):
            crypt_utils.b64decode(invalid)


def test_usage_example(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_win32crypt: MagicMock
) -> None:
    """Seção 8.14: exemplo de uso do módulo (DPAPI simulado)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "output").mkdir(parents=True)
    (tmp_path / "data" / "output" / "relatorio.csv").write_text("a;b\n", encoding="utf-8")
    salt = crypt_utils.generate_salt()
    cifra = crypt_utils.FernetCipher.from_password("frase-do-cofre", salt)
    token = cifra.encrypt("conteúdo sigiloso")
    assert cifra.decrypt(token) == "conteúdo sigiloso"
    arquivo_cifrado = cifra.encrypt_file("data/output/relatorio.csv")
    blob = crypt_utils.dpapi_protect(b"cache local")
    assinatura = crypt_utils.hmac_sign(b"chave", "payload")
    assert arquivo_cifrado.name == "relatorio.csv.enc"
    assert crypt_utils.dpapi_unprotect(blob) == b"cache local"
    assert crypt_utils.hmac_verify(b"chave", "payload", assinatura)
