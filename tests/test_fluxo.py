"""Testes do caminho feliz: planilha -> DPS -> XML -> assinatura -> pacote."""

from __future__ import annotations

import base64
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from lxml import etree

from nfse.assinatura import (
    NAMESPACE_XMLDSIG,
    assinar_dps,
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


def test_assinatura_embute_a_cadeia_do_certificado(config, linha, certificado_teste):
    """Sem a cadeia até a AC raiz, o validador da Sefin pode não conseguir montar
    o caminho de certificação — encontrado numa rejeição real ([E0714] "Arquivo
    enviado com erro na assinatura") assim que o [E1228] de namespace foi
    corrigido e o certificado real (com cadeia) entrou em cena."""
    import datetime as dt

    from cryptography import x509
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    from nfse.certificado import CertificadoA1

    chave_ac = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nome_ac = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "AC INTERMEDIARIA DE TESTE")])
    agora = dt.datetime.now(dt.timezone.utc)
    certificado_ac = (
        x509.CertificateBuilder()
        .subject_name(nome_ac).issuer_name(nome_ac)
        .public_key(chave_ac.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(agora - dt.timedelta(days=1))
        .not_valid_after(agora + dt.timedelta(days=365))
        .sign(chave_ac, hashes.SHA256())
    )
    com_cadeia = CertificadoA1(
        certificado=certificado_teste.certificado,
        chave_privada=certificado_teste.chave_privada,
        cadeia=[certificado_ac],
        cert_pem=certificado_teste.cert_pem,
        chave_pem=certificado_teste.chave_pem,
    )

    assinado = assinar_dps(dps_para_xml(montar_dps(config, linha, 1)), com_cadeia)
    raiz = etree.fromstring(assinado)
    certificados = raiz.findall(".//ds:X509Data/ds:X509Certificate", NS)
    assert len(certificados) == 2, "titular + cadeia deveriam gerar dois <X509Certificate>"


def test_assinatura_confere_apos_reserializar(config, linha, certificado_teste):
    """A árvore da assinatura é montada manualmente (não por uma lib de XMLDSig)
    porque a forma documentada do signxml para namespace sem prefixo gera uma
    assinatura que não bate mais consigo mesma depois de serializada e
    reinterpretada. Confere aqui, de ponta a ponta e sem depender de nenhuma
    lib de assinatura, que o SignedInfo reproduz byte a byte após reparse e que
    a assinatura RSA e o digest de infDPS conferem de forma independente."""
    assinado = assinar_dps(dps_para_xml(montar_dps(config, linha, 1)), certificado_teste)

    original = etree.fromstring(assinado)
    signed_info_original = original.find(f".//{{{NAMESPACE_XMLDSIG}}}SignedInfo")
    c14n_original = etree.tostring(signed_info_original, method="c14n")

    reparsed = etree.fromstring(etree.tostring(original))
    signed_info_reparsed = reparsed.find(f".//{{{NAMESPACE_XMLDSIG}}}SignedInfo")
    assert etree.tostring(signed_info_reparsed, method="c14n") == c14n_original

    inf_dps = reparsed.find(f"{{{NAMESPACE_DPS}}}infDPS")
    resumo = hashes.Hash(hashes.SHA1())
    resumo.update(etree.tostring(inf_dps, method="c14n"))
    digest_calculado = base64.b64encode(resumo.finalize()).decode("ascii")
    digest_no_xml = reparsed.find(f".//{{{NAMESPACE_XMLDSIG}}}DigestValue").text
    assert digest_calculado == digest_no_xml

    assinatura_valor = reparsed.find(f".//{{{NAMESPACE_XMLDSIG}}}SignatureValue").text
    chave_publica = certificado_teste.certificado.public_key()
    chave_publica.verify(
        base64.b64decode(assinatura_valor),
        etree.tostring(signed_info_reparsed, method="c14n"),
        padding.PKCS1v15(),
        hashes.SHA1(),
    )  # levanta InvalidSignature se não bater — a asserção é não ter lançado


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
