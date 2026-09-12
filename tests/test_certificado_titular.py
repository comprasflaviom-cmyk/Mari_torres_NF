"""Quem assina precisa ser o prestador declarado na nota.

Rejeição real da Sefin Nacional em homologação: uma nota com a assinatura
matematicamente perfeita (digest e RSA conferidos por fora do app) foi recusada
com `[E0714] Arquivo enviado com erro na assinatura` — porque o certificado era
de outro CNPJ. A mensagem do governo aponta para a assinatura, então sem esta
checagem local a investigação vai para o lado errado.
"""

from __future__ import annotations

import datetime as dt

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from nfse.certificado import OID_CNPJ_ICP_BRASIL, CertificadoA1, ErroCertificado

CNPJ_PRESTADOR = "11222333000181"
CNPJ_DE_OUTRA_EMPRESA = "24465499000170"


def _montar_certificado(nome_comum: str, cnpj_no_san: str | None) -> CertificadoA1:
    chave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nome = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, nome_comum)])
    agora = dt.datetime.now(dt.timezone.utc)
    construtor = (
        x509.CertificateBuilder()
        .subject_name(nome).issuer_name(nome)
        .public_key(chave.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(agora - dt.timedelta(days=1))
        .not_valid_after(agora + dt.timedelta(days=365))
    )
    if cnpj_no_san is not None:
        # Num e-CNPJ de verdade o CNPJ vem como otherName, num DER de string:
        # 0x0C = UTF8String, depois o tamanho, depois os 14 dígitos.
        bruto = bytes([0x0C, len(cnpj_no_san)]) + cnpj_no_san.encode("latin-1")
        construtor = construtor.add_extension(
            x509.SubjectAlternativeName([
                x509.OtherName(x509.ObjectIdentifier(OID_CNPJ_ICP_BRASIL), bruto)
            ]),
            critical=False,
        )
    certificado = construtor.sign(chave, hashes.SHA256())
    return CertificadoA1(
        certificado=certificado, chave_privada=chave, cadeia=[],
        cert_pem=certificado.public_bytes(serialization.Encoding.PEM),
        chave_pem=chave.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )


def test_cnpj_vem_do_subject_alternative_name():
    """O caminho do e-CNPJ real: o CNPJ está no otherName 2.16.76.1.3.3."""
    cert = _montar_certificado("EMPRESA ALFA LTDA", CNPJ_PRESTADOR)
    assert cert.cnpj_titular == CNPJ_PRESTADOR


def test_cnpj_cai_para_o_sufixo_do_common_name():
    """Plano B para certificados fora do padrão: `RAZAO SOCIAL:<cnpj>` no CN."""
    cert = _montar_certificado(f"EMPRESA ALFA LTDA:{CNPJ_PRESTADOR}", None)
    assert cert.cnpj_titular == CNPJ_PRESTADOR


def test_certificado_de_outro_cnpj_e_barrado_com_mensagem_clara():
    cert = _montar_certificado("OUTRA EMPRESA LTDA", CNPJ_DE_OUTRA_EMPRESA)

    with pytest.raises(ErroCertificado) as erro:
        cert.validar_titular(CNPJ_PRESTADOR)

    mensagem = str(erro.value)
    assert CNPJ_DE_OUTRA_EMPRESA in mensagem, "a mensagem precisa dizer de quem é o certificado"
    assert CNPJ_PRESTADOR in mensagem, "a mensagem precisa dizer qual prestador a nota declara"


def test_certificado_do_proprio_prestador_passa():
    cert = _montar_certificado("EMPRESA ALFA LTDA", CNPJ_PRESTADOR)
    cert.validar_titular("11.222.333/0001-81")  # com máscara, para conferir a normalização


def test_certificado_sem_cnpj_legivel_nao_bloqueia():
    """Não dá para afirmar que está errado — bloquear aqui seria adivinhar."""
    cert = _montar_certificado("EMPRESA SEM CNPJ NO NOME", None)
    assert cert.cnpj_titular == ""
    cert.validar_titular(CNPJ_PRESTADOR)
