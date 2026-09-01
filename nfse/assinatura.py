"""
Assinatura digital XMLDSig da DPS e empacotamento para o envelope JSON.

A Sefin Nacional rejeita DPS não assinada. O padrão exigido é o mesmo da
NF-e/NFS-e: **XMLDSig enveloped**, com `Reference URI="#<Id do infDPS>"`,
transformações `enveloped-signature` + `c14n` e o certificado do prestador
embutido em `<X509Data>`.
"""

from __future__ import annotations

import base64
import gzip
import os

from lxml import etree
from signxml import DigestAlgorithm, SignatureMethod, XMLSigner, methods

from .certificado import CertificadoA1
from .config import NAMESPACE_DPS


class ErroAssinatura(RuntimeError):
    pass


class _AssinadorFiscal(XMLSigner):
    """XMLSigner que aceita RSA-SHA1.

    A partir da versão 4, o signxml recusa SHA-1 por ser criptograficamente
    fraco. A recusa é tecnicamente correta, mas os webservices fiscais
    brasileiros (NF-e e, por herança, a NFS-e Nacional) padronizaram RSA-SHA1
    no XMLDSig e rejeitam o que fugir disso. Esta subclasse desativa apenas a
    checagem — nada mais do signxml é alterado.

    Se o seu ambiente já exigir SHA-256, basta ASSINATURA_ALGORITMO=sha256 no
    .env: aí o signxml roda com a validação original.
    """

    def check_deprecated_methods(self):  # noqa: D102
        return


# Confira o algoritmo exigido no Manual de Orientação ao Contribuinte (MOC) da
# NFS-e Nacional antes de emitir em produção.
ALGORITMOS = {
    "sha1": (SignatureMethod.RSA_SHA1, DigestAlgorithm.SHA1, _AssinadorFiscal),
    "sha256": (SignatureMethod.RSA_SHA256, DigestAlgorithm.SHA256, XMLSigner),
}


def assinar_dps(xml_dps: bytes, cert: CertificadoA1) -> bytes:
    """Assina o XML da DPS e devolve o XML assinado em bytes."""
    try:
        raiz = etree.fromstring(xml_dps)
    except etree.XMLSyntaxError as exc:
        raise ErroAssinatura(f"XML da DPS malformado: {exc}") from exc

    inf_dps = raiz.find(f"{{{NAMESPACE_DPS}}}infDPS")
    if inf_dps is None or not inf_dps.get("Id"):
        raise ErroAssinatura("Elemento <infDPS> sem atributo Id — não é possível assinar.")

    escolha = os.getenv("ASSINATURA_ALGORITMO", "sha1").strip().lower()
    if escolha not in ALGORITMOS:
        raise ErroAssinatura(
            f"ASSINATURA_ALGORITMO inválido: {escolha!r}. Use 'sha1' ou 'sha256'."
        )
    algoritmo_assinatura, algoritmo_digest, classe_assinador = ALGORITMOS[escolha]

    assinador = classe_assinador(
        method=methods.enveloped,
        signature_algorithm=algoritmo_assinatura,
        digest_algorithm=algoritmo_digest,
        # C14N sem InclusiveNamespaces PrefixList, como exigido pelo MOC fiscal.
        c14n_algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315",
    )
    # A assinatura referencia o Id do infDPS e é anexada ao final do elemento <DPS>.
    assinado = assinador.sign(
        raiz,
        key=cert.chave_pem,
        cert=cert.cert_pem,
        reference_uri="#" + inf_dps.get("Id"),
    )
    return etree.tostring(assinado, encoding="utf-8", xml_declaration=True)


def empacotar_para_envio(xml_assinado: bytes) -> str:
    """gzip + Base64 — é o conteúdo do campo `dpsXmlGZipB64` do POST /nfse."""
    comprimido = gzip.compress(xml_assinado)
    return base64.b64encode(comprimido).decode("ascii")


def desempacotar_retorno(conteudo_b64: str) -> bytes:
    """Inverso de `empacotar_para_envio`: usado no XML da NFS-e devolvido pela API."""
    bruto = base64.b64decode(conteudo_b64)
    try:
        return gzip.decompress(bruto)
    except (OSError, gzip.BadGzipFile):
        # Alguns retornos vêm em Base64 puro, sem compressão.
        return bruto
