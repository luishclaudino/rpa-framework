"""Manipulação de arquivos, diretórios e arquivos TXT.

Trata particularidades do Windows: atributo somente leitura, arquivos bloqueados, nomes
reservados e codificações legadas.

Toda `OSError` é relançada como `FileOperationError`, com o caminho absoluto em `details`.
Os caminhos retornados são absolutos e resolvidos (RNF-016).
"""

from __future__ import annotations

import contextlib
import errno
import os
import shutil
import stat
import tempfile
import time
import unicodedata
import uuid
from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any, Final

from rpa_3003.core.config import PathLike
from rpa_3003.core.exceptions import DataFormatError, FileOperationError
from rpa_3003.core.logger import get_logger

__all__: list[str] = [
    "DEFAULT_ENCODINGS",
    "INVALID_FILENAME_CHARS",
    "TEMP_SUFFIXES",
    "WINDOWS_RESERVED_NAMES",
    "append_text",
    "clean_dir",
    "copy_file",
    "create_temp_dir",
    "delete_dir",
    "delete_file",
    "detect_encoding",
    "ensure_dir",
    "file_hash",
    "get_latest_file",
    "get_size",
    "is_file_locked",
    "list_files",
    "move_file",
    "read_lines",
    "read_text",
    "safe_filename",
    "wait_for_file",
    "write_lines",
    "write_text",
]

TEMP_SUFFIXES: Final[tuple[str, ...]] = (".crdownload", ".tmp", ".partial", ".part", ".download")
DEFAULT_ENCODINGS: Final[tuple[str, ...]] = ("utf-8-sig", "cp1252", "latin-1")
WINDOWS_RESERVED_NAMES: Final[frozenset[str]] = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)
INVALID_FILENAME_CHARS: Final[str] = '<>:"/\\|?*'

_ATOMIC_ATTEMPTS: Final[int] = 3
_ATOMIC_RETRY_DELAY: Final[float] = 0.5
_SECONDS_PER_DAY: Final[int] = 86_400
_FALLBACK_FILENAME: Final[str] = "_"
_RESERVED_PREFIX: Final[str] = "_"
# ERROR_NOT_SAME_DEVICE: `os.replace` entre volumes diferentes no Windows.
_WINDOWS_NOT_SAME_DEVICE: Final[int] = 17


def ensure_dir(path: PathLike) -> Path:
    """Cria a pasta e as pastas intermediárias, se ainda não existirem.

    Args:
        path: Pasta a garantir.

    Returns:
        Caminho absoluto da pasta.

    Raises:
        FileOperationError: Se a pasta não puder ser criada (por exemplo, existe um arquivo com
            o mesmo nome).
    """
    target = Path(path)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _os_error("Falha ao criar pasta", target, exc) from exc
    return target.resolve()


def list_files(
    directory: PathLike, pattern: str = "*", *, recursive: bool = False, include_dirs: bool = False
) -> list[Path]:
    """Lista os itens de uma pasta que casam com o padrão glob, ordenados por caminho.

    Args:
        directory: Pasta a listar.
        pattern: Padrão glob (ex.: `"*.csv"`).
        recursive: Percorre as subpastas (`Path.rglob`).
        include_dirs: Inclui pastas no resultado; por padrão, somente arquivos.

    Returns:
        Caminhos absolutos ordenados.

    Raises:
        FileOperationError: Se a pasta não existir ou não puder ser lida.
    """
    base = _existing_dir(directory)
    try:
        matches = base.rglob(pattern) if recursive else base.glob(pattern)
        return sorted(
            item for item in matches if item.is_file() or (include_dirs and item.is_dir())
        )
    except OSError as exc:
        raise _os_error("Falha ao listar pasta", base, exc, padrao=pattern) from exc


def get_latest_file(directory: PathLike, pattern: str = "*") -> Path | None:
    """Retorna o arquivo mais recente (maior `st_mtime`) que casa com o padrão.

    Args:
        directory: Pasta a pesquisar.
        pattern: Padrão glob.

    Returns:
        Caminho absoluto do arquivo mais recente, ou `None` se nenhum arquivo casar.

    Raises:
        FileOperationError: Se a pasta não existir ou não puder ser lida.
    """
    latest: tuple[float, Path] | None = None
    for item in list_files(directory, pattern):
        mtime = _mtime_or_none(item)
        if mtime is not None and (latest is None or mtime > latest[0]):
            latest = (mtime, item)
    return None if latest is None else latest[1]


def copy_file(src: PathLike, dst: PathLike, *, overwrite: bool = False) -> Path:
    """Copia um arquivo preservando metadados (`shutil.copy2`).

    Args:
        src: Arquivo de origem.
        dst: Arquivo ou pasta de destino; pasta existente mantém o nome original.
        overwrite: Substitui o destino existente (inclusive somente leitura).

    Returns:
        Caminho absoluto da cópia.

    Raises:
        FileOperationError: Se a origem não existir, o destino existir sem `overwrite` ou a
            cópia falhar.
    """
    source = _existing_file(src)
    target = _prepare_target(source, dst, overwrite=overwrite)
    try:
        shutil.copy2(source, target)
    except OSError as exc:
        raise _os_error("Falha ao copiar arquivo", target, exc, origem=str(source)) from exc
    get_logger().debug("Arquivo copiado | origem={} | destino={}", source, target)
    return target.resolve()


def move_file(src: PathLike, dst: PathLike, *, overwrite: bool = False) -> Path:
    """Move um arquivo, inclusive entre volumes diferentes.

    Args:
        src: Arquivo de origem.
        dst: Arquivo ou pasta de destino; pasta existente mantém o nome original.
        overwrite: Substitui o destino existente (inclusive somente leitura).

    Returns:
        Caminho absoluto do arquivo no destino.

    Raises:
        FileOperationError: Se a origem não existir, o destino existir sem `overwrite` ou a
            movimentação falhar.
    """
    source = _existing_file(src)
    target = _prepare_target(source, dst, overwrite=overwrite)
    try:
        try:
            os.replace(source, target)
        except OSError as exc:
            if not _is_cross_device(exc):
                raise
            shutil.move(source, target)  # volumes diferentes: copia e remove a origem
    except OSError as exc:
        raise _os_error("Falha ao mover arquivo", target, exc, origem=str(source)) from exc
    get_logger().debug("Arquivo movido | origem={} | destino={}", source, target)
    return target.resolve()


def delete_file(path: PathLike, *, missing_ok: bool = True) -> bool:
    """Remove um arquivo, inclusive com atributo somente leitura.

    Args:
        path: Arquivo a remover.
        missing_ok: Quando `False`, arquivo inexistente gera `FileOperationError`.

    Returns:
        `True` se o arquivo foi removido; `False` se não existia.

    Raises:
        FileOperationError: Se o caminho for uma pasta, o arquivo não existir com
            `missing_ok=False` ou a remoção falhar.
    """
    target = Path(path)
    if target.is_dir():
        raise _error("Caminho é uma pasta; use delete_dir", target)
    if not target.exists() and not target.is_symlink():
        if missing_ok:
            return False
        raise _error("Arquivo não encontrado", target)
    try:
        _unlink(target)
    except OSError as exc:
        raise _os_error("Falha ao remover arquivo", target, exc) from exc
    get_logger().debug("Arquivo removido | arquivo={}", _display(target))
    return True


def delete_dir(path: PathLike, *, missing_ok: bool = True) -> bool:
    """Remove uma pasta e todo o seu conteúdo, inclusive arquivos somente leitura.

    Args:
        path: Pasta a remover.
        missing_ok: Quando `False`, pasta inexistente gera `FileOperationError`.

    Returns:
        `True` se a pasta foi removida; `False` se não existia.

    Raises:
        FileOperationError: Se o caminho for um arquivo, a pasta não existir com
            `missing_ok=False` ou a remoção falhar.
    """
    target = Path(path)
    if not target.exists():
        if missing_ok:
            return False
        raise _error("Pasta não encontrada", target)
    if not target.is_dir():
        raise _error("Caminho não é uma pasta", target)
    try:
        shutil.rmtree(target, onexc=_remove_readonly)
    except OSError as exc:
        raise _os_error("Falha ao remover pasta", target, exc) from exc
    get_logger().debug("Pasta removida | pasta={}", _display(target))
    return True


def clean_dir(
    directory: PathLike,
    pattern: str = "*",
    *,
    older_than_days: int | None = None,
    recursive: bool = False,
) -> int:
    """Remove os arquivos da pasta que casam com o padrão e, opcionalmente, são antigos.

    Pastas nunca são removidas. Pasta inexistente não é erro: nada há a limpar.

    Args:
        directory: Pasta a limpar.
        pattern: Padrão glob dos arquivos a remover.
        older_than_days: Remove apenas arquivos com `st_mtime` anterior a agora menos N dias.
        recursive: Inclui as subpastas.

    Returns:
        Quantidade de arquivos removidos.

    Raises:
        ValueError: Se `older_than_days` for negativo.
        FileOperationError: Se o caminho não for uma pasta ou a listagem ou a remoção falhar.
    """
    if older_than_days is not None and older_than_days < 0:
        raise ValueError(f"older_than_days não pode ser negativo (recebido: {older_than_days})")
    if not Path(directory).exists():
        return 0
    cutoff = None if older_than_days is None else time.time() - older_than_days * _SECONDS_PER_DAY
    removed = 0
    for item in list_files(directory, pattern, recursive=recursive):
        mtime = _mtime_or_none(item)
        if mtime is None or (cutoff is not None and mtime >= cutoff):
            continue
        if delete_file(item):
            removed += 1
    get_logger().info(
        "Pasta limpa | pasta={} | padrao={} | removidos={}", _display(directory), pattern, removed
    )
    return removed


def wait_for_file(
    directory: PathLike,
    pattern: str = "*",
    *,
    timeout: float = 60.0,
    poll_interval: float = 0.5,
    stable_seconds: float = 1.0,
    ignore_suffixes: Sequence[str] = TEMP_SUFFIXES,
) -> Path:
    """Aguarda um arquivo que case com o padrão e cujo tamanho fique estável.

    Arquivos com sufixo de `ignore_suffixes` (downloads em andamento) são ignorados. A pasta
    pode ainda não existir no início da espera. Havendo mais de um arquivo estável, retorna o
    de maior `st_mtime`.

    Args:
        directory: Pasta monitorada.
        pattern: Padrão glob do arquivo esperado.
        timeout: Prazo máximo em segundos.
        poll_interval: Intervalo entre verificações em segundos.
        stable_seconds: Tempo em segundos sem mudança de tamanho para considerar o arquivo pronto.
        ignore_suffixes: Sufixos de arquivos temporários, comparados sem diferenciar maiúsculas.

    Returns:
        Caminho absoluto do arquivo.

    Raises:
        ValueError: Se `poll_interval` não for positivo ou `timeout`/`stable_seconds` forem
            negativos.
        FileOperationError: Se o prazo se esgotar, com pasta, padrão e timeout em `details`.
    """
    if poll_interval <= 0 or timeout < 0 or stable_seconds < 0:
        raise ValueError("poll_interval deve ser positivo; timeout e stable_seconds, não negativos")
    base = Path(directory)
    suffixes = tuple(suffix.lower() for suffix in ignore_suffixes)
    deadline = time.monotonic() + timeout
    observed: dict[Path, tuple[int, float]] = {}
    while True:
        now = time.monotonic()
        found = _stable_candidate(base, pattern, suffixes, observed, now, stable_seconds)
        if found is not None:
            get_logger().info("Arquivo disponível | arquivo={}", found)
            return found
        if now >= deadline:
            raise FileOperationError(
                "Arquivo não encontrado no prazo",
                details={"pasta": _display(base), "padrao": pattern, "timeout": timeout},
            )
        time.sleep(min(poll_interval, max(deadline - now, 0.0)))


def is_file_locked(path: PathLike) -> bool:
    """Indica se o arquivo está bloqueado para escrita por outro processo.

    Tenta abrir o arquivo em modo `r+b`. Um arquivo com atributo somente leitura também
    retorna `True`, pois não pode ser aberto para escrita.

    Args:
        path: Arquivo a verificar.

    Returns:
        `True` em `PermissionError`; `False` se o arquivo abrir ou não existir.

    Raises:
        FileOperationError: Em outras falhas do sistema operacional.
    """
    target = Path(path)
    if target.is_dir():
        return False
    try:
        with open(target, "r+b"):
            return False
    except PermissionError:
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise _os_error("Falha ao verificar bloqueio de arquivo", target, exc) from exc


def file_hash(
    path: PathLike,
    algorithm: str = "sha256",
    *,
    chunk_size: int = 1_048_576,
    allow_insecure: bool = False,
) -> str:
    """Calcula o hash hexadecimal do arquivo, lendo em blocos.

    Args:
        path: Arquivo de entrada.
        algorithm: Algoritmo de `crypt_utils.SECURE_HASH_ALGORITHMS` ou, com `allow_insecure`,
            de `crypt_utils.INSECURE_HASH_ALGORITHMS` (`md5`, `sha1`).
        chunk_size: Tamanho do bloco de leitura em bytes.
        allow_insecure: Permite `md5` e `sha1`, apenas como checksum (RNF-009).

    Returns:
        Hash em hexadecimal.

    Raises:
        ValueError: Se `chunk_size` não for positivo.
        CryptoError: Se o algoritmo for inseguro sem `allow_insecure` ou não suportado.
        FileOperationError: Se o arquivo não puder ser lido.
    """
    from rpa_3003.utils.crypt_utils import _new_hash

    if chunk_size <= 0:
        raise ValueError(f"chunk_size deve ser positivo (recebido: {chunk_size})")
    hasher = _new_hash(algorithm, allow_insecure=allow_insecure)
    source = Path(path)
    try:
        with source.open("rb") as handle:
            while chunk := handle.read(chunk_size):
                hasher.update(chunk)
    except OSError as exc:
        raise _os_error("Falha ao ler arquivo", source, exc) from exc
    return hasher.hexdigest()


def safe_filename(name: str, *, replacement: str = "_", max_length: int = 200) -> str:
    """Converte um texto em nome de arquivo válido no Windows.

    Substitui os caracteres de `INVALID_FILENAME_CHARS` e de controle por `replacement`,
    remove espaços e pontos finais, prefixa `_` em nomes reservados (`CON`, `COM1`...) e trunca
    em `max_length` preservando a extensão. Nome vazio resulta em `"_"`.

    Args:
        name: Texto de origem (ex.: descrição vinda de um sistema).
        replacement: Texto que substitui cada caractere inválido.
        max_length: Tamanho máximo do resultado.

    Returns:
        Nome de arquivo seguro.

    Raises:
        ValueError: Se `max_length` for menor que 1 ou `replacement` contiver caractere
            inválido.

    Example:
        >>> safe_filename("rel:2026/10?.csv")
        'rel_2026_10_.csv'
    """
    if max_length < 1:
        raise ValueError(f"max_length deve ser maior ou igual a 1 (recebido: {max_length})")
    if any(_is_invalid_char(char) for char in replacement):
        raise ValueError("replacement contém caractere inválido em nomes de arquivo")
    cleaned = "".join(replacement if _is_invalid_char(char) else char for char in name)
    cleaned = cleaned.rstrip(" .") or _FALLBACK_FILENAME
    if _is_reserved_name(cleaned):
        cleaned = _RESERVED_PREFIX + cleaned
    result = _truncate_filename(cleaned, max_length)
    if _is_reserved_name(result):
        result = _truncate_filename(_RESERVED_PREFIX + result, max_length)
    return result


def get_size(path: PathLike) -> int:
    """Retorna o tamanho em bytes de um arquivo ou a soma dos arquivos de uma pasta.

    Args:
        path: Arquivo ou pasta (percorrida recursivamente).

    Returns:
        Tamanho em bytes.

    Raises:
        FileOperationError: Se o caminho não existir ou não puder ser lido.
    """
    target = Path(path)
    if not target.exists():
        raise _error("Caminho não encontrado", target)
    try:
        if target.is_file():
            return target.stat().st_size
        return sum(item.stat().st_size for item in target.rglob("*") if item.is_file())
    except OSError as exc:
        raise _os_error("Falha ao obter tamanho", target, exc) from exc


def detect_encoding(path: PathLike, candidates: Sequence[str] = DEFAULT_ENCODINGS) -> str:
    """Detecta a codificação tentando decodificar o arquivo inteiro com cada candidata.

    Args:
        path: Arquivo de texto.
        candidates: Codificações na ordem de preferência.

    Returns:
        A primeira codificação que decodifica o arquivo sem erro.

    Raises:
        FileOperationError: Se o arquivo não puder ser lido.
        DataFormatError: Se nenhuma candidata decodificar o arquivo.
    """
    source = Path(path)
    encoding, _text = _decode_any(_read_bytes(source), source, candidates)
    return encoding


def read_text(path: PathLike, encoding: str | None = None) -> str:
    """Lê um arquivo de texto com quebras de linha normalizadas para `\\n`.

    Args:
        path: Arquivo de texto.
        encoding: Codificação; `None` usa `detect_encoding` (UTF-8, cp1252 e latin-1).

    Returns:
        Conteúdo do arquivo.

    Raises:
        FileOperationError: Se o arquivo não puder ser lido.
        DataFormatError: Se o conteúdo não puder ser decodificado.
    """
    source = Path(path)
    data = _read_bytes(source)
    if encoding is None:
        _encoding, text = _decode_any(data, source, DEFAULT_ENCODINGS)
    else:
        text = _decode(data, source, encoding)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def write_text(
    path: PathLike, content: str, *, encoding: str = "utf-8", atomic: bool = True
) -> Path:
    """Grava um arquivo de texto, criando a pasta de destino.

    Com `atomic=True`, grava em `<nome>.<uuid>.tmp` na mesma pasta e substitui o destino com
    `os.replace`; se o destino estiver bloqueado (`PermissionError`), faz 3 tentativas com
    0,5 s de intervalo.

    Args:
        path: Arquivo de destino.
        content: Texto a gravar; `\\n` vira a quebra de linha da plataforma.
        encoding: Codificação de gravação.
        atomic: Usa gravação atômica.

    Returns:
        Caminho absoluto do arquivo gravado.

    Raises:
        FileOperationError: Se a gravação falhar.
        DataFormatError: Se o conteúdo não puder ser representado na codificação.
    """
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if atomic:
            _atomic_write(target, content, encoding)
        else:
            with target.open("w", encoding=encoding) as handle:
                handle.write(content)
    except UnicodeEncodeError as exc:
        raise _encode_error(target, encoding, exc) from exc
    except OSError as exc:
        raise _os_error("Falha ao gravar arquivo", target, exc) from exc
    return target.resolve()


def append_text(
    path: PathLike, content: str, *, encoding: str = "utf-8", newline: bool = True
) -> Path:
    """Acrescenta texto ao final do arquivo, criando o arquivo e a pasta se necessário.

    Args:
        path: Arquivo de destino.
        content: Texto a acrescentar.
        encoding: Codificação de gravação.
        newline: Acrescenta uma quebra de linha após o texto.

    Returns:
        Caminho absoluto do arquivo.

    Raises:
        FileOperationError: Se a gravação falhar.
        DataFormatError: Se o conteúdo não puder ser representado na codificação.
    """
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding=encoding) as handle:
            handle.write(f"{content}\n" if newline else content)
    except UnicodeEncodeError as exc:
        raise _encode_error(target, encoding, exc) from exc
    except OSError as exc:
        raise _os_error("Falha ao gravar arquivo", target, exc) from exc
    return target.resolve()


def read_lines(
    path: PathLike, encoding: str | None = None, *, strip: bool = True, skip_empty: bool = False
) -> list[str]:
    """Lê as linhas de um arquivo de texto.

    Somente `\\n`, `\\r\\n` e `\\r` separam linhas; caracteres como o form feed de relatórios
    legados são preservados.

    Args:
        path: Arquivo de texto.
        encoding: Codificação; `None` usa `detect_encoding`.
        strip: Remove espaços nas pontas de cada linha.
        skip_empty: Descarta linhas vazias ou só com espaços.

    Returns:
        Linhas do arquivo, sem as quebras de linha.

    Raises:
        FileOperationError: Se o arquivo não puder ser lido.
        DataFormatError: Se o conteúdo não puder ser decodificado.
    """
    lines = read_text(path, encoding).split("\n")
    if lines[-1] == "":
        lines.pop()
    if strip:
        lines = [line.strip() for line in lines]
    if skip_empty:
        lines = [line for line in lines if line.strip()]
    return lines


def write_lines(
    path: PathLike, lines: Iterable[str], *, encoding: str = "utf-8", atomic: bool = True
) -> Path:
    """Grava as linhas no arquivo, cada uma seguida de quebra de linha.

    Args:
        path: Arquivo de destino.
        lines: Linhas a gravar.
        encoding: Codificação de gravação.
        atomic: Usa gravação atômica (ver `write_text`).

    Returns:
        Caminho absoluto do arquivo gravado.

    Raises:
        FileOperationError: Se a gravação falhar.
        DataFormatError: Se o conteúdo não puder ser representado na codificação.
    """
    content = "".join(f"{line}\n" for line in lines)
    return write_text(path, content, encoding=encoding, atomic=atomic)


def create_temp_dir(prefix: str = "rpa3003_") -> Path:
    """Cria uma pasta temporária exclusiva (`tempfile.mkdtemp`).

    A remoção fica a cargo de quem chama (ex.: `ctx.register_cleanup`).

    Args:
        prefix: Prefixo do nome da pasta.

    Returns:
        Caminho absoluto da pasta criada.

    Raises:
        FileOperationError: Se a pasta não puder ser criada.
    """
    try:
        return Path(tempfile.mkdtemp(prefix=prefix)).resolve()
    except OSError as exc:
        raise _os_error(
            "Falha ao criar pasta temporária", Path(tempfile.gettempdir()), exc
        ) from exc


def _display(path: PathLike) -> str:
    """Caminho absoluto resolvido para mensagens (RNF-016), sem falhar."""
    try:
        return str(Path(path).resolve())
    except (OSError, RuntimeError):
        return str(Path(path).absolute())


def _describe_os_error(exc: OSError) -> str:
    return f"{type(exc).__name__}: {exc.strerror}" if exc.strerror else type(exc).__name__


def _error(message: str, path: PathLike, **extra: object) -> FileOperationError:
    return FileOperationError(message, details={"caminho": _display(path), **extra})


def _os_error(message: str, path: PathLike, exc: OSError, **extra: object) -> FileOperationError:
    return _error(message, path, **extra, erro=_describe_os_error(exc))


def _encode_error(path: Path, encoding: str, exc: UnicodeEncodeError) -> DataFormatError:
    return DataFormatError(
        "Conteúdo não representável na codificação",
        details={"caminho": _display(path), "codificacao": encoding, "posicao": exc.start},
    )


def _existing_dir(directory: PathLike) -> Path:
    base = Path(directory)
    if not base.exists():
        raise _error("Pasta não encontrada", base)
    if not base.is_dir():
        raise _error("Caminho não é uma pasta", base)
    return base.resolve()


def _existing_file(path: PathLike) -> Path:
    source = Path(path)
    if not source.is_file():
        raise _error("Arquivo de origem não encontrado", source)
    return source.resolve()


def _prepare_target(source: Path, dst: PathLike, *, overwrite: bool) -> Path:
    """Resolve o destino de cópia/movimentação, valida `overwrite` e cria a pasta."""
    target = Path(dst)
    if target.is_dir():
        target = target / source.name
    if target.exists():
        if not overwrite:
            raise _error("Destino já existe", target, origem=str(source))
        if target.is_file():
            _make_writable(target)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _os_error("Falha ao criar pasta de destino", target.parent, exc) from exc
    return target


def _make_writable(path: Path) -> None:
    """Remove o atributo somente leitura, quando presente."""
    try:
        mode = path.stat().st_mode
        if not mode & stat.S_IWRITE:
            path.chmod(mode | stat.S_IWRITE)
    except OSError as exc:
        raise _os_error("Falha ao remover atributo somente leitura", path, exc) from exc


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except PermissionError:
        os.chmod(path, stat.S_IWRITE)  # atributo somente leitura impede a remoção no Windows
        path.unlink()


def _remove_readonly(func: Callable[..., Any], path: str, exc: BaseException) -> None:
    """Handler `onexc` de `shutil.rmtree`: remove o atributo somente leitura e repete."""
    if isinstance(exc, FileNotFoundError):
        return
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _is_cross_device(exc: OSError) -> bool:
    return exc.errno == errno.EXDEV or getattr(exc, "winerror", None) == _WINDOWS_NOT_SAME_DEVICE


def _mtime_or_none(path: Path) -> float | None:
    """`st_mtime` do arquivo; `None` se ele sumiu entre a listagem e a consulta."""
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _os_error("Falha ao consultar arquivo", path, exc) from exc


def _iter_candidates(base: Path, pattern: str, suffixes: tuple[str, ...]) -> Iterator[Path]:
    if not base.is_dir():
        return
    for item in sorted(base.glob(pattern)):
        if not item.name.lower().endswith(suffixes) and item.is_file():
            yield item


def _stable_candidate(
    base: Path,
    pattern: str,
    suffixes: tuple[str, ...],
    observed: dict[Path, tuple[int, float]],
    now: float,
    stable_seconds: float,
) -> Path | None:
    """Atualiza os tamanhos observados e retorna o arquivo estável mais recente, se houver."""
    stable: list[tuple[float, Path]] = []
    try:
        for item in _iter_candidates(base, pattern, suffixes):
            info = item.stat()
            size_since = observed.get(item)
            if size_since is None or size_since[0] != info.st_size:
                size_since = observed[item] = (info.st_size, now)
            if now - size_since[1] >= stable_seconds:
                stable.append((info.st_mtime, item))
    except FileNotFoundError:
        return None  # arquivo renomeado durante a verificação; tenta de novo no próximo ciclo
    except OSError as exc:
        raise _os_error("Falha ao verificar pasta", base, exc, padrao=pattern) from exc
    return max(stable)[1].resolve() if stable else None


def _is_invalid_char(char: str) -> bool:
    return char in INVALID_FILENAME_CHARS or unicodedata.category(char) == "Cc"


def _is_reserved_name(name: str) -> bool:
    """Compara a parte antes do primeiro ponto, sem diferenciar maiúsculas."""
    return name.split(".", 1)[0].rstrip(" ").upper() in WINDOWS_RESERVED_NAMES


def _truncate_filename(name: str, max_length: int) -> str:
    if len(name) <= max_length:
        return name
    suffix = Path(name).suffix
    if not suffix or len(suffix) >= max_length:
        return name[:max_length].rstrip(" .") or _FALLBACK_FILENAME
    stem = name[: max_length - len(suffix)].rstrip(" .") or _FALLBACK_FILENAME
    return f"{stem}{suffix}"[:max_length]


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise _os_error("Falha ao ler arquivo", path, exc) from exc


def _decode(data: bytes, path: Path, encoding: str) -> str:
    try:
        return data.decode(encoding)
    except UnicodeDecodeError as exc:
        raise DataFormatError(
            "Falha ao decodificar arquivo de texto",
            details={"caminho": _display(path), "codificacao": encoding, "posicao": exc.start},
        ) from exc


def _decode_any(data: bytes, path: Path, candidates: Sequence[str]) -> tuple[str, str]:
    for encoding in candidates:
        try:
            return encoding, data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise DataFormatError(
        "Codificação do arquivo não identificada",
        details={"caminho": _display(path), "candidatas": ", ".join(candidates)},
    )


def _atomic_write(target: Path, content: str, encoding: str) -> None:
    """Grava em arquivo temporário na mesma pasta e substitui o destino (RNF-007)."""
    temp = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("w", encoding=encoding) as handle:
            handle.write(content)
        _replace_with_retry(temp, target)
    finally:
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)


def _replace_with_retry(temp: Path, target: Path) -> None:
    for attempt in range(1, _ATOMIC_ATTEMPTS + 1):
        try:
            os.replace(temp, target)
            return
        except PermissionError as exc:
            if attempt == _ATOMIC_ATTEMPTS:
                raise _os_error(
                    "Destino bloqueado na gravação atômica", target, exc, tentativas=attempt
                ) from exc
            get_logger().warning(
                "Destino bloqueado na gravação atômica; nova tentativa | arquivo={} | tentativa={}",
                _display(target),
                attempt,
            )
            time.sleep(_ATOMIC_RETRY_DELAY)
