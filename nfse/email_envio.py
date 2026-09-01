"""
Envio automático da NFS-e ao cliente por e-mail (SMTP).

O e-mail vai para o endereço da coluna `Email_Cliente` da planilha, com o
DANFSe (PDF) e o XML da NFS-e anexados.

GUARDA-CHUVA DE SEGURANÇA
-------------------------
Em `AMBIENTE=homologacao` o envio fica **bloqueado por padrão**: as notas de
teste não têm valor fiscal e mandá-las ao cliente real gera confusão. Para
testar o e-mail, aponte `EMAIL_TESTE_DESTINO` no .env para a sua própria caixa
— todos os envios são redirecionados para lá.
"""

from __future__ import annotations

import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path


class ErroEmail(RuntimeError):
    pass


@dataclass
class ConfiguracaoEmail:
    """Credenciais SMTP. Todas vêm do .env — nada fica hardcoded.

    >>> ONDE COLOCAR AS CREDENCIAIS: seção "E-mail" do .env.
    Gmail/Google Workspace exigem uma "Senha de app" (Conta Google > Segurança >
    Verificação em duas etapas > Senhas de app). A senha normal não funciona.
    """

    ativo: bool
    servidor: str
    porta: int
    usuario: str
    senha: str
    remetente_email: str
    remetente_nome: str
    usar_starttls: bool           # True para porta 587; False para SSL direto (465)
    copia_oculta: list[str]       # BCC — normalmente o seu próprio e-mail/contador
    destino_teste: str | None     # se preenchido, TODO envio vai para cá
    permitir_homologacao: bool
    assunto_modelo: str
    corpo_modelo: str

    @classmethod
    def do_ambiente(cls) -> "ConfiguracaoEmail":
        copia = [e.strip() for e in os.getenv("EMAIL_BCC", "").split(",") if e.strip()]
        return cls(
            ativo=os.getenv("EMAIL_ENVIAR", "false").strip().lower() in ("1", "true", "sim"),
            servidor=os.getenv("EMAIL_SMTP_SERVIDOR", "smtp.gmail.com").strip(),
            porta=int(os.getenv("EMAIL_SMTP_PORTA", "587")),
            usuario=os.getenv("EMAIL_SMTP_USUARIO", "").strip(),
            senha=os.getenv("EMAIL_SMTP_SENHA", ""),
            remetente_email=os.getenv("EMAIL_REMETENTE", "").strip(),
            remetente_nome=os.getenv("EMAIL_REMETENTE_NOME", "").strip(),
            usar_starttls=os.getenv("EMAIL_STARTTLS", "true").strip().lower() in ("1", "true", "sim"),
            copia_oculta=copia,
            destino_teste=os.getenv("EMAIL_TESTE_DESTINO", "").strip() or None,
            permitir_homologacao=os.getenv("EMAIL_PERMITIR_HOMOLOGACAO", "false").strip().lower()
            in ("1", "true", "sim"),
            assunto_modelo=os.getenv(
                "EMAIL_ASSUNTO",
                "NFS-e {chave_curta} - {prestador} - {competencia}",
            ),
            corpo_modelo=os.getenv("EMAIL_CORPO", "").strip() or CORPO_PADRAO,
        )

    def validar(self) -> None:
        faltando = [
            nome
            for nome, valor in (
                ("EMAIL_SMTP_SERVIDOR", self.servidor),
                ("EMAIL_SMTP_USUARIO", self.usuario),
                ("EMAIL_SMTP_SENHA", self.senha),
                ("EMAIL_REMETENTE", self.remetente_email),
            )
            if not valor
        ]
        if faltando:
            raise ErroEmail(
                "EMAIL_ENVIAR=true, mas faltam variáveis no .env: " + ", ".join(faltando)
            )


CORPO_PADRAO = """Olá, {tomador}!

Segue em anexo a Nota Fiscal de Serviço Eletrônica (NFS-e) referente ao serviço prestado.

  Competência .... {competencia}
  Descrição ...... {descricao}
  Valor .......... R$ {valor}
  Chave de acesso  {chave}

Anexos: DANFSe em PDF (para conferência e arquivo) e o XML da nota (para a escrituração do seu contador).

Qualquer dúvida, é só responder este e-mail.

Atenciosamente,
{prestador}
"""


def enviar_nfse(
    config_email: ConfiguracaoEmail,
    ambiente: str,
    destinatario: str,
    dados: dict[str, str],
    anexos: dict[str, str],
) -> str:
    """Envia a nota ao cliente. Devolve uma frase descrevendo o que aconteceu.

    `anexos` é o dicionário retornado por `armazenamento.salvar_nota`.
    Levanta ErroEmail em falha de SMTP — o chamador NÃO deve tratar isso como
    falha de emissão: a nota já está autorizada na Sefin.
    """
    if not config_email.ativo:
        return "envio de e-mail desativado (EMAIL_ENVIAR=false)"

    if ambiente != "producao" and not config_email.permitir_homologacao:
        return (
            "e-mail não enviado: ambiente de homologação. "
            "Defina EMAIL_PERMITIR_HOMOLOGACAO=true e EMAIL_TESTE_DESTINO para testar."
        )

    if not destinatario:
        return "e-mail não enviado: coluna Email_Cliente vazia nesta linha"

    config_email.validar()

    # Redirecionamento de teste: protege o cliente real durante os ensaios.
    destino_real = config_email.destino_teste or destinatario
    redirecionado = config_email.destino_teste is not None

    mensagem = _montar_mensagem(config_email, destino_real, dados, anexos)
    _entregar(config_email, mensagem, destino_real)

    if redirecionado:
        return f"e-mail redirecionado para {destino_real} (EMAIL_TESTE_DESTINO ativo)"
    return f"e-mail enviado para {destino_real}"


def _montar_mensagem(
    config_email: ConfiguracaoEmail,
    destinatario: str,
    dados: dict[str, str],
    anexos: dict[str, str],
) -> EmailMessage:
    mensagem = EmailMessage()
    mensagem["From"] = formataddr((config_email.remetente_nome or None, config_email.remetente_email))
    mensagem["To"] = destinatario
    if config_email.copia_oculta:
        mensagem["Bcc"] = ", ".join(config_email.copia_oculta)
    mensagem["Subject"] = config_email.assunto_modelo.format(**dados)
    mensagem["Message-ID"] = make_msgid()
    mensagem.set_content(config_email.corpo_modelo.format(**dados))

    # Anexa o PDF (DANFSe) e o XML da NFS-e, quando existirem.
    for chave, tipo_mime in (("pdf", ("application", "pdf")), ("xml_nfse", ("application", "xml"))):
        caminho_texto = anexos.get(chave)
        if not caminho_texto:
            continue
        caminho = Path(caminho_texto)
        if not caminho.exists():
            continue
        mensagem.add_attachment(
            caminho.read_bytes(),
            maintype=tipo_mime[0],
            subtype=tipo_mime[1],
            filename=caminho.name,
        )
    return mensagem


def _entregar(config_email: ConfiguracaoEmail, mensagem: EmailMessage, destino: str) -> None:
    contexto = ssl.create_default_context()
    try:
        if config_email.usar_starttls:
            with smtplib.SMTP(config_email.servidor, config_email.porta, timeout=30) as smtp:
                smtp.ehlo()
                smtp.starttls(context=contexto)
                smtp.login(config_email.usuario, config_email.senha)
                smtp.send_message(mensagem)
        else:
            with smtplib.SMTP_SSL(
                config_email.servidor, config_email.porta, context=contexto, timeout=30
            ) as smtp:
                smtp.login(config_email.usuario, config_email.senha)
                smtp.send_message(mensagem)
    except smtplib.SMTPAuthenticationError as exc:
        raise ErroEmail(
            "Autenticação SMTP recusada. No Gmail/Workspace use uma Senha de app, "
            f"não a senha da conta. ({exc.smtp_code})"
        ) from exc
    except smtplib.SMTPException as exc:
        raise ErroEmail(f"Falha SMTP ao enviar para {destino}: {exc}") from exc
    except OSError as exc:
        raise ErroEmail(f"Falha de rede ao conectar em {config_email.servidor}: {exc}") from exc
