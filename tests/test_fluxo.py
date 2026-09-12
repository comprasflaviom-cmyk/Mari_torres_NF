"""Testes do caminho feliz: planilha -> DPS -> XML -> assinatura -> pacote."""

from __future__ import annotations

import base64
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from lxml import etree

from nfse.assinatura import (
    NAMESPACE_XMLDSIG,
    PERFIS,
    assinar_dps,
    canonizar,
    desempacotar_retorno,
    empacotar_para_envio,
)
from nfse.config import NAMESPACE_DPS
from nfse.dps import ErroDPS, dps_para_xml, gerar_id_dps, montar_dps
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


def test_dhEmi_fica_no_passado_com_folga_de_relogio(config, linha):
    """Rejeição real: `[E0008] A data de emissão da DPS não pode ser posterior à
    data do seu processamento`. Quem decide o dhEmi é o relógio desta máquina, e
    numa das tentativas ele saiu só 0,145 s antes do processamento da Sefin —
    qualquer adiantamento do relógio local derrubava a nota."""
    from datetime import datetime

    from nfse.dps import FUSO_BRASILIA, MARGEM_RELOGIO

    dps = montar_dps(config, linha, numero_dps=1)
    emitido_em = datetime.fromisoformat(dps["infDPS"]["dhEmi"])
    folga = datetime.now(FUSO_BRASILIA) - emitido_em

    assert folga >= MARGEM_RELOGIO - timedelta(seconds=1), "dhEmi sem a folga de relógio"
    assert folga < MARGEM_RELOGIO + timedelta(minutes=1), "dhEmi atrasado demais"


def test_competencia_nao_escorrega_de_mes_por_causa_da_folga(config, linha):
    """A folga desconta segundos do dhEmi, mas não pode empurrar a competência
    para o mês anterior quando a emissão acontece logo depois da meia-noite."""
    virada = datetime(2026, 9, 1, 0, 0, 2, tzinfo=timezone(timedelta(hours=-3)))

    dps = montar_dps(config, linha, numero_dps=1, emitido_em=virada)

    assert dps["infDPS"]["dCompet"] == "2026-09-01"


def test_endereco_completo_gera_bloco_end(config, linha):
    """CEP, logradouro, número e bairro presentes: end sai completo e na ordem do schema."""
    dps = montar_dps(config, linha, numero_dps=1, competencia=date(2026, 9, 1))
    end = dps["infDPS"]["toma"]["end"]
    assert list(end.keys()) == ["endNac", "xLgr", "nro", "xBairro"]
    assert end["endNac"] == {"cMun": "3304557", "CEP": "20040901"}
    assert end["xLgr"] == "Av. Rio Branco"


def test_endereco_so_municipio_da_erro_claro(config, linha):
    """O schema oficial exige CEP/logradouro/número/bairro juntos quando há
    município — gerar um <end> pela metade seria XML inválido."""
    linha.extras = {"Cod_Municipio": "3304557"}  # só o município, nada mais

    with pytest.raises(ErroDPS, match="CEP, logradouro, número e bairro"):
        montar_dps(config, linha, numero_dps=1, competencia=date(2026, 9, 1))


def test_endereco_sem_municipio_omite_o_bloco(config, linha):
    """Sem código de município, o <end> inteiro é opcional — está tudo bem omitir."""
    linha.extras = {}

    dps = montar_dps(config, linha, numero_dps=1, competencia=date(2026, 9, 1))
    assert "end" not in dps["infDPS"]["toma"]


def test_xml_respeita_a_ordem_do_schema(config, linha):
    xml = dps_para_xml(montar_dps(config, linha, 1))
    raiz = etree.fromstring(xml)
    assert raiz.tag == f"{{{NAMESPACE_DPS}}}DPS"
    assert raiz.get("versao") == "1.01"

    inf = raiz.find("n:infDPS", NS)
    tags = [etree.QName(filho).localname for filho in inf]
    assert tags == [
        "tpAmb", "dhEmi", "verAplic", "serie", "nDPS", "dCompet",
        "tpEmit", "cLocEmi", "prest", "toma", "serv", "valores",
    ]

    # tribMun é onde já pegamos uma regressão real: pAliq colocado antes de
    # tpRetISSQN gera XML inválido no schema oficial (TCTribMunicipal exige
    # tpRetISSQN antes, com pAliq por último, mesmo sendo o único campo cujo
    # nome sugeriria vir logo após tribISSQN).
    trib_mun = inf.find("n:valores/n:trib/n:tribMun", NS)
    tags_trib_mun = [etree.QName(filho).localname for filho in trib_mun]
    assert tags_trib_mun == ["tribISSQN", "tpRetISSQN", "pAliq"]


def test_assinatura_gera_reference_para_o_id(config, linha, certificado_teste):
    xml = dps_para_xml(montar_dps(config, linha, 7))
    assinado = assinar_dps(xml, certificado_teste)
    raiz = etree.fromstring(assinado)

    referencia = raiz.find(".//ds:Reference", NS)
    id_inf = raiz.find("n:infDPS", NS).get("Id")
    assert referencia is not None, "assinatura sem <Reference>"
    assert referencia.get("URI") == "#" + id_inf
    assert raiz.find(".//ds:X509Certificate", NS) is not None, "certificado não embutido"


def test_assinatura_nao_usa_prefixo_de_namespace(config, linha, certificado_teste):
    """A Sefin Nacional rejeita com [E1228] qualquer <ds:...>: exige xmlns="..."
    sem prefixo, tanto no <Signature> quanto em seus filhos."""
    assinado = assinar_dps(dps_para_xml(montar_dps(config, linha, 1)), certificado_teste)
    assert b"<ds:" not in assinado
    assert b'xmlns:ds="' not in assinado
    raiz = etree.fromstring(assinado)
    assinatura = raiz.find(f".//{{{NAMESPACE_XMLDSIG}}}Signature")
    assert assinatura is not None
    for elemento in assinatura.iter():
        assert etree.QName(elemento).namespace == NAMESPACE_XMLDSIG


def test_assinatura_leva_so_o_certificado_do_titular(config, linha, certificado_teste):
    """A cadeia até a AC raiz não vai junto.

    Chegamos a enviar a cadeia inteira tentando destravar a rejeição real
    [E0714], e não mudou nada — e o `.pfx` devolve essa cadeia fora de ordem,
    o que atrapalha quem tenta montar o caminho de certificação. As
    implementações de NFS-e Nacional mandam só o certificado do titular.
    """
    from dataclasses import replace

    com_cadeia = replace(certificado_teste, cadeia=[certificado_teste.certificado])

    assinado = assinar_dps(dps_para_xml(montar_dps(config, linha, 1)), com_cadeia)
    raiz = etree.fromstring(assinado)
    certificados = raiz.findall(".//ds:X509Data/ds:X509Certificate", NS)
    assert len(certificados) == 1, "só o certificado do titular deveria ir no <X509Data>"


def test_declaracao_xml_no_formato_do_ecossistema_fiscal(config, linha, certificado_teste):
    """Aspas duplas e UTF-8 em maiúsculas, como todo emissor fiscal escreve.

    O lxml escreveria `<?xml version='1.0' encoding='utf-8'?>`. É XML válido,
    mas os validadores fiscais analisam o arquivo de forma literal (é o que
    está por trás da recusa a prefixo de namespace), e não vale arriscar.
    """
    assinado = assinar_dps(dps_para_xml(montar_dps(config, linha, 1)), certificado_teste)
    assert assinado.startswith(b'<?xml version="1.0" encoding="UTF-8"?>')


@pytest.mark.parametrize("nome_perfil", ["classico", "moderno"])
def test_canonicalizacao_nao_desdeclara_namespace(
    config, linha, certificado_teste, monkeypatch, nome_perfil
):
    """Nenhum `xmlns=""` pode aparecer na forma canônica do que foi assinado.

    O `etree.tostring(elemento, method="c14n")` do lxml, sobre um elemento
    dentro de outra árvore, emite `xmlns=""` em `Transforms`, `Transform`,
    `DigestMethod` e `DigestValue` — que pertencem, sim, ao namespace xmldsig.
    A assinatura saía calculada sobre bytes que nenhum outro validador
    reproduz, e a Sefin recusava com [E0714] enquanto a conferência local
    dizia que estava tudo certo: as duas pontas usavam a mesma função
    defeituosa e combinavam entre si. É o teste que teria pego isso.
    """
    monkeypatch.setenv("ASSINATURA_ALGORITMO", nome_perfil)
    exclusivo = PERFIS[nome_perfil]["exclusivo"]
    assinado = assinar_dps(dps_para_xml(montar_dps(config, linha, 1)), certificado_teste)
    raiz = etree.fromstring(assinado)

    for caminho in (f".//{{{NAMESPACE_XMLDSIG}}}SignedInfo", f"{{{NAMESPACE_DPS}}}infDPS"):
        canonico = canonizar(raiz.find(caminho), exclusivo)
        assert b'xmlns=""' not in canonico, f"{caminho} saiu com namespace desdeclarado"


@pytest.mark.parametrize("nome_perfil", ["classico", "moderno"])
def test_assinatura_confere_apos_reserializar(
    config, linha, certificado_teste, monkeypatch, nome_perfil
):
    """A árvore da assinatura é montada manualmente (não por uma lib de XMLDSig)
    porque a forma documentada do signxml para namespace sem prefixo gera uma
    assinatura que não bate mais consigo mesma depois de serializada e
    reinterpretada. Confere aqui, nos dois perfis e sem depender de nenhuma lib
    de assinatura, que o SignedInfo reproduz byte a byte após reparse e que a
    assinatura RSA e o digest de infDPS conferem de forma independente."""
    monkeypatch.setenv("ASSINATURA_ALGORITMO", nome_perfil)
    perfil = PERFIS[nome_perfil]
    exclusivo = perfil["exclusivo"]
    classe_hash = perfil["hash"]

    assinado = assinar_dps(dps_para_xml(montar_dps(config, linha, 1)), certificado_teste)

    original = etree.fromstring(assinado)
    c14n_original = canonizar(original.find(f".//{{{NAMESPACE_XMLDSIG}}}SignedInfo"), exclusivo)

    reparsed = etree.fromstring(etree.tostring(original))
    signed_info_reparsed = reparsed.find(f".//{{{NAMESPACE_XMLDSIG}}}SignedInfo")
    assert canonizar(signed_info_reparsed, exclusivo) == c14n_original

    inf_dps = reparsed.find(f"{{{NAMESPACE_DPS}}}infDPS")
    resumo = hashes.Hash(classe_hash())
    resumo.update(canonizar(inf_dps, exclusivo))
    digest_calculado = base64.b64encode(resumo.finalize()).decode("ascii")
    assert digest_calculado == reparsed.find(f".//{{{NAMESPACE_XMLDSIG}}}DigestValue").text

    assinatura_valor = reparsed.find(f".//{{{NAMESPACE_XMLDSIG}}}SignatureValue").text
    certificado_teste.certificado.public_key().verify(
        base64.b64decode(assinatura_valor),
        canonizar(signed_info_reparsed, exclusivo),
        padding.PKCS1v15(),
        classe_hash(),
    )  # levanta InvalidSignature se não bater — a asserção é não ter lançado


def test_assinatura_confere_por_biblioteca_independente(
    config, linha, certificado_teste, monkeypatch
):
    """Conferência por uma biblioteca de XMLDSig completa, não pela nossa.

    Só no perfil `moderno`. No `classico` o signxml não serve de árbitro: ele
    canoniza chamando o mesmo lxml, e portanto reproduz o `xmlns=""` indevido
    que `canonizar` existe para evitar — concordaria com o erro em vez de
    apontá-lo. Lá quem cuida disso é
    `test_canonicalizacao_nao_desdeclara_namespace`.
    """
    signxml = pytest.importorskip("signxml", reason="só roda com requirements-dev.txt")

    monkeypatch.setenv("ASSINATURA_ALGORITMO", "moderno")
    assinado = assinar_dps(dps_para_xml(montar_dps(config, linha, 1)), certificado_teste)

    verificado = signxml.XMLVerifier().verify(assinado, x509_cert=certificado_teste.cert_pem)
    assert verificado.signed_xml.tag == f"{{{NAMESPACE_DPS}}}infDPS"


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
