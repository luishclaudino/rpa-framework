"""Integração com servidores SMTP e IMAP reais (RF-044, RNF-007, RNF-009, seção 8.16).

Requer `RPA3003_TEST_SMTP_HOST`, `RPA3003_TEST_IMAP_HOST`, `RPA3003_TEST_EMAIL_USER`,
`RPA3003_TEST_EMAIL_PASSWORD` e `RPA3003_TEST_EMAIL_TO` (seção 12.4); sem elas, os testes são
pulados. Execute com `pytest -m integration`.

Usa as portas padrão (SMTP 587 com STARTTLS, IMAP 993 com SSL) e verificação de certificado.
O endereço `RPA3003_TEST_EMAIL_TO` deve ser entregue na caixa IMAP de
`RPA3003_TEST_EMAIL_USER` (por exemplo, o próprio endereço do usuário), para que a mensagem
enviada seja encontrada na leitura.
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pytest

from rpa_3003.utils.email_utils import (
    EmailMessageData,
    IMAPEmailClient,
    ReceivedEmail,
    SMTPEmailClient,
)

pytestmark = pytest.mark.integration

VARIABLES = (
    "RPA3003_TEST_SMTP_HOST",
    "RPA3003_TEST_IMAP_HOST",
    "RPA3003_TEST_EMAIL_USER",
    "RPA3003_TEST_EMAIL_PASSWORD",
    "RPA3003_TEST_EMAIL_TO",
)
DELIVERY_TIMEOUT_SECONDS = 60.0
POLL_INTERVAL_SECONDS = 5.0


@dataclass(frozen=True)
class EmailEnv:
    smtp_host: str
    imap_host: str
    user: str
    password: str
    to: str


@pytest.fixture
def email_env() -> EmailEnv:
    """Parâmetros lidos das variáveis RPA3003_TEST_*; pula o teste se faltar alguma."""
    values = [os.environ.get(name, "").strip() for name in VARIABLES]
    missing = [name for name, value in zip(VARIABLES, values, strict=True) if not value]
    if missing:
        pytest.skip(f"variáveis ausentes: {', '.join(missing)}")
    return EmailEnv(*values)


def _wait_for(mailbox: IMAPEmailClient, token: str) -> ReceivedEmail:
    deadline = time.monotonic() + DELIVERY_TIMEOUT_SECONDS
    while True:
        found = mailbox.search(
            unseen_only=False, since=date.today() - timedelta(days=1), subject_contains=token
        )
        if found:
            return found[0]
        if time.monotonic() >= deadline:
            pytest.fail(f"mensagem {token} não chegou em {DELIVERY_TIMEOUT_SECONDS:.0f}s")
        time.sleep(POLL_INTERVAL_SECONDS)


def test_smtp_send_then_imap_read_attachment_and_mark(email_env: EmailEnv, tmp_path: Path) -> None:
    """RF-044: envio SMTP com anexo e leitura IMAP com download do anexo e marcação de lido."""
    token = uuid.uuid4().hex[:12]
    subject = f"rpa_3003 teste de integração {token}"
    attachment = tmp_path / f"relatório_{token}.txt"
    attachment.write_text("Conteúdo com acentuação: ação, conciliação.", encoding="utf-8")
    sender = SMTPEmailClient(
        email_env.smtp_host, username=email_env.user, password=email_env.password
    )
    sender.send(
        EmailMessageData(
            subject=subject,
            body="<p>Mensagem automática de teste do <b>rpa_3003</b>.</p>",
            html=True,
            to=[email_env.to],
            attachments=[attachment],
        )
    )
    with IMAPEmailClient(
        email_env.imap_host, username=email_env.user, password=email_env.password
    ) as mailbox:
        received = _wait_for(mailbox, token)
        assert received.subject == subject
        assert "rpa_3003" in received.body_html
        saved = mailbox.save_attachments(received, tmp_path / "recebidos", pattern="*.TXT")
        assert [path.read_bytes() for path in saved] == [attachment.read_bytes()]
        mailbox.mark_as_read(received)
        unread = mailbox.search(since=date.today() - timedelta(days=1), subject_contains=token)
        assert unread == []
