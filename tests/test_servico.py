"""Testes do Emissor — o miolo compartilhado entre o CLI e a interface gráfica."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from nfse.cliente import RespostaEmissao
from nfse.email_envio import ConfiguracaoEmail
from nfse.estado import ControleEmissao, impressao_da_linha
from nfse.planilha import LinhaFaturamento
from nfse.servico import Emissor, EventoProgresso, OpcoesEmissao

COMPETENCIA = date(2026, 9, 1)


class ClienteFalso:
    """Substitui `ClienteNFSe` sem tocar a rede."""

    def __init__(self, respostas: list[RespostaEmissao]):
        self.respostas = list(respostas)
        self.enviados: list[str] = []
        self.pdfs_pedidos: list[str] = []

    def emitir(self, pacote: str) -> RespostaEmissao:
        self.enviados.append(pacote)
        return self.respostas.pop(0)

    def baixar_danfse(self, chave: str) -> bytes:
        self.pdfs_pedidos.append(chave)
        return b"%PDF-1.4 conteudo"


def _autorizada(chave: str, alertas: list[str] | None = None) -> RespostaEmissao:
    return RespostaEmissao(
        autorizada=True, status_http=201, chave_acesso=chave, id_dps="DPS" + chave,
        xml_nfse=b"<NFSe/>", mensagens=alertas or [],
    )


def _rejeitada(motivo: str) -> RespostaEmissao:
    return RespostaEmissao(autorizada=False, status_http=422, mensagens=[motivo])


@pytest.fixture
def email_desligado() -> ConfiguracaoEmail:
    return ConfiguracaoEmail(
        ativo=False, servidor="", porta=587, usuario="", senha="",
        remetente_email="", remetente_nome="", usar_starttls=True, copia_oculta=[],
        destino_teste=None, permitir_homologacao=False, assunto_modelo="", corpo_modelo="",
    )


def _montar(config, certificado_teste, email_desligado, respostas) -> tuple[Emissor, ClienteFalso]:
    cliente = ClienteFalso(respostas)
    controle = ControleEmissao.carregar(config.diretorio_logs, "homologacao", "1", 1)
    emissor = Emissor(
        config=config, config_email=email_desligado, certificado=certificado_teste,
        cliente=cliente, controle=controle,
    )
    return emissor, cliente


def _outra_linha(numero: int, valor: str) -> LinhaFaturamento:
    return LinhaFaturamento(
        numero_linha=numero, documento_tomador="11222333000181",
        razao_social=f"Cliente {numero}", email="x@y.com.br",
        valor_servico=Decimal(valor), descricao="Consultoria.", extras={},
    )


# ---------------------------------------------------------------------------
def test_nota_autorizada_arquiva_e_registra_numeracao(
    config, linha, certificado_teste, email_desligado
):
    emissor, cliente = _montar(config, certificado_teste, email_desligado, [_autorizada("CHAVE1")])

    registro = emissor.emitir_uma(linha, OpcoesEmissao(competencia=COMPETENCIA))

    assert registro.situacao == "AUTORIZADA"
    assert registro.chave_acesso == "CHAVE1"
    assert registro.numero_dps == "1"
    assert cliente.pdfs_pedidos == ["CHAVE1"]

    # Arquivos gravados na pasta do mês da competência.
    pasta = config.diretorio_notas / "2026" / "09-setembro"
    assert (pasta / "CHAVE1_11222333000181_nfse.xml").exists()
    assert (pasta / "CHAVE1_11222333000181_danfse.pdf").exists()
    assert (pasta / "CHAVE1_11222333000181_dps-assinada.xml").exists()

    # A numeração foi consumida e persistida.
    assert emissor.controle.ultimo_numero == 1
    assert emissor.controle.ja_emitida(impressao_da_linha(linha, "2026-09-01"))


def test_rejeicao_nao_consome_numeracao(config, linha, certificado_teste, email_desligado):
    """A Sefin não registrou nada, então o número continua livre."""
    emissor, _ = _montar(
        config, certificado_teste, email_desligado,
        [_rejeitada("[E0123] cTribNac inválido"), _autorizada("CHAVE2")],
    )
    opcoes = OpcoesEmissao(competencia=COMPETENCIA)

    rejeitado = emissor.emitir_uma(linha, opcoes)
    assert rejeitado.situacao == "REJEITADA"
    assert "cTribNac inválido" in rejeitado.detalhe
    assert emissor.controle.ultimo_numero == 0

    # A próxima nota reaproveita o número 1.
    seguinte = emissor.emitir_uma(_outra_linha(3, "100.00"), opcoes)
    assert seguinte.situacao == "AUTORIZADA"
    assert seguinte.numero_dps == "1"


def test_dry_run_nao_transmite_nem_consome_numeracao(
    config, linha, certificado_teste, email_desligado
):
    emissor, cliente = _montar(config, certificado_teste, email_desligado, [])

    registro = emissor.emitir_uma(linha, OpcoesEmissao(competencia=COMPETENCIA, dry_run=True))

    assert cliente.enviados == [], "dry-run não pode transmitir nada"
    assert registro.situacao == "PULADA"
    assert emissor.controle.ultimo_numero == 0
    assert (config.diretorio_notas / "dry-run" / "dps_simulada_linha002.xml").exists()


def test_linha_ja_emitida_e_pulada(config, linha, certificado_teste, email_desligado):
    emissor, cliente = _montar(
        config, certificado_teste, email_desligado, [_autorizada("CHAVE1")]
    )
    opcoes = OpcoesEmissao(competencia=COMPETENCIA)

    emissor.emitir_uma(linha, opcoes)
    repetida = emissor.emitir_uma(linha, opcoes)

    assert repetida.situacao == "PULADA"
    assert repetida.chave_acesso == "CHAVE1"
    assert len(cliente.enviados) == 1, "não pode transmitir a mesma nota duas vezes"


def test_reemitir_ignora_a_trava(config, linha, certificado_teste, email_desligado):
    emissor, cliente = _montar(
        config, certificado_teste, email_desligado, [_autorizada("CHAVE1"), _autorizada("CHAVE2")]
    )
    emissor.emitir_uma(linha, OpcoesEmissao(competencia=COMPETENCIA))
    forcada = emissor.emitir_uma(linha, OpcoesEmissao(competencia=COMPETENCIA, reemitir=True))

    assert forcada.situacao == "AUTORIZADA"
    assert len(cliente.enviados) == 2


def test_falha_numa_linha_nao_derruba_o_lote(config, certificado_teste, email_desligado):
    """Linha inválida, rejeitada e autorizada convivem no mesmo relatório."""
    emissor, _ = _montar(
        config, certificado_teste, email_desligado, [_rejeitada("erro do governo"), _autorizada("CHAVE9")]
    )
    entrada = [
        (0, None, "CNPJ_Cliente com dígito verificador inválido: 11222333000100"),
        (1, _outra_linha(3, "100.00"), None),
        (2, _outra_linha(4, "200.00"), None),
    ]

    relatorio = emissor.emitir_lote(entrada, OpcoesEmissao(competencia=COMPETENCIA))

    situacoes = [r.situacao for r in relatorio.registros]
    assert situacoes == ["INVALIDA", "REJEITADA", "AUTORIZADA"]
    assert relatorio.contagem("AUTORIZADA") == 1
    assert "dígito verificador" in relatorio.registros[0].detalhe


def test_eventos_de_progresso_alimentam_a_interface(
    config, certificado_teste, email_desligado
):
    """A interface depende destes eventos para a barra de progresso."""
    emissor, _ = _montar(config, certificado_teste, email_desligado, [_autorizada("CHAVE1")])
    eventos: list[EventoProgresso] = []

    emissor.emitir_lote(
        [(0, _outra_linha(2, "100.00"), None)],
        OpcoesEmissao(competencia=COMPETENCIA),
        eventos.append,
    )

    tipos = [e.tipo for e in eventos]
    assert tipos[0] == "inicio_lote"
    assert tipos[-1] == "fim_lote"
    assert "inicio_linha" in tipos and "fim_linha" in tipos

    inicio_lote = eventos[0]
    assert inicio_lote.total == 1

    fim_linha = next(e for e in eventos if e.tipo == "fim_linha")
    assert fim_linha.situacao == "AUTORIZADA"
    assert fim_linha.indice == 1 and fim_linha.total == 1
    assert fim_linha.registro.chave_acesso == "CHAVE1"


def test_selecao_de_linhas_filtra_o_lote(config, certificado_teste, email_desligado):
    emissor, cliente = _montar(config, certificado_teste, email_desligado, [_autorizada("CHAVE5")])
    entrada = [
        (0, _outra_linha(2, "100.00"), None),
        (1, _outra_linha(5, "500.00"), None),
        (2, _outra_linha(7, "700.00"), None),
    ]

    relatorio = emissor.emitir_lote(
        entrada, OpcoesEmissao(competencia=COMPETENCIA, linhas_selecionadas={5})
    )

    assert len(relatorio.registros) == 1
    assert relatorio.registros[0].linha_planilha == 5
    assert len(cliente.enviados) == 1


def test_pdf_indisponivel_nao_invalida_a_nota(
    config, linha, certificado_teste, email_desligado, monkeypatch
):
    """DANFSe é acessório: a nota já está autorizada na Sefin."""
    emissor, cliente = _montar(config, certificado_teste, email_desligado, [_autorizada("CHAVE1")])
    monkeypatch.setattr(
        cliente, "baixar_danfse",
        lambda chave: (_ for _ in ()).throw(RuntimeError("servidor fora do ar")),
    )
    eventos: list[EventoProgresso] = []

    registro = emissor.emitir_uma(linha, OpcoesEmissao(competencia=COMPETENCIA), eventos.append)

    assert registro.situacao == "AUTORIZADA"
    assert any("DANFSe indisponível" in e.mensagem for e in eventos if e.tipo == "aviso")


# ---------------------------------------------------------------------------
# Chave de envio por e-mail vinda do cadastro
# ---------------------------------------------------------------------------
def _email_ligado() -> ConfiguracaoEmail:
    return ConfiguracaoEmail(
        ativo=True, servidor="smtp.exemplo.com", porta=587, usuario="u", senha="s",
        remetente_email="eu@exemplo.com", remetente_nome="Eu", usar_starttls=True,
        copia_oculta=[], destino_teste=None, permitir_homologacao=True,
        assunto_modelo="NFS-e {chave_curta}", corpo_modelo="Olá {tomador}",
    )


def test_cliente_marcado_para_nao_receber_nao_dispara_email(
    config, linha, certificado_teste, monkeypatch
):
    """A nota é emitida e arquivada normalmente — só não sai e-mail."""
    enviados = []
    monkeypatch.setattr("nfse.email_envio._entregar", lambda c, m, d: enviados.append(m))

    linha.enviar_email = False
    emissor, _ = _montar(config, certificado_teste, _email_ligado(), [_autorizada("CHAVE1")])

    registro = emissor.emitir_uma(linha, OpcoesEmissao(competencia=COMPETENCIA))

    assert registro.situacao == "AUTORIZADA"
    assert enviados == [], "cliente marcado para não receber não pode receber e-mail"
    assert "não receber" in registro.email


def test_cliente_que_recebe_dispara_o_email(config, linha, certificado_teste, monkeypatch):
    enviados = []
    monkeypatch.setattr("nfse.email_envio._entregar", lambda c, m, d: enviados.append(m))

    emissor, _ = _montar(config, certificado_teste, _email_ligado(), [_autorizada("CHAVE1")])
    registro = emissor.emitir_uma(linha, OpcoesEmissao(competencia=COMPETENCIA))

    assert len(enviados) == 1
    assert enviados[0]["To"] == linha.email
    assert "e-mail enviado" in registro.email
