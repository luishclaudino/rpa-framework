"""Testes de `rpa_3003.utils.email_utils` (RF-044, RF-045, RNF-007, RNF-009, seção 8.16)."""

# Credenciais e senhas deste arquivo são fictícias (valores de teste, não segredos).
# ruff: noqa: S105

from __future__ import annotations

import imaplib
import locale
import mimetypes
import smtplib
import ssl
import sys
import types
from collections.abc import Iterator
from datetime import UTC, date, datetime
from email import policy
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call

import pytest
from pydantic import SecretStr
from pytest_mock import MockerFixture

from rpa_3003.core.exceptions import EmailError, FileOperationError
from rpa_3003.core.logger import MASK, get_logger
from rpa_3003.utils import email_utils
from rpa_3003.utils.email_utils import (
    EmailAttachment,
    EmailMessageData,
    EmailReader,
    EmailSender,
    IMAPEmailClient,
    OutlookEmailClient,
    ReceivedEmail,
    SMTPEmailClient,
    render_template,
    validate_email_address,
)

pytestmark = pytest.mark.unit

ROBOT = "robo@empresa.com"
PASSWORD = "S3nh@Email"


@pytest.fixture
def log_messages() -> list[tuple[str, str]]:
    """Mensagens registradas no logger (nível, mensagem já mascarada), a partir de DEBUG."""
    from loguru import logger

    get_logger()
    records: list[tuple[str, str]] = []
    logger.add(
        lambda message: records.append((message.record["level"].name, message.record["message"])),
        level="DEBUG",
    )
    return records


@pytest.fixture
def attachments(tmp_path: Path) -> dict[str, Path]:
    """Arquivos de anexo: PDF, CSV e extensão desconhecida."""
    files = {
        "pdf": tmp_path / "relatório.pdf",
        "csv": tmp_path / "pendentes.csv",
        "bin": tmp_path / "dados.xyz123",
    }
    files["pdf"].write_bytes(b"%PDF-1.4 conteudo")
    files["csv"].write_text("id;nome\n1;José\n", encoding="utf-8")
    files["bin"].write_bytes(b"\x00\x01")
    return files


@pytest.fixture
def smtp_class(mocker: MockerFixture) -> MagicMock:
    """`smtplib.SMTP` simulado; o cliente é `smtp_class.return_value.__enter__.return_value`."""
    smtp_mock: MagicMock = mocker.patch("smtplib.SMTP")
    smtp_mock.return_value.__enter__.return_value = smtp_mock.return_value
    return smtp_mock


def _sent_message(smtp_class: MagicMock) -> tuple[EmailMessage, dict[str, Any]]:
    send_call = smtp_class.return_value.send_message.call_args
    return send_call.args[0], send_call.kwargs


# --- SMTP -------------------------------------------------------------------------------------


def test_smtp_send_has_subject_recipients_and_attachments(
    smtp_class: MagicMock, attachments: dict[str, Path]
) -> None:
    """RF-044: com smtplib.SMTP simulado, a mensagem tem assunto, destinatários e anexos."""
    client = SMTPEmailClient("smtp.empresa.com", username=ROBOT, password=PASSWORD)
    client.send(
        EmailMessageData(
            subject="Resumo RPA de outubro (ação)",
            body="<p>Processados: 12</p>",
            html=True,
            to=["fin@empresa.com"],
            cc=["gestor@empresa.com"],
            bcc=["auditoria@empresa.com"],
            reply_to="suporte@empresa.com",
            attachments=[attachments["pdf"], attachments["csv"], attachments["bin"]],
        )
    )
    smtp_class.assert_called_once_with("smtp.empresa.com", 587, timeout=30.0)
    server = smtp_class.return_value
    server.login.assert_called_once_with(ROBOT, PASSWORD)
    mime, kwargs = _sent_message(smtp_class)
    assert kwargs == {
        "from_addr": ROBOT,
        "to_addrs": ["fin@empresa.com", "gestor@empresa.com", "auditoria@empresa.com"],
    }
    assert mime["Subject"] == "Resumo RPA de outubro (ação)"
    assert mime["From"] == ROBOT
    assert mime["To"] == "fin@empresa.com"
    assert mime["Cc"] == "gestor@empresa.com"
    assert mime["Reply-To"] == "suporte@empresa.com"
    assert mime["Bcc"] is None
    assert mime["Date"] and mime["Message-ID"].endswith("@empresa.com>")
    assert mime.get_body(("html",)).get_content().strip() == "<p>Processados: 12</p>"
    parts = {
        part.get_filename(): (part.get_content_type(), part.get_payload(decode=True))
        for part in mime.iter_attachments()
    }
    assert parts == {
        "relatório.pdf": ("application/pdf", b"%PDF-1.4 conteudo"),
        # tipo do mimetypes (no Windows vem do registro) e bytes gravados em modo texto
        "pendentes.csv": (
            mimetypes.guess_type("pendentes.csv")[0],
            attachments["csv"].read_bytes(),
        ),
        "dados.xyz123": ("application/octet-stream", b"\x00\x01"),
    }


def test_smtp_starttls_uses_verified_tls_context(smtp_class: MagicMock) -> None:
    """RNF-009: STARTTLS com ssl.create_default_context() (certificado e hostname verificados)."""
    SMTPEmailClient("smtp.empresa.com", default_from=ROBOT).send(
        EmailMessageData(subject="s", body="b", to=["fin@empresa.com"])
    )
    context = smtp_class.return_value.starttls.call_args.kwargs["context"]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    smtp_class.return_value.login.assert_not_called()  # sem username, sem login


def test_smtp_ssl_mode_uses_smtp_ssl(mocker: MockerFixture) -> None:
    """Seção 8.16: security='ssl' usa smtplib.SMTP_SSL(context=...) com timeout."""
    ssl_class: MagicMock = mocker.patch("smtplib.SMTP_SSL")
    ssl_class.return_value.__enter__.return_value = ssl_class.return_value
    SMTPEmailClient("smtp.empresa.com", 465, security="ssl", timeout=12.5, default_from=ROBOT).send(
        EmailMessageData(subject="s", body="texto", to=["fin@empresa.com"])
    )
    args, kwargs = ssl_class.call_args
    assert args == ("smtp.empresa.com", 465)
    assert kwargs["timeout"] == 12.5
    assert isinstance(kwargs["context"], ssl.SSLContext)
    mime = ssl_class.return_value.send_message.call_args.args[0]
    assert mime.get_content_type() == "text/plain"


def test_smtp_without_tls_logs_warning(
    smtp_class: MagicMock, log_messages: list[tuple[str, str]]
) -> None:
    """Seção 8.16: security='none' não usa TLS e registra WARNING."""
    SMTPEmailClient("relay.interno", 25, security="none", default_from=ROBOT).send(
        EmailMessageData(subject="s", body="b", to=["fin@empresa.com"])
    )
    smtp_class.return_value.starttls.assert_not_called()
    assert ("WARNING", "Conexão SMTP sem TLS | host=relay.interno | porta=25") in log_messages
    assert any(
        m.startswith("E-mail enviado | assunto=s | destinatarios=1") for _l, m in log_messages
    )


def test_smtp_starttls_failure_closes_connection(smtp_class: MagicMock) -> None:
    """Seção 8.16: falha no STARTTLS fecha a conexão e gera EmailError."""
    smtp_class.return_value.starttls.side_effect = smtplib.SMTPNotSupportedError("sem STARTTLS")
    with pytest.raises(EmailError, match="Falha ao enviar e-mail via SMTP") as info:
        SMTPEmailClient("smtp.empresa.com", default_from=ROBOT).send(
            EmailMessageData(subject="s", body="b", to=["fin@empresa.com"])
        )
    smtp_class.return_value.close.assert_called_once()
    assert info.value.details["host"] == "smtp.empresa.com"


def test_smtp_sender_precedence(smtp_class: MagicMock) -> None:
    """Seção 8.16: remetente = from_addr → default_from → username."""
    client = SMTPEmailClient(
        "smtp", username=ROBOT, password=PASSWORD, default_from="noreply@empresa.com"
    )
    client.send(
        EmailMessageData(subject="s", body="b", to=["a@empresa.com"], from_addr="x@empresa.com")
    )
    assert _sent_message(smtp_class)[1]["from_addr"] == "x@empresa.com"
    client.send(EmailMessageData(subject="s", body="b", to=["a@empresa.com"]))
    assert _sent_message(smtp_class)[1]["from_addr"] == "noreply@empresa.com"
    SMTPEmailClient("smtp", username=ROBOT, password=PASSWORD).send(
        EmailMessageData(subject="s", body="b", to=["a@empresa.com"])
    )
    assert _sent_message(smtp_class)[1]["from_addr"] == ROBOT


def test_smtp_without_sender_raises(smtp_class: MagicMock) -> None:
    """Seção 8.16: sem remetente resolvível, EmailError antes de conectar."""
    with pytest.raises(EmailError, match="Remetente"):
        SMTPEmailClient("smtp").send(EmailMessageData(subject="s", body="b", to=["a@empresa.com"]))
    smtp_class.assert_not_called()


@pytest.mark.parametrize(
    ("message", "match"),
    [
        (EmailMessageData(subject="s", body="b", to=[]), "destinatários"),
        (EmailMessageData(subject="s", body="b", to=["sem-arroba"]), "inválidos"),
        (
            EmailMessageData(subject="s", body="b", to=["a@empresa.com"], cc=["b@empresa"]),
            "inválidos",
        ),
        (
            EmailMessageData(subject="s", body="b", to=["a@empresa.com"], bcc=["c d@e.com"]),
            "inválidos",
        ),
        (
            EmailMessageData(subject="s", body="b", to=["a@empresa.com"], reply_to="resposta"),
            "inválidos",
        ),
        (
            EmailMessageData(subject="s", body="b", to=["a@empresa.com"], from_addr="robo"),
            "inválidos",
        ),
        (
            EmailMessageData(
                subject="s", body="b", to=["a@empresa.com"], attachments=[Path("nao_existe.pdf")]
            ),
            "não encontrados",
        ),
    ],
)
def test_smtp_validation_happens_before_connecting(
    smtp_class: MagicMock, message: EmailMessageData, match: str
) -> None:
    """Seção 8.16: destinatários, endereços e anexos são validados antes de conectar."""
    with pytest.raises(EmailError, match=match):
        SMTPEmailClient("smtp", default_from=ROBOT).send(message)
    smtp_class.assert_not_called()


def test_smtp_attachments_over_limit_raise_before_connecting(
    smtp_class: MagicMock, tmp_path: Path
) -> None:
    """RF-044: anexos que somam mais que max_attachments_mb geram EmailError antes da conexão."""
    first = tmp_path / "a.bin"
    second = tmp_path / "b.bin"
    first.write_bytes(b"x" * 600_000)
    second.write_bytes(b"x" * 600_000)
    client = SMTPEmailClient("smtp", default_from=ROBOT, max_attachments_mb=1.0)
    with pytest.raises(EmailError, match="limite") as info:
        client.send(
            EmailMessageData(
                subject="s", body="b", to=["a@empresa.com"], attachments=[first, second]
            )
        )
    assert info.value.details == {"total_mb": 1.14, "limite_mb": 1.0}
    smtp_class.assert_not_called()
    client.send(EmailMessageData(subject="s", body="b", to=["a@empresa.com"], attachments=[first]))
    smtp_class.assert_called_once()


def test_smtp_errors_become_email_error(smtp_class: MagicMock) -> None:
    """Seção 8.16: erros do smtplib e de rede viram EmailError sem expor a senha."""
    smtp_class.return_value.login.side_effect = smtplib.SMTPAuthenticationError(535, b"invalid")
    client = SMTPEmailClient("smtp.empresa.com", username=ROBOT, password=SecretStr(PASSWORD))
    with pytest.raises(EmailError) as info:
        client.send(EmailMessageData(subject="s", body="b", to=["a@empresa.com"]))
    assert "SMTPAuthenticationError" in info.value.details["erro"]
    assert PASSWORD not in str(info.value)
    smtp_class.side_effect = TimeoutError("timed out")
    with pytest.raises(EmailError, match="Falha ao enviar"):
        client.send(EmailMessageData(subject="s", body="b", to=["a@empresa.com"]))


def test_smtp_header_injection_is_rejected(smtp_class: MagicMock) -> None:
    """RNF-010: assunto com quebra de linha gera EmailError antes de conectar."""
    with pytest.raises(EmailError, match="inválida"):
        SMTPEmailClient("smtp", default_from=ROBOT).send(
            EmailMessageData(subject="ok\r\nBcc: x@evil.com", body="b", to=["a@empresa.com"])
        )
    smtp_class.assert_not_called()


def test_smtp_accepts_single_address_string(smtp_class: MagicMock) -> None:
    """Seção 8.16: um único endereço em texto é tratado como lista."""
    message = EmailMessageData(subject="s", body="b", to="a@empresa.com", cc="b@empresa.com")  # type: ignore[arg-type]
    SMTPEmailClient("smtp", default_from=ROBOT).send(message)
    assert _sent_message(smtp_class)[1]["to_addrs"] == ["a@empresa.com", "b@empresa.com"]


def test_smtp_password_registered_for_masking(log_messages: list[tuple[str, str]]) -> None:
    """RNF-008: a senha do SMTP é registrada para mascaramento."""
    SMTPEmailClient("smtp", username=ROBOT, password=PASSWORD)
    get_logger().info("senha={}", PASSWORD)
    assert ("INFO", f"senha={MASK}") in log_messages


@pytest.mark.parametrize(
    "kwargs", [{"security": "tls"}, {"timeout": 0}, {"max_attachments_mb": -1}]
)
def test_smtp_rejects_invalid_arguments(kwargs: dict[str, Any]) -> None:
    """Seção 8.16: parâmetros inválidos do cliente SMTP geram ValueError."""
    with pytest.raises(ValueError, match="deve"):
        SMTPEmailClient("smtp", **kwargs)


def test_smtp_reading_attachment_failure_becomes_email_error(
    smtp_class: MagicMock, attachments: dict[str, Path], mocker: MockerFixture
) -> None:
    """Seção 8.16: falha ao ler um anexo vira EmailError."""
    mocker.patch.object(Path, "read_bytes", side_effect=PermissionError("bloqueado"))
    with pytest.raises(EmailError, match="Falha ao ler anexo"):
        SMTPEmailClient("smtp", default_from=ROBOT).send(
            EmailMessageData(
                subject="s", body="b", to=["a@empresa.com"], attachments=[attachments["pdf"]]
            )
        )


# --- IMAP -------------------------------------------------------------------------------------


def _raw_message(
    *,
    subject: str,
    sender: str = "banco@cobranca.com",
    to: str = ROBOT,
    cc: str | None = None,
    sent: str = "Sat, 31 Oct 2026 10:15:00 -0300",
    text: str | None = "Segue o boleto.",
    html: str | None = None,
    files: dict[str, bytes] | None = None,
) -> bytes:
    message = EmailMessage(policy=policy.SMTP)
    message["Subject"] = subject
    message["From"] = f"Banco <{sender}>"
    message["To"] = to
    if cc:
        message["Cc"] = cc
    message["Date"] = sent
    if text is not None:
        message.set_content(text)
    if html is not None:
        if text is None:
            message.set_content(html, subtype="html")
        else:
            message.add_alternative(html, subtype="html")
    for name, data in (files or {}).items():
        message.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    return message.as_bytes()


class FakeImap:
    """Servidor IMAP mínimo em memória para o `IMAP4_SSL` simulado."""

    def __init__(
        self, folders: dict[str, dict[str, bytes]], capabilities: str = "IMAP4rev1"
    ) -> None:
        self.folders = folders
        self.selected: str | None = None
        self.capabilities = capabilities
        self.mock = MagicMock(name="IMAP4_SSL()")
        self.mock.login.return_value = ("OK", [b"LOGIN completed"])
        self.mock.select.side_effect = self._select
        self.mock.uid.side_effect = self._uid
        self.mock.capability.return_value = ("OK", [capabilities.encode()])
        self.mock.expunge.return_value = ("OK", [None])
        self.mock.logout.return_value = ("BYE", [b"bye"])

    def _select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        name = mailbox.strip('"')
        if name not in self.folders:
            return "NO", [b"Mailbox doesn't exist"]
        self.selected = name
        return "OK", [str(len(self.folders[name])).encode()]

    def _uid(self, command: str, *args: str) -> tuple[str, list[Any]]:
        assert self.selected is not None
        messages = self.folders[self.selected]
        if command == "SEARCH":
            return "OK", [" ".join(messages).encode()]
        if command == "FETCH":
            uid = args[0]
            if uid not in messages:
                return "OK", [None]
            return "OK", [(f"1 (UID {uid} BODY[] {{10}}".encode(), messages[uid]), b")"]
        return "OK", [b"done"]


@pytest.fixture
def mailbox() -> FakeImap:
    return FakeImap(
        {
            "INBOX": {
                "7": _raw_message(
                    subject="Boleto outubro", files={"Boleto 10:2026.PDF": b"%PDF-7"}
                ),
                "12": _raw_message(
                    subject="=?utf-8?b?Qk9MRVRPIEHDp8Ojbw==?=",
                    cc="gestor@empresa.com",
                    text="texto",
                    html="<p>html</p>",
                    files={
                        "boleto.pdf": b"%PDF-12a",
                        "Boleto.pdf": b"%PDF-12b",
                        "nota.xml": b"<x/>",
                    },
                ),
                "9": _raw_message(subject="Reunião", text=None, html="<b>só html</b>"),
            },
            "Boletos": {"7": _raw_message(subject="Arquivo antigo")},
        }
    )


@pytest.fixture
def imap_class(mocker: MockerFixture, mailbox: FakeImap) -> MagicMock:
    imap_mock: MagicMock = mocker.patch("imaplib.IMAP4_SSL", return_value=mailbox.mock)
    return imap_mock


@pytest.fixture
def imap_client(imap_class: MagicMock) -> Iterator[IMAPEmailClient]:
    with IMAPEmailClient("imap.empresa.com", username=ROBOT, password=PASSWORD) as client:
        yield client


def _uid_calls(mailbox: FakeImap, command: str) -> list[tuple[Any, ...]]:
    return [c.args for c in mailbox.mock.uid.call_args_list if c.args[0] == command]


def test_imap_connect_uses_ssl_context_and_timeout(
    imap_class: MagicMock, mailbox: FakeImap
) -> None:
    """Seção 8.16/RNF-007/RNF-009: IMAP4_SSL(host, port, ssl_context, timeout) e login."""
    client = IMAPEmailClient(
        "imap.empresa.com", username=ROBOT, password=SecretStr(PASSWORD), timeout=15
    )
    assert client.connect() is client
    assert client.connect() is client  # idempotente
    args, kwargs = imap_class.call_args
    assert args == ("imap.empresa.com", 993)
    assert kwargs["timeout"] == 15
    assert isinstance(kwargs["ssl_context"], ssl.SSLContext)
    assert kwargs["ssl_context"].verify_mode == ssl.CERT_REQUIRED
    imap_class.assert_called_once()
    mailbox.mock.login.assert_called_once_with(ROBOT, PASSWORD)
    client.close()
    client.close()
    mailbox.mock.logout.assert_called_once()


def test_imap_since_criterion_independent_of_locale(
    imap_client: IMAPEmailClient, mailbox: FakeImap
) -> None:
    """RF-044: since=date(2026, 10, 31) envia `SINCE 31-Oct-2026`, independente do locale."""
    previous = locale.setlocale(locale.LC_TIME)
    for name in ("Portuguese_Brazil.1252", "pt_BR.UTF-8", "pt_BR"):
        try:
            locale.setlocale(locale.LC_TIME, name)
            break
        except locale.Error:
            continue
    try:
        imap_client.search(since=date(2026, 10, 31))
    finally:
        locale.setlocale(locale.LC_TIME, previous)
    assert _uid_calls(mailbox, "SEARCH") == [("SEARCH", "UNSEEN SINCE 31-Oct-2026")]


@pytest.mark.parametrize(
    ("kwargs", "criteria"),
    [
        ({}, "UNSEEN"),
        ({"unseen_only": False}, "ALL"),
        ({"unseen_only": False, "since": date(2026, 1, 5)}, "SINCE 05-Jan-2026"),
        ({"from_addr": "banco@cobranca.com"}, 'UNSEEN FROM "banco@cobranca.com"'),
        ({"unseen_only": False, "from_addr": 'a"b\\c'}, 'FROM "a\\"b\\\\c"'),
    ],
)
def test_imap_search_criteria(
    imap_client: IMAPEmailClient, mailbox: FakeImap, kwargs: dict[str, Any], criteria: str
) -> None:
    """Seção 8.16: critério com UNSEEN, SINCE dd-Mon-yyyy e FROM entre aspas."""
    imap_client.search(**kwargs)
    assert _uid_calls(mailbox, "SEARCH")[-1] == ("SEARCH", criteria)
    mailbox.mock.select.assert_called_with("INBOX", readonly=False)


def test_imap_fetch_uses_body_peek_and_orders_newest_first(
    imap_client: IMAPEmailClient, mailbox: FakeImap
) -> None:
    """Seção 8.16: UID FETCH <uid> (BODY.PEEK[]) e resultado do mais recente ao mais antigo."""
    found = imap_client.search()
    assert [email.uid for email in found] == ["12", "9", "7"]
    assert _uid_calls(mailbox, "FETCH") == [
        ("FETCH", "12", "(BODY.PEEK[])"),
        ("FETCH", "9", "(BODY.PEEK[])"),
        ("FETCH", "7", "(BODY.PEEK[])"),
    ]
    assert _uid_calls(mailbox, "STORE") == []  # a busca não marca como lido


def test_imap_limit_stops_fetching(imap_client: IMAPEmailClient, mailbox: FakeImap) -> None:
    """Seção 8.16: resultado limitado por `limit`; as demais mensagens não são baixadas."""
    assert [email.uid for email in imap_client.search(limit=1)] == ["12"]
    assert len(_uid_calls(mailbox, "FETCH")) == 1
    assert len(imap_client.search(limit=None)) == 3
    with pytest.raises(ValueError, match="limit"):
        imap_client.search(limit=0)


def test_imap_subject_filter_is_client_side_case_insensitive_after_decoding(
    imap_client: IMAPEmailClient,
) -> None:
    """Seção 8.16: subject_contains aplicado no cliente, sem diferenciar caixa, após decodificar."""
    found = imap_client.search(subject_contains="boleto")
    assert [(email.uid, email.subject) for email in found] == [
        ("12", "BOLETO Ação"),
        ("7", "Boleto outubro"),
    ]
    assert [email.uid for email in imap_client.search(subject_contains="AÇÃO")] == ["12"]


def test_imap_parses_addresses_dates_bodies_and_attachments(imap_client: IMAPEmailClient) -> None:
    """Seção 8.16: parsing com policy.default: remetente, destinatários, data, corpos e anexos."""
    newest, html_only, oldest = imap_client.search()
    assert newest.from_addr == "banco@cobranca.com"
    assert newest.to == [ROBOT]
    assert newest.cc == ["gestor@empresa.com"]
    assert newest.date is not None
    assert newest.date.astimezone(UTC) == datetime(2026, 10, 31, 13, 15, tzinfo=UTC)
    assert newest.body_text.strip() == "texto"
    assert newest.body_html.strip() == "<p>html</p>"
    assert [(a.filename, a.content_type, a.size) for a in newest.attachments] == [
        ("boleto.pdf", "application/pdf", 8),
        ("Boleto.pdf", "application/pdf", 8),
        ("nota.xml", "application/pdf", 4),
    ]
    assert html_only.body_text == ""
    assert html_only.body_html.strip() == "<b>só html</b>"
    assert oldest.attachments[0].payload == b"%PDF-7"
    assert "payload" not in repr(oldest.attachments[0])


def test_imap_save_attachments_pattern_sanitizing_and_suffixes(
    imap_client: IMAPEmailClient, tmp_path: Path
) -> None:
    """Seção 8.16: pattern sem diferenciar caixa, safe_filename e sufixos _1, _2."""
    newest, _html_only, oldest = imap_client.search()
    dest = tmp_path / "entrada"
    saved = imap_client.save_attachments(newest, dest, pattern="*.PDF")
    # No Windows "boleto.pdf" e "Boleto.pdf" são o mesmo arquivo: o segundo recebe sufixo.
    second = "Boleto_1.pdf" if sys.platform == "win32" else "Boleto.pdf"
    assert [path.name for path in saved] == ["boleto.pdf", second]
    assert all(path.is_absolute() for path in saved)
    assert [path.read_bytes() for path in saved] == [b"%PDF-12a", b"%PDF-12b"]
    again = imap_client.save_attachments(newest, dest, pattern="boleto.pdf")
    assert again[0].name == "boleto_1.pdf" if sys.platform != "win32" else "boleto_2.pdf"
    assert len({*saved, *again}) == 4  # nenhum arquivo sobrescrito
    sanitized = imap_client.save_attachments(oldest, dest)
    assert [path.name for path in sanitized] == ["Boleto 10_2026.PDF"]
    assert imap_client.save_attachments(newest, dest, pattern="*.zip") == []


def test_save_attachments_write_failure_becomes_file_operation_error(tmp_path: Path) -> None:
    """Seção 8.16: falha ao gravar anexos vira FileOperationError."""
    blocker = tmp_path / "arquivo"
    blocker.write_text("x", encoding="utf-8")
    email = ReceivedEmail(
        "1",
        "s",
        "a@b.com",
        [],
        [],
        None,
        "",
        "",
        [EmailAttachment("a.pdf", "application/pdf", 1, b"x")],
    )
    with pytest.raises(FileOperationError, match="Falha ao gravar anexos"):
        email_utils._save_attachments(email, blocker / "sub", "*")


def test_imap_mark_as_read_uses_uid_store_seen(
    imap_client: IMAPEmailClient, mailbox: FakeImap
) -> None:
    """Seção 8.16: mark_as_read envia UID STORE <uid> +FLAGS (\\Seen)."""
    email = imap_client.search(limit=1)[0]
    imap_client.mark_as_read(email)
    assert _uid_calls(mailbox, "STORE") == [("STORE", "12", "+FLAGS", "(\\Seen)")]


def test_imap_mark_as_read_selects_folder_where_email_was_found(
    imap_client: IMAPEmailClient, mailbox: FakeImap
) -> None:
    """Seção 8.16: mark_as_read atua na pasta em que o e-mail foi encontrado."""
    from_inbox = imap_client.search(limit=None)[-1]
    from_folder = imap_client.search(folder="Boletos")[0]
    assert from_inbox.uid == from_folder.uid == "7"
    imap_client.mark_as_read(from_inbox)
    assert mailbox.selected == "INBOX"
    imap_client.mark_as_read(from_folder)
    assert mailbox.selected == "Boletos"


def test_imap_mark_as_read_without_known_folder_raises(imap_client: IMAPEmailClient) -> None:
    """Seção 8.16: e-mail sem pasta conhecida (nenhuma selecionada) gera EmailError."""
    email = ReceivedEmail("1", "s", "a@b.com", [], [], None, "", "", [])
    with pytest.raises(EmailError, match="Pasta do e-mail desconhecida"):
        imap_client.mark_as_read(email)


def test_imap_move_uses_uid_move_when_supported(mocker: MockerFixture, mailbox: FakeImap) -> None:
    """Seção 8.16: move usa UID MOVE quando o servidor anuncia MOVE."""
    mailbox.mock.capability.return_value = ("OK", [b"IMAP4rev1 UIDPLUS MOVE"])
    mocker.patch("imaplib.IMAP4_SSL", return_value=mailbox.mock)
    with IMAPEmailClient("imap", username=ROBOT, password=PASSWORD) as client:
        email = client.search(limit=1)[0]
        client.move(email, "Processados")
    assert _uid_calls(mailbox, "MOVE") == [("MOVE", "12", "Processados")]
    assert _uid_calls(mailbox, "COPY") == []
    mailbox.mock.expunge.assert_not_called()


def test_imap_move_falls_back_to_copy_delete_expunge(
    imap_client: IMAPEmailClient, mailbox: FakeImap
) -> None:
    """Seção 8.16: sem MOVE, usa UID COPY + \\Deleted + EXPUNGE; pasta acentuada em UTF-7."""
    email = imap_client.search(limit=1)[0]
    imap_client.move(email, "Itens Excluídos")
    assert _uid_calls(mailbox, "COPY") == [("COPY", "12", '"Itens Exclu&AO0-dos"')]
    assert _uid_calls(mailbox, "STORE") == [("STORE", "12", "+FLAGS", "(\\Deleted)")]
    mailbox.mock.expunge.assert_called_once()


@pytest.mark.parametrize(
    ("name", "encoded"),
    [
        ("INBOX", "INBOX"),
        ("INBOX/Boletos", "INBOX/Boletos"),
        ("Caixa de Entrada", '"Caixa de Entrada"'),
        ("Ações & Notas", '"A&AOcA9Q-es &- Notas"'),
        ("日本", '"&ZeVnLA-"'),
    ],
)
def test_imap_mailbox_names_use_modified_utf7(name: str, encoded: str) -> None:
    """Seção 8.16: nomes de pasta em UTF-7 modificado (RFC 3501), com aspas quando necessário."""
    assert email_utils._imap_mailbox(name) == encoded


def test_imap_errors_become_email_error(imap_client: IMAPEmailClient, mailbox: FakeImap) -> None:
    """Seção 8.16: resposta diferente de OK e exceções do imaplib viram EmailError."""
    with pytest.raises(EmailError, match="recusou o comando SELECT") as info:
        imap_client.search(folder="Inexistente")
    assert "doesn't exist" in info.value.details["resposta"]
    mailbox.mock.uid.side_effect = imaplib.IMAP4.abort("socket error")
    with pytest.raises(EmailError, match="Falha no comando IMAP SEARCH"):
        imap_client.search()


def test_imap_skips_message_removed_between_search_and_fetch(
    imap_client: IMAPEmailClient, mailbox: FakeImap
) -> None:
    """Seção 8.16: mensagem que deixou de existir entre a busca e a leitura é ignorada."""
    original = mailbox.mock.uid.side_effect

    def without_12(command: str, *args: str) -> tuple[str, list[Any]]:
        if command == "FETCH" and args[0] == "12":
            return "OK", [None]
        return original(command, *args)

    mailbox.mock.uid.side_effect = without_12
    assert [email.uid for email in imap_client.search()] == ["9", "7"]


def test_imap_connection_and_login_failures(mocker: MockerFixture, mailbox: FakeImap) -> None:
    """Seção 8.16: falha de conexão ou de login gera EmailError; o socket é encerrado."""
    mocker.patch("imaplib.IMAP4_SSL", side_effect=ConnectionRefusedError("recusada"))
    with pytest.raises(EmailError, match="Falha ao conectar ao servidor IMAP"):
        IMAPEmailClient("imap", username=ROBOT, password=PASSWORD).connect()
    mailbox.mock.login.side_effect = imaplib.IMAP4.error("AUTHENTICATIONFAILED")
    mocker.patch("imaplib.IMAP4_SSL", return_value=mailbox.mock)
    client = IMAPEmailClient("imap", username=ROBOT, password=PASSWORD)
    with pytest.raises(EmailError, match="autenticação") as info:
        client.search()  # conecta automaticamente
    assert PASSWORD not in str(info.value)
    mailbox.mock.shutdown.assert_called_once()


def test_imap_rejects_invalid_timeout() -> None:
    """Seção 8.16: timeout não positivo gera ValueError."""
    with pytest.raises(ValueError, match="timeout"):
        IMAPEmailClient("imap", username=ROBOT, password=PASSWORD, timeout=0)


def test_parse_email_without_filename_and_with_bad_charset() -> None:
    """Seção 8.16: anexo sem nome recebe nome gerado; charset desconhecido não quebra a leitura."""
    raw = (
        b"From: a@b.com\r\nTo: c@d.com\r\nSubject: x\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: text/plain; charset=desconhecido\r\n\r\nol\xe1\r\n"
        b"--B\r\nContent-Type: image/png\r\nContent-Disposition: attachment\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\niVBORw==\r\n--B--\r\n"
    )
    parsed = email_utils._parse_email("5", raw)
    assert parsed.date is None
    assert parsed.body_text.startswith("ol")
    assert [(a.filename, a.content_type) for a in parsed.attachments] == [
        ("anexo_1.png", "image/png")
    ]


def test_clients_satisfy_protocols() -> None:
    """Seção 7.3: SMTP, IMAP e Outlook são intercambiáveis via EmailSender/EmailReader."""
    sender: EmailSender = SMTPEmailClient("smtp")
    reader: EmailReader = IMAPEmailClient("imap", username=ROBOT, password=PASSWORD)
    assert callable(sender.send) and callable(reader.search)


# --- Outlook ----------------------------------------------------------------------------------


def _outlook_item(
    entry_id: str,
    *,
    subject: str,
    unread: bool = True,
    received: datetime = datetime(2026, 10, 31, 9, 30),
    sender: str = "banco@cobranca.com",
    attachments: dict[str, bytes] | None = None,
    item_class: int = 43,
) -> MagicMock:
    item = MagicMock(name=f"MailItem({entry_id})")
    item.Class = item_class
    item.EntryID = entry_id
    item.Subject = subject
    item.UnRead = unread
    item.ReceivedTime = received
    item.SenderEmailType = "SMTP"
    item.SenderEmailAddress = sender
    item.Body = f"corpo {entry_id}"
    item.HTMLBody = f"<p>{entry_id}</p>"
    item.BodyFormat = 2
    to_recipient = MagicMock(Type=1, Address="robo@empresa.com")
    to_recipient.PropertyAccessor.GetProperty.side_effect = Exception("sem PR_SMTP_ADDRESS")
    cc_recipient = MagicMock(Type=2, Address="/O=EXCHANGE/CN=GESTOR")
    cc_recipient.PropertyAccessor.GetProperty.return_value = "gestor@empresa.com"
    item.Recipients = [to_recipient, cc_recipient]
    files = list((attachments or {}).items())
    item.Attachments.Count = len(files)

    def attachment(index: int) -> MagicMock:
        name, data = files[index - 1]
        att = MagicMock(FileName=name)
        att.SaveAsFile.side_effect = lambda path: Path(path).write_bytes(data)
        return att

    item.Attachments.Item.side_effect = attachment
    return item


class FakeItems:
    """Coleção `Items` do Outlook com Sort, GetFirst e GetNext."""

    def __init__(self, items: list[MagicMock]) -> None:
        self.items = items
        self.position = 0
        self.Sort = MagicMock(name="Sort")

    def GetFirst(self) -> MagicMock | None:  # noqa: N802 - nome do método COM
        self.position = 0
        return self.GetNext()

    def GetNext(self) -> MagicMock | None:  # noqa: N802 - nome do método COM
        if self.position >= len(self.items):
            return None
        self.position += 1
        return self.items[self.position - 1]


@pytest.fixture
def outlook(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Módulo `win32com.client` simulado; retorna `Outlook.Application` (`Dispatch()`)."""
    client_module = types.ModuleType("win32com.client")
    application = MagicMock(name="Outlook.Application")
    client_module.Dispatch = MagicMock(return_value=application)  # type: ignore[attr-defined]
    package = types.ModuleType("win32com")
    package.client = client_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "win32com", package)
    monkeypatch.setitem(sys.modules, "win32com.client", client_module)
    monkeypatch.setattr(email_utils, "_is_windows", lambda: True)
    namespace = application.GetNamespace.return_value
    inbox = namespace.GetDefaultFolder.return_value
    inbox.Items = FakeItems(
        [
            _outlook_item("E5", subject="Convite", item_class=26),
            _outlook_item(
                "E4", subject="Boleto novembro", attachments={"boleto:11.pdf": b"%PDF-4"}
            ),
            _outlook_item("E3", subject="Boleto lido", unread=False),
            _outlook_item("E2", subject="Outro assunto", sender="rh@empresa.com"),
            _outlook_item("E1", subject="Boleto antigo", received=datetime(2026, 9, 1, 8, 0)),
        ]
    )
    subfolder = MagicMock(Name="Boletos")
    subfolder.Items = FakeItems([_outlook_item("S1", subject="Boleto arquivado")])
    nested = MagicMock(Name="2026")
    nested.Items = FakeItems([_outlook_item("N1", subject="Boleto 2026")])
    subfolder.Folders = [nested]
    inbox.Folders = [MagicMock(Name="Lixo"), subfolder]
    return application


def test_outlook_send_fills_fields_and_attachments(
    outlook: MagicMock, attachments: dict[str, Path]
) -> None:
    """RF-045: com o COM simulado, send preenche To, CC, Subject, HTMLBody e adiciona anexos."""
    sys.modules["win32com.client"].Dispatch.assert_not_called()  # type: ignore[attr-defined]
    client = OutlookEmailClient()
    client.send(
        EmailMessageData(
            subject="Resumo",
            body="<p>ok</p>",
            html=True,
            to=["fin@empresa.com", "ctb@empresa.com"],
            cc=["gestor@empresa.com"],
            bcc=["auditoria@empresa.com"],
            attachments=[attachments["pdf"], attachments["csv"]],
            from_addr="financeiro@empresa.com",
            reply_to="suporte@empresa.com",
        )
    )
    sys.modules["win32com.client"].Dispatch.assert_called_once_with("Outlook.Application")  # type: ignore[attr-defined]
    outlook.CreateItem.assert_called_once_with(0)
    mail = outlook.CreateItem.return_value
    assert mail.To == "fin@empresa.com;ctb@empresa.com"
    assert mail.CC == "gestor@empresa.com"
    assert mail.BCC == "auditoria@empresa.com"
    assert mail.Subject == "Resumo"
    assert mail.HTMLBody == "<p>ok</p>"
    assert mail.SentOnBehalfOfName == "financeiro@empresa.com"
    mail.ReplyRecipients.Add.assert_called_once_with("suporte@empresa.com")
    assert mail.Attachments.Add.call_args_list == [
        call(str(attachments["pdf"].resolve())),
        call(str(attachments["csv"].resolve())),
    ]
    mail.Send.assert_called_once_with()


def test_outlook_send_plain_text_with_account(outlook: MagicMock) -> None:
    """Seção 8.16: texto puro vai em Body; a conta escolhida vira SendUsingAccount."""
    account = MagicMock(SmtpAddress="robo@empresa.com", DisplayName="Robô")
    other = MagicMock(SmtpAddress="outro@empresa.com", DisplayName="Outro")
    outlook.GetNamespace.return_value.Accounts = [other, account]
    client = OutlookEmailClient(account="ROBO@empresa.com")
    client.send(EmailMessageData(subject="s", body="texto", to=["a@empresa.com"]))
    mail = outlook.CreateItem.return_value
    assert mail.Body == "texto"
    assert mail.SendUsingAccount is account
    mail.ReplyRecipients.Add.assert_not_called()


def test_outlook_unknown_account_raises(outlook: MagicMock) -> None:
    """Seção 8.16: conta inexistente no perfil gera EmailError listando as contas."""
    outlook.GetNamespace.return_value.Accounts = [
        MagicMock(SmtpAddress="a@empresa.com", DisplayName="A")
    ]
    with pytest.raises(EmailError, match="Conta do Outlook não encontrada") as info:
        OutlookEmailClient(account="robo@empresa.com")
    assert info.value.details["contas"] == "a@empresa.com"


def test_outlook_send_validates_before_using_com(outlook: MagicMock) -> None:
    """Seção 8.16: a validação ocorre antes de criar o item no Outlook."""
    client = OutlookEmailClient()
    with pytest.raises(EmailError, match="destinatários"):
        client.send(EmailMessageData(subject="s", body="b", to=[]))
    outlook.CreateItem.assert_not_called()


def test_outlook_send_com_failure_becomes_email_error(outlook: MagicMock) -> None:
    """Seção 8.16: falha de COM no envio vira EmailError."""
    outlook.CreateItem.return_value.Send.side_effect = Exception("com_error")
    with pytest.raises(EmailError, match="Falha ao enviar e-mail pelo Outlook"):
        OutlookEmailClient().send(EmailMessageData(subject="s", body="b", to=["a@empresa.com"]))


def test_outlook_search_filters_sorts_and_builds_emails(outlook: MagicMock) -> None:
    """RF-045: leitura ordenada por ReceivedTime, filtrada em Python, com a estrutura do RF-044."""
    client = OutlookEmailClient()
    found = client.search(subject_contains="BOLETO")
    inbox = outlook.GetNamespace.return_value.GetDefaultFolder
    inbox.assert_called_with(6)
    inbox.return_value.Items.Sort.assert_called_once_with("[ReceivedTime]", True)
    assert [email.uid for email in found] == ["E4", "E1"]
    newest = found[0]
    assert newest.subject == "Boleto novembro"
    assert newest.from_addr == "banco@cobranca.com"
    assert newest.to == ["robo@empresa.com"]
    assert newest.cc == ["gestor@empresa.com"]
    assert newest.date == datetime(2026, 10, 31, 9, 30).astimezone()
    assert newest.body_text == "corpo E4"
    assert newest.body_html == "<p>E4</p>"
    assert [(a.filename, a.content_type, a.payload) for a in newest.attachments] == [
        ("boleto:11.pdf", "application/pdf", b"%PDF-4")
    ]


def test_outlook_search_since_from_unseen_and_limits(outlook: MagicMock) -> None:
    """Seção 8.16: filtros since, from_addr, unseen_only, limit e max_scan."""
    client = OutlookEmailClient()
    assert [e.uid for e in client.search(since=date(2026, 10, 1))] == ["E4", "E2"]
    assert [e.uid for e in client.search(unseen_only=False, from_addr="COBRANCA")] == [
        "E4",
        "E3",
        "E1",
    ]
    assert [e.uid for e in client.search(unseen_only=False, limit=2)] == ["E4", "E3"]
    assert [
        e.uid for e in OutlookEmailClient(max_scan=2).search(unseen_only=False, limit=None)
    ] == ["E4"]
    with pytest.raises(ValueError, match="max_scan"):
        OutlookEmailClient(max_scan=0)
    with pytest.raises(ValueError, match="limit"):
        client.search(limit=0)


def test_outlook_search_subfolders_by_name(outlook: MagicMock) -> None:
    """Seção 8.16: subpasta da Caixa de Entrada pelo nome (sem diferenciar caixa), com níveis."""
    client = OutlookEmailClient()
    assert [e.uid for e in client.search(folder="boletos")] == ["S1"]
    assert [e.uid for e in client.search(folder="Boletos/2026")] == ["N1"]
    assert [e.uid for e in client.search(folder="Caixa de Entrada", limit=1)] == ["E4"]
    with pytest.raises(EmailError, match="Pasta do Outlook não encontrada: Notas") as info:
        client.search(folder="Notas")
    assert info.value.details["disponiveis"] == "Lixo, Boletos"


def test_outlook_search_with_account_uses_its_inbox(outlook: MagicMock) -> None:
    """Seção 8.16: com conta escolhida, a leitura usa a Caixa de Entrada dessa conta."""
    account = MagicMock(SmtpAddress="robo@empresa.com", DisplayName="Robô")
    store_inbox = account.DeliveryStore.GetDefaultFolder.return_value
    store_inbox.Items = FakeItems([_outlook_item("A1", subject="Conta")])
    outlook.GetNamespace.return_value.Accounts = [account]
    assert [e.uid for e in OutlookEmailClient(account="Robô").search()] == ["A1"]
    account.DeliveryStore.GetDefaultFolder.assert_called_with(6)


def test_outlook_exchange_sender_resolution_and_plain_body(outlook: MagicMock) -> None:
    """Seção 8.16: remetente Exchange resolvido para SMTP; corpo não HTML deixa body_html vazio."""
    item = _outlook_item("X1", subject="Exchange")
    item.SenderEmailType = "EX"
    item.Sender.GetExchangeUser.return_value.PrimarySmtpAddress = "colega@empresa.com"
    item.BodyFormat = 1
    broken = _outlook_item("X2", subject="Exchange sem usuário")
    broken.SenderEmailType = "EX"
    broken.Sender.GetExchangeUser.side_effect = Exception("sem usuário")
    broken.SenderEmailAddress = "/O=EXCHANGE/CN=X"
    outlook.GetNamespace.return_value.GetDefaultFolder.return_value.Items = FakeItems(
        [item, broken]
    )
    first, second = OutlookEmailClient().search()
    assert first.from_addr == "colega@empresa.com"
    assert first.body_html == ""
    assert second.from_addr == "/O=EXCHANGE/CN=X"


def test_outlook_search_com_failure_becomes_email_error(outlook: MagicMock) -> None:
    """Seção 8.16: falha de COM na leitura vira EmailError."""
    outlook.GetNamespace.return_value.GetDefaultFolder.side_effect = Exception("com_error")
    with pytest.raises(EmailError, match="Falha ao ler e-mails do Outlook"):
        OutlookEmailClient().search()


def test_outlook_save_attachments_and_mark_as_read(outlook: MagicMock, tmp_path: Path) -> None:
    """Seção 8.16: anexos gravados com nome saneado; mark_as_read usa EntryID, UnRead e Save."""
    client = OutlookEmailClient()
    email = client.search(subject_contains="novembro")[0]
    saved = client.save_attachments(email, tmp_path, pattern="*.pdf")
    assert [path.name for path in saved] == ["boleto_11.pdf"]
    assert saved[0].read_bytes() == b"%PDF-4"
    client.mark_as_read(email)
    namespace = outlook.GetNamespace.return_value
    namespace.GetItemFromID.assert_called_once_with("E4")
    item = namespace.GetItemFromID.return_value
    assert item.UnRead is False
    item.Save.assert_called_once_with()
    namespace.GetItemFromID.side_effect = Exception("item removido")
    with pytest.raises(EmailError, match="marcar e-mail como lido"):
        client.mark_as_read(email)


def test_outlook_outside_windows_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """RF-045: fora do Windows, a instância lança EmailError com mensagem explicativa."""
    monkeypatch.setattr(email_utils, "_is_windows", lambda: False)
    with pytest.raises(EmailError, match="só está disponível no Windows"):
        OutlookEmailClient()


def test_outlook_without_pywin32_or_outlook_raises(
    outlook: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RF-045: sem pywin32 ou sem Outlook instalado, EmailError com mensagem explicativa."""
    dispatch = sys.modules["win32com.client"].Dispatch  # type: ignore[attr-defined]
    dispatch.side_effect = Exception("Invalid class string")
    with pytest.raises(EmailError, match="Outlook indisponível") as info:
        OutlookEmailClient()
    assert "Invalid class string" in info.value.details["erro"]
    outlook.GetNamespace.side_effect = Exception("perfil")
    dispatch.side_effect = None
    with pytest.raises(EmailError, match="perfil do Outlook"):
        OutlookEmailClient()
    monkeypatch.setitem(sys.modules, "win32com.client", None)
    with pytest.raises(EmailError, match="pywin32"):
        OutlookEmailClient()


# --- validação de endereços e templates -------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "valid"),
    [
        ("fin@empresa.com", True),
        ("joao.silva+rpa@sub.empresa.com.br", True),
        ("fin@empresa", False),
        ("fin.empresa.com", False),
        ("fin @empresa.com", False),
        ("fin@empresa.com\n", False),
        ("a@b@c.com", False),
        ("", False),
        ("Nome <fin@empresa.com>", False),
    ],
)
def test_validate_email_address(address: str, valid: bool) -> None:
    """Seção 8.16: endereços validados por ^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$."""
    assert validate_email_address(address) is valid


def test_render_template_from_text_and_file(tmp_path: Path) -> None:
    """Seção 8.16: string.Template.substitute com texto ou arquivo .html/.txt."""
    assert render_template("<p>Processados: $total</p>", {"total": 12}) == "<p>Processados: 12</p>"
    assert render_template("Valor: R$$ ${valor}", {"valor": "1.500,00"}) == "Valor: R$ 1.500,00"
    html = tmp_path / "resumo.html"
    html.write_text("<h1>Olá, $nome</h1>", encoding="utf-8")
    assert render_template(html, {"nome": "Conceição"}) == "<h1>Olá, Conceição</h1>"
    assert render_template(str(html), {"nome": "José"}) == "<h1>Olá, José</h1>"
    legacy = tmp_path / "aviso.txt"
    legacy.write_bytes("Atenção: $item".encode("cp1252"))
    assert render_template(legacy, {"item": "boleto"}) == "Atenção: boleto"
    assert render_template("Veja o arquivo relatorio.txt", {}) == "Veja o arquivo relatorio.txt"
    assert render_template("linha 1\nnome.html", {}) == "linha 1\nnome.html"


def test_render_template_errors(tmp_path: Path) -> None:
    """Seção 8.16: variável ausente ou template inválido geram EmailError."""
    with pytest.raises(EmailError, match="Variável ausente no template: total") as info:
        render_template("Total: $total", {})
    assert info.value.details == {"variavel": "total"}
    with pytest.raises(EmailError, match="Template de e-mail inválido"):
        render_template("Valor: $ 10", {})
    with pytest.raises(FileOperationError):
        render_template(tmp_path / "inexistente.html", {})
