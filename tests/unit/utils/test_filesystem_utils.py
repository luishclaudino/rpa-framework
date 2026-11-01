"""Testes de `rpa_3003.utils.filesystem_utils` (RF-037, RNF-007, RNF-009, RNF-011, seção 8.10)."""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import stat
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from rpa_3003.core.exceptions import CryptoError, DataFormatError, FileOperationError
from rpa_3003.utils import filesystem_utils as fs

pytestmark = pytest.mark.unit


class FakeClock:
    """Relógio monotônico simulado: `sleep` avança o tempo e chama `on_sleep`."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self.on_sleep: Callable[[float], None] | None = None

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep(self.now)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(time, "monotonic", fake.monotonic)
    monkeypatch.setattr(time, "sleep", fake.sleep)
    return fake


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """Pasta com a.txt, b.csv e sub/c.txt."""
    root = tmp_path / "arvore"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("a", encoding="utf-8")
    (root / "b.csv").write_text("bb", encoding="utf-8")
    (root / "sub" / "c.txt").write_text("ccc", encoding="utf-8")
    return root


def _make_readonly(path: Path) -> None:
    path.chmod(stat.S_IREAD)


def _set_mtime(path: Path, seconds_ago: float) -> None:
    moment = time.time() - seconds_ago
    os.utime(path, (moment, moment))


# --- diretórios e listagem --------------------------------------------------------------------


def test_ensure_dir_creates_nested_dirs_and_is_idempotent(tmp_path: Path) -> None:
    """RF-037: ensure_dir cria pastas intermediárias e retorna o caminho absoluto."""
    target = tmp_path / "data" / "output" / "2026"
    assert fs.ensure_dir(target) == target.resolve()
    assert target.is_dir()
    assert fs.ensure_dir(str(target)) == target.resolve()


def test_ensure_dir_wraps_os_error_with_path(tmp_path: Path) -> None:
    """RF-037: OSError vira FileOperationError com o caminho em details."""
    blocker = tmp_path / "arquivo"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(FileOperationError) as info:
        fs.ensure_dir(blocker)
    assert info.value.details["caminho"] == str(blocker.resolve())
    assert isinstance(info.value.__cause__, OSError)


def test_list_files_sorted_files_only_by_default(tree: Path) -> None:
    """RF-037: list_files ordena por caminho e retorna somente arquivos."""
    assert fs.list_files(tree) == [(tree / "a.txt").resolve(), (tree / "b.csv").resolve()]


def test_list_files_include_dirs_and_recursive(tree: Path) -> None:
    """RF-037: include_dirs inclui pastas; recursive=True percorre subpastas (rglob)."""
    assert fs.list_files(tree, include_dirs=True)[-1] == (tree / "sub").resolve()
    assert fs.list_files(tree, "*.txt", recursive=True) == [
        (tree / "a.txt").resolve(),
        (tree / "sub" / "c.txt").resolve(),
    ]


def test_list_files_rejects_missing_dir_and_file(tree: Path) -> None:
    """RF-037: pasta inexistente ou caminho de arquivo geram FileOperationError."""
    with pytest.raises(FileOperationError, match="Pasta não encontrada"):
        fs.list_files(tree / "nada")
    with pytest.raises(FileOperationError, match="não é uma pasta"):
        fs.list_files(tree / "a.txt")


def test_get_latest_file_returns_highest_mtime(tree: Path) -> None:
    """RF-037: get_latest_file retorna o maior st_mtime; None quando nada casa."""
    _set_mtime(tree / "a.txt", 300)
    _set_mtime(tree / "b.csv", 10)
    assert fs.get_latest_file(tree) == (tree / "b.csv").resolve()
    assert fs.get_latest_file(tree, "*.txt") == (tree / "a.txt").resolve()
    assert fs.get_latest_file(tree, "*.pdf") is None


# --- cópia e movimentação ---------------------------------------------------------------------


def test_copy_file_into_directory_keeps_name(tree: Path, tmp_path: Path) -> None:
    """RF-037: destino pasta mantém o nome original; a pasta de destino é criada."""
    dest_dir = tmp_path / "copias"
    dest_dir.mkdir()
    copied = fs.copy_file(tree / "a.txt", dest_dir)
    assert copied == (dest_dir / "a.txt").resolve()
    assert copied.read_text(encoding="utf-8") == "a"
    nested = fs.copy_file(tree / "b.csv", tmp_path / "novo" / "pasta" / "b2.csv")
    assert nested.read_text(encoding="utf-8") == "bb"
    assert (tree / "a.txt").exists()


def test_copy_file_existing_destination_requires_overwrite(tree: Path) -> None:
    """RF-037: destino existente sem overwrite gera FileOperationError."""
    target = tree / "sub" / "c.txt"
    with pytest.raises(FileOperationError, match="Destino já existe"):
        fs.copy_file(tree / "a.txt", target)
    assert target.read_text(encoding="utf-8") == "ccc"


def test_copy_file_overwrites_readonly_destination(tree: Path) -> None:
    """RF-037: overwrite substitui destino somente leitura."""
    target = tree / "sub" / "c.txt"
    _make_readonly(target)
    fs.copy_file(tree / "a.txt", target, overwrite=True)
    assert target.read_text(encoding="utf-8") == "a"


def test_copy_file_missing_source(tmp_path: Path) -> None:
    """RF-037: origem inexistente gera FileOperationError."""
    with pytest.raises(FileOperationError, match="origem não encontrado"):
        fs.copy_file(tmp_path / "nada.txt", tmp_path / "x.txt")


def test_copy_file_wraps_copy_failure(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RF-037: falha do shutil.copy2 vira FileOperationError com origem e destino."""

    def failing_copy(*_args: object) -> None:
        raise PermissionError(errno.EACCES, "Acesso negado")

    monkeypatch.setattr(shutil, "copy2", failing_copy)
    with pytest.raises(FileOperationError) as info:
        fs.copy_file(tree / "a.txt", tree / "z.txt")
    assert info.value.details["origem"] == str((tree / "a.txt").resolve())
    assert info.value.details["erro"] == "PermissionError: Acesso negado"


def test_move_file_moves_and_respects_overwrite(tree: Path, tmp_path: Path) -> None:
    """RF-037: move_file move o arquivo; destino existente exige overwrite."""
    moved = fs.move_file(tree / "a.txt", tmp_path / "destino")
    assert moved == (tmp_path / "destino").resolve()
    assert not (tree / "a.txt").exists()
    with pytest.raises(FileOperationError, match="Destino já existe"):
        fs.move_file(tree / "b.csv", moved)
    _make_readonly(moved)
    fs.move_file(tree / "b.csv", moved, overwrite=True)
    assert moved.read_text(encoding="utf-8") == "bb"


def test_move_file_falls_back_to_copy_between_volumes(
    tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RF-037: os.replace entre volumes (EXDEV) usa shutil.move."""
    calls: list[tuple[object, object]] = []

    def cross_device(*_args: object) -> None:
        raise OSError(errno.EXDEV, "volumes diferentes")

    def fake_move(src: object, dst: object) -> None:
        calls.append((src, dst))
        Path(str(dst)).write_bytes(Path(str(src)).read_bytes())
        Path(str(src)).unlink()

    monkeypatch.setattr(os, "replace", cross_device)
    monkeypatch.setattr(shutil, "move", fake_move)
    moved = fs.move_file(tree / "a.txt", tmp_path / "outro_volume.txt")
    assert len(calls) == 1
    assert moved.read_text(encoding="utf-8") == "a"


def test_move_file_other_errors_are_not_retried_as_copy(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RF-037: erro que não é de volume vira FileOperationError sem tentar copiar."""

    def locked(*_args: object) -> None:
        raise PermissionError(errno.EACCES, "arquivo em uso")

    def unexpected_move(*_args: object) -> None:
        raise AssertionError("shutil.move não deveria ser chamado")

    monkeypatch.setattr(os, "replace", locked)
    monkeypatch.setattr(shutil, "move", unexpected_move)
    with pytest.raises(FileOperationError, match="Falha ao mover arquivo"):
        fs.move_file(tree / "a.txt", tree / "z.txt")


# --- remoção e limpeza ------------------------------------------------------------------------


def test_delete_file_returns_status_and_handles_readonly(tree: Path) -> None:
    """RF-037: delete_file remove arquivo somente leitura e indica se removeu."""
    target = tree / "a.txt"
    _make_readonly(target)
    assert fs.delete_file(target) is True
    assert not target.exists()
    assert fs.delete_file(target) is False
    with pytest.raises(FileOperationError, match="Arquivo não encontrado"):
        fs.delete_file(target, missing_ok=False)


def test_delete_file_rejects_directory(tree: Path) -> None:
    """RF-037: delete_file não remove pastas."""
    with pytest.raises(FileOperationError, match="use delete_dir"):
        fs.delete_file(tree / "sub")


def test_delete_dir_removes_tree_with_readonly_file(tree: Path) -> None:
    """RF-037: delete_dir remove pasta com arquivo somente leitura."""
    _make_readonly(tree / "sub" / "c.txt")
    _make_readonly(tree / "a.txt")
    assert fs.delete_dir(tree) is True
    assert not tree.exists()
    assert fs.delete_dir(tree) is False
    with pytest.raises(FileOperationError, match="Pasta não encontrada"):
        fs.delete_dir(tree, missing_ok=False)


def test_delete_dir_rejects_file(tree: Path) -> None:
    """RF-037: delete_dir em arquivo gera FileOperationError."""
    with pytest.raises(FileOperationError, match="não é uma pasta"):
        fs.delete_dir(tree / "a.txt")


def test_remove_readonly_handler_retries_after_chmod(tmp_path: Path) -> None:
    """RF-037: o handler onexc aplica S_IWRITE e repete a operação."""
    target = tmp_path / "ro.txt"
    target.write_text("x", encoding="utf-8")
    _make_readonly(target)
    fs._remove_readonly(os.remove, str(target), PermissionError())
    assert not target.exists()
    fs._remove_readonly(os.remove, str(target), FileNotFoundError())  # já removido: ignora


def test_clean_dir_removes_matching_old_files(tree: Path) -> None:
    """RF-037: clean_dir remove só os arquivos do padrão anteriores a N dias."""
    old_tmp, new_tmp = tree / "velho.tmp", tree / "novo.tmp"
    old_tmp.write_text("x", encoding="utf-8")
    new_tmp.write_text("x", encoding="utf-8")
    _set_mtime(old_tmp, 10 * 86_400)
    _set_mtime(tree / "a.txt", 10 * 86_400)
    assert fs.clean_dir(tree, "*.tmp", older_than_days=7) == 1
    assert not old_tmp.exists()
    assert new_tmp.exists()
    assert (tree / "a.txt").exists()


def test_clean_dir_without_age_and_recursive(tree: Path) -> None:
    """RF-037: sem older_than_days remove tudo que casa; recursive inclui subpastas."""
    assert fs.clean_dir(tree, "*.txt", recursive=True) == 2
    assert sorted(path.name for path in tree.rglob("*") if path.is_file()) == ["b.csv"]
    assert (tree / "sub").is_dir()


def test_clean_dir_missing_dir_and_invalid_age(tmp_path: Path) -> None:
    """RF-037: pasta inexistente retorna 0; idade negativa gera ValueError."""
    assert fs.clean_dir(tmp_path / "nada") == 0
    with pytest.raises(ValueError, match="negativo"):
        fs.clean_dir(tmp_path, older_than_days=-1)


# --- espera por arquivo -----------------------------------------------------------------------


def test_wait_for_file_ignores_partial_download(tmp_path: Path, clock: FakeClock) -> None:
    """RF-037: ignora arquivo.pdf.crdownload e retorna arquivo.pdf quando o tamanho estabiliza."""
    partial = tmp_path / "arquivo.pdf.crdownload"
    partial.write_bytes(b"x" * 10)

    def download(elapsed: float) -> None:
        if elapsed == 1.0:
            partial.write_bytes(b"x" * 20)
        elif elapsed == 2.0:
            partial.rename(tmp_path / "arquivo.pdf")

    clock.on_sleep = download
    result = fs.wait_for_file(tmp_path, timeout=30, poll_interval=0.5, stable_seconds=1.0)
    assert result == (tmp_path / "arquivo.pdf").resolve()
    assert clock.now == 3.0


def test_wait_for_file_waits_until_size_is_stable(tmp_path: Path, clock: FakeClock) -> None:
    """RF-037: mudança de tamanho reinicia a contagem de estabilidade."""
    target = tmp_path / "relatorio.csv"
    target.write_bytes(b"1")

    def grow(elapsed: float) -> None:
        if elapsed <= 2.0:
            target.write_bytes(b"1" * int(elapsed * 10 + 1))

    clock.on_sleep = grow
    result = fs.wait_for_file(tmp_path, "*.csv", poll_interval=0.5, stable_seconds=1.0)
    assert result == target.resolve()
    assert clock.now == 3.0


def test_wait_for_file_directory_created_later(tmp_path: Path, clock: FakeClock) -> None:
    """RF-037: a pasta pode ainda não existir no início da espera."""
    downloads = tmp_path / "downloads"

    def create(elapsed: float) -> None:
        if elapsed == 1.0:
            downloads.mkdir()
            (downloads / "extrato_10.txt").write_text("ok", encoding="utf-8")

    clock.on_sleep = create
    result = fs.wait_for_file(downloads, "extrato_*.txt", stable_seconds=0)
    assert result.name == "extrato_10.txt"
    assert clock.now == 1.0


def test_wait_for_file_prefers_most_recent_stable_file(tmp_path: Path, clock: FakeClock) -> None:
    """RF-037: entre arquivos estáveis, retorna o de maior st_mtime."""
    older, newer = tmp_path / "a.pdf", tmp_path / "b.pdf"
    older.write_bytes(b"a")
    newer.write_bytes(b"b")
    _set_mtime(older, 60)
    assert fs.wait_for_file(tmp_path, "*.pdf", stable_seconds=0) == newer.resolve()


def test_wait_for_file_timeout(tmp_path: Path, clock: FakeClock) -> None:
    """RF-037: prazo esgotado gera FileOperationError com pasta, padrão e timeout."""
    with pytest.raises(FileOperationError, match="não encontrado no prazo") as info:
        fs.wait_for_file(tmp_path, "*.pdf", timeout=2.0, poll_interval=0.5)
    assert info.value.details == {
        "pasta": str(tmp_path.resolve()),
        "padrao": "*.pdf",
        "timeout": 2.0,
    }
    assert clock.now == 2.0
    assert clock.sleeps == [0.5, 0.5, 0.5, 0.5]


def test_wait_for_file_validates_arguments(tmp_path: Path) -> None:
    """RF-037: intervalos inválidos geram ValueError."""
    with pytest.raises(ValueError, match="poll_interval"):
        fs.wait_for_file(tmp_path, poll_interval=0)


# --- bloqueio, hash e tamanho -----------------------------------------------------------------


def test_is_file_locked_false_for_free_missing_or_directory(tree: Path) -> None:
    """RF-037: arquivo livre, inexistente ou pasta não estão bloqueados."""
    assert fs.is_file_locked(tree / "a.txt") is False
    assert fs.is_file_locked(tree / "nada.txt") is False
    assert fs.is_file_locked(tree / "sub") is False


def test_is_file_locked_true_on_permission_error(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RF-037: PermissionError ao abrir em r+b indica bloqueio."""
    opened: list[tuple[object, str]] = []

    def locked_open(path: object, mode: str) -> None:
        opened.append((path, mode))
        raise PermissionError(errno.EACCES, "em uso por outro processo")

    monkeypatch.setattr(fs, "open", locked_open, raising=False)
    assert fs.is_file_locked(tree / "a.txt") is True
    assert opened[0][1] == "r+b"


def test_is_file_locked_wraps_other_os_errors(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RF-037: outras falhas do sistema viram FileOperationError."""

    def broken_open(*_args: object) -> None:
        raise OSError(errno.EIO, "falha de E/S")

    monkeypatch.setattr(fs, "open", broken_open, raising=False)
    with pytest.raises(FileOperationError, match="bloqueio"):
        fs.is_file_locked(tree / "a.txt")


@pytest.mark.windows
def test_is_file_locked_detects_exclusive_windows_handle(tmp_path: Path) -> None:
    """RF-037: arquivo aberto sem compartilhamento por outro handle é detectado (Windows)."""
    import win32file

    target = tmp_path / "planilha.xlsx"
    target.write_bytes(b"conteudo")
    handle = win32file.CreateFile(
        str(target), win32file.GENERIC_READ, 0, None, win32file.OPEN_EXISTING, 0, None
    )
    try:
        assert fs.is_file_locked(target) is True
    finally:
        handle.Close()
    assert fs.is_file_locked(target) is False


def test_file_hash_matches_hashlib_with_small_chunks(tree: Path) -> None:
    """RF-037: file_hash lê em blocos e coincide com hashlib."""
    data = (tree / "sub" / "c.txt").read_bytes()
    expected = hashlib.sha256(data).hexdigest()
    assert fs.file_hash(tree / "sub" / "c.txt") == expected
    assert fs.file_hash(tree / "sub" / "c.txt", chunk_size=1) == expected
    assert fs.file_hash(tree / "sub" / "c.txt", "sha512") == hashlib.sha512(data).hexdigest()


def test_file_hash_insecure_algorithms_require_flag(tree: Path) -> None:
    """RNF-009: md5 e sha1 exigem allow_insecure=True; algoritmo desconhecido é recusado."""
    path = tree / "a.txt"
    with pytest.raises(CryptoError, match="inseguro"):
        fs.file_hash(path, "md5")
    expected = hashlib.md5(b"a", usedforsecurity=False).hexdigest()
    assert fs.file_hash(path, "md5", allow_insecure=True) == expected
    with pytest.raises(CryptoError, match="não suportado"):
        fs.file_hash(path, "crc32")


def test_file_hash_errors(tree: Path) -> None:
    """RF-037: chunk_size inválido gera ValueError; arquivo inexistente, FileOperationError."""
    with pytest.raises(ValueError, match="chunk_size"):
        fs.file_hash(tree / "a.txt", chunk_size=0)
    with pytest.raises(FileOperationError, match="Falha ao ler"):
        fs.file_hash(tree / "nada.txt")


def test_get_size_of_file_and_directory(tree: Path) -> None:
    """RF-037: get_size soma os arquivos da pasta recursivamente."""
    assert fs.get_size(tree / "b.csv") == 2
    assert fs.get_size(tree) == 6
    with pytest.raises(FileOperationError, match="não encontrado"):
        fs.get_size(tree / "nada")


# --- nomes de arquivo -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("rel:2026/10?.csv", "rel_2026_10_.csv"),
        ("CON", "_CON"),
        ("Relatório: 10/2026.pdf", "Relatório_ 10_2026.pdf"),
        ("con.txt", "_con.txt"),
        ("Lpt9.log", "_Lpt9.log"),
        ("NUL .tar.gz", "_NUL .tar.gz"),
        ("COM10.txt", "COM10.txt"),
        ("CONSOLE.txt", "CONSOLE.txt"),
        ("nota fiscal. . ", "nota fiscal"),
        ("a\x00b\tc\x7fd", "a_b_c_d"),
        ('<>:"|?*', "_______"),
        ("", "_"),
        ("...", "_"),
    ],
)
def test_safe_filename(name: str, expected: str) -> None:
    """RF-037: caracteres inválidos e de controle, pontos finais e nomes reservados."""
    assert fs.safe_filename(name) == expected


def test_safe_filename_truncates_preserving_extension() -> None:
    """RF-037: trunca em max_length preservando a extensão."""
    assert fs.safe_filename("a" * 300 + ".pdf", max_length=20) == "a" * 16 + ".pdf"
    assert fs.safe_filename("abc def.pdf", max_length=8) == "abc.pdf"
    assert fs.safe_filename("x" * 10 + ".extensaolonga", max_length=8) == "x" * 8
    assert fs.safe_filename("CONSOLE", max_length=3) == "_CO"
    assert len(fs.safe_filename("b" * 500)) == 200


def test_safe_filename_custom_replacement_and_validation() -> None:
    """RF-037: replacement customizado; argumentos inválidos geram ValueError."""
    assert fs.safe_filename("a/b", replacement="-") == "a-b"
    with pytest.raises(ValueError, match="replacement"):
        fs.safe_filename("a/b", replacement="/")
    with pytest.raises(ValueError, match="max_length"):
        fs.safe_filename("a", max_length=0)


# --- texto e codificação ----------------------------------------------------------------------


def test_read_text_cp1252_without_encoding(tmp_path: Path) -> None:
    """RF-037 e RNF-011: read_text de arquivo cp1252 com "ação" retorna "ação" sem informar a
    codificação."""
    path = tmp_path / "legado.txt"
    path.write_bytes("ação\r\nRelatório €\r\n".encode("cp1252"))
    assert fs.detect_encoding(path) == "cp1252"
    assert fs.read_text(path) == "ação\nRelatório €\n"


def test_read_text_utf8_bom_and_latin1_fallback(tmp_path: Path) -> None:
    """RNF-011: UTF-8 com BOM é lido sem o BOM; byte indefinido no cp1252 cai no latin-1."""
    bom = tmp_path / "bom.txt"
    bom.write_bytes("café".encode("utf-8-sig"))
    assert fs.detect_encoding(bom) == "utf-8-sig"
    assert fs.read_text(bom) == "café"
    latin = tmp_path / "latin.txt"
    latin.write_bytes(b"a\x81b\xe7")
    assert fs.detect_encoding(latin) == "latin-1"
    assert fs.read_text(latin) == "a\x81bç"


def test_read_text_explicit_encoding_and_errors(tmp_path: Path) -> None:
    """RF-037: codificação informada é respeitada; falhas viram DataFormatError."""
    path = tmp_path / "legado.txt"
    path.write_bytes("ação".encode("cp1252"))
    assert fs.read_text(path, "cp1252") == "ação"
    with pytest.raises(DataFormatError, match="decodificar") as info:
        fs.read_text(path, "utf-8")
    assert info.value.details["codificacao"] == "utf-8"
    with pytest.raises(DataFormatError, match="não identificada"):
        fs.detect_encoding(path, candidates=("ascii",))
    with pytest.raises(FileOperationError, match="Falha ao ler"):
        fs.read_text(tmp_path / "nada.txt")


def test_read_lines_strip_skip_empty_and_form_feed(tmp_path: Path) -> None:
    """RF-037: read_lines remove espaços, descarta vazias e preserva form feed."""
    path = tmp_path / "linhas.txt"
    path.write_bytes(b"  a  \r\n\r\n  b\x0cc\r\n   \r\n")
    assert fs.read_lines(path) == ["a", "", "b\x0cc", ""]
    assert fs.read_lines(path, skip_empty=True) == ["a", "b\x0cc"]
    assert fs.read_lines(path, strip=False) == ["  a  ", "", "  b\x0cc", "   "]
    empty = tmp_path / "vazio.txt"
    empty.write_bytes(b"")
    assert fs.read_lines(empty) == []


def test_write_text_atomic_leaves_no_temp_file(tmp_path: Path) -> None:
    """RF-037: write_text atômico cria a pasta e não deixa .tmp."""
    path = fs.write_text(tmp_path / "saida" / "resumo.txt", "Total: 10\nação")
    assert path == (tmp_path / "saida" / "resumo.txt").resolve()
    assert fs.read_text(path) == "Total: 10\nação"
    assert [item.name for item in path.parent.iterdir()] == ["resumo.txt"]
    fs.write_text(path, "substituído", atomic=False)
    assert fs.read_text(path) == "substituído"


def test_write_text_retries_when_destination_is_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    """RNF-007: PermissionError no os.replace gera nova tentativa após 0,5 s."""
    path = tmp_path / "resumo.txt"
    real_replace = os.replace
    attempts: list[int] = []

    def flaky_replace(src: str, dst: str) -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise PermissionError(errno.EACCES, "em uso")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky_replace)
    fs.write_text(path, "ok")
    assert path.read_text(encoding="utf-8") == "ok"
    assert no_sleep == [0.5]


def test_write_text_gives_up_after_three_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    """RF-037 e RNF-007: 3 tentativas com 0,5 s de intervalo antes de FileOperationError."""
    path = tmp_path / "resumo.txt"
    path.write_text("original", encoding="utf-8")

    def locked(*_args: object) -> None:
        raise PermissionError(errno.EACCES, "em uso")

    monkeypatch.setattr(os, "replace", locked)
    with pytest.raises(FileOperationError, match="bloqueado") as info:
        fs.write_text(path, "novo")
    assert info.value.details["tentativas"] == 3
    assert no_sleep == [0.5, 0.5]
    assert path.read_text(encoding="utf-8") == "original"
    assert [item.name for item in tmp_path.iterdir()] == ["resumo.txt"]


def test_write_text_encoding_and_os_errors(tmp_path: Path) -> None:
    """RF-037: caractere não representável gera DataFormatError; OSError, FileOperationError."""
    with pytest.raises(DataFormatError, match="não representável"):
        fs.write_text(tmp_path / "a.txt", "€", encoding="latin-1")
    with pytest.raises(DataFormatError):
        fs.append_text(tmp_path / "b.txt", "€", encoding="latin-1")
    assert list(tmp_path.glob("*.tmp")) == []
    (tmp_path / "pasta").mkdir()
    with pytest.raises(FileOperationError, match="Falha ao gravar"):
        fs.write_text(tmp_path / "pasta", "x", atomic=False)
    with pytest.raises(FileOperationError, match="Falha ao gravar"):
        fs.append_text(tmp_path / "pasta", "x")


def test_append_text_and_write_lines(tmp_path: Path) -> None:
    """RF-037: append_text acrescenta com ou sem quebra; write_lines grava uma linha por item."""
    path = tmp_path / "log" / "eventos.txt"
    fs.append_text(path, "início")
    fs.append_text(path, "fim", newline=False)
    assert fs.read_text(path) == "início\nfim"
    lines_path = fs.write_lines(tmp_path / "linhas.txt", ["a", "ç"], encoding="cp1252")
    assert lines_path.read_bytes() == "a\nç\n".replace("\n", os.linesep).encode("cp1252")
    assert fs.read_lines(lines_path) == ["a", "ç"]


def test_create_temp_dir_uses_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RF-037: create_temp_dir cria pasta exclusiva com o prefixo."""
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    created = fs.create_temp_dir()
    assert created.is_dir()
    assert created.name.startswith("rpa3003_")
    assert created.parent == tmp_path.resolve()


def test_create_temp_dir_wraps_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """RF-037: falha ao criar pasta temporária vira FileOperationError."""
    import tempfile

    def failing_mkdtemp(prefix: str) -> str:
        raise PermissionError(errno.EACCES, "sem permissão")

    monkeypatch.setattr(tempfile, "mkdtemp", failing_mkdtemp)
    with pytest.raises(FileOperationError, match="temporária"):
        fs.create_temp_dir()


def test_usage_example(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: FakeClock) -> None:
    """Seção 8.10: exemplo de uso do módulo."""
    monkeypatch.chdir(tmp_path)
    entrada = fs.ensure_dir("data/input")
    (entrada / "extrato_10.txt").write_bytes("linha ação\r\n\r\nfim\r\n".encode("cp1252"))
    arquivo = fs.wait_for_file(entrada, "extrato_*.txt", timeout=120)
    linhas = fs.read_lines(arquivo, skip_empty=True)
    fs.write_text("data/output/resumo.txt", f"Total de linhas: {len(linhas)}")
    assert linhas == ["linha ação", "fim"]
    assert fs.read_text("data/output/resumo.txt") == "Total de linhas: 2"
    assert fs.clean_dir("data/output", "*.tmp", older_than_days=7) == 0
    assert fs.safe_filename("Relatório: 10/2026.pdf") == "Relatório_ 10_2026.pdf"


@pytest.mark.skipif(sys.platform != "win32", reason="nomes reservados só bloqueiam no Windows")
def test_safe_filename_output_is_creatable_on_windows(tmp_path: Path) -> None:
    """RF-037: nomes gerados por safe_filename podem ser criados no Windows."""
    for raw in ("CON", "aux.txt", "rel:2026/10?.csv", "fim. "):
        (tmp_path / fs.safe_filename(raw)).write_text("x", encoding="utf-8")
    assert len(list(tmp_path.iterdir())) == 4
