"""Testes do caminho feliz: planilha -> DPS -> XML -> assinatura -> pacote."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest
from lxml import etree

from nfse.assinatura import assinar_dps, desempacotar_retorno, empacotar_para_envio
from nfse.config import NAMESPACE_DPS
from nfse.dps import dps_para_xml, gerar_id_dps, montar_dps
from nfse.estado import ControleEmissao, impressao_da_linha
from nfse.planilha import ErroPlanilha, iterar_faturamento

NS = {"n": NAMESPACE_DPS, "ds": "http://www.w3.org/2000/09/xmldsig#"}


def test_id_dps_tem_45_posicoes(config):
    identificador = gerar_id_dps(config, 1)
    assert len(identificador) == 45
    assert identificador.startswith("DPS33045572")
    assert identificador.endswith("00001" + "1".zfill(15))


def test_dps_contem_campos_obrigatorios(config, linha):
    dps = montar_dps(config, linha, numero_dps=1, competencia=date(2026, 9, 1))
    inf = dps["infDPS"]
    assert inf["tpAmb"] == 2                      # homologação
    assert inf["cLocEmi"] == "3304557"
    assert inf["prest"]["CNPJ"] == "11222333000181"
    assert inf["toma"]["xNome"] == "Cliente Alfa Tecnologia LTDA"
    assert inf["serv"]["cServ"]["cTribNac"] == "170101"
    assert inf["valores"]["vServPrest"]["vServ"] == "4500.00"
    assert inf["valores"]["trib"]["tribMun"]["pAliq"] == "2.00"
    assert inf["dCompet"] == "2026-09-01"


def test_xml_respeita_a_ordem_do_schema(config, linha):
    xml = dps_para_xml(montar_dps(config, linha, 1))
    raiz = etree.fromstring(xml)
    assert raiz.tag == f"{{{NAMESPACE_DPS}}}DPS"
    assert raiz.get("versao") == "1.00"

    inf = raiz.find("n:infDPS", NS)
    tags = [etree.QName(filho).localname for filho in inf]
    assert tags == [
        "tpAmb", "dhEmi", "verAplic", "serie", "nDPS", "dCompet",
        "tpEmit", "cLocEmi", "prest", "toma", "serv", "valores",
    ]


def test_assinatura_gera_reference_para_o_id(config, linha, certificado_teste):
    xml = dps_para_xml(montar_dps(config, linha, 7))
    assinado = assinar_dps(xml, certificado_teste)
    raiz = etree.fromstring(assinado)

    referencia = raiz.find(".//ds:Reference", NS)
    id_inf = raiz.find("n:infDPS", NS).get("Id")
    assert referencia is not None, "assinatura sem <Reference>"
    assert referencia.get("URI") == "#" + id_inf
    assert raiz.find(".//ds:X509Certificate", NS) is not None, "certificado não embutido"


def test_pacote_gzip_base64_roundtrip(config, linha, certificado_teste):
    assinado = assinar_dps(dps_para_xml(montar_dps(config, linha, 1)), certificado_teste)
    pacote = empacotar_para_envio(assinado)
    assert isinstance(pacote, str)
    assert desempacotar_retorno(pacote) == assinado


def test_leitura_da_planilha_valida_e_normaliza(tmp_path):
    caminho = tmp_path / "faturamento.xlsx"
    pd.DataFrame([
        {   # linha 2 — válida, valor em formato brasileiro
            "CNPJ_Cliente": "11.222.333/0001-81",
            "Razao_Social": "Cliente Alfa",
            "Email_Cliente": "a@alfa.com.br",
            "Valor_Servico": "R$ 1.234,56",
            "Descricao_Servico": "Consultoria.",
        },
        {   # linha 3 — CNPJ com DV errado
            "CNPJ_Cliente": "11222333000100",
            "Razao_Social": "Cliente Beta",
            "Email_Cliente": "b@beta.com.br",
            "Valor_Servico": "100",
            "Descricao_Servico": "Consultoria.",
        },
        {   # linha 4 — valor zerado
            "CNPJ_Cliente": "11222333000181",
            "Razao_Social": "Cliente Gama",
            "Email_Cliente": "c@gama.com.br",
            "Valor_Servico": "0",
            "Descricao_Servico": "Consultoria.",
        },
    ]).to_excel(caminho, index=False)

    resultados = list(iterar_faturamento(caminho))
    assert len(resultados) == 3

    _, ok, erro = resultados[0]
    assert erro is None
    assert ok.numero_linha == 2
    assert ok.documento_tomador == "11222333000181"
    assert ok.valor_servico == Decimal("1234.56")

    assert "dígito verificador" in resultados[1][2]
    assert "maior que zero" in resultados[2][2]


def test_planilha_sem_coluna_obrigatoria(tmp_path):
    caminho = tmp_path / "ruim.xlsx"
    pd.DataFrame([{"CNPJ_Cliente": "1"}]).to_excel(caminho, index=False)
    with pytest.raises(ErroPlanilha, match="Razao_Social"):
        list(iterar_faturamento(caminho))


def test_controle_impede_emissao_duplicada(tmp_path, linha):
    controle = ControleEmissao.carregar(tmp_path, "homologacao", "1", 1)
    impressao = impressao_da_linha(linha, "2026-09-01")

    assert controle.ja_emitida(impressao) is None
    numero = controle.proximo_numero()
    assert numero == 1
    controle.registrar(impressao, numero, "3304557...CHAVE")
    controle.salvar()

    recarregado = ControleEmissao.carregar(tmp_path, "homologacao", "1", 1)
    assert recarregado.ultimo_numero == 1
    assert recarregado.ja_emitida(impressao)["chave_acesso"] == "3304557...CHAVE"
    assert recarregado.proximo_numero() == 2
