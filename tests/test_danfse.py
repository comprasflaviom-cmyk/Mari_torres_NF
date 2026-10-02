"""DANFSe gerado localmente (NT 008/2026): a API de download foi desligada."""

from __future__ import annotations

from io import BytesIO

import pytest
from lxml import etree
from pypdf import PdfReader

from nfse.danfse import ErroDANFSe, gerar_danfse, ler_nfse
from tests.conftest import CHAVE_EXEMPLO, montar_nfse_xml


def _texto(pdf: bytes) -> tuple[int, str]:
    leitor = PdfReader(BytesIO(pdf))
    return len(leitor.pages), "\n".join(p.extract_text() for p in leitor.pages)


def test_danfse_uma_pagina_com_os_dados_da_nota(config, linha):
    paginas, texto = _texto(gerar_danfse(montar_nfse_xml(config, linha)))

    assert paginas == 1, "a NT exige o DANFSe numa única página"
    for esperado in (
        "DANFSe v2.0", "Documento Auxiliar da NFS-e", CHAVE_EXEMPLO,
        "11.222.333/0001-81",              # prestador, com máscara
        "Cliente Alfa Tecnologia LTDA",    # tomador
        "17.01.01",                        # cTribNac formatado nn.nn.nn
        "R$ 4.500,00", "Operação Tributável", "Não Retido",
        "Rio de Janeiro / RJ",             # nome do município vem da tabela IBGE
        "DESTINATÁRIO DA OPERAÇÃO NÃO IDENTIFICADO NA NFS-e",
        "Totais Aproximados dos Tributos cfe. Lei nº 12.741/2012",
    ):
        assert esperado in texto, esperado


def test_homologacao_traz_a_marca_sem_validade(config, linha):
    _, texto = _texto(gerar_danfse(montar_nfse_xml(config, linha)))
    assert "NFS-e SEM VALIDADE JURÍDICA" in texto


def test_producao_nao_traz_a_marca(config, linha):
    config.ambiente = "producao"
    config.__post_init__()
    _, texto = _texto(gerar_danfse(montar_nfse_xml(config, linha)))
    assert "SEM VALIDADE" not in texto


def test_codigo_municipal_aparece_junto_do_nacional(config, linha):
    from dataclasses import replace

    config.servico = replace(config.servico, codigo_tributacao_municipal="001")
    dados = ler_nfse(montar_nfse_xml(config, linha))
    assert dados["servico"]["codigo"] == "17.01.01 / 001"


def test_sem_tomador_imprime_nao_identificado(config, linha):
    raiz = etree.fromstring(montar_nfse_xml(config, linha))
    toma = raiz.xpath("//*[local-name()='toma']")[0]
    toma.getparent().remove(toma)
    _, texto = _texto(gerar_danfse(etree.tostring(raiz)))
    assert "TOMADOR/ADQUIRENTE DA OPERAÇÃO NÃO IDENTIFICADO NA NFS-e" in texto


def test_campos_ausentes_viram_traco(config, linha):
    dados = ler_nfse(montar_nfse_xml(config, linha))
    assert dados["servico"]["nbs"] == "-"
    assert dados["totais"]["liquido_ibscbs"] == "-"


def test_xml_que_nao_e_nfse_e_recusado():
    with pytest.raises(ErroDANFSe):
        gerar_danfse(b"<DPS/>")
