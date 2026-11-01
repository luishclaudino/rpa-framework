"""Arquivos compactados TAR e ZIP.

Criação, listagem, anexação e extração segura de arquivos ZIP e TAR.

Segurança (RNF-010): antes de extrair, todos os membros são validados contra path traversal
(Zip Slip). Nomes absolutos, com letra de unidade, com `..` ou que resolvam para fora da pasta
de destino geram `ArchiveError` e nada é extraído. No TAR, o filtro `"data"` do `tarfile` também
é aplicado previamente a todos os membros (links para fora do destino, arquivos de dispositivo).
"""

from __future__ import annotations

import contextlib
import lzma
import os
import re
import tarfile
import uuid
import zipfile
import zlib
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Final, Literal, TypeAlias

from rpa_3003.core.config import PathLike
from rpa_3003.core.exceptions import ArchiveError, FileOperationError
from rpa_3003.core.logger import get_logger

__all__: list[str] = [
    "SUPPORTED_EXTENSIONS",
    "TarCompression",
    "add_to_zip",
    "create_tar",
    "create_zip",
    "extract_archive",
    "extract_tar",
    "extract_zip",
    "is_supported_archive",
    "list_tar",
    "list_zip",
]

TarCompression: TypeAlias = Literal["", "gz", "bz2", "xz"]  # noqa: UP040 - convenção 7.7
SUPPORTED_EXTENSIONS: Final[tuple[str, ...]] = (
    ".zip",
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tbz2",
    ".tar.xz",
    ".txz",
)

_ZIP_EXTENSION: Final[str] = ".zip"
_TAR_WRITE_MODES: Final[dict[str, Literal["w", "w:gz", "w:bz2", "w:xz"]]] = {
    "": "w",
    "gz": "w:gz",
    "bz2": "w:bz2",
    "xz": "w:xz",
}
_DRIVE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z]:")
_ARCHIVE_SEPARATOR: Final[str] = "/"
_SEPARATORS_PATTERN: Final[re.Pattern[str]] = re.compile(r"[/\\]")
# Erros de formato/conteúdo de arquivos compactados (além de OSError).
_ZIP_ERRORS: Final[tuple[type[Exception], ...]] = (
    zipfile.BadZipFile,
    zipfile.LargeZipFile,
    RuntimeError,  # senha ausente ou incorreta
    NotImplementedError,  # método de compressão ou criptografia não suportado
    zlib.error,
    EOFError,
)
_TAR_ERRORS: Final[tuple[type[Exception], ...]] = (
    tarfile.TarError,
    zlib.error,
    lzma.LZMAError,
    EOFError,
)


def create_zip(
    sources: PathLike | Sequence[PathLike],
    dest: PathLike,
    *,
    base_dir: PathLike | None = None,
    compression_level: int = 6,
    overwrite: bool = False,
) -> Path:
    """Cria um arquivo ZIP (`ZIP_DEFLATED`) com arquivos e pastas.

    Nomes internos: relativos a `base_dir` quando informado; sem `base_dir`, uma pasta entra
    com o próprio nome como raiz e um arquivo entra pelo nome. Separador sempre `/`; pastas são
    percorridas recursivamente em ordem alfabética. O ZIP é gravado em arquivo temporário e
    renomeado ao final, para nunca deixar um arquivo parcial no destino.

    Args:
        sources: Arquivo, pasta ou lista deles.
        dest: Arquivo ZIP de destino; a pasta é criada se necessário.
        base_dir: Pasta de referência dos nomes internos.
        compression_level: Nível de compressão de 0 a 9.
        overwrite: Substitui o destino existente.

    Returns:
        Caminho absoluto do ZIP criado.

    Raises:
        ValueError: Se `compression_level` estiver fora de 0 a 9.
        ArchiveError: Se o destino existir sem `overwrite`, uma origem estiver fora de
            `base_dir` ou houver nomes internos duplicados.
        FileOperationError: Se uma origem não existir ou a gravação falhar.
    """
    if not 0 <= compression_level <= 9:
        raise ValueError(
            f"compression_level deve estar entre 0 e 9 (recebido: {compression_level})"
        )
    target = _prepare_dest(dest, overwrite=overwrite)
    entries = _collect_entries(sources, base_dir, exclude=target)
    with (
        _archive_errors(target, _ZIP_ERRORS),
        _staging_file(target) as staging,
        zipfile.ZipFile(
            staging, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=compression_level
        ) as archive,
    ):
        for path, arcname in entries:
            archive.write(path, arcname)
    get_logger().info("Arquivo ZIP criado | arquivo={} | itens={}", target, len(entries))
    return target


def add_to_zip(
    archive: PathLike,
    sources: PathLike | Sequence[PathLike],
    *,
    base_dir: PathLike | None = None,
) -> Path:
    """Acrescenta arquivos e pastas a um ZIP (cria o ZIP se não existir).

    Usa as mesmas regras de nomes internos de `create_zip`.

    Args:
        archive: Arquivo ZIP.
        sources: Arquivo, pasta ou lista deles.
        base_dir: Pasta de referência dos nomes internos.

    Returns:
        Caminho absoluto do ZIP.

    Raises:
        ArchiveError: Se o arquivo não for ZIP válido, uma origem estiver fora de `base_dir` ou
            algum nome interno já existir no ZIP.
        FileOperationError: Se uma origem não existir ou a gravação falhar.
    """
    target = Path(archive).resolve()
    if target.exists() and not zipfile.is_zipfile(target):
        # O modo "a" do zipfile anexaria um ZIP novo ao final de um arquivo que não é ZIP.
        raise ArchiveError("Arquivo existente não é ZIP válido", details={"arquivo": str(target)})
    entries = _collect_entries(sources, base_dir, exclude=target)
    with _archive_errors(target, _ZIP_ERRORS):
        target.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(target, "a", compression=zipfile.ZIP_DEFLATED) as zip_file:
            existing = set(zip_file.namelist())
            repeated = sorted(arcname for _path, arcname in entries if arcname in existing)
            if repeated:
                raise ArchiveError(
                    "Nomes já existem no ZIP",
                    details={"arquivo": str(target), "nomes": ", ".join(repeated)},
                )
            for path, arcname in entries:
                zip_file.write(path, arcname)
    get_logger().info("Itens adicionados ao ZIP | arquivo={} | itens={}", target, len(entries))
    return target


def list_zip(archive: PathLike, *, metadata_encoding: str | None = None) -> list[str]:
    """Lista os nomes internos de um ZIP, na ordem em que foram gravados.

    Args:
        archive: Arquivo ZIP.
        metadata_encoding: Codificação dos nomes sem o indicador UTF-8 (ex.: `"cp850"` para ZIPs
            criados pelo Explorer do Windows com nomes acentuados).

    Returns:
        Nomes internos; pastas terminam com `/`.

    Raises:
        ArchiveError: Se o arquivo não for ZIP válido.
        FileOperationError: Se o arquivo não existir ou não puder ser lido.
    """
    source = _existing_archive(archive)
    with (
        _archive_errors(source, _ZIP_ERRORS),
        zipfile.ZipFile(source, metadata_encoding=metadata_encoding) as zip_file,
    ):
        return zip_file.namelist()


def extract_zip(
    archive: PathLike,
    dest_dir: PathLike,
    *,
    members: Sequence[str] | None = None,
    password: str | None = None,
    metadata_encoding: str | None = None,
) -> list[Path]:
    """Extrai um ZIP com proteção contra Zip Slip.

    Args:
        archive: Arquivo ZIP.
        dest_dir: Pasta de destino; criada se necessário.
        members: Nomes internos a extrair; `None` extrai todos.
        password: Senha de ZIP com criptografia tradicional (ZipCrypto); AES não é suportado
            pelo `zipfile`.
        metadata_encoding: Codificação dos nomes sem o indicador UTF-8 (ex.: `"cp850"`).

    Returns:
        Caminhos absolutos dos arquivos extraídos (sem pastas), ordenados.

    Raises:
        ArchiveError: Se algum membro for inseguro ou inexistente (nada é extraído), a senha
            estiver ausente ou incorreta, ou o arquivo estiver corrompido.
        FileOperationError: Se o arquivo não existir ou a gravação falhar.
    """
    source = _existing_archive(archive)
    destination = _prepare_dest_dir(dest_dir)
    pwd = password.encode("utf-8") if password is not None else None
    with (
        _archive_errors(source, _ZIP_ERRORS),
        zipfile.ZipFile(source, metadata_encoding=metadata_encoding) as zip_file,
    ):
        selected = _select(zip_file.infolist(), members, lambda info: info.filename, source)
        for info in selected:
            _check_member_name(info.filename, destination, source)
        results = [(info, zip_file.extract(info, destination, pwd=pwd)) for info in selected]
    extracted = [Path(result).resolve() for info, result in results if not info.is_dir()]
    get_logger().info(
        "Arquivo ZIP extraído | arquivo={} | destino={} | arquivos={}",
        source,
        destination,
        len(extracted),
    )
    return sorted(extracted)


def create_tar(
    sources: PathLike | Sequence[PathLike],
    dest: PathLike,
    *,
    compression: TarCompression = "gz",
    base_dir: PathLike | None = None,
    overwrite: bool = False,
) -> Path:
    """Cria um arquivo TAR, opcionalmente compactado.

    Usa as mesmas regras de nomes internos de `create_zip` e também grava em arquivo temporário.

    Args:
        sources: Arquivo, pasta ou lista deles.
        dest: Arquivo TAR de destino; a pasta é criada se necessário.
        compression: `""` (sem compressão), `"gz"`, `"bz2"` ou `"xz"`.
        base_dir: Pasta de referência dos nomes internos.
        overwrite: Substitui o destino existente.

    Returns:
        Caminho absoluto do TAR criado.

    Raises:
        ValueError: Se `compression` não for suportada.
        ArchiveError: Se o destino existir sem `overwrite`, uma origem estiver fora de
            `base_dir` ou houver nomes internos duplicados.
        FileOperationError: Se uma origem não existir ou a gravação falhar.
    """
    mode = _TAR_WRITE_MODES.get(compression)
    if mode is None:
        raise ValueError(
            f"compression deve ser '', 'gz', 'bz2' ou 'xz' (recebido: {compression!r})"
        )
    target = _prepare_dest(dest, overwrite=overwrite)
    entries = _collect_entries(sources, base_dir, exclude=target)
    with (
        _archive_errors(target, _TAR_ERRORS),
        _staging_file(target) as staging,
        tarfile.open(staging, mode) as tar_file,
    ):
        for path, arcname in entries:
            tar_file.add(path, arcname=arcname.rstrip(_ARCHIVE_SEPARATOR), recursive=False)
    get_logger().info("Arquivo TAR criado | arquivo={} | itens={}", target, len(entries))
    return target


def list_tar(archive: PathLike) -> list[str]:
    """Lista os nomes internos de um TAR (detecta a compressão automaticamente).

    Args:
        archive: Arquivo TAR.

    Returns:
        Nomes internos, na ordem em que foram gravados.

    Raises:
        ArchiveError: Se o arquivo não for TAR válido.
        FileOperationError: Se o arquivo não existir ou não puder ser lido.
    """
    source = _existing_archive(archive)
    with _archive_errors(source, _TAR_ERRORS), tarfile.open(source, "r:*") as tar_file:
        return tar_file.getnames()


def extract_tar(
    archive: PathLike, dest_dir: PathLike, *, members: Sequence[str] | None = None
) -> list[Path]:
    """Extrai um TAR com `filter="data"` e proteção contra path traversal.

    O filtro `"data"` é aplicado a todos os membros antes da extração; qualquer membro inseguro
    gera `ArchiveError` sem que nada seja extraído.

    Args:
        archive: Arquivo TAR (compressão detectada automaticamente).
        dest_dir: Pasta de destino; criada se necessário.
        members: Nomes internos a extrair; `None` extrai todos.

    Returns:
        Caminhos absolutos dos arquivos extraídos (sem pastas), ordenados.

    Raises:
        ArchiveError: Se algum membro for inseguro ou inexistente, ou o arquivo estiver
            corrompido.
        FileOperationError: Se o arquivo não existir ou a gravação falhar.
    """
    source = _existing_archive(archive)
    destination = _prepare_dest_dir(dest_dir)
    with _archive_errors(source, _TAR_ERRORS), tarfile.open(source, "r:*") as tar_file:
        selected = _select(tar_file.getmembers(), members, lambda info: info.name, source)
        for info in selected:
            _check_member_name(info.name, destination, source)
            tarfile.data_filter(info, str(destination))  # FilterError antes de extrair
        tar_file.extractall(destination, members=selected, filter="data")
    extracted = [_member_path(destination, info.name) for info in selected if not info.isdir()]
    get_logger().info(
        "Arquivo TAR extraído | arquivo={} | destino={} | arquivos={}",
        source,
        destination,
        len(extracted),
    )
    return sorted(extracted)


def is_supported_archive(path: PathLike) -> bool:
    """Indica se a extensão do arquivo está em `SUPPORTED_EXTENSIONS` (sem abrir o arquivo).

    Args:
        path: Caminho do arquivo.

    Returns:
        `True` para `.zip`, `.tar`, `.tar.gz`, `.tgz`, `.tar.bz2`, `.tbz2`, `.tar.xz` e `.txz`.
    """
    return Path(path).name.lower().endswith(SUPPORTED_EXTENSIONS)


def extract_archive(archive: PathLike, dest_dir: PathLike) -> list[Path]:
    """Extrai ZIP ou TAR conforme a extensão do arquivo.

    Args:
        archive: Arquivo compactado.
        dest_dir: Pasta de destino.

    Returns:
        Caminhos absolutos dos arquivos extraídos (sem pastas), ordenados.

    Raises:
        ArchiveError: Se a extensão não for suportada ou a extração falhar.
        FileOperationError: Se o arquivo não existir ou a gravação falhar.
    """
    if not is_supported_archive(archive):
        raise ArchiveError(
            "Formato de arquivo compactado não suportado",
            details={
                "arquivo": str(Path(archive).resolve()),
                "suportados": ", ".join(SUPPORTED_EXTENSIONS),
            },
        )
    if Path(archive).name.lower().endswith(_ZIP_EXTENSION):
        return extract_zip(archive, dest_dir)
    return extract_tar(archive, dest_dir)


@contextlib.contextmanager
def _archive_errors(path: Path, format_errors: tuple[type[Exception], ...]) -> Iterator[None]:
    """Converte erros de formato em `ArchiveError` e `OSError` em `FileOperationError`."""
    try:
        yield
    except tarfile.FilterError as exc:
        raise ArchiveError(
            "Membro inseguro no arquivo TAR",
            details={"arquivo": str(path), "membro": exc.tarinfo.name, "erro": str(exc)},
        ) from exc
    except format_errors as exc:
        raise ArchiveError(
            "Arquivo compactado inválido ou ilegível",
            details={"arquivo": str(path), "erro": f"{type(exc).__name__}: {exc}"},
        ) from exc
    except OSError as exc:
        raise _file_error("Falha de leitura ou gravação", path, exc) from exc


@contextlib.contextmanager
def _staging_file(target: Path) -> Iterator[Path]:
    """Arquivo temporário na pasta do destino, renomeado para o destino ao final com sucesso."""
    staging = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        yield staging
        os.replace(staging, target)
    finally:
        with contextlib.suppress(OSError):
            staging.unlink(missing_ok=True)


def _prepare_dest(dest: PathLike, *, overwrite: bool) -> Path:
    target = Path(dest).resolve()
    if target.exists() and not overwrite:
        raise ArchiveError("Arquivo de destino já existe", details={"arquivo": str(target)})
    if target.is_dir():
        raise ArchiveError("Destino é uma pasta", details={"arquivo": str(target)})
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _file_error("Falha ao criar pasta de destino", target.parent, exc) from exc
    return target


def _prepare_dest_dir(dest_dir: PathLike) -> Path:
    destination = Path(dest_dir).resolve()
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _file_error("Falha ao criar pasta de destino", destination, exc) from exc
    return destination


def _existing_archive(archive: PathLike) -> Path:
    source = Path(archive).resolve()
    if not source.is_file():
        raise FileOperationError("Arquivo não encontrado", details={"caminho": str(source)})
    return source


def _as_source_list(sources: PathLike | Sequence[PathLike]) -> list[Path]:
    if isinstance(sources, str | os.PathLike):
        return [Path(sources)]
    return [Path(source) for source in sources]


def _collect_entries(
    sources: PathLike | Sequence[PathLike], base_dir: PathLike | None, *, exclude: Path
) -> list[tuple[Path, str]]:
    """Lista (caminho, nome interno) de todas as origens, sem duplicados."""
    base = Path(base_dir).resolve() if base_dir is not None else None
    entries: list[tuple[Path, str]] = []
    for source in _as_source_list(sources):
        root = source.resolve()
        if not root.exists():
            raise FileOperationError("Origem não encontrada", details={"caminho": str(root)})
        anchor = base if base is not None else root.parent
        items = [root, *_walk(root)] if root.is_dir() else [root]
        entries.extend(
            (item, _arcname(item, anchor, root))
            for item in items
            if item != exclude and item != anchor
        )
    _check_duplicates(entries)
    return entries


def _walk(directory: Path) -> Iterator[Path]:
    """Percorre a pasta recursivamente em ordem alfabética (pasta antes do conteúdo)."""
    for child in sorted(directory.iterdir(), key=lambda item: (item.name.casefold(), item.name)):
        yield child
        if child.is_dir() and not child.is_symlink():
            yield from _walk(child)


def _arcname(item: Path, anchor: Path, root: Path) -> str:
    if not item.is_relative_to(anchor):
        raise ArchiveError(
            "Origem fora de base_dir", details={"origem": str(root), "base_dir": str(anchor)}
        )
    name = item.relative_to(anchor).as_posix()
    return f"{name}{_ARCHIVE_SEPARATOR}" if item.is_dir() else name


def _check_duplicates(entries: list[tuple[Path, str]]) -> None:
    seen: set[str] = set()
    repeated: set[str] = set()
    for _path, arcname in entries:
        (repeated if arcname in seen else seen).add(arcname)
    if repeated:
        raise ArchiveError(
            "Nomes internos duplicados", details={"nomes": ", ".join(sorted(repeated))}
        )


def _select[MemberT](
    infos: list[MemberT],
    members: Sequence[str] | None,
    name_of: Callable[[MemberT], str],
    source: Path,
) -> list[MemberT]:
    """Filtra os membros pelos nomes pedidos; nome inexistente gera `ArchiveError`."""
    if members is None:
        return infos
    by_name = {name_of(info): info for info in infos}
    missing = [name for name in members if name not in by_name]
    if missing:
        raise ArchiveError(
            "Membro inexistente no arquivo",
            details={"arquivo": str(source), "membros": ", ".join(missing)},
        )
    return [by_name[name] for name in members]


def _check_member_name(name: str, destination: Path, source: Path) -> None:
    """Proteção Zip Slip: rejeita nomes absolutos, com unidade, com `..` ou fora do destino."""
    parts = _SEPARATORS_PATTERN.split(name)
    unsafe = (
        name.startswith(("/", "\\"))
        or _DRIVE_PATTERN.match(name) is not None
        or ".." in parts
        or not _member_path(destination, name).is_relative_to(destination)
    )
    if unsafe:
        raise ArchiveError(
            "Membro com caminho inseguro (path traversal)",
            details={"arquivo": str(source), "membro": name, "destino": str(destination)},
        )


def _member_path(destination: Path, name: str) -> Path:
    parts = [part for part in _SEPARATORS_PATTERN.split(name) if part]
    return destination.joinpath(*parts).resolve()


def _file_error(message: str, path: Path, exc: OSError) -> FileOperationError:
    reason = f"{type(exc).__name__}: {exc.strerror}" if exc.strerror else type(exc).__name__
    return FileOperationError(message, details={"caminho": str(path), "erro": reason})
