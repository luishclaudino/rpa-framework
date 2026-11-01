"""Testes de `rpa_3003.utils.archive_utils` (RF-040, RNF-010, seção 8.12)."""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from rpa_3003.core.exceptions import ArchiveError, FileOperationError
from rpa_3003.utils import archive_utils

pytestmark = pytest.mark.unit


@pytest.fixture
def source_tree(tmp_path: Path) -> Path:
    """Pasta `pdfs` com arquivos, subpasta com nome acentuado e subpasta vazia."""
    root = tmp_path / "origem" / "pdfs"
    (root / "Sub").mkdir(parents=True)
    (root / "vazia").mkdir()
    (root / "b.pdf").write_bytes(b"%PDF-b")
    (root / "a.pdf").write_bytes(b"%PDF-a")
    (root / "Sub" / "relatório.txt").write_text("ação", encoding="utf-8")
    return root


def _snapshot(root: Path) -> dict[str, bytes | None]:
    """Mapa caminho relativo → conteúdo (None para pastas)."""
    return {
        item.relative_to(root).as_posix(): (item.read_bytes() if item.is_file() else None)
        for item in root.rglob("*")
    }


def _zip_with(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(zipfile.ZipInfo(name), data)
    return path


def _tar_with(path: Path, members: list[tarfile.TarInfo]) -> Path:
    with tarfile.open(path, "w") as archive:
        for info in members:
            data = b"conteudo" if info.isfile() else b""
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data) if data else None)
    return path


def _tar_member(name: str, kind: bytes = tarfile.REGTYPE, linkname: str = "") -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = linkname
    return info


# --- ZIP --------------------------------------------------------------------------------------


def test_zip_roundtrip_preserves_structure_and_content(source_tree: Path, tmp_path: Path) -> None:
    """RF-040: ida e volta de pasta com subpastas preserva estrutura e conteúdo (ZIP)."""
    archive = archive_utils.create_zip(source_tree, tmp_path / "saida" / "pdfs.zip")
    assert archive == (tmp_path / "saida" / "pdfs.zip").resolve()
    extracted = archive_utils.extract_zip(archive, tmp_path / "destino")
    assert _snapshot(tmp_path / "destino" / "pdfs") == _snapshot(source_tree)
    assert extracted == sorted(
        (tmp_path / "destino" / "pdfs" / name).resolve()
        for name in ("a.pdf", "b.pdf", "Sub/relatório.txt")
    )


def test_zip_arcnames_are_alphabetical_with_forward_slashes(
    source_tree: Path, tmp_path: Path
) -> None:
    """RF-040: pasta entra com o próprio nome como raiz; separador '/'; ordem alfabética."""
    archive = archive_utils.create_zip(source_tree, tmp_path / "a.zip")
    assert archive_utils.list_zip(archive) == [
        "pdfs/",
        "pdfs/a.pdf",
        "pdfs/b.pdf",
        "pdfs/Sub/",
        "pdfs/Sub/relatório.txt",
        "pdfs/vazia/",
    ]
    with zipfile.ZipFile(archive) as zip_file:
        files = [info for info in zip_file.infolist() if not info.is_dir()]
        assert {info.compress_type for info in files} == {zipfile.ZIP_DEFLATED}


def test_zip_arcnames_with_base_dir_and_file_sources(source_tree: Path, tmp_path: Path) -> None:
    """RF-040: com base_dir, nomes relativos a ele; arquivo sem base_dir entra pelo nome."""
    archive = archive_utils.create_zip(
        [source_tree / "Sub", source_tree / "a.pdf"], tmp_path / "a.zip", base_dir=source_tree
    )
    assert archive_utils.list_zip(archive) == ["Sub/", "Sub/relatório.txt", "a.pdf"]
    single = archive_utils.create_zip(str(source_tree / "b.pdf"), tmp_path / "b.zip")
    assert archive_utils.list_zip(single) == ["b.pdf"]
    root_as_base = archive_utils.create_zip(source_tree, tmp_path / "c.zip", base_dir=source_tree)
    assert archive_utils.list_zip(root_as_base)[0] == "a.pdf"


def test_create_zip_validations(source_tree: Path, tmp_path: Path) -> None:
    """RF-040: destino existente sem overwrite, origem fora de base_dir, nomes duplicados."""
    dest = archive_utils.create_zip(source_tree, tmp_path / "a.zip")
    with pytest.raises(ArchiveError, match="já existe"):
        archive_utils.create_zip(source_tree, dest)
    archive_utils.create_zip(source_tree / "a.pdf", dest, overwrite=True)
    assert archive_utils.list_zip(dest) == ["a.pdf"]
    with pytest.raises(ArchiveError, match="fora de base_dir"):
        archive_utils.create_zip(source_tree, tmp_path / "b.zip", base_dir=source_tree / "Sub")
    other = tmp_path / "outra"
    other.mkdir()
    (other / "a.pdf").write_bytes(b"x")
    with pytest.raises(ArchiveError, match="duplicados"):
        archive_utils.create_zip([source_tree / "a.pdf", other / "a.pdf"], tmp_path / "c.zip")
    with pytest.raises(FileOperationError, match="Origem não encontrada"):
        archive_utils.create_zip(tmp_path / "nada", tmp_path / "d.zip")
    with pytest.raises(ValueError, match="compression_level"):
        archive_utils.create_zip(source_tree, tmp_path / "e.zip", compression_level=10)
    with pytest.raises(ArchiveError, match="pasta"):
        archive_utils.create_zip(source_tree, other, overwrite=True)
    assert not (tmp_path / "b.zip").exists()


def test_create_zip_inside_source_excludes_itself(source_tree: Path) -> None:
    """RF-040: o arquivo de destino dentro da pasta de origem não entra no próprio ZIP."""
    dest = source_tree / "backup.zip"
    archive_utils.create_zip(source_tree, dest)
    archive_utils.create_zip(source_tree, dest, overwrite=True)
    assert "pdfs/backup.zip" not in archive_utils.list_zip(dest)


def test_create_zip_failure_leaves_no_partial_file(
    source_tree: Path, tmp_path: Path, mocker: MockerFixture
) -> None:
    """RF-040: falha na gravação não deixa ZIP parcial nem temporário no destino."""
    mocker.patch.object(zipfile.ZipFile, "write", side_effect=OSError(28, "Disco cheio"))
    with pytest.raises(FileOperationError, match="Falha de leitura ou gravação"):
        archive_utils.create_zip(source_tree, tmp_path / "saida" / "a.zip")
    assert list((tmp_path / "saida").iterdir()) == []


def test_add_to_zip_appends_and_rejects_duplicates(source_tree: Path, tmp_path: Path) -> None:
    """RF-040: add_to_zip anexa itens; nome repetido gera ArchiveError."""
    archive = archive_utils.create_zip(source_tree / "a.pdf", tmp_path / "a.zip")
    archive_utils.add_to_zip(archive, source_tree / "Sub", base_dir=source_tree)
    assert archive_utils.list_zip(archive) == ["a.pdf", "Sub/", "Sub/relatório.txt"]
    with pytest.raises(ArchiveError, match="já existem") as info:
        archive_utils.add_to_zip(archive, source_tree / "a.pdf")
    assert info.value.details["nomes"] == "a.pdf"
    created = archive_utils.add_to_zip(tmp_path / "novo" / "b.zip", source_tree / "b.pdf")
    assert archive_utils.list_zip(created) == ["b.pdf"]


def test_add_to_zip_refuses_non_zip_file(source_tree: Path, tmp_path: Path) -> None:
    """RF-040: add_to_zip não anexa ZIP ao final de um arquivo que não é ZIP."""
    not_zip = tmp_path / "planilha.zip"
    not_zip.write_bytes(b"isto nao e zip")
    with pytest.raises(ArchiveError, match="não é ZIP"):
        archive_utils.add_to_zip(not_zip, source_tree / "a.pdf")
    assert not_zip.read_bytes() == b"isto nao e zip"


def test_extract_zip_selected_members(source_tree: Path, tmp_path: Path) -> None:
    """RF-040: members restringe a extração; membro inexistente gera ArchiveError."""
    archive = archive_utils.create_zip(source_tree, tmp_path / "a.zip")
    extracted = archive_utils.extract_zip(archive, tmp_path / "out", members=["pdfs/b.pdf"])
    assert extracted == [(tmp_path / "out" / "pdfs" / "b.pdf").resolve()]
    with pytest.raises(ArchiveError, match="inexistente") as info:
        archive_utils.extract_zip(archive, tmp_path / "out2", members=["pdfs/x.pdf"])
    assert info.value.details["membros"] == "pdfs/x.pdf"


@pytest.mark.parametrize(
    "evil_name",
    [
        "../evil.txt",
        "a/../../evil.txt",
        "a\\..\\..\\evil.txt",
        "/evil.txt",
        "\\evil.txt",
        "C:/evil.txt",
        "C:evil.txt",
        "ok/..",
    ],
)
def test_zip_slip_is_blocked_and_nothing_is_extracted(tmp_path: Path, evil_name: str) -> None:
    """RF-040 e RNF-010: ZIP com membro '../evil.txt' (e variantes) lança ArchiveError e nada
    é escrito fora (nem dentro) do destino."""
    archive = _zip_with(tmp_path / "malicioso.zip", {"ok.txt": b"ok", evil_name: b"pwned"})
    dest = tmp_path / "dest" / "interno"
    with pytest.raises(ArchiveError, match="path traversal") as info:
        archive_utils.extract_zip(archive, dest)
    assert info.value.details["destino"] == str(dest.resolve())
    assert list(dest.iterdir()) == []
    assert not (tmp_path / "evil.txt").exists()
    assert not (tmp_path / "dest" / "evil.txt").exists()


def test_extract_zip_metadata_encoding_cp850(tmp_path: Path) -> None:
    """RF-040: metadata_encoding decodifica nomes de ZIP do Explorer sem indicador UTF-8."""

    class ExplorerZipInfo(zipfile.ZipInfo):
        def _encodeFilenameFlags(self) -> tuple[bytes, int]:  # noqa: N802 - API do zipfile
            return self.filename.encode("cp850"), self.flag_bits

    archive = tmp_path / "explorer.zip"
    with zipfile.ZipFile(archive, "w") as zip_file:
        zip_file.writestr(ExplorerZipInfo("relatório_ação.txt"), b"x")
    assert archive_utils.list_zip(archive) != ["relatório_ação.txt"]
    assert archive_utils.list_zip(archive, metadata_encoding="cp850") == ["relatório_ação.txt"]
    extracted = archive_utils.extract_zip(archive, tmp_path / "out", metadata_encoding="cp850")
    assert [path.name for path in extracted] == ["relatório_ação.txt"]


def test_extract_zip_passes_password_and_wraps_bad_password(
    source_tree: Path, tmp_path: Path, mocker: MockerFixture
) -> None:
    """RF-040: a senha é repassada ao zipfile (ZipCrypto); senha errada vira ArchiveError."""
    archive = archive_utils.create_zip(source_tree / "a.pdf", tmp_path / "a.zip")
    spy = mocker.spy(zipfile.ZipFile, "extract")
    archive_utils.extract_zip(archive, tmp_path / "out", password="segredo")  # noqa: S106
    assert spy.call_args.kwargs["pwd"] == b"segredo"
    mocker.patch.object(
        zipfile.ZipFile, "extract", side_effect=RuntimeError("Bad password for file 'a.pdf'")
    )
    with pytest.raises(ArchiveError, match="inválido ou ilegível") as info:
        archive_utils.extract_zip(archive, tmp_path / "out2", password="errada")  # noqa: S106
    assert "Bad password" in info.value.details["erro"]


def test_zip_invalid_or_missing_archive(tmp_path: Path) -> None:
    """RF-040: arquivo corrompido gera ArchiveError; inexistente, FileOperationError."""
    corrupt = tmp_path / "ruim.zip"
    corrupt.write_bytes(b"PK\x03\x04 corrompido")
    with pytest.raises(ArchiveError):
        archive_utils.list_zip(corrupt)
    with pytest.raises(ArchiveError):
        archive_utils.extract_zip(corrupt, tmp_path / "out")
    with pytest.raises(FileOperationError, match="não encontrado"):
        archive_utils.list_zip(tmp_path / "nada.zip")


# --- TAR --------------------------------------------------------------------------------------


@pytest.mark.parametrize("compression", ["", "gz", "bz2", "xz"])
def test_tar_roundtrip_preserves_structure_and_content(
    source_tree: Path, tmp_path: Path, compression: archive_utils.TarCompression
) -> None:
    """RF-040: ida e volta de pasta com subpastas preserva estrutura e conteúdo (TAR)."""
    archive = archive_utils.create_tar(source_tree, tmp_path / "pdfs.tar", compression=compression)
    assert archive_utils.list_tar(archive) == [
        "pdfs",
        "pdfs/a.pdf",
        "pdfs/b.pdf",
        "pdfs/Sub",
        "pdfs/Sub/relatório.txt",
        "pdfs/vazia",
    ]
    extracted = archive_utils.extract_tar(archive, tmp_path / "destino")
    assert _snapshot(tmp_path / "destino" / "pdfs") == _snapshot(source_tree)
    assert len(extracted) == 3
    assert all(path.is_absolute() and path.is_file() for path in extracted)


def test_create_tar_validations(source_tree: Path, tmp_path: Path) -> None:
    """RF-040: compressão inválida gera ValueError; destino existente exige overwrite."""
    with pytest.raises(ValueError, match="compression"):
        archive_utils.create_tar(source_tree, tmp_path / "a.tar", compression="zip")  # type: ignore[arg-type]
    archive = archive_utils.create_tar([source_tree / "a.pdf"], tmp_path / "a.tar.gz")
    with pytest.raises(ArchiveError, match="já existe"):
        archive_utils.create_tar(source_tree, archive)
    archive_utils.create_tar(source_tree / "b.pdf", archive, overwrite=True, base_dir=source_tree)
    assert archive_utils.list_tar(archive) == ["b.pdf"]


def test_extract_tar_selected_members(source_tree: Path, tmp_path: Path) -> None:
    """RF-040: members restringe a extração do TAR."""
    archive = archive_utils.create_tar(source_tree, tmp_path / "a.tar.gz")
    extracted = archive_utils.extract_tar(
        archive, tmp_path / "out", members=["pdfs/Sub/relatório.txt"]
    )
    assert extracted == [(tmp_path / "out" / "pdfs" / "Sub" / "relatório.txt").resolve()]
    with pytest.raises(ArchiveError, match="inexistente"):
        archive_utils.extract_tar(archive, tmp_path / "out", members=["nada"])


@pytest.mark.parametrize(
    ("member", "message"),
    [
        (_tar_member("../evil.txt"), "path traversal"),
        (_tar_member("a/../../evil.txt"), "path traversal"),
        (_tar_member("/tmp/evil.txt"), "path traversal"),  # noqa: S108 - nome de membro
        (_tar_member("C:/evil.txt"), "path traversal"),
        (_tar_member("link", tarfile.SYMTYPE, "../../fora.txt"), "Membro inseguro"),
        (_tar_member("link", tarfile.SYMTYPE, "/etc/passwd"), "Membro inseguro"),
        (_tar_member("hard", tarfile.LNKTYPE, "../fora.txt"), "Membro inseguro"),
        (_tar_member("dispositivo", tarfile.CHRTYPE), "Membro inseguro"),
    ],
    ids=[
        "dotdot",
        "nested-dotdot",
        "absolute",
        "drive",
        "symlink-out",
        "symlink-abs",
        "hardlink",
        "device",
    ],
)
def test_malicious_tar_is_blocked_and_nothing_is_extracted(
    tmp_path: Path, member: tarfile.TarInfo, message: str
) -> None:
    """RF-040 e RNF-010: TAR malicioso (traversal, links para fora, dispositivo) lança
    ArchiveError e nada é extraído."""
    archive = _tar_with(tmp_path / "malicioso.tar", [_tar_member("ok.txt"), member])
    dest = tmp_path / "dest" / "interno"
    with pytest.raises(ArchiveError, match=message):
        archive_utils.extract_tar(archive, dest)
    assert list(dest.iterdir()) == []
    assert not (tmp_path / "evil.txt").exists()
    assert not (tmp_path / "dest" / "evil.txt").exists()


def test_extract_tar_allows_internal_links(tmp_path: Path) -> None:
    """RF-040: links que apontam para dentro do destino passam pelo filtro "data"."""
    archive = _tar_with(
        tmp_path / "links.tar",
        [
            _tar_member("dados/real.txt"),
            _tar_member("dados/copia.txt", tarfile.LNKTYPE, "dados/real.txt"),
        ],
    )
    extracted = archive_utils.extract_tar(archive, tmp_path / "out")
    assert [path.name for path in extracted] == ["copia.txt", "real.txt"]
    assert (tmp_path / "out" / "dados" / "copia.txt").read_bytes() == b"conteudo"


def test_filter_error_during_extraction_becomes_archive_error(
    source_tree: Path, tmp_path: Path, mocker: MockerFixture
) -> None:
    """RF-040: tarfile.FilterError na extração vira ArchiveError."""
    archive = archive_utils.create_tar(source_tree / "a.pdf", tmp_path / "a.tar")
    member = tarfile.TarInfo("a.pdf")
    mocker.patch.object(
        tarfile.TarFile, "extractall", side_effect=tarfile.OutsideDestinationError(member, "fora")
    )
    with pytest.raises(ArchiveError, match="Membro inseguro") as info:
        archive_utils.extract_tar(archive, tmp_path / "out")
    assert info.value.details["membro"] == "a.pdf"


def test_tar_invalid_or_missing_archive(tmp_path: Path) -> None:
    """RF-040: TAR corrompido gera ArchiveError; inexistente, FileOperationError."""
    corrupt = tmp_path / "ruim.tar.gz"
    corrupt.write_bytes(b"\x1f\x8b corrompido")
    with pytest.raises(ArchiveError):
        archive_utils.list_tar(corrupt)
    with pytest.raises(ArchiveError):
        archive_utils.extract_tar(corrupt, tmp_path / "out")
    with pytest.raises(FileOperationError):
        archive_utils.extract_tar(tmp_path / "nada.tar", tmp_path / "out")


# --- detecção de formato ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("lote.zip", True),
        ("LOTE.ZIP", True),
        ("backup.tar", True),
        ("backup.tar.gz", True),
        ("backup.tgz", True),
        ("backup.tar.bz2", True),
        ("backup.tbz2", True),
        ("backup.tar.xz", True),
        ("backup.txz", True),
        ("backup.gz", False),
        ("lote.rar", False),
        ("zip", False),
    ],
)
def test_is_supported_archive(name: str, expected: bool) -> None:
    """RF-040: detecção de formato pela extensão."""
    assert archive_utils.is_supported_archive(name) is expected


@pytest.mark.parametrize(
    ("name", "compression"),
    [
        ("lote.zip", None),
        ("lote.tar", ""),
        ("lote.tar.gz", "gz"),
        ("lote.tgz", "gz"),
        ("lote.tar.bz2", "bz2"),
        ("lote.tbz2", "bz2"),
        ("lote.tar.xz", "xz"),
        ("lote.txz", "xz"),
    ],
)
def test_extract_archive_recognizes_extensions(
    source_tree: Path, tmp_path: Path, name: str, compression: archive_utils.TarCompression | None
) -> None:
    """RF-040: extract_archive reconhece .zip, .tar, .tar.gz, .tgz, .tar.bz2 e .tar.xz."""
    dest = tmp_path / name
    if compression is None:
        archive_utils.create_zip(source_tree, dest)
    else:
        archive_utils.create_tar(source_tree, dest, compression=compression)
    extracted = archive_utils.extract_archive(dest, tmp_path / "out")
    assert [path.name for path in extracted] == ["a.pdf", "b.pdf", "relatório.txt"]


def test_extract_archive_rejects_unsupported(tmp_path: Path) -> None:
    """RF-040: extensão não suportada gera ArchiveError."""
    with pytest.raises(ArchiveError, match="não suportado") as info:
        archive_utils.extract_archive(tmp_path / "lote.rar", tmp_path / "out")
    assert ".tar.gz" in info.value.details["suportados"]


def test_usage_example(source_tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Seção 8.12: exemplo de uso do módulo."""
    monkeypatch.chdir(source_tree.parent.parent)
    (Path("data") / "output").mkdir(parents=True)
    (Path("logs")).mkdir()
    (Path("logs") / "rpa.log").write_text("log", encoding="utf-8")
    pacote = archive_utils.create_zip("origem/pdfs", "data/output/pdfs_2026-10-31.zip")
    backup = archive_utils.create_tar(
        ["data/output", "logs"], "data/backup.tar.gz", compression="gz"
    )
    extraidos = archive_utils.extract_archive(pacote, "data/input/lote")
    assert len(extraidos) == 3
    assert "output/pdfs_2026-10-31.zip" in archive_utils.list_tar(backup)
