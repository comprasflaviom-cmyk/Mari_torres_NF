"""Testes do backup automático e da trava de uma série por computador."""

from __future__ import annotations

import json
from datetime import date

import pytest

from nfse.backup import Espelho, espelhar_nota
from nfse.estado import ConflitoDeMaquina, ControleEmissao, nome_da_maquina
from nfse.servico import OpcoesEmissao


# ---------------------------------------------------------------------------
# Espelho de backup
# ---------------------------------------------------------------------------
def test_espelho_desligado_nao_copia_nada(tmp_path):
    espelho = Espelho(None)
    assert espelho.ativo is False
    assert espelho.copiar([tmp_path / "algo.xml"], tmp_path) == []
    assert espelho.ultimo_backup() is None


def test_espelho_preserva_a_estrutura_de_pastas(tmp_path):
    origem = tmp_path / "notas" / "2026" / "09-setembro"
    origem.mkdir(parents=True)
    arquivo = origem / "CHAVE1_nfse.xml"
    arquivo.write_bytes(b"<NFSe/>")

    espelho = Espelho(tmp_path / "backup")
    copiados = espelho.copiar([arquivo], tmp_path / "notas")

    destino = tmp_path / "backup" / "2026" / "09-setembro" / "CHAVE1_nfse.xml"
    assert copiados == [destino]
    assert destino.read_bytes() == b"<NFSe/>"


def test_arquivo_fora_da_raiz_e_copiado_solto(tmp_path):
    externo = tmp_path / "controle.json"
    externo.write_text("{}", encoding="utf-8")

    espelho = Espelho(tmp_path / "backup")
    copiados = espelho.copiar([externo], tmp_path / "outra-raiz")

    assert copiados == [tmp_path / "backup" / "controle.json"]


def test_arquivo_inexistente_e_ignorado(tmp_path):
    espelho = Espelho(tmp_path / "backup")
    assert espelho.copiar([tmp_path / "sumiu.xml"], tmp_path) == []


def test_marca_do_ultimo_backup(tmp_path):
    espelho = Espelho(tmp_path / "backup")
    assert espelho.ultimo_backup() is None

    espelho.registrar_sucesso(3)
    marca = espelho.ultimo_backup()
    assert marca is not None
    assert json.loads((tmp_path / "backup" / "ultimo-backup.json").read_text())["arquivos"] == 3


def test_marca_corrompida_nao_quebra(tmp_path):
    destino = tmp_path / "backup"
    destino.mkdir()
    (destino / "ultimo-backup.json").write_text("não é json", encoding="utf-8")
    assert Espelho(destino).ultimo_backup() is None


def test_espelhar_nota_leva_o_controle_junto(tmp_path):
    """O controle de numeração é o arquivo cuja perda custa mais caro."""
    notas = tmp_path / "notas" / "2026" / "09-setembro"
    notas.mkdir(parents=True)
    xml = notas / "CHAVE1_nfse.xml"
    xml.write_bytes(b"<NFSe/>")
    pdf = notas / "CHAVE1_danfse.pdf"
    pdf.write_bytes(b"%PDF")

    logs = tmp_path / "logs"
    logs.mkdir()
    controle = logs / "controle_homologacao_serie1.json"
    controle.write_text('{"ultimo_numero": 7}', encoding="utf-8")

    total = espelhar_nota(
        Espelho(tmp_path / "backup"),
        {"xml_nfse": str(xml), "pdf": str(pdf)},
        tmp_path / "notas",
        controle,
    )

    assert total == 3
    assert (tmp_path / "backup" / "2026" / "09-setembro" / "CHAVE1_nfse.xml").exists()
    assert (tmp_path / "backup" / "controle_homologacao_serie1.json").exists()


# ---------------------------------------------------------------------------
# Uma série por computador
# ---------------------------------------------------------------------------
def test_controle_novo_grava_o_nome_da_maquina(tmp_path):
    controle = ControleEmissao.carregar(tmp_path, "homologacao", "1", 1)
    assert controle.maquina == ""

    controle.salvar()
    gravado = json.loads(controle.caminho.read_text(encoding="utf-8"))
    assert gravado["maquina"] == nome_da_maquina()


def test_controle_da_mesma_maquina_nao_conflita(tmp_path):
    ControleEmissao.carregar(tmp_path, "homologacao", "1", 1).salvar()
    recarregado = ControleEmissao.carregar(tmp_path, "homologacao", "1", 1)
    assert recarregado.conflito_de_maquina() is None


def test_controle_de_outra_maquina_conflita(tmp_path):
    controle = ControleEmissao.carregar(tmp_path, "homologacao", "1", 1)
    controle.maquina = "LAPTOP-DA-COLEGA"
    controle.salvar()

    recarregado = ControleEmissao.carregar(tmp_path, "homologacao", "1", 1)
    assert recarregado.conflito_de_maquina() == "LAPTOP-DA-COLEGA"


def test_assumir_maquina_resolve_o_conflito(tmp_path):
    controle = ControleEmissao.carregar(tmp_path, "homologacao", "1", 1)
    controle.maquina = "LAPTOP-DA-COLEGA"
    controle.salvar()

    recarregado = ControleEmissao.carregar(tmp_path, "homologacao", "1", 1)
    recarregado.assumir_maquina()
    recarregado.salvar()

    assert ControleEmissao.carregar(tmp_path, "homologacao", "1", 1).conflito_de_maquina() is None


def test_series_diferentes_nao_se_atrapalham(tmp_path):
    """A saída correta para várias máquinas: uma série para cada."""
    serie1 = ControleEmissao.carregar(tmp_path, "homologacao", "1", 1)
    serie2 = ControleEmissao.carregar(tmp_path, "homologacao", "2", 1)
    assert serie1.caminho != serie2.caminho

    serie1.proximo_numero(); serie1.salvar()
    serie2.proximo_numero(); serie2.proximo_numero(); serie2.salvar()

    assert ControleEmissao.carregar(tmp_path, "homologacao", "1", 1).ultimo_numero == 1
    assert ControleEmissao.carregar(tmp_path, "homologacao", "2", 1).ultimo_numero == 2


def test_montar_emissor_bloqueia_maquina_diferente(config, monkeypatch, tmp_path):
    from nfse import servico

    controle = ControleEmissao.carregar(
        config.diretorio_logs, config.ambiente, config.serie_dps, 1
    )
    controle.maquina = "LAPTOP-DA-COLEGA"
    controle.salvar()

    # Não deve nem chegar a abrir conexão: o certificado é dispensado no teste.
    monkeypatch.setattr(servico, "carregar_certificado", lambda c: _CertificadoFalso())
    monkeypatch.setattr(servico, "criar_sessao_mtls", lambda c: None)

    with pytest.raises(ConflitoDeMaquina, match="LAPTOP-DA-COLEGA"):
        servico.montar_emissor(config, _email_desligado())

    # Com a autorização explícita, passa e o controle muda de dono.
    emissor = servico.montar_emissor(config, _email_desligado(), permitir_outra_maquina=True)
    assert emissor.controle.conflito_de_maquina() is None


class _CertificadoFalso:
    def validar_vigencia(self):
        return None


def _email_desligado():
    from nfse.email_envio import ConfiguracaoEmail

    return ConfiguracaoEmail(
        ativo=False, servidor="", porta=587, usuario="", senha="",
        remetente_email="", remetente_nome="", usar_starttls=True, copia_oculta=[],
        destino_teste=None, permitir_homologacao=False, assunto_modelo="", corpo_modelo="",
    )


# ---------------------------------------------------------------------------
# Backup dentro da emissão
# ---------------------------------------------------------------------------
def test_nota_autorizada_e_espelhada(config, linha, certificado_teste, tmp_path):
    from tests.test_servico import ClienteFalso, _autorizada

    config.diretorio_backup = tmp_path / "backup"
    controle = ControleEmissao.carregar(config.diretorio_logs, "homologacao", "1", 1)
    emissor = __import__("nfse.servico", fromlist=["Emissor"]).Emissor(
        config=config, config_email=_email_desligado(), certificado=certificado_teste,
        cliente=ClienteFalso([_autorizada("CHAVE1")]), controle=controle,
        espelho=Espelho(config.diretorio_backup),
    )

    registro = emissor.emitir_uma(linha, OpcoesEmissao(competencia=date(2026, 9, 1)))

    assert registro.situacao == "AUTORIZADA"
    copiados = list((tmp_path / "backup").rglob("*"))
    nomes = {c.name for c in copiados if c.is_file()}
    assert "CHAVE1_11222333000181_nfse.xml" in nomes
    assert "controle_homologacao_serie1.json" in nomes, "o controle precisa ir junto"


def test_falha_no_backup_nao_invalida_a_nota(
    config, linha, certificado_teste, tmp_path, monkeypatch
):
    """A nota já está autorizada na Sefin: backup quebrado vira aviso, não erro."""
    from nfse import servico
    from tests.test_servico import ClienteFalso, _autorizada

    config.diretorio_backup = tmp_path / "backup"
    controle = ControleEmissao.carregar(config.diretorio_logs, "homologacao", "1", 1)
    emissor = servico.Emissor(
        config=config, config_email=_email_desligado(), certificado=certificado_teste,
        cliente=ClienteFalso([_autorizada("CHAVE1")]), controle=controle,
        espelho=Espelho(config.diretorio_backup),
    )
    monkeypatch.setattr(
        servico, "espelhar_nota",
        lambda *a, **k: (_ for _ in ()).throw(OSError("unidade de rede indisponível")),
    )
    eventos = []

    registro = emissor.emitir_uma(
        linha, OpcoesEmissao(competencia=date(2026, 9, 1)), eventos.append
    )

    assert registro.situacao == "AUTORIZADA"
    assert any("Backup falhou" in e.mensagem for e in eventos if e.tipo == "aviso")
