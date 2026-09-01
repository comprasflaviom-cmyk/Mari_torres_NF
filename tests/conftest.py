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
        servico=ParametrosServico(aliquota_iss=Decimal("2.00")),
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
        extras={"Cod_Municipio": "3304557", "CEP": "20040901", "Logradouro": "Av. Rio Branco", "Numero": "156"},
    )
