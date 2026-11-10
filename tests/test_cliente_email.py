"""Testes do cliente HTTP (retorno normalizado) e das travas do envio por e-mail."""

from __future__ import annotations

import base64
import gzip
import json

import pytest

from nfse.cliente import ClienteNFSe
from nfse.email_envio import ConfiguracaoEmail, enviar_nfse


class _RespostaFalsa:
    def __init__(self, status, corpo, conteudo=b""):
        self.status_code = status
        self._corpo = corpo
        self.content = conteudo
        self.text = json.dumps(corpo) if isinstance(corpo, dict) else str(corpo)

    def json(self):
        if isinstance(self._corpo, dict):
            return self._corpo
        raise ValueError("corpo não é JSON")


class _SessaoFalsa:
    def __init__(self, resposta):
        self.resposta = resposta
        self.chamadas = []

    def request(self, metodo, url, **kwargs):
        self.chamadas.append((metodo, url, kwargs))
        return self.resposta


def test_emissao_autorizada_descomprime_o_xml(config):
    xml = b"<NFSe>conteudo</NFSe>"
    corpo = {
        "chaveAcesso": "33045572" + "0" * 42,
        "idDps": "DPS" + "0" * 42,
        "nfseXmlGZipB64": base64.b64encode(gzip.compress(xml)).decode(),
        "alertas": [{"codigo": "A001", "descricao": "Nota emitida fora do prazo."}],
    }
    sessao = _SessaoFalsa(_RespostaFalsa(201, corpo))
    resposta = ClienteNFSe(config, sessao).emitir("pacote-base64")

    assert resposta.autorizada is True
    assert resposta.xml_nfse == xml
    assert resposta.chave_acesso.startswith("33045572")
    assert resposta.mensagens == ["[A001] Nota emitida fora do prazo."]
    # O envelope é sempre {"dpsXmlGZipB64": ...}
    assert sessao.chamadas[0][2]["json"] == {"dpsXmlGZipB64": "pacote-base64"}


def test_rejeicao_traz_o_motivo_do_governo(config):
    corpo = {"erros": [
        {"codigo": "E0123", "descricao": "cTribNac inválido", "complemento": "campo cServ/cTribNac"},
        {"codigo": "E0456", "descricao": "Tomador não localizado"},
    ]}
    resposta = ClienteNFSe(config, _SessaoFalsa(_RespostaFalsa(422, corpo))).emitir("x")

    assert resposta.autorizada is False
    assert resposta.status_http == 422
    assert "cTribNac inválido" in resposta.motivo_erro
    assert "Tomador não localizado" in resposta.motivo_erro


def test_rejeicao_nao_e_reenviada(config):
    """4xx é resposta definitiva: repetir só geraria risco de duplicidade."""
    sessao = _SessaoFalsa(_RespostaFalsa(400, {"erros": [{"descricao": "erro"}]}))
    config.max_tentativas = 3
    ClienteNFSe(config, sessao).emitir("x")
    assert len(sessao.chamadas) == 1


def _config_email(**sobrepor) -> ConfiguracaoEmail:
    base = dict(
        ativo=True, servidor="smtp.exemplo.com", porta=587, usuario="u", senha="s",
        remetente_email="eu@exemplo.com", remetente_nome="Eu", usar_starttls=True,
        copia_oculta=[], destino_teste=None, permitir_homologacao=False,
        assunto_modelo="NFS-e {chave_curta}", corpo_modelo="Olá {tomador}",
    )
    base.update(sobrepor)
    return ConfiguracaoEmail(**base)


DADOS = {
    "tomador": "Cliente Alfa", "prestador": "11222333000181", "competencia": "09/2026",
    "descricao": "Consultoria.", "valor": "4.500,00", "chave": "3304557XYZ", "chave_curta": "557XYZ",
}


def test_homologacao_nao_envia_para_o_cliente_real():
    resultado = enviar_nfse(_config_email(), "homologacao", "cliente@real.com", DADOS, {})
    assert "homologação" in resultado
    assert "NÃO enviado" in resultado


def test_email_desativado_nao_envia():
    resultado = enviar_nfse(_config_email(ativo=False), "producao", "cliente@real.com", DADOS, {})
    assert "desligado" in resultado


def test_sem_destinatario_nao_envia():
    resultado = enviar_nfse(_config_email(), "producao", "", DADOS, {})
    assert "sem e-mail" in resultado


def test_envio_em_producao_anexa_pdf_e_xml(tmp_path, monkeypatch):
    pdf = tmp_path / "nota.pdf"; pdf.write_bytes(b"%PDF-1.4 fake")
    xml = tmp_path / "nota.xml"; xml.write_bytes(b"<NFSe/>")

    enviadas = []
    monkeypatch.setattr("nfse.email_envio._entregar", lambda c, m, d: enviadas.append(m))

    resultado = enviar_nfse(
        _config_email(copia_oculta=["contador@escritorio.com"]),
        "producao", "cliente@real.com", DADOS,
        {"pdf": str(pdf), "xml_nfse": str(xml)},
    )

    assert resultado == "e-mail enviado para cliente@real.com"
    mensagem = enviadas[0]
    assert mensagem["To"] == "cliente@real.com"
    assert mensagem["Bcc"] == "contador@escritorio.com"
    assert mensagem["Subject"] == "NFS-e 557XYZ"
    nomes = [p.get_filename() for p in mensagem.iter_attachments()]
    assert sorted(nomes) == ["nota.pdf", "nota.xml"]


def test_destino_de_teste_protege_o_cliente(monkeypatch):
    enviadas = []
    monkeypatch.setattr("nfse.email_envio._entregar", lambda c, m, d: enviadas.append(m))

    resultado = enviar_nfse(
        _config_email(destino_teste="eu@exemplo.com"),
        "producao", "cliente@real.com", DADOS, {},
    )
    assert "redirecionado para eu@exemplo.com" in resultado
    assert enviadas[0]["To"] == "eu@exemplo.com"


# ---------------------------------------------------------------------------
# Entrega SMTP: porta/segurança e mensagens de erro
# ---------------------------------------------------------------------------
def _config_smtp(porta: int, starttls: bool):
    return ConfiguracaoEmail(
        ativo=True, servidor="smtp.exemplo.com.br", porta=porta, usuario="eu@x.com.br",
        senha="s", remetente_email="eu@x.com.br", remetente_nome="", usar_starttls=starttls,
        copia_oculta=[], destino_teste=None, permitir_homologacao=False,
        assunto_modelo="", corpo_modelo="",
    )


class _SMTPFalso:
    usados: list = []

    def __init__(self, *a, **k):
        _SMTPFalso.usados.append(type(self).__name__)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        pass

    def starttls(self, context=None):
        pass

    def login(self, u, s):
        pass

    def send_message(self, m):
        pass


class SMTP(_SMTPFalso):
    pass


class SMTP_SSL(_SMTPFalso):
    pass


def test_porta_465_usa_ssl_direto_mesmo_com_starttls_marcado(monkeypatch):
    from email.message import EmailMessage

    from nfse import email_envio

    _SMTPFalso.usados = []
    monkeypatch.setattr(email_envio.smtplib, "SMTP", SMTP)
    monkeypatch.setattr(email_envio.smtplib, "SMTP_SSL", SMTP_SSL)
    email_envio._entregar(_config_smtp(465, True), EmailMessage(), "a@b.com")
    email_envio._entregar(_config_smtp(587, True), EmailMessage(), "a@b.com")
    assert _SMTPFalso.usados == ["SMTP_SSL", "SMTP"]


def test_conexao_fechada_vira_explicacao(monkeypatch):
    import smtplib
    from email.message import EmailMessage

    from nfse import email_envio

    class Fecha(SMTP):
        def starttls(self, context=None):
            raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")

    monkeypatch.setattr(email_envio.smtplib, "SMTP", Fecha)
    with pytest.raises(email_envio.ErroEmail) as erro:
        email_envio._entregar(_config_smtp(587, True), EmailMessage(), "a@b.com")
    assert "fechou a conexão ao ativar a criptografia" in str(erro.value)
    assert "porta" in str(erro.value)
