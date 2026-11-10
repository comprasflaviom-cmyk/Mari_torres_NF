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
        return ("e-mail NÃO enviado: o envio automático está desligado "
                "(Configuração → \"Enviar a nota por e-mail assim que for autorizada\"). "
                "Use Histórico → Reenviar e-mail")

    if ambiente != "producao" and not config_email.permitir_homologacao:
        return (
            "e-mail NÃO enviado: nota de teste (homologação) não vai para o cliente"
        )

    if not destinatario:
        return ("e-mail NÃO enviado: cliente sem e-mail (na planilha ou no cadastro). "
                "Cadastre o e-mail e use Histórico → Reenviar e-mail")

    config_email.validar()

    # Redirecionamento de teste: protege o cliente real durante os ensaios.
    destino_real = config_email.destino_teste or destinatario
    redirecionado = config_email.destino_teste is not None

    mensagem = _montar_mensagem(config_email, destino_real, dados, anexos)
    _entregar(config_email, mensagem, destino_real)

    if redirecionado:
        return f"e-mail redirecionado para {destino_real} (EMAIL_TESTE_DESTINO ativo)"
    return f"e-mail enviado para {destino_real}"


def enviar_documento(
    config_email: ConfiguracaoEmail,
    destinatario: str,
    assunto: str,
    corpo: str,
    anexo: Path,
) -> str:
    """Envia um documento avulso (ex.: recibo) com um PDF anexo.

    Não depende do ambiente nem da chave "enviar automaticamente": quem chama
    é um clique explícito. O redirecionamento de teste e a cópia oculta valem.
    """
    if not destinatario:
        raise ErroEmail("Este cliente não tem e-mail cadastrado.")
    config_email.validar()
    destino_real = config_email.destino_teste or destinatario

    mensagem = EmailMessage()
    mensagem["From"] = formataddr((config_email.remetente_nome or None, config_email.remetente_email))
    mensagem["To"] = destino_real
    if config_email.copia_oculta:
        mensagem["Bcc"] = ", ".join(config_email.copia_oculta)
    mensagem["Subject"] = assunto
    mensagem["Message-ID"] = make_msgid()
    mensagem.set_content(corpo)
    mensagem.add_attachment(anexo.read_bytes(), maintype="application", subtype="pdf",
                            filename=anexo.name)
    _entregar(config_email, mensagem, destino_real)

    if config_email.destino_teste:
        return f"e-mail redirecionado para {destino_real} (redirecionamento de teste ligado)"
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


DICA_PORTA = (
    "Confira em Configuração → Envio automático: servidor, porta e a caixa STARTTLS. "
    "Porta 587 = STARTTLS marcado; porta 465 = SSL direto (o app ajusta sozinho)."
)


def usa_ssl_direto(config_email: ConfiguracaoEmail) -> bool:
    """Porta 465 é sempre SSL direto: STARTTLS nela derruba a conexão sem
    explicação ("Connection unexpectedly closed") — o erro mais comum de
    configuração, então o app nem deixa acontecer."""
    return config_email.porta == 465 or not config_email.usar_starttls


def _entregar(config_email: ConfiguracaoEmail, mensagem: EmailMessage, destino: str) -> None:
    contexto = ssl.create_default_context()
    servidor = f"{config_email.servidor}:{config_email.porta}"
    etapa = "conectar ao servidor"
    try:
        if usa_ssl_direto(config_email):
            smtp = smtplib.SMTP_SSL(config_email.servidor, config_email.porta, context=contexto, timeout=30)
        else:
            smtp = smtplib.SMTP(config_email.servidor, config_email.porta, timeout=30)
        with smtp:
            if not usa_ssl_direto(config_email):
                etapa = "ativar a criptografia (STARTTLS)"
                smtp.ehlo()
                smtp.starttls(context=contexto)
            etapa = "entrar com usuário e senha"
            smtp.login(config_email.usuario, config_email.senha)
            etapa = "enviar a mensagem"
            smtp.send_message(mensagem)
    except smtplib.SMTPAuthenticationError as exc:
        raise ErroEmail(
            "O servidor de e-mail recusou o usuário ou a senha. Confira o usuário (o e-mail "
            "completo) e a senha. Gmail/Google Workspace exigem uma \"senha de app\", não a "
            f"senha normal. (código {exc.smtp_code})"
        ) from exc
    except smtplib.SMTPServerDisconnected as exc:
        raise ErroEmail(
            f"O servidor {servidor} fechou a conexão ao {etapa}. Quase sempre é porta ou "
            f"servidor errado. {DICA_PORTA}"
        ) from exc
    except smtplib.SMTPNotSupportedError as exc:
        raise ErroEmail(
            f"O servidor {servidor} não aceita STARTTLS nesta porta. Tente a porta 465. ({exc})"
        ) from exc
    except smtplib.SMTPRecipientsRefused as exc:
        raise ErroEmail(f"O servidor recusou o destinatário {destino}. Confira o e-mail do cliente.") from exc
    except smtplib.SMTPSenderRefused as exc:
        raise ErroEmail(
            "O servidor recusou o remetente. O \"E-mail remetente\" precisa ser o mesmo "
            f"e-mail do usuário SMTP (ou um alias autorizado dele). ({exc.smtp_code})"
        ) from exc
    except smtplib.SMTPException as exc:
        raise ErroEmail(f"O servidor {servidor} recusou ao {etapa}: {exc}") from exc
    except ssl.SSLError as exc:
        raise ErroEmail(
            f"Falha de segurança (SSL) com {servidor} ao {etapa}. {DICA_PORTA} ({exc.reason})"
        ) from exc
    except (TimeoutError, OSError) as exc:
        raise ErroEmail(
            f"Não foi possível {etapa} em {servidor}: o servidor não respondeu. Confira o nome "
            f"do servidor e a porta, e se a internet está funcionando. ({exc})"
        ) from exc
