"""E-mails.

Envio (SMTP, Outlook) e leitura (IMAP, Outlook) de e-mails com anexos.

As mensagens são validadas antes de qualquer conexão (destinatários, endereços, anexos e limite
de tamanho). SMTP e IMAP verificam o certificado TLS por padrão (RNF-009) e têm tempo limite
explícito (RNF-007). O Outlook é acessado por COM (`pywin32`), importado somente quando usado.
"""

from __future__ import annotations

import base64
import contextlib
import email
import fnmatch
import imaplib
import mimetypes
import re
import smtplib
import ssl
import string
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from email import policy
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Any, Final, Literal, Protocol, Self, cast

from pydantic import SecretStr

from rpa_3003.core.config import PathLike
from rpa_3003.core.exceptions import EmailError, FileOperationError
from rpa_3003.core.logger import get_logger, register_secret
from rpa_3003.utils.filesystem_utils import read_text, safe_filename

__all__: list[str] = [
    "IMAP_MONTHS",
    "EmailAttachment",
    "EmailMessageData",
    "EmailReader",
    "EmailSender",
    "IMAPEmailClient",
    "OutlookEmailClient",
    "ReceivedEmail",
    "SMTPEmailClient",
    "render_template",
    "validate_email_address",
]

IMAP_MONTHS: Final[tuple[str, ...]] = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

_EMAIL_ADDRESS: Final[re.Pattern[str]] = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_BYTES_PER_MB: Final[int] = 1024 * 1024
# Limite de anexos do Outlook, que não recebe max_attachments_mb (mesmo padrão do SMTP).
_OUTLOOK_MAX_ATTACHMENTS_MB: Final[float] = 25.0
_DEFAULT_CONTENT_TYPE: Final[str] = "application/octet-stream"
_TEMPLATE_SUFFIXES: Final[frozenset[str]] = frozenset({".html", ".htm", ".txt"})
_IMAP_ERRORS: Final[tuple[type[Exception], ...]] = (imaplib.IMAP4.error, OSError, ValueError)
_SMTP_ERRORS: Final[tuple[type[Exception], ...]] = (smtplib.SMTPException, OSError)
# Nome de pasta IMAP que pode ser enviado sem aspas (atom).
_IMAP_ATOM: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_./-]+")
# Constantes do modelo de objetos do Outlook.
_OL_MAIL_ITEM: Final[int] = 0  # olMailItem
_OL_FOLDER_INBOX: Final[int] = 6  # olFolderInbox
_OL_MAIL_CLASS: Final[int] = 43  # olMail
_OL_TO: Final[int] = 1  # olTo
_OL_CC: Final[int] = 2  # olCC
_OL_FORMAT_HTML: Final[int] = 2  # olFormatHTML
_OL_EXCHANGE_TYPE: Final[str] = "EX"
_OL_INBOX_NAMES: Final[frozenset[str]] = frozenset({"inbox", "caixa de entrada"})
_PR_SMTP_ADDRESS: Final[str] = "http://schemas.microsoft.com/mapi/proptag/0x39FE001E"


@dataclass(slots=True)
class EmailMessageData:
    """Mensagem a enviar.

    Attributes:
        subject: Assunto.
        body: Corpo em texto ou HTML.
        to: Destinatários (ao menos um).
        cc: Destinatários em cópia.
        bcc: Destinatários em cópia oculta (não aparecem nos cabeçalhos).
        html: Indica que `body` é HTML.
        attachments: Arquivos anexados.
        from_addr: Remetente; o padrão depende do cliente.
        reply_to: Endereço de resposta.
    """

    subject: str
    body: str
    to: Sequence[str]
    cc: Sequence[str] = ()
    bcc: Sequence[str] = ()
    html: bool = False
    attachments: Sequence[Path] = ()
    from_addr: str | None = None
    reply_to: str | None = None


@dataclass(slots=True)
class EmailAttachment:
    """Anexo de um e-mail recebido; o conteúdo não aparece em `repr()`."""

    filename: str
    content_type: str
    size: int
    payload: bytes = field(repr=False)


@dataclass(slots=True)
class ReceivedEmail:
    """E-mail recebido, com a mesma estrutura no IMAP e no Outlook.

    Attributes:
        uid: UID IMAP ou `EntryID` do Outlook.
        subject: Assunto decodificado.
        from_addr: Endereço do remetente.
        to: Endereços dos destinatários.
        cc: Endereços em cópia.
        date: Data de envio (IMAP) ou de recebimento (Outlook).
        body_text: Corpo em texto puro, ou `""`.
        body_html: Corpo em HTML, ou `""`.
        attachments: Anexos com conteúdo.
    """

    uid: str
    subject: str
    from_addr: str
    to: list[str]
    cc: list[str]
    date: datetime | None
    body_text: str
    body_html: str
    attachments: list[EmailAttachment]


class EmailSender(Protocol):
    """Cliente capaz de enviar e-mails (Strategy)."""

    def send(self, message: EmailMessageData) -> None:
        """Envia a mensagem."""
        ...


class EmailReader(Protocol):
    """Cliente capaz de ler e-mails (Strategy)."""

    def search(
        self,
        *,
        folder: str = "INBOX",
        unseen_only: bool = True,
        since: date | None = None,
        from_addr: str | None = None,
        subject_contains: str | None = None,
        limit: int | None = 50,
    ) -> list[ReceivedEmail]:
        """Busca e-mails, do mais recente para o mais antigo."""
        ...

    def save_attachments(
        self, email: ReceivedEmail, dest_dir: PathLike, *, pattern: str = "*"
    ) -> list[Path]:
        """Grava os anexos que casam com `pattern`."""
        ...

    def mark_as_read(self, email: ReceivedEmail) -> None:
        """Marca o e-mail como lido."""
        ...


class SMTPEmailClient:
    """Envio de e-mails via SMTP com STARTTLS, SSL ou, explicitamente, sem TLS.

    Cada `send` abre uma conexão, autentica (se houver `username`), envia e encerra.

    Example:
        >>> smtp = SMTPEmailClient("smtp.empresa.com", username="robo@empresa.com", password="***")
        >>> smtp.send(EmailMessageData(subject="Resumo", body="ok", to=["fin@empresa.com"]))
    """

    def __init__(
        self,
        host: str,
        port: int = 587,
        *,
        username: str | None = None,
        password: str | SecretStr | None = None,
        security: Literal["starttls", "ssl", "none"] = "starttls",
        timeout: float = 30.0,
        default_from: str | None = None,
        max_attachments_mb: float = 25.0,
    ) -> None:
        """Configura o cliente; nenhuma conexão é aberta aqui.

        Args:
            host: Servidor SMTP.
            port: Porta (587 para STARTTLS, 465 para SSL).
            username: Usuário para `login`; sem usuário, não há autenticação.
            password: Senha, registrada para mascaramento.
            security: `"starttls"`, `"ssl"` ou `"none"` (sem TLS, registra `WARNING`).
            timeout: Tempo limite de rede em segundos.
            default_from: Remetente quando a mensagem não informa `from_addr`.
            max_attachments_mb: Soma máxima dos anexos, em MB.

        Raises:
            ValueError: Se `security`, `timeout` ou `max_attachments_mb` forem inválidos.
        """
        if security not in ("starttls", "ssl", "none"):
            raise ValueError(
                f"security deve ser 'starttls', 'ssl' ou 'none' (recebido: {security!r})"
            )
        _check_positive(timeout=timeout, max_attachments_mb=max_attachments_mb)
        self._host = host
        self._port = port
        self._username = username
        self._password = _as_secret(password)
        self._security = security
        self._timeout = timeout
        self._default_from = default_from
        self._max_attachments_mb = max_attachments_mb

    def send(self, message: EmailMessageData) -> None:
        """Valida, monta e envia a mensagem.

        Remetente: `from_addr`, depois `default_from`, depois `username`. Os destinatários em
        `bcc` vão apenas no envelope SMTP.

        Args:
            message: Mensagem a enviar.

        Raises:
            EmailError: Se a validação falhar (antes de conectar) ou o envio falhar.
        """
        sender = message.from_addr or self._default_from or self._username
        if not sender:
            raise EmailError("Remetente não definido: informe from_addr, default_from ou username")
        attachments = _validate_message(message, self._max_attachments_mb, sender=sender)
        mime = _build_mime(message, sender, attachments)
        recipients = _all_recipients(message)
        try:
            with self._open() as smtp:
                if self._username:
                    smtp.login(self._username, _reveal(self._password))
                smtp.send_message(mime, from_addr=sender, to_addrs=recipients)
        except _SMTP_ERRORS as exc:
            raise EmailError(
                "Falha ao enviar e-mail via SMTP",
                details={"host": self._host, "porta": self._port, "erro": _describe(exc)},
            ) from exc
        _log_sent(message, len(recipients), len(attachments))

    def _open(self) -> smtplib.SMTP:
        if self._security == "ssl":
            return smtplib.SMTP_SSL(
                self._host, self._port, timeout=self._timeout, context=ssl.create_default_context()
            )
        smtp = smtplib.SMTP(self._host, self._port, timeout=self._timeout)
        if self._security == "none":
            get_logger().warning(
                "Conexão SMTP sem TLS | host={} | porta={}", self._host, self._port
            )
            return smtp
        try:
            smtp.starttls(context=ssl.create_default_context())
        except BaseException:
            smtp.close()
            raise
        return smtp


class IMAPEmailClient:
    """Leitura de e-mails via IMAP sobre SSL, sem marcar como lido na busca.

    `mark_as_read` e `move` atuam na pasta em que o e-mail foi encontrado por `search`.

    Example:
        >>> with IMAPEmailClient("imap.empresa.com", username="robo", password="***") as caixa:
        ...     for msg in caixa.search(subject_contains="Boleto"):
        ...         caixa.save_attachments(msg, "data/input", pattern="*.pdf")
        ...         caixa.mark_as_read(msg)
    """

    def __init__(
        self,
        host: str,
        port: int = 993,
        *,
        username: str,
        password: str | SecretStr,
        timeout: float = 30.0,
    ) -> None:
        """Configura o cliente; a conexão é aberta por `connect` ou no primeiro uso.

        Args:
            host: Servidor IMAP.
            port: Porta SSL.
            username: Usuário.
            password: Senha, registrada para mascaramento.
            timeout: Tempo limite de rede em segundos.

        Raises:
            ValueError: Se `timeout` não for positivo.
        """
        _check_positive(timeout=timeout)
        self._host = host
        self._port = port
        self._username = username
        self._password = cast(SecretStr, _as_secret(password))
        self._timeout = timeout
        self._imap: imaplib.IMAP4_SSL | None = None
        self._selected: str | None = None
        self._capabilities: frozenset[str] | None = None
        # id(email) → (uid, pasta) dos e-mails retornados por search nesta conexão.
        self._origins: dict[int, tuple[str, str]] = {}

    def connect(self) -> Self:
        """Conecta com `IMAP4_SSL` e autentica; sem efeito se já estiver conectado.

        Returns:
            A própria instância.

        Raises:
            EmailError: Se a conexão ou a autenticação falharem.
        """
        if self._imap is not None:
            return self
        try:
            imap = imaplib.IMAP4_SSL(
                self._host,
                self._port,
                ssl_context=ssl.create_default_context(),
                timeout=self._timeout,
            )
        except _IMAP_ERRORS as exc:
            raise self._error("Falha ao conectar ao servidor IMAP", exc) from exc
        try:
            imap.login(self._username, _reveal(self._password))
        except _IMAP_ERRORS as exc:
            _quietly(imap.shutdown)
            raise self._error("Falha na autenticação IMAP", exc) from exc
        self._imap = imap
        get_logger().info("Conexão IMAP aberta | host={} | usuario={}", self._host, self._username)
        return self

    def close(self) -> None:
        """Encerra a sessão (`LOGOUT`); sem efeito se não estiver conectado."""
        imap, self._imap = self._imap, None
        self._selected = None
        self._capabilities = None
        self._origins.clear()
        if imap is not None:
            _quietly(imap.logout)

    def __enter__(self) -> Self:
        """Conecta e retorna a instância."""
        return self.connect()

    def __exit__(self, *exc_info: object) -> None:
        """Encerra a sessão, inclusive após exceção."""
        self.close()

    def search(
        self,
        *,
        folder: str = "INBOX",
        unseen_only: bool = True,
        since: date | None = None,
        from_addr: str | None = None,
        subject_contains: str | None = None,
        limit: int | None = 50,
    ) -> list[ReceivedEmail]:
        """Busca e-mails na pasta, do mais recente (maior UID) para o mais antigo.

        O critério IMAP usa `UNSEEN`, `SINCE dd-Mon-yyyy` e `FROM`; o filtro de assunto é
        aplicado no cliente, sem diferenciar maiúsculas. As mensagens são obtidas com
        `BODY.PEEK[]`, que não as marca como lidas.

        Args:
            folder: Pasta IMAP.
            unseen_only: Somente não lidos.
            since: Data mínima (critério `SINCE`, independente de locale).
            from_addr: Trecho do remetente.
            subject_contains: Trecho do assunto.
            limit: Quantidade máxima; `None` retorna todos.

        Returns:
            E-mails encontrados, com anexos.

        Raises:
            ValueError: Se `limit` for menor que 1.
            EmailError: Se um comando IMAP falhar.
        """
        _check_limit(limit)
        imap = self._select(folder)
        criteria = _imap_criteria(unseen_only=unseen_only, since=since, from_addr=from_addr)
        data = self._call("SEARCH", lambda: imap.uid("SEARCH", criteria))
        uids = sorted({int(uid) for uid in _response_text(data).split()}, reverse=True)
        found: list[ReceivedEmail] = []
        for uid in uids:
            received = self._fetch(imap, str(uid))
            if received is None or not _contains(received.subject, subject_contains):
                continue
            self._origins[id(received)] = (received.uid, folder)
            found.append(received)
            if limit is not None and len(found) >= limit:
                break
        get_logger().info(
            "E-mails encontrados | pasta={} | criterio={} | total={}", folder, criteria, len(found)
        )
        return found

    def save_attachments(
        self, email: ReceivedEmail, dest_dir: PathLike, *, pattern: str = "*"
    ) -> list[Path]:
        """Grava os anexos que casam com `pattern` (`fnmatch`, sem diferenciar maiúsculas).

        Args:
            email: E-mail obtido por `search`.
            dest_dir: Pasta de destino (criada se necessário).
            pattern: Padrão do nome do anexo (ex.: `"*.pdf"`).

        Returns:
            Caminhos absolutos gravados; nomes repetidos recebem sufixo `_1`, `_2`.

        Raises:
            FileOperationError: Se a gravação falhar.
        """
        return _save_attachments(email, dest_dir, pattern)

    def mark_as_read(self, email: ReceivedEmail) -> None:
        """Marca como lido com `UID STORE <uid> +FLAGS (\\Seen)`.

        Args:
            email: E-mail obtido por `search`.

        Raises:
            EmailError: Se o comando falhar ou a pasta do e-mail for desconhecida.
        """
        imap = self._select_origin(email)
        self._call("STORE", lambda: imap.uid("STORE", email.uid, "+FLAGS", "(\\Seen)"))

    def move(self, email: ReceivedEmail, folder: str) -> None:
        """Move o e-mail com `UID MOVE` ou, sem essa capacidade, `UID COPY` + `EXPUNGE`.

        Args:
            email: E-mail obtido por `search`.
            folder: Pasta de destino.

        Raises:
            EmailError: Se algum comando falhar ou a pasta do e-mail for desconhecida.
        """
        imap = self._select_origin(email)
        destination = _imap_mailbox(folder)
        if "MOVE" in self._server_capabilities(imap):
            self._call("MOVE", lambda: imap.uid("MOVE", email.uid, destination))
        else:
            self._call("COPY", lambda: imap.uid("COPY", email.uid, destination))
            self._call("STORE", lambda: imap.uid("STORE", email.uid, "+FLAGS", "(\\Deleted)"))
            self._call("EXPUNGE", imap.expunge)
        self._origins.pop(id(email), None)
        get_logger().info("E-mail movido | uid={} | destino={}", email.uid, folder)

    def _connection(self) -> imaplib.IMAP4_SSL:
        self.connect()
        return cast(imaplib.IMAP4_SSL, self._imap)

    def _select(self, folder: str) -> imaplib.IMAP4_SSL:
        imap = self._connection()
        if self._selected != folder:
            mailbox = _imap_mailbox(folder)
            self._call("SELECT", lambda: imap.select(mailbox, readonly=False))
            self._selected = folder
        return imap

    def _select_origin(self, email: ReceivedEmail) -> imaplib.IMAP4_SSL:
        origin = self._origins.get(id(email))
        folder = origin[1] if origin is not None and origin[0] == email.uid else self._selected
        if folder is None:
            raise EmailError(
                "Pasta do e-mail desconhecida: obtenha o e-mail com search() neste cliente",
                details={"uid": email.uid},
            )
        return self._select(folder)

    def _server_capabilities(self, imap: imaplib.IMAP4_SSL) -> frozenset[str]:
        if self._capabilities is None:
            data = self._call("CAPABILITY", imap.capability)
            self._capabilities = frozenset(_response_text(data).upper().split())
        return self._capabilities

    def _fetch(self, imap: imaplib.IMAP4_SSL, uid: str) -> ReceivedEmail | None:
        data = self._call("FETCH", lambda: imap.uid("FETCH", uid, "(BODY.PEEK[])"))
        raw = next((item[1] for item in data if isinstance(item, tuple) and len(item) > 1), None)
        if not isinstance(raw, bytes):
            return None  # mensagem removida entre a busca e a leitura
        return _parse_email(uid, raw)

    def _call(self, command: str, call: Callable[[], tuple[str, Any]]) -> list[Any]:
        try:
            status, data = call()
        except _IMAP_ERRORS as exc:
            raise self._error(f"Falha no comando IMAP {command}", exc) from exc
        if status != "OK":
            raise EmailError(
                f"Servidor IMAP recusou o comando {command}",
                details={"host": self._host, "resposta": _response_text(data)},
            )
        return list(data or [])

    def _error(self, message: str, exc: BaseException) -> EmailError:
        return EmailError(
            message, details={"host": self._host, "porta": self._port, "erro": _describe(exc)}
        )


class OutlookEmailClient:
    """Envio e leitura pelo Outlook desktop via COM (somente Windows, sessão interativa).

    A leitura usa a Caixa de Entrada (`GetDefaultFolder(6)`) ou uma subpasta dela pelo nome
    (`"Boletos"` ou `"Boletos/2026"`), com itens ordenados por `ReceivedTime` decrescente e
    filtrados em Python.
    """

    def __init__(self, *, account: str | None = None, max_scan: int = 500) -> None:
        """Conecta ao Outlook (`Dispatch("Outlook.Application")`).

        Args:
            account: Conta (endereço SMTP ou nome de exibição) usada no envio e na leitura;
                `None` usa a conta padrão do perfil.
            max_scan: Quantidade máxima de itens inspecionados por busca.

        Raises:
            ValueError: Se `max_scan` for menor que 1.
            EmailError: Fora do Windows, sem `pywin32`, sem Outlook ou com falha de COM.
        """
        if max_scan < 1:
            raise ValueError(f"max_scan deve ser maior ou igual a 1 (recebido: {max_scan})")
        self._max_scan = max_scan
        application = _dispatch_outlook()
        try:
            namespace = application.GetNamespace("MAPI")
            selected = _find_outlook_account(namespace, account) if account else None
        except EmailError:
            raise
        except Exception as exc:
            raise EmailError(
                "Falha ao acessar o perfil do Outlook", details={"erro": _describe(exc)}
            ) from exc
        self._application: Any = application
        self._namespace: Any = namespace
        self._account: Any = selected

    def send(self, message: EmailMessageData) -> None:
        """Envia pelo Outlook (`CreateItem(0)`, destinatários unidos por `;`, `Send()`).

        Args:
            message: Mensagem a enviar; `from_addr` vira `SentOnBehalfOfName`.

        Raises:
            EmailError: Se a validação falhar (antes de usar o Outlook) ou o envio falhar.
        """
        attachments = _validate_message(message, _OUTLOOK_MAX_ATTACHMENTS_MB)
        try:
            mail = self._application.CreateItem(_OL_MAIL_ITEM)
            mail.To = ";".join(_address_list(message.to))
            mail.CC = ";".join(_address_list(message.cc))
            mail.BCC = ";".join(_address_list(message.bcc))
            mail.Subject = message.subject
            if message.html:
                mail.HTMLBody = message.body
            else:
                mail.Body = message.body
            self._apply_sender(mail, message)
            for path in attachments:
                mail.Attachments.Add(str(path))
            mail.Send()
        except Exception as exc:
            raise EmailError(
                "Falha ao enviar e-mail pelo Outlook", details={"erro": _describe(exc)}
            ) from exc
        _log_sent(message, len(_all_recipients(message)), len(attachments))

    def search(
        self,
        *,
        folder: str = "Inbox",
        unseen_only: bool = True,
        since: date | None = None,
        from_addr: str | None = None,
        subject_contains: str | None = None,
        limit: int | None = 50,
    ) -> list[ReceivedEmail]:
        """Busca e-mails do mais recente para o mais antigo, inspecionando até `max_scan` itens.

        Args:
            folder: `"Inbox"` (ou `"Caixa de Entrada"`) ou subpasta da Caixa de Entrada.
            unseen_only: Somente não lidos.
            since: Data mínima de recebimento.
            from_addr: Trecho do remetente, sem diferenciar maiúsculas.
            subject_contains: Trecho do assunto, sem diferenciar maiúsculas.
            limit: Quantidade máxima; `None` retorna todos os encontrados.

        Returns:
            E-mails encontrados, com anexos; `uid` é o `EntryID` do item.

        Raises:
            ValueError: Se `limit` for menor que 1.
            EmailError: Se a pasta não existir ou o Outlook falhar.
        """
        _check_limit(limit)
        criteria = _SearchCriteria(unseen_only, since, from_addr, subject_contains)
        try:
            items = self._folder(folder).Items
            items.Sort("[ReceivedTime]", True)
            found = self._collect(_outlook_items(items, self._max_scan), criteria, limit)
        except EmailError:
            raise
        except Exception as exc:
            raise EmailError(
                "Falha ao ler e-mails do Outlook",
                details={"pasta": folder, "erro": _describe(exc)},
            ) from exc
        get_logger().info("E-mails encontrados | pasta={} | total={}", folder, len(found))
        return found

    def save_attachments(
        self, email: ReceivedEmail, dest_dir: PathLike, *, pattern: str = "*"
    ) -> list[Path]:
        """Grava os anexos que casam com `pattern` (mesmas regras do `IMAPEmailClient`).

        Args:
            email: E-mail obtido por `search`.
            dest_dir: Pasta de destino (criada se necessário).
            pattern: Padrão do nome do anexo.

        Returns:
            Caminhos absolutos gravados.

        Raises:
            FileOperationError: Se a gravação falhar.
        """
        return _save_attachments(email, dest_dir, pattern)

    def mark_as_read(self, email: ReceivedEmail) -> None:
        """Marca o item como lido (`UnRead = False` e `Save()`).

        Args:
            email: E-mail obtido por `search`.

        Raises:
            EmailError: Se o item não existir mais ou o Outlook falhar.
        """
        try:
            item = self._namespace.GetItemFromID(email.uid)
            item.UnRead = False
            item.Save()
        except Exception as exc:
            raise EmailError(
                "Falha ao marcar e-mail como lido no Outlook",
                details={"uid": email.uid, "erro": _describe(exc)},
            ) from exc

    def _apply_sender(self, mail: Any, message: EmailMessageData) -> None:  # noqa: ANN401
        if message.from_addr:
            mail.SentOnBehalfOfName = message.from_addr
        if message.reply_to:
            mail.ReplyRecipients.Add(message.reply_to)
        if self._account is not None:
            mail.SendUsingAccount = self._account

    def _folder(self, name: str) -> Any:  # noqa: ANN401 - pasta COM do Outlook
        if self._account is not None:
            current = self._account.DeliveryStore.GetDefaultFolder(_OL_FOLDER_INBOX)
        else:
            current = self._namespace.GetDefaultFolder(_OL_FOLDER_INBOX)
        if name.strip().casefold() in _OL_INBOX_NAMES:
            return current
        for part in name.replace("\\", "/").split("/"):
            if part.strip():
                current = _outlook_child_folder(current, part.strip())
        return current

    def _collect(
        self, items: Iterator[Any], criteria: _SearchCriteria, limit: int | None
    ) -> list[ReceivedEmail]:
        found: list[ReceivedEmail] = []
        for item in items:
            if getattr(item, "Class", None) != _OL_MAIL_CLASS:
                continue
            received = _outlook_datetime(item.ReceivedTime)
            if criteria.since and received and received.date() < criteria.since:
                break  # itens em ordem decrescente: os seguintes são mais antigos
            if criteria.matches(item):
                found.append(_outlook_email(item, received))
                if limit is not None and len(found) >= limit:
                    break
        return found


@dataclass(frozen=True, slots=True)
class _SearchCriteria:
    """Filtros aplicados em Python aos itens do Outlook."""

    unseen_only: bool
    since: date | None
    from_addr: str | None
    subject_contains: str | None

    def matches(self, item: Any) -> bool:  # noqa: ANN401 - item COM do Outlook
        if self.unseen_only and not item.UnRead:
            return False
        if not _contains(str(item.Subject or ""), self.subject_contains):
            return False
        return _contains(_outlook_sender(item), self.from_addr)


def validate_email_address(address: str) -> bool:
    """Indica se o endereço segue `^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$`.

    Args:
        address: Endereço a validar (sem nome de exibição).

    Returns:
        `True` se o endereço for válido.

    Example:
        >>> validate_email_address("fin@empresa.com"), validate_email_address("fin@empresa")
        (True, False)
    """
    return isinstance(address, str) and _EMAIL_ADDRESS.fullmatch(address) is not None


def render_template(template: str | PathLike, context: Mapping[str, Any]) -> str:
    """Renderiza um template `string.Template` (`$variavel`, `${variavel}`, `$$` para `$`).

    Um texto de uma linha terminado em `.html`, `.htm` ou `.txt` que aponte para arquivo
    existente é lido como arquivo; os demais textos são o próprio template.

    Args:
        template: Texto do template ou caminho de arquivo `.html`/`.txt`.
        context: Valores das variáveis.

    Returns:
        Texto renderizado.

    Raises:
        EmailError: Se faltar variável ou o template for inválido.
        FileOperationError: Se o arquivo do template não puder ser lido.

    Example:
        >>> render_template("<p>Processados: $total</p>", {"total": 12})
        '<p>Processados: 12</p>'
    """
    text = read_text(template) if _is_template_file(template) else str(template)
    try:
        return string.Template(text).substitute(context)
    except KeyError as exc:
        name = exc.args[0] if exc.args else ""
        raise EmailError(
            f"Variável ausente no template: {name}", details={"variavel": name}
        ) from exc
    except ValueError as exc:
        raise EmailError("Template de e-mail inválido", details={"erro": str(exc)}) from exc


# --- validação e montagem -------------------------------------------------------------------


def _validate_message(
    message: EmailMessageData, max_attachments_mb: float, *, sender: str | None = None
) -> list[Path]:
    """Valida destinatários, endereços e anexos; retorna os anexos como caminhos absolutos."""
    if not _address_list(message.to):
        raise EmailError("E-mail sem destinatários em 'to'")
    optional = [value for value in (message.from_addr, message.reply_to, sender) if value]
    invalid = [
        address
        for address in [*_all_recipients(message), *optional]
        if not validate_email_address(address)
    ]
    if invalid:
        raise EmailError(
            f"Endereços de e-mail inválidos: {', '.join(invalid)}", details={"enderecos": invalid}
        )
    return _validate_attachments(message.attachments, max_attachments_mb)


def _validate_attachments(attachments: Sequence[PathLike], max_mb: float) -> list[Path]:
    paths = [Path(item) for item in attachments]
    missing = [str(path.resolve()) for path in paths if not path.is_file()]
    if missing:
        raise EmailError("Anexos não encontrados", details={"arquivos": missing})
    total = sum(path.stat().st_size for path in paths)
    if total > max_mb * _BYTES_PER_MB:
        raise EmailError(
            "Anexos excedem o limite de tamanho",
            details={"total_mb": round(total / _BYTES_PER_MB, 2), "limite_mb": max_mb},
        )
    return [path.resolve() for path in paths]


def _build_mime(message: EmailMessageData, sender: str, attachments: list[Path]) -> EmailMessage:
    mime = EmailMessage(policy=policy.default)
    try:
        mime["Subject"] = message.subject
        mime["From"] = sender
        mime["To"] = ", ".join(_address_list(message.to))
        if cc := _address_list(message.cc):
            mime["Cc"] = ", ".join(cc)
        if message.reply_to:
            mime["Reply-To"] = message.reply_to
        mime["Date"] = formatdate(localtime=True)
        mime["Message-ID"] = make_msgid(domain=sender.rpartition("@")[2])
        mime.set_content(message.body, subtype="html" if message.html else "plain")
        for path in attachments:
            maintype, subtype = _guess_content_type(path.name).split("/", 1)
            mime.add_attachment(
                path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name
            )
    except OSError as exc:
        raise EmailError("Falha ao ler anexo", details={"erro": _describe(exc)}) from exc
    except ValueError as exc:  # cabeçalho com quebra de linha, entre outros
        raise EmailError("Mensagem de e-mail inválida", details={"erro": _describe(exc)}) from exc
    return mime


def _address_list(value: Sequence[str] | str) -> list[str]:
    return [value] if isinstance(value, str) else list(value)


def _all_recipients(message: EmailMessageData) -> list[str]:
    return [
        *_address_list(message.to),
        *_address_list(message.cc),
        *_address_list(message.bcc),
    ]


def _guess_content_type(filename: str) -> str:
    content_type, encoding = mimetypes.guess_type(filename)
    if content_type is None or encoding is not None:
        return _DEFAULT_CONTENT_TYPE
    return content_type


def _log_sent(message: EmailMessageData, recipients: int, attachments: int) -> None:
    get_logger().info(
        "E-mail enviado | assunto={} | destinatarios={} | anexos={}",
        message.subject,
        recipients,
        attachments,
    )


# --- leitura ----------------------------------------------------------------------------------


def _parse_email(uid: str, raw: bytes) -> ReceivedEmail:
    message = email.message_from_bytes(raw, policy=policy.default)
    return ReceivedEmail(
        uid=uid,
        subject=str(message.get("Subject", "")),
        from_addr=next(iter(_header_addresses(message, "From")), ""),
        to=_header_addresses(message, "To"),
        cc=_header_addresses(message, "Cc"),
        date=getattr(message.get("Date"), "datetime", None),
        body_text=_body(message, "plain"),
        body_html=_body(message, "html"),
        attachments=_attachments(message),
    )


def _header_addresses(message: EmailMessage, name: str) -> list[str]:
    header = message.get(name)
    addresses = getattr(header, "addresses", ())
    return [address.addr_spec for address in addresses if address.addr_spec]


def _body(message: EmailMessage, subtype: str) -> str:
    part = message.get_body(preferencelist=(subtype,))
    if part is None:
        return ""
    try:
        return str(part.get_content())
    except (LookupError, ValueError):  # charset desconhecido ou conteúdo malformado
        payload = part.get_payload(decode=True)
        return payload.decode("utf-8", "replace") if isinstance(payload, bytes) else ""


def _attachments(message: EmailMessage) -> list[EmailAttachment]:
    """Partes com nome de arquivo ou `Content-Disposition: attachment`, em qualquer nível."""
    found: list[EmailAttachment] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        if filename is None and part.get_content_disposition() != "attachment":
            continue
        payload = part.get_payload(decode=True)
        data = payload if isinstance(payload, bytes) else b""
        content_type = part.get_content_type()
        if not filename:
            extension = mimetypes.guess_extension(content_type) or ".bin"
            filename = f"anexo_{len(found) + 1}{extension}"
        found.append(EmailAttachment(filename, content_type, len(data), data))
    return found


def _save_attachments(email: ReceivedEmail, dest_dir: PathLike, pattern: str) -> list[Path]:
    target = Path(dest_dir)
    wanted = pattern.casefold()
    saved: list[Path] = []
    try:
        target.mkdir(parents=True, exist_ok=True)
        for attachment in email.attachments:
            if fnmatch.fnmatchcase(attachment.filename.casefold(), wanted):
                saved.append(_write_unique(target / safe_filename(attachment.filename), attachment))
    except OSError as exc:
        raise FileOperationError(
            "Falha ao gravar anexos",
            details={"pasta": str(target.resolve()), "erro": _describe(exc)},
        ) from exc
    get_logger().info(
        "Anexos salvos | uid={} | pasta={} | total={}", email.uid, target.resolve(), len(saved)
    )
    return saved


def _write_unique(path: Path, attachment: EmailAttachment) -> Path:
    """Grava sem sobrescrever: nome existente recebe sufixo `_1`, `_2`..."""
    candidate = path
    counter = 0
    while True:
        try:
            with candidate.open("xb") as handle:
                handle.write(attachment.payload)
            return candidate.resolve()
        except FileExistsError:
            counter += 1
            candidate = path.with_name(f"{path.stem}_{counter}{path.suffix}")


def _contains(text: str, fragment: str | None) -> bool:
    return not fragment or fragment.casefold() in text.casefold()


def _check_limit(limit: int | None) -> None:
    if limit is not None and limit < 1:
        raise ValueError(f"limit deve ser maior ou igual a 1 ou None (recebido: {limit})")


def _check_positive(**values: float) -> None:
    for name, value in values.items():
        if value <= 0:
            raise ValueError(f"{name} deve ser maior que zero (recebido: {value})")


# --- IMAP -----------------------------------------------------------------------------------


def _imap_criteria(*, unseen_only: bool, since: date | None, from_addr: str | None) -> str:
    """Critério de `UID SEARCH`; datas com meses de `IMAP_MONTHS` (independe de locale)."""
    parts: list[str] = []
    if unseen_only:
        parts.append("UNSEEN")
    if since is not None:
        parts.append(f"SINCE {since.day:02d}-{IMAP_MONTHS[since.month - 1]}-{since.year:04d}")
    if from_addr:
        parts.append(f"FROM {_imap_quote(from_addr)}")
    return " ".join(parts) or "ALL"


def _imap_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _imap_mailbox(name: str) -> str:
    """Nome de pasta em UTF-7 modificado (RFC 3501), entre aspas quando necessário."""
    encoded = _imap_utf7(name)
    return encoded if _IMAP_ATOM.fullmatch(encoded) else _imap_quote(encoded)


def _imap_utf7(name: str) -> str:
    result: list[str] = []
    pending: list[str] = []

    def flush() -> None:
        if pending:
            raw = "".join(pending).encode("utf-16-be")
            result.append(
                "&" + base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",") + "-"
            )
            pending.clear()

    for char in name:
        if 0x20 <= ord(char) <= 0x7E:
            flush()
            result.append("&-" if char == "&" else char)
        else:
            pending.append(char)
    flush()
    return "".join(result)


def _response_text(data: Sequence[Any] | None) -> str:
    parts: list[str] = []
    for item in data or ():
        if isinstance(item, bytes):
            parts.append(item.decode("utf-8", "replace"))
        elif isinstance(item, str):
            parts.append(item)
    return " ".join(parts)


# --- Outlook --------------------------------------------------------------------------------


def _is_windows() -> bool:
    return sys.platform == "win32"


def _dispatch_outlook() -> Any:  # noqa: ANN401 - objeto COM Outlook.Application
    if not _is_windows():
        raise EmailError("O Outlook só está disponível no Windows (pywin32 e Outlook instalados)")
    try:
        import win32com.client
    except ImportError as exc:
        raise EmailError("O Outlook requer o pacote pywin32") from exc
    try:
        return win32com.client.Dispatch("Outlook.Application")
    except Exception as exc:  # pywintypes.com_error não herda de OSError
        raise EmailError(
            "Outlook indisponível: verifique se está instalado e com perfil configurado na "
            "sessão interativa atual",
            details={"erro": _describe(exc)},
        ) from exc


def _find_outlook_account(namespace: Any, name: str) -> Any:  # noqa: ANN401 - objetos COM
    wanted = name.casefold()
    names: list[str] = []
    for account in namespace.Accounts:
        candidates = (str(account.SmtpAddress or ""), str(account.DisplayName or ""))
        if wanted in (candidate.casefold() for candidate in candidates):
            return account
        names.append(candidates[0] or candidates[1])
    raise EmailError(
        f"Conta do Outlook não encontrada: {name}", details={"contas": ", ".join(names)}
    )


def _outlook_child_folder(parent: Any, name: str) -> Any:  # noqa: ANN401 - pastas COM
    wanted = name.casefold()
    names: list[str] = []
    for child in parent.Folders:
        child_name = str(child.Name)
        if child_name.casefold() == wanted:
            return child
        names.append(child_name)
    raise EmailError(
        f"Pasta do Outlook não encontrada: {name}", details={"disponiveis": ", ".join(names)}
    )


def _outlook_items(items: Any, max_scan: int) -> Iterator[Any]:  # noqa: ANN401
    item = items.GetFirst()
    scanned = 0
    while item is not None and scanned < max_scan:
        yield item
        scanned += 1
        item = items.GetNext()


def _outlook_datetime(value: Any) -> datetime | None:  # noqa: ANN401 - pywintypes.datetime
    """Data do COM (hora local rotulada como UTC pelo pywin32) com o fuso local."""
    if value is None:
        return None
    return datetime(
        value.year, value.month, value.day, value.hour, value.minute, value.second
    ).astimezone()


def _outlook_sender(item: Any) -> str:  # noqa: ANN401 - item COM do Outlook
    if item.SenderEmailType == _OL_EXCHANGE_TYPE:
        try:
            user = item.Sender.GetExchangeUser()
            if user is not None and user.PrimarySmtpAddress:
                return str(user.PrimarySmtpAddress)
        except Exception:  # noqa: S110 - remetente sem usuário Exchange resolvível
            pass
    return str(item.SenderEmailAddress or "")


def _outlook_recipients(item: Any, kind: int) -> list[str]:  # noqa: ANN401
    addresses: list[str] = []
    for recipient in item.Recipients:
        if recipient.Type != kind:
            continue
        try:
            address = str(recipient.PropertyAccessor.GetProperty(_PR_SMTP_ADDRESS) or "")
        except Exception:  # propriedade ausente em destinatários sem Exchange
            address = ""
        addresses.append(address or str(recipient.Address or ""))
    return addresses


def _outlook_email(item: Any, received: datetime | None) -> ReceivedEmail:  # noqa: ANN401
    is_html = item.BodyFormat == _OL_FORMAT_HTML
    return ReceivedEmail(
        uid=str(item.EntryID),
        subject=str(item.Subject or ""),
        from_addr=_outlook_sender(item),
        to=_outlook_recipients(item, _OL_TO),
        cc=_outlook_recipients(item, _OL_CC),
        date=received,
        body_text=str(item.Body or ""),
        body_html=str(item.HTMLBody or "") if is_html else "",
        attachments=_outlook_attachments(item),
    )


def _outlook_attachments(item: Any) -> list[EmailAttachment]:  # noqa: ANN401
    count = int(item.Attachments.Count)
    if count == 0:
        return []
    found: list[EmailAttachment] = []
    with tempfile.TemporaryDirectory(prefix="rpa3003_outlook_") as temp_dir:
        for index in range(1, count + 1):
            attachment = item.Attachments.Item(index)
            filename = str(attachment.FileName)
            temp_file = Path(temp_dir) / f"{index}_{safe_filename(filename)}"
            attachment.SaveAsFile(str(temp_file))
            data = temp_file.read_bytes()
            found.append(EmailAttachment(filename, _guess_content_type(filename), len(data), data))
    return found


# --- auxiliares gerais ------------------------------------------------------------------------


def _is_template_file(template: str | PathLike) -> bool:
    if not isinstance(template, str):
        return True
    if "\n" in template or "\r" in template:
        return False
    try:
        path = Path(template)
        return path.suffix.lower() in _TEMPLATE_SUFFIXES and path.is_file()
    except (OSError, ValueError):
        return False


def _as_secret(value: str | SecretStr | None) -> SecretStr | None:
    secret = SecretStr(value) if isinstance(value, str) else value
    if secret is not None:
        register_secret(secret.get_secret_value())
    return secret


def _reveal(secret: SecretStr | None) -> str:
    return "" if secret is None else secret.get_secret_value()


def _quietly(action: Callable[[], object]) -> None:
    """Executa uma ação de encerramento de sessão; o erro original prevalece."""
    with contextlib.suppress(Exception):
        action()


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {' '.join(str(exc).split())}"
