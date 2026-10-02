"""Fixtures compartilhadas: certificado autoassinado e configuração de teste."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from nfse.certificado import CertificadoA1
from nfse.config import Configuracao, ParametrosServico, Prestador
from nfse.planilha import LinhaFaturamento


@pytest.fixture(scope="session")
def certificado_teste() -> CertificadoA1:
    """Certificado autoassinado — só para testar a assinatura, nunca para emitir."""
    chave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nome = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "BR"),
        x509.NameAttribute(NameOID.COMMON_NAME, "EMPRESA TESTE LTDA:11222333000181"),
    ])
    agora = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(nome)
        .issuer_name(nome)
        .public_key(chave.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(agora - dt.timedelta(days=1))
        .not_valid_after(agora + dt.timedelta(days=365))
        .sign(chave, hashes.SHA256())
    )
    return CertificadoA1(
        certificado=cert,
        chave_privada=chave,
        cadeia=[],
        cert_pem=cert.public_bytes(serialization.Encoding.PEM),
        chave_pem=chave.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )


@pytest.fixture
def config(tmp_path: Path) -> Configuracao:
    return Configuracao(
        ambiente="homologacao",
        prestador=Prestador(
            cnpj="11222333000181",
            inscricao_municipal="1234567",
            codigo_municipio="3304557",
        ),
        servico=ParametrosServico(aliquota_iss=Decimal("2.00"), percentual_tributos_sn=Decimal("8.63")),
        caminho_pfx=None,
        senha_pfx=None,
        caminho_certificado=None,
        caminho_chave=None,
        senha_chave=None,
        caminho_planilha=tmp_path / "faturamento.xlsx",
        diretorio_notas=tmp_path / "notas",
        diretorio_logs=tmp_path / "logs",
        serie_dps="1",
        numero_dps_inicial=1,
        timeout_segundos=30,
        max_tentativas=1,
    )


@pytest.fixture
def linha() -> LinhaFaturamento:
    return LinhaFaturamento(
        numero_linha=2,
        documento_tomador="11222333000181",
        razao_social="Cliente Alfa Tecnologia LTDA",
        email="financeiro@clientealfa.com.br",
        valor_servico=Decimal("4500.00"),
        descricao="Consultoria estratégica em processos comerciais.",
        extras={
            "Cod_Municipio": "3304557", "CEP": "20040901",
            "Logradouro": "Av. Rio Branco", "Numero": "156", "Bairro": "Centro",
        },
    )


CHAVE_EXEMPLO = "33045572224465499000170000000000008426102498851380"


def montar_nfse_xml(config: Configuracao, linha: LinhaFaturamento, chave: str = CHAVE_EXEMPLO) -> bytes:
    """XML de NFS-e autorizada, como a Sefin devolve: a DPS do app embrulhada
    nos campos que a Sefin acrescenta (número, município por extenso, emit...).
    Base para testar o DANFSe sem depender da rede."""
    from datetime import date

    from lxml import etree

    from nfse.config import NAMESPACE_DPS
    from nfse.dps import dps_para_xml, montar_dps

    dps = etree.fromstring(dps_para_xml(montar_dps(config, linha, 1, competencia=date(2026, 10, 1))))
    n = NAMESPACE_DPS
    raiz = etree.Element(f"{{{n}}}NFSe", nsmap={None: n}, versao="1.01")
    inf = etree.SubElement(raiz, f"{{{n}}}infNFSe", Id="NFS" + chave)

    def el(pai, tag, texto=None):
        no = etree.SubElement(pai, f"{{{n}}}{tag}")
        no.text = texto
        return no

    for tag, texto in (
        ("xLocEmi", "Rio de Janeiro"), ("xLocPrestacao", "Rio de Janeiro"), ("nNFSe", "84"),
        ("cLocIncid", "3304557"), ("xLocIncid", "Rio de Janeiro"),
        ("xTribNac", "Assessoria ou consultoria de qualquer natureza."),
        ("verAplic", "SefinNac"), ("ambGer", "2"), ("tpEmis", "1"), ("procEmi", "1"),
        ("cStat", "100"), ("dhProc", "2026-10-02T16:41:12-03:00"), ("nDFSe", "1"),
    ):
        el(inf, tag, texto)
    emit = el(inf, "emit")
    el(emit, "CNPJ", config.prestador.cnpj)
    el(emit, "xNome", "EMPRESA TESTE LTDA")
    end = el(emit, "enderNac")
    for tag, texto in (("xLgr", "RUA DO PRESTADOR"), ("nro", "60"), ("xBairro", "LEBLON"),
                       ("cMun", "3304557"), ("UF", "RJ"), ("CEP", "22430130")):
        el(end, tag, texto)
    valores = el(inf, "valores")
    el(valores, "vLiq", str(linha.valor_servico))
    inf.append(dps)
    return etree.tostring(raiz, xml_declaration=True, encoding="UTF-8")
